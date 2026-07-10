"""OSF (Open Science Framework) fetcher.

Resolves a project/registration (osf.io/<guid>, or 10.17605/OSF.IO/<GUID> DOI),
walks its osfstorage tree via API v2, and downloads every file. Supports a
``view_only`` anonymized review token (osf.io/<guid>/?view_only=<token>), which
is flagged in the report.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from ..models import FetchResult, Pin
from .base import FetchError

_GUID = re.compile(r"osf\.io/([a-z0-9]{4,})|10\.17605/OSF\.IO/(\w+)", re.I)
_API = "https://api.osf.io/v2"


class OSFFetcher:
    name = "osf"

    def can_handle(self, ref: str) -> bool:
        r = ref.lower()
        return "osf.io" in r or "10.17605/osf.io" in r

    def fetch(self, ref: str, dest: Path) -> FetchResult:
        m = _GUID.search(ref)
        guid = (m.group(1) or m.group(2)) if m else None
        if not guid or guid in ("download", "files"):
            raise FetchError("could not parse OSF guid")
        guid = guid.lower()
        view_only = parse_qs(urlparse(ref).query).get("view_only", [None])[0]
        params = {"view_only": view_only} if view_only else {}
        dest.mkdir(parents=True, exist_ok=True)

        warnings: list[str] = []
        start = f"{_API}/nodes/{guid}/files/osfstorage/"
        try:
            n = self._walk(start, dest, params, warnings)
        except Exception as e:
            raise FetchError(f"OSF API error: {e}")
        if n == 0:
            warnings.append("no files found in osfstorage (private without token, or empty)")

        return FetchResult(
            input=ref, resolved_type="osf", src_dir=str(dest),
            pin=Pin(kind="version_doi", value=f"10.17605/OSF.IO/{guid.upper()}"),
            fetch_layer="osf-api", anonymized=bool(view_only), checksum_verified=False,
            warnings=warnings, metadata={"guid": guid, "view_only": bool(view_only)},
        )

    def _walk(self, url: str, dest: Path, params: dict, warnings: list[str], depth: int = 0) -> int:
        if depth > 8:
            return 0
        count = 0
        while url:
            r = requests.get(url, params=params, timeout=60)
            r.raise_for_status()
            body = r.json()
            for item in body.get("data", []):
                attr = item.get("attributes", {})
                kind = attr.get("kind")
                name = attr.get("name", "item")
                if kind == "file":
                    dl = (item.get("links") or {}).get("download")
                    if dl:
                        out = dest / name
                        out.parent.mkdir(parents=True, exist_ok=True)
                        ok, note = _stream(dl, out, params)
                        count += 1 if ok else 0
                        if not ok:
                            warnings.append(f"{name}: {note}")
                elif kind == "folder":
                    sub = (((item.get("relationships") or {}).get("files") or {})
                           .get("links", {}).get("related", {}).get("href"))
                    if sub:
                        count += self._walk(sub, dest / name, params, warnings, depth + 1)
            url = (body.get("links") or {}).get("next")
        return count


def _stream(url: str, out: Path, params: dict) -> tuple[bool, str]:
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(url, params=params, stream=True, timeout=300) as r:
            r.raise_for_status()
            with out.open("wb") as fh:
                for chunk in r.iter_content(65536):
                    fh.write(chunk)
        return True, "ok"
    except Exception as e:
        return False, str(e)
