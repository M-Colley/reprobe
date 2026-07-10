"""figshare fetcher. Resolves an article (URL or 10.6084/m9.figshare.<id> DOI)
via the public API, downloads its files, and verifies md5. Pin = version DOI."""

from __future__ import annotations

import re
from pathlib import Path

import requests

from ..models import FetchResult, Pin
from .base import FetchError, download, maybe_unzip

_ID = re.compile(r"figshare\.com/articles/(?:[^/]+/)?[^/]*?/(\d+)|10\.6084/m9\.figshare\.(\d+)", re.I)


class FigshareFetcher:
    name = "figshare"

    def can_handle(self, ref: str) -> bool:
        r = ref.lower()
        return "figshare.com" in r or "10.6084/m9.figshare" in r

    def fetch(self, ref: str, dest: Path) -> FetchResult:
        m = _ID.search(ref)
        article_id = (m.group(1) or m.group(2)) if m else None
        if not article_id:
            raise FetchError("could not parse figshare article id")
        dest.mkdir(parents=True, exist_ok=True)
        try:
            api = requests.get(f"https://api.figshare.com/v2/articles/{article_id}", timeout=60)
            api.raise_for_status()
            meta = api.json()
        except Exception as e:
            raise FetchError(f"figshare API error: {e}")

        doi = meta.get("doi") or f"10.6084/m9.figshare.{article_id}"
        files = meta.get("files", []) or []
        verified, warnings = True, []
        for f in files:
            url = f.get("download_url")
            name = f.get("name", "file")
            md5 = f.get("computed_md5") or f.get("supplied_md5")
            if not url:
                warnings.append(f"no download_url for {name}")
                continue
            ok, note = download(url, dest / name, expected_md5=md5)
            if not ok:
                verified = False
                warnings.append(f"{name}: {note}")
        maybe_unzip(dest, warnings)
        return FetchResult(
            input=ref, resolved_type="figshare", src_dir=str(dest),
            pin=Pin(kind="version_doi", value=doi), fetch_layer="figshare-api",
            checksum_verified=verified and bool(files), warnings=warnings,
            metadata={"article_id": article_id, "title": meta.get("title")},
        )
