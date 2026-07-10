"""Dryad fetcher. Resolves a dataset (URL or 10.5061/dryad.<id> DOI) and pulls
the whole-dataset archive via the v2 API. Pin = version DOI."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

import requests

from ..models import FetchResult, Pin
from .base import FetchError, download, maybe_unzip

_DOI = re.compile(r"(10\.5061/dryad\.[^\s/?#]+)", re.I)


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
        dest.mkdir(parents=True, exist_ok=True)
        encoded = quote(f"doi:{doi}", safe="")
        warnings = []
        # The dataset-level download endpoint streams a zip of the latest version.
        url = f"https://datadryad.org/api/v2/datasets/{encoded}/download"
        ok, note = download(url, dest / "dryad_dataset.zip", timeout=600)
        if not ok:
            raise FetchError(f"Dryad download failed: {note}")
        maybe_unzip(dest, warnings)
        return FetchResult(
            input=ref, resolved_type="dryad", src_dir=str(dest),
            pin=Pin(kind="version_doi", value=doi), fetch_layer="dryad-api",
            checksum_verified=False, warnings=warnings, metadata={"doi": doi},
        )
