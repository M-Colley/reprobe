"""Software Heritage fetcher.

Accepts a SWHID (swh:1:dir:... / rev / rel / snp) or an
archive.softwareheritage.org URL. The SWHID is itself the archival pin (the
strongest 'Available' evidence). Retrieving the bytes uses the SWH **vault**,
which cooks bundles asynchronously; we request the cook and grab it if it
finishes quickly, otherwise we return with the archival pin and a 'cooking in
progress' note.

Verification: a ``swh:1:dir:`` id is by definition the git tree hash of the
directory, so the fetched tree is re-hashed with the reference implementation
(``swh.model``, the optional ``reprobe[swh]`` extra) and compared. A rev/rel
SWHID is first resolved to its root directory through the SWH API; the tree is
then verified against THAT id, and the report says the commit object itself was
not re-hashed. A snapshot names many branches and is pinned but not retrieved.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from ..models import FetchResult, Pin
from .base import FetchError, download, get, maybe_unzip, post

_SWHID = re.compile(r"(swh:1:(?:dir|rev|snp|rel|cnt):[0-9a-f]{40})", re.I)
_SHA1_GIT = re.compile(r"^[0-9a-f]{40}$")
_API = "https://archive.softwareheritage.org/api/1"
NO_SWH_MODEL = "install reprobe[swh] to verify the SWHID"


def _directory_of(swhid: str) -> str | None:
    """The root directory SWHID of a revision or release, per the SWH API."""
    _, _, kind, obj = swhid.lower().split(":")
    if kind == "rel":
        r = get(f"{_API}/release/{obj}/", timeout=30)
        r.raise_for_status()
        body = r.json() or {}
        target, ttype = str(body.get("target") or ""), body.get("target_type")
        if not _SHA1_GIT.match(target):
            return None
        if ttype == "directory":
            return f"swh:1:dir:{target}"
        if ttype != "revision":
            return None
        obj = target
    r = get(f"{_API}/revision/{obj}/", timeout=30)
    r.raise_for_status()
    directory = str((r.json() or {}).get("directory") or "")
    return f"swh:1:dir:{directory}" if _SHA1_GIT.match(directory) else None


def _bundle_root(dest: Path, dir_swhid: str) -> Path | None:
    """The directory the vault bundle unpacked to (named after the SWHID)."""
    for name in (dir_swhid, dir_swhid.rsplit(":", 1)[-1]):
        cand = dest / name
        try:
            if cand.is_dir() and not cand.is_symlink():
                return cand
        except OSError:                          # ':' is not a legal name on Windows
            continue
    dirs = [d for d in dest.iterdir() if d.is_dir() and not d.is_symlink()]
    return dirs[0] if len(dirs) == 1 else None


def verify_directory(root: Path, dir_swhid: str, warnings: list[str],
                     meta: dict) -> bool:
    """Re-hash ``root`` as a SWH directory and compare it to ``dir_swhid``."""
    try:
        from swh.model.from_disk import Directory
    except ImportError:
        meta["swhid_verification"] = NO_SWH_MODEL
        warnings.append(f"the fetched tree was NOT checked against {dir_swhid}: {NO_SWH_MODEL}")
        return False
    try:
        node = Directory.from_disk(path=os.fsencode(str(root)), max_content_length=None)
        got = str(node.swhid())
    except Exception as e:                       # noqa: BLE001 - verification is best-effort
        meta["swhid_verification"] = f"hashing failed: {type(e).__name__}"
        warnings.append(f"the fetched tree was NOT checked against {dir_swhid}: "
                        f"hashing it failed ({type(e).__name__})")
        return False
    meta["swhid_computed"] = got
    if got.lower() != dir_swhid.lower():
        meta["swhid_verification"] = "mismatch"
        warnings.append(f"SWHID MISMATCH: the fetched tree hashes to {got}, not {dir_swhid} "
                        "(see any extraction warning above)")
        return False
    meta["swhid_verification"] = "verified"
    return True


class SoftwareHeritageFetcher:
    name = "software_heritage"

    def can_handle(self, ref: str) -> bool:
        r = ref.lower()
        return r.startswith("swh:1:") or "softwareheritage.org" in r

    def fetch(self, ref: str, dest: Path) -> FetchResult:
        m = _SWHID.search(ref)
        if not m:
            raise FetchError("could not parse a SWHID")
        swhid = m.group(1).lower()
        dest.mkdir(parents=True, exist_ok=True)
        warnings: list[str] = []
        meta: dict = {"swhid": swhid}

        kind = swhid.split(":")[2]
        dir_swhid = swhid if kind == "dir" else None
        if kind in ("rev", "rel"):
            try:
                dir_swhid = _directory_of(swhid)
            except Exception as e:               # noqa: BLE001 - the pin stands regardless
                warnings.append(f"could not resolve {swhid} to its root directory: {e}")
            if dir_swhid:
                meta["directory"] = dir_swhid
                warnings.append(f"{swhid} resolves to {dir_swhid} per the Software Heritage API; "
                                "the fetched tree is checked against that directory id (the "
                                f"{'release/' if kind == 'rel' else ''}revision object itself "
                                "was not re-hashed)")
        elif kind != "dir":
            warnings.append(f"{swhid} is not a directory SWHID; resolve to a swh:1:dir: for retrieval")

        verified = False
        if dir_swhid:
            try:
                post(f"{_API}/vault/flat/{dir_swhid}/", timeout=60)
                bundle = None
                for _ in range(3):                       # brief poll; cooking is async
                    st = get(f"{_API}/vault/flat/{dir_swhid}/", timeout=30).json()
                    if st.get("status") == "done":
                        bundle = st.get("fetch_url") or f"{_API}/vault/flat/{dir_swhid}/raw/"
                        break
                    time.sleep(5)
                if bundle:
                    ok, note = download(bundle, dest / "swh_bundle.tar.gz", timeout=600, restrict_public=True)
                    if ok:
                        maybe_unzip(dest, warnings)
                        root = _bundle_root(dest, dir_swhid)
                        if root is None:
                            warnings.append(f"the fetched tree was NOT checked against {dir_swhid}: "
                                            "the bundle's root directory could not be found")
                        else:
                            verified = verify_directory(root, dir_swhid, warnings, meta)
                    else:
                        warnings.append(f"bundle download: {note}")
                else:
                    warnings.append("SWH vault cooking in progress; archival pin recorded, re-run to fetch bytes")
            except Exception as e:
                warnings.append(f"SWH vault error: {e}")

        return FetchResult(
            input=ref, resolved_type="software_heritage", src_dir=str(dest),
            pin=Pin(kind="swhid", value=swhid), fetch_layer="swh-vault",
            checksum_verified=verified, warnings=warnings, metadata=meta,
        )
