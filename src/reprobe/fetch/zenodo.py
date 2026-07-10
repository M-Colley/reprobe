"""Zenodo fetcher. Resolves a Zenodo record (or a 10.5281/zenodo.<id> DOI) via
the public REST API, downloads its files, and verifies md5 checksums. The pin is
the version DOI — exactly what the Available badge should attest."""

from __future__ import annotations

import re
from pathlib import Path

from ..models import FetchResult, Pin
from .base import (FetchError, checksum_verdict, download, get,
                   new_checksum_stats, record_download, safe_join)

_ID_RE = re.compile(r"(?:zenodo\.org/records?/|10\.5281/zenodo\.)(\d+)", re.I)


class ZenodoFetcher:
    name = "zenodo"

    def can_handle(self, ref: str) -> bool:
        return bool(_ID_RE.search(ref))

    def fetch(self, ref: str, dest: Path) -> FetchResult:
        m = _ID_RE.search(ref)
        if not m:
            raise FetchError("not a Zenodo reference")
        rec_id = m.group(1)
        dest.mkdir(parents=True, exist_ok=True)
        try:
            api = get(f"https://zenodo.org/api/records/{rec_id}", timeout=60)
            api.raise_for_status()
            meta = api.json()
        except Exception as e:
            raise FetchError(f"Zenodo API error: {e}")

        doi = meta.get("doi") or f"10.5281/zenodo.{rec_id}"
        files = meta.get("files", []) or []
        warnings = []
        stats = new_checksum_stats()
        for f in files:
            url = (f.get("links") or {}).get("self") or (f.get("links") or {}).get("download")
            key = f.get("key") or f.get("filename") or "file"
            md5 = f.get("checksum")
            if not url:
                warnings.append(f"no download link for {key}")
                continue
            ok, note = download(url, safe_join(dest, key), expected_md5=md5)
            record_download(stats, ok, note, bool(md5))
            if not ok:
                warnings.append(f"{key}: {note}")
        verified = checksum_verdict(stats, warnings) if files else False

        # auto-unzip a single archive for convenience
        _maybe_unzip(dest, warnings)

        return FetchResult(
            input=ref, resolved_type="zenodo", src_dir=str(dest),
            pin=Pin(kind="version_doi", value=doi),
            fetch_layer="zenodo-api", checksum_verified=verified,
            warnings=warnings,
            metadata={"record_id": rec_id, "title": meta.get("metadata", {}).get("title"),
                      "license": (meta.get("metadata", {}).get("license") or {}),
                      "checksums": stats},
        )


def _maybe_unzip(dest: Path, warnings: list[str]) -> None:
    import zipfile
    zips = list(dest.glob("*.zip"))
    if len(zips) == 1:
        try:
            with zipfile.ZipFile(zips[0]) as z:
                z.extractall(dest)
        except Exception as e:
            warnings.append(f"could not extract {zips[0].name}: {e}")
