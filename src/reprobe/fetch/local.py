"""Local path 'fetcher' — used for fixtures, offline testing, and pre-downloaded
artifacts. Records the directory as-is with no archival pin."""

from __future__ import annotations

import shutil
from pathlib import Path

from ..models import FetchResult, Pin
from .base import FetchError


class LocalFetcher:
    name = "local"

    def can_handle(self, ref: str) -> bool:
        try:
            return Path(ref).exists()
        except OSError:
            return False

    def fetch(self, ref: str, dest: Path) -> FetchResult:
        src = Path(ref).resolve()
        if not src.exists():
            raise FetchError(f"path does not exist: {ref}")
        dest.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dest / src.name)
        return FetchResult(
            input=ref, resolved_type="local", src_dir=str(dest),
            pin=Pin(kind="none", value=str(src)),
            fetch_layer="local-copy",
            warnings=["local path: no archival pin (Available badge requires an archival deposit)"],
        )
