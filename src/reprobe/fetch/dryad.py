"""Dryad fetcher. Resolves a dataset (URL or 10.5061/dryad.<id> DOI) via the v2
API, lists the files of its current published version, and downloads each one,
verifying the digest Dryad records for it (``digest`` / ``digestType``).
Pin = the dataset DOI, plus the concrete version fetched (metadata ``version``):
a Dryad DOI names the dataset, not one version of it."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote, urljoin

from ..models import FetchResult, Pin
from .base import (
    FetchError,
    checksum_verdict,
    download,
    get,
    hash_algorithm,
    maybe_unzip,
    new_checksum_stats,
    record_download,
    safe_join,
)

_DOI = re.compile(r"(10\.5061/dryad\.[^\s/?#]+)", re.I)
_SITE = "https://datadryad.org"
_MAX_PAGES = 500                     # file-listing pages; a listing that never ends is an error


def _href(obj: dict, rel: str) -> str | None:
    return (((obj or {}).get("_links") or {}).get(rel) or {}).get("href")


class DryadFetcher:
    name = "dryad"

    def can_handle(self, ref: str) -> bool:
        r = ref.lower()
        return "datadryad.org" in r or "10.5061/dryad" in r

    def fetch(self, ref: str, dest: Path) -> FetchResult:
        m = _DOI.search(ref)
        if not m:
            raise FetchError("could not parse Dryad DOI")
        doi = m.group(1)
        encoded = quote(f"doi:{doi}", safe="")
        try:
            r = get(f"{_SITE}/api/v2/datasets/{encoded}", timeout=60)
            r.raise_for_status()
            meta = r.json() or {}
            version_href = _href(meta, "stash:version")
            if not version_href:
                raise FetchError("the dataset record names no version")
            files = self._list_files(urljoin(_SITE, version_href.rstrip("/") + "/files"))
        except Exception as e:
            raise FetchError(f"Dryad API error: {e}")

        dest.mkdir(parents=True, exist_ok=True)
        version = meta.get("versionNumber")
        warnings = [f"Dryad serves the latest published version of a dataset (version "
                    f"{version if version is not None else 'unknown'} was fetched); its DOI "
                    "names the dataset, not this version"]
        stats = new_checksum_stats()
        attempted = 0
        notes: list[str] = []
        for f in files:
            if str(f.get("status") or "").lower() == "deleted":
                continue
            path = f.get("path") or "file"
            href = _href(f, "stash:download")
            if not href:
                warnings.append(f"no download link for {path}")
                continue
            algo = hash_algorithm(f.get("digestType"))
            digest = (algo, str(f["digest"])) if algo and f.get("digest") else None
            attempted += 1
            ok, note = download(urljoin(_SITE, href), safe_join(dest, path),
                                expected_digest=digest, timeout=600)
            record_download(stats, ok, note, bool(digest))
            if not ok:
                notes.append(note)
                warnings.append(f"{path}: {note}")
        if attempted and len(notes) == attempted:
            hint = (" — Dryad's download API answered 401: it requires an API bearer token"
                    if any("401" in n for n in notes) else "")
            raise FetchError(f"Dryad download failed: {notes[0]}{hint}")
        if not files:
            warnings.append("the Dryad version lists no files")
        verified = checksum_verdict(stats, warnings) if attempted else False
        maybe_unzip(dest, warnings)
        return FetchResult(
            input=ref, resolved_type="dryad", src_dir=str(dest),
            pin=Pin(kind="version_doi", value=doi), fetch_layer="dryad-api",
            checksum_verified=verified, warnings=warnings,
            metadata={"doi": doi, "version": version,
                      "version_id": version_href.rstrip("/").rsplit("/", 1)[-1],
                      "checksums": stats},
        )

    @staticmethod
    def _list_files(url: str) -> list[dict]:
        out: list[dict] = []
        for _ in range(_MAX_PAGES):
            r = get(url, timeout=60)
            r.raise_for_status()
            body = r.json() or {}
            out.extend(((body.get("_embedded") or {}).get("stash:files")) or [])
            nxt = _href(body, "next")
            if not nxt:
                return out
            url = urljoin(_SITE, nxt)
        raise FetchError(f"Dryad file listing exceeded {_MAX_PAGES} pages")
