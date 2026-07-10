"""Fetcher contract + shared helpers. A fetcher resolves an input (URL / DOI /
path) to a local source tree and an archival *pin* (the thing that makes the
Available badge meaningful: a version DOI, a commit SHA, or a SWHID — never a
moving tag)."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

import requests

from ..models import FetchResult


@runtime_checkable
class Fetcher(Protocol):
    name: str
    def can_handle(self, ref: str) -> bool: ...
    def fetch(self, ref: str, dest: Path) -> FetchResult: ...


class FetchError(RuntimeError):
    pass


def run_git(args: list[str], cwd: Optional[Path] = None, timeout: int = 600) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["GIT_LFS_SKIP_SMUDGE"] = "1"        # don't pull large LFS blobs during fetch
    env["GIT_TERMINAL_PROMPT"] = "0"        # never hang on a credential prompt
    return subprocess.run(["git", *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=timeout)


def maybe_unzip(dest: Path, warnings: list[str]) -> None:
    """If the deposit is a single .zip/.tar.gz archive, extract it in place."""
    import tarfile
    import zipfile
    zips = list(dest.glob("*.zip"))
    tars = list(dest.glob("*.tar.gz")) + list(dest.glob("*.tgz"))
    try:
        if len(zips) == 1 and not tars:
            with zipfile.ZipFile(zips[0]) as z:
                z.extractall(dest)
        elif len(tars) == 1 and not zips:
            with tarfile.open(tars[0]) as t:
                t.extractall(dest)
    except Exception as e:  # pragma: no cover - defensive
        warnings.append(f"could not extract archive: {e}")


def download(url: str, dest: Path, *, expected_md5: str | None = None, timeout: int = 300) -> tuple[bool, str]:
    """Stream a file to dest; verify md5 if provided. Returns (ok, note)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.md5()
    try:
        with requests.get(url, stream=True, timeout=timeout) as r:
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
