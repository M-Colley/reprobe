"""OSF (Open Science Framework) fetcher.

Resolves a project/registration (osf.io/<guid>, or 10.17605/OSF.IO/<GUID> DOI),
walks its osfstorage tree via API v2, and downloads every file. Supports a
``view_only`` anonymized review token (osf.io/<guid>/?view_only=<token>), which
is flagged in the report.

Pin honesty: OSF *project* storage is mutable and a 10.17605 DOI only exists if
the author minted one — so the pin is only kind=version_doi when the API shows
a minted DOI on a frozen registration; otherwise kind=none plus a warning.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..models import FetchResult, Pin
from .base import (FetchError, checksum_verdict, download, get,
                   new_checksum_stats, record_download, safe_join)

_GUID = re.compile(r"osf\.io/([a-z0-9]{4,})|10\.17605/OSF\.IO/(\w+)", re.I)
_API = "https://api.osf.io/v2"

# Path segments that look like a guid but name a route, not a node.
_NOT_A_GUID = {"download", "files", "project", "search", "settings", "dashboard",
               "preprints", "registries", "institutions"}


def guid_of(ref: str) -> str | None:
    """The project/registration guid in ``ref``, or None if there isn't one."""
    m = _GUID.search(ref)
    guid = (m.group(1) or m.group(2)) if m else None
    if not guid or guid.lower() in _NOT_A_GUID:
        return None
    return guid.lower()


def _decide_pin(node_type: str, doi: str | None, guid: str) -> tuple[Pin, list[str]]:
    """version_doi only for a minted DOI on a frozen registration — never
    fabricate an archival pin for mutable project storage."""
    if doi and node_type == "registrations":
        return Pin(kind="version_doi", value=doi), []
    if node_type == "registrations":
        return Pin(kind="none", value=f"osf.io/{guid}"), [
            "OSF registration has no minted DOI; mint one for an archival pin"]
    return Pin(kind="none", value=f"osf.io/{guid}"), [
        "OSF project storage is mutable — register the project or deposit in an "
        "archival repository for the Available badge"]


class OSFFetcher:
    name = "osf"

    def can_handle(self, ref: str) -> bool:
        # Claim only what fetch() can actually resolve. "osf.io" also appears in
        # OSF's file-server bundle links — files.de-1.osf.io/v1/resources/<guid>/
        # providers/osfstorage/<id>/?zip= — which is the form READMEs paste and
        # which carries no node guid in the position fetch() walks. Claiming them
        # turned a perfectly usable direct download into "could not parse OSF guid".
        return guid_of(ref) is not None

    def fetch(self, ref: str, dest: Path) -> FetchResult:
        guid = guid_of(ref)
        if not guid:
            raise FetchError("could not parse OSF guid")
        view_only = parse_qs(urlparse(ref).query).get("view_only", [None])[0]
        params = {"view_only": view_only} if view_only else {}
        dest.mkdir(parents=True, exist_ok=True)

        warnings: list[str] = []
        node_type, doi = self._resolve_node(guid, params, warnings)
        pin, pin_warnings = _decide_pin(node_type, doi, guid)
        warnings.extend(pin_warnings)

        stats = new_checksum_stats()
        start = f"{_API}/{node_type}/{guid}/files/osfstorage/"
        try:
            n = self._walk(start, dest, params, warnings, stats)
        except Exception as e:
            raise FetchError(f"OSF API error: {e}")
        if n == 0:
            warnings.append("no files found in osfstorage (private without token, or empty)")

        return FetchResult(
            input=ref, resolved_type="osf", src_dir=str(dest),
            pin=pin, fetch_layer="osf-api", anonymized=bool(view_only),
            checksum_verified=checksum_verdict(stats, warnings) if n else False,
            warnings=warnings,
            metadata={"guid": guid, "view_only": bool(view_only),
                      "osf_type": node_type, "doi": doi, "checksums": stats},
        )

    def _resolve_node(self, guid: str, params: dict, warnings: list[str]) -> tuple[str, str | None]:
        """Resolve the guid's resource type (nodes vs registrations) and its
        minted DOI, if any. Failures degrade to (nodes, None) — never a pin."""
        node_type = "nodes"
        try:
            g = get(f"{_API}/guids/{guid}/", params=params, timeout=60)
            g.raise_for_status()
            node_type = (g.json().get("data") or {}).get("type") or "nodes"
        except Exception:
            warnings.append("could not resolve OSF guid metadata; assuming a project node")
        doi = None
        try:
            idr = get(f"{_API}/{node_type}/{guid}/identifiers/", params=params, timeout=60)
            if idr.ok:
                for item in idr.json().get("data", []):
                    attr = item.get("attributes", {})
                    if attr.get("category") == "doi" and attr.get("value"):
                        doi = attr["value"]
                        break
        except Exception:
            pass
        return node_type, doi

    def _walk(self, url: str, dest: Path, params: dict, warnings: list[str],
              stats: dict[str, int], depth: int = 0) -> int:
        if depth > 8:
            return 0
        count = 0
        while url:
            r = get(url, params=params, timeout=60)
            r.raise_for_status()
            body = r.json()
            for item in body.get("data", []):
                attr = item.get("attributes", {})
                kind = attr.get("kind")
                name = attr.get("name", "item")
                if kind == "file":
                    dl = (item.get("links") or {}).get("download")
                    if dl:
                        md5 = ((attr.get("extra") or {}).get("hashes") or {}).get("md5")
                        ok, note = download(dl, safe_join(dest, name),
                                            expected_md5=md5, params=params)
                        record_download(stats, ok, note, bool(md5))
                        count += 1 if ok else 0
                        if not ok:
                            warnings.append(f"{name}: {note}")
                elif kind == "folder":
                    sub = (((item.get("relationships") or {}).get("files") or {})
                           .get("links", {}).get("related", {}).get("href"))
                    if sub:
                        count += self._walk(sub, safe_join(dest, name), params,
                                            warnings, stats, depth + 1)
            url = (body.get("links") or {}).get("next")
        return count
