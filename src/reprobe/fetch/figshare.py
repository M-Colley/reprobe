"""figshare fetcher. Resolves an article (URL or 10.6084/m9.figshare.<id> DOI)
via the public API, downloads its files, and verifies md5. Pin = version DOI.
A version-explicit reference (…figshare.<id>.v2 / …/articles/…/<id>/2) fetches
exactly that version instead of silently getting latest."""

from __future__ import annotations

import re
from pathlib import Path

from ..models import FetchResult, Pin
from .base import (FetchError, checksum_verdict, download, get, maybe_unzip,
                   new_checksum_stats, record_download, safe_join)

_ID = re.compile(r"figshare\.com/articles/(?:[^/]+/)?[^/]*?/(\d+)(?:/(\d+))?"
                 r"|10\.6084/m9\.figshare\.(\d+)(?:\.v(\d+))?", re.I)


def _parse(ref: str) -> tuple[str | None, str | None]:
    """Extract (article_id, version) — version is None when the ref is unversioned."""
    m = _ID.search(ref)
    if not m:
        return None, None
    return (m.group(1) or m.group(3)), (m.group(2) or m.group(4))


class FigshareFetcher:
    name = "figshare"

    def can_handle(self, ref: str) -> bool:
        r = ref.lower()
        return "figshare.com" in r or "10.6084/m9.figshare" in r

    def fetch(self, ref: str, dest: Path) -> FetchResult:
        article_id, version = _parse(ref)
        if not article_id:
            raise FetchError("could not parse figshare article id")
        dest.mkdir(parents=True, exist_ok=True)
        api_url = (f"https://api.figshare.com/v2/articles/{article_id}/versions/{version}"
                   if version else f"https://api.figshare.com/v2/articles/{article_id}")
        try:
            api = get(api_url, timeout=60)
            api.raise_for_status()
            meta = api.json()
        except Exception as e:
            raise FetchError(f"figshare API error: {e}")

        doi = meta.get("doi") or f"10.6084/m9.figshare.{article_id}"
        files = meta.get("files", []) or []
        warnings = []
        if not version:
            warnings.append("reference did not pin a figshare version; fetched latest "
                            f"({doi}) — cite the version DOI for a stable pin")
        stats = new_checksum_stats()
        for f in files:
            url = f.get("download_url")
            name = f.get("name", "file")
            md5 = f.get("computed_md5") or f.get("supplied_md5")
            if not url:
                warnings.append(f"no download_url for {name}")
                continue
            ok, note = download(url, safe_join(dest, name), expected_md5=md5)
            record_download(stats, ok, note, bool(md5))
            if not ok:
                warnings.append(f"{name}: {note}")
        verified = checksum_verdict(stats, warnings) if files else False
        maybe_unzip(dest, warnings)
        return FetchResult(
            input=ref, resolved_type="figshare", src_dir=str(dest),
            pin=Pin(kind="version_doi", value=doi), fetch_layer="figshare-api",
            checksum_verified=verified, warnings=warnings,
            metadata={"article_id": article_id, "version": version,
                      "title": meta.get("title"), "checksums": stats},
        )
