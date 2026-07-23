"""Anonymous GitHub (anonymous.4open.science) fetcher for double-blind review.

These links can expire mid-review and serve a snapshot, not git history, so the
pin is a (non-archival) snapshot id and the report flags ``anonymized=True`` and
that no durable archival pin is available."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from ..models import FetchResult, Pin
from .base import FetchError, download, guard_zip

_ID_RE = re.compile(r"anonymous\.4open\.science/(?:r|api/repo)/([A-Za-z0-9_-]+)", re.I)


class AnonymousGithubFetcher:
    name = "anonymous_github"

    def can_handle(self, ref: str) -> bool:
        return "anonymous.4open.science" in ref.lower()

    def fetch(self, ref: str, dest: Path) -> FetchResult:
        m = _ID_RE.search(ref)
        if not m:
            raise FetchError("could not parse anonymous.4open.science id")
        repo_id = m.group(1)
        dest.mkdir(parents=True, exist_ok=True)
        url = f"https://anonymous.4open.science/api/repo/{repo_id}/zip"
        # Stream to disk with a byte cap instead of buffering the whole response
        # in RAM (r.content) — a large/hostile snapshot could otherwise OOM the host.
        zip_path = dest / "_anon_repo.zip"
        ok, note = download(url, zip_path, timeout=120)
        if not ok:
            raise FetchError(f"anonymous github download failed: {note}")
        try:
            with zipfile.ZipFile(zip_path) as z:
                guard_zip(z)                # reject a decompression bomb
                z.extractall(dest)
        except Exception as e:
            raise FetchError(f"anonymous github extract failed: {e}")
        finally:
            zip_path.unlink(missing_ok=True)

        return FetchResult(
            input=ref, resolved_type="anonymous_github", src_dir=str(dest),
            pin=Pin(kind="none", value=repo_id),
            fetch_layer="anon-zip", anonymized=True, checksum_verified=False,
            warnings=["anonymized review snapshot: no archival pin; link may expire. "
                      "Available badge needs a durable archival deposit before publication."],
            metadata={"repo_id": repo_id},
        )
