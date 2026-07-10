"""Fetcher contract + shared helpers. A fetcher resolves an input (URL / DOI /
path) to a local source tree and an archival *pin* (the thing that makes the
Available badge meaningful: a version DOI, a commit SHA, or a SWHID — never a
moving tag)."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tarfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Optional, Protocol, runtime_checkable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..models import FetchResult


@runtime_checkable
class Fetcher(Protocol):
    name: str
    def can_handle(self, ref: str) -> bool: ...
    def fetch(self, ref: str, dest: Path) -> FetchResult: ...


class FetchError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# HTTP: one shared session with retry/backoff so a transient 429/5xx from a
# platform API doesn't abort a whole fetch (or silently truncate an artifact).
# ---------------------------------------------------------------------------

def _make_session() -> requests.Session:
    kw = dict(total=3, backoff_factor=2,
              status_forcelist=[429, 500, 502, 503, 504],
              respect_retry_after_header=True)
    try:
        retry = Retry(allowed_methods=None, **kw)   # None = retry all methods (incl. the idempotent SWH vault POST)
    except TypeError:  # urllib3 < 1.26
        retry = Retry(method_whitelist=None, **kw)
    s = requests.Session()
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


_SESSION = _make_session()


def get(url: str, **kwargs) -> requests.Response:
    """GET via the shared retrying session. All fetcher API calls go through here."""
    return _SESSION.get(url, **kwargs)


def post(url: str, **kwargs) -> requests.Response:
    """POST via the shared retrying session."""
    return _SESSION.post(url, **kwargs)


def run_git(args: list[str], cwd: Optional[Path] = None, timeout: int = 600) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["GIT_LFS_SKIP_SMUDGE"] = "1"        # don't pull large LFS blobs during fetch
    env["GIT_TERMINAL_PROMPT"] = "0"        # never hang on a credential prompt
    try:
        return subprocess.run(["git", *args], cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise FetchError("git is not installed or not on PATH — see the chair runbook prerequisites")
    except subprocess.TimeoutExpired:
        raise FetchError(f"git {args[0]} timed out after {timeout}s "
                         f"(large repo or slow network); re-run or clone manually")


# ---------------------------------------------------------------------------
# Host-side path safety. Archive members and platform-supplied file names are
# untrusted bytes handled outside the container sandbox — never let them write
# outside the fetch destination.
# ---------------------------------------------------------------------------

def safe_join(dest: Path, name: str) -> Path:
    """Join a platform-supplied file name/path under dest, refusing traversal.

    Normalizes both separator styles, drops drive/anchor and '.'/'..' components
    (so legitimate directory-style names like Zenodo keys still nest), then
    asserts containment as a belt-and-braces check."""
    norm = str(name).replace("\\", "/")
    parts = [p for p in PurePosixPath(norm).parts
             if p not in ("/", ".", "..") and ":" not in p]
    out = dest.joinpath(*parts) if parts else dest / "file"
    if not out.resolve().is_relative_to(dest.resolve()):
        raise FetchError(f"unsafe file name from platform API: {name!r}")
    return out


def _check_tar_member(m: tarfile.TarInfo) -> None:
    """Manual member validation for interpreters without extractall(filter=)."""
    name = m.name.replace("\\", "/")
    pp = PurePosixPath(name)
    if pp.is_absolute() or PureWindowsPath(m.name).drive or ".." in pp.parts:
        raise ValueError(f"unsafe tar member path: {m.name!r}")
    if m.issym() or m.islnk():
        link = m.linkname.replace("\\", "/")
        if PurePosixPath(link).is_absolute() or PureWindowsPath(m.linkname).drive:
            raise ValueError(f"tar link target is absolute: {m.name!r} -> {m.linkname!r}")
        # symlink targets are relative to the member's directory; hardlink
        # targets are relative to the archive root
        base = str(pp.parent) if m.issym() else "."
        target = os.path.normpath(os.path.join(base, link)).replace("\\", "/")
        if target == ".." or target.startswith("../"):
            raise ValueError(f"tar link escapes destination: {m.name!r} -> {m.linkname!r}")
    elif not (m.isfile() or m.isdir()):
        raise ValueError(f"unsupported tar member type: {m.name!r}")


def _extract_tar(t: tarfile.TarFile, dest: Path) -> None:
    try:
        t.extractall(dest, filter="data")   # sanitizes members (3.12+, backported to 3.10.12/3.11.4)
        return
    except TypeError:
        pass                                # older interpreter: validate by hand
    members = t.getmembers()
    for m in members:
        _check_tar_member(m)
    t.extractall(dest, members=members)


def maybe_unzip(dest: Path, warnings: list[str]) -> None:
    """If the deposit is a single .zip/.tar.gz archive, extract it in place."""
    import zipfile
    zips = list(dest.glob("*.zip"))
    tars = list(dest.glob("*.tar.gz")) + list(dest.glob("*.tgz"))
    try:
        if len(zips) == 1 and not tars:
            with zipfile.ZipFile(zips[0]) as z:
                z.extractall(dest)          # zipfile sanitizes member paths itself
        elif len(tars) == 1 and not zips:
            with tarfile.open(tars[0]) as t:
                _extract_tar(t, dest)
    except Exception as e:
        warnings.append(f"could not extract archive: {e}")


# ---------------------------------------------------------------------------
# Downloads + checksum honesty. checksum_verified=True means at least one
# platform checksum was actually compared and every compared one matched —
# never "nothing failed because nothing was checked".
# ---------------------------------------------------------------------------

def download(url: str, dest: Path, *, expected_md5: str | None = None,
             params: dict | None = None, timeout: int = 300) -> tuple[bool, str]:
    """Stream a file to dest; verify md5 if provided. Returns (ok, note)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.md5()
    try:
        with _SESSION.get(url, params=params, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in r.iter_content(65536):
                    fh.write(chunk)
                    h.update(chunk)
    except Exception as e:
        return False, f"download failed: {e}"
    if expected_md5:
        if h.hexdigest() != expected_md5.replace("md5:", ""):
            return False, "checksum mismatch"
        return True, "checksum verified"
    return True, "downloaded (no checksum provided)"


def new_checksum_stats() -> dict[str, int]:
    return {"verified": 0, "not_provided": 0, "mismatch": 0, "failed": 0}


def record_download(stats: dict[str, int], ok: bool, note: str, had_checksum: bool) -> None:
    """Accumulate per-file checksum tri-state (verified / not_provided / mismatch / failed)."""
    if ok:
        stats["verified" if had_checksum else "not_provided"] += 1
    else:
        stats["mismatch" if note == "checksum mismatch" else "failed"] += 1


def checksum_verdict(stats: dict[str, int], warnings: list[str]) -> bool:
    """Fold per-file stats into the report's checksum_verified, appending
    honesty warnings for whatever was *not* verified."""
    if stats["not_provided"] and not stats["verified"] and not stats["mismatch"]:
        warnings.append("platform provided no checksums; file integrity not verified")
    elif stats["not_provided"]:
        warnings.append(f"{stats['not_provided']} file(s) had no platform checksum")
    return stats["verified"] > 0 and not stats["mismatch"] and not stats["failed"]
