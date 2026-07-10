"""Software Heritage fetcher.

Accepts a SWHID (swh:1:dir:... / rev / snp) or an archive.softwareheritage.org
URL. The SWHID is itself the archival pin (the strongest 'Available' evidence).
Retrieving the bytes uses the SWH **vault**, which cooks bundles asynchronously;
we request the cook and grab it if it finishes quickly, otherwise we return with
the archival pin and a 'cooking in progress' note (Available still stands).
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import requests

from ..models import FetchResult, Pin
from .base import FetchError, download, maybe_unzip

_SWHID = re.compile(r"(swh:1:(?:dir|rev|snp|rel|cnt):[0-9a-f]{40})", re.I)
_API = "https://archive.softwareheritage.org/api/1"


class SoftwareHeritageFetcher:
    name = "software_heritage"

    def can_handle(self, ref: str) -> bool:
        r = ref.lower()
        return r.startswith("swh:1:") or "softwareheritage.org" in r

    def fetch(self, ref: str, dest: Path) -> FetchResult:
        m = _SWHID.search(ref)
        if not m:
            raise FetchError("could not parse a SWHID")
        swhid = m.group(1)
        dest.mkdir(parents=True, exist_ok=True)
        warnings: list[str] = []

        dir_swhid = swhid if swhid.split(":")[2] == "dir" else None
        if dir_swhid is None:
            warnings.append(f"{swhid} is not a directory SWHID; resolve to a swh:1:dir: for retrieval")
        else:
            try:
                requests.post(f"{_API}/vault/flat/{dir_swhid}/", timeout=60)
                bundle = None
                for _ in range(3):                       # brief poll; cooking is async
                    st = requests.get(f"{_API}/vault/flat/{dir_swhid}/", timeout=30).json()
                    if st.get("status") == "done":
                        bundle = st.get("fetch_url") or f"{_API}/vault/flat/{dir_swhid}/raw/"
                        break
                    time.sleep(5)
                if bundle:
                    ok, note = download(bundle, dest / "swh_bundle.tar.gz", timeout=600)
                    if ok:
                        maybe_unzip(dest, warnings)
                    else:
                        warnings.append(f"bundle download: {note}")
                else:
                    warnings.append("SWH vault cooking in progress; archival pin recorded, re-run to fetch bytes")
            except Exception as e:
                warnings.append(f"SWH vault error: {e}")

        return FetchResult(
            input=ref, resolved_type="software_heritage", src_dir=str(dest),
            pin=Pin(kind="swhid", value=swhid), fetch_layer="swh-vault",
            checksum_verified=False, warnings=warnings, metadata={"swhid": swhid},
        )
