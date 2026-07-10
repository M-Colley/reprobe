"""Dataverse fetcher (Harvard Dataverse and any Dataverse install).

Resolves a dataset by persistent id (a DOI/Handle in the URL's persistentId, or
a /dataset.xhtml?persistentId=doi:... link) and downloads the whole-dataset zip
from the host in the URL. Pin = version DOI.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from ..models import FetchResult, Pin
from .base import FetchError, download, maybe_unzip

_PID = re.compile(r"persistentId=([^&]+)", re.I)
_DOI = re.compile(r"(doi:10\.\S+|hdl:\S+)", re.I)


class DataverseFetcher:
    name = "dataverse"

    def can_handle(self, ref: str) -> bool:
        r = ref.lower()
        return "dataverse" in r and ("persistentid=" in r or "/dataset" in r)

    def fetch(self, ref: str, dest: Path) -> FetchResult:
        parsed = urlparse(ref)
        host = f"{parsed.scheme or 'https'}://{parsed.netloc}"
        pid = None
        m = _PID.search(ref)
        if m:
            pid = m.group(1)
        else:
            d = _DOI.search(ref)
            pid = d.group(1) if d else None
        if not pid or not parsed.netloc:
            raise FetchError("could not parse Dataverse host + persistentId")
        dest.mkdir(parents=True, exist_ok=True)
        warnings: list[str] = []
        url = f"{host}/api/access/dataset/:persistentId/?persistentId={pid}"
        ok, note = download(url, dest / "dataverse_dataset.zip", timeout=600)
        if not ok:
            raise FetchError(f"Dataverse download failed: {note}")
        maybe_unzip(dest, warnings)
        doi = pid[4:] if pid.lower().startswith("doi:") else pid
        return FetchResult(
            input=ref, resolved_type="dataverse", src_dir=str(dest),
            pin=Pin(kind="version_doi" if pid.lower().startswith("doi:") else "none", value=doi),
            fetch_layer="dataverse-api", checksum_verified=False, warnings=warnings,
            metadata={"host": host, "persistent_id": pid},
        )
