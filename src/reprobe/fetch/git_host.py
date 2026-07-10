"""Git host fetcher (GitHub / GitLab / Bitbucket / Codeberg / any .git URL).

Clones and pins the exact commit SHA. An optional ``...@<ref>`` suffix checks
out a specific commit or tag (then re-pins to the resolved SHA)."""

from __future__ import annotations

import re
from pathlib import Path

from ..models import FetchResult, Pin
from .base import Fetcher, FetchError, run_git

_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org")


class GitHostFetcher:
    name = "git"

    def can_handle(self, ref: str) -> bool:
        r = ref.lower()
        if r.endswith(".git"):
            return True
        if "anonymous.4open.science" in r:
            return False
        return any(h in r for h in _HOSTS) and r.startswith(("http://", "https://", "git@"))

    def fetch(self, ref: str, dest: Path) -> FetchResult:
        url, want_ref = ref, None
        m = re.match(r"^(.*?)(?:@([^@/]+))$", ref)
        # only treat trailing @ref as a pin when it's not part of an scp-like git@ URL
        if m and not ref.startswith("git@") and "/" not in (m.group(2) or ""):
            url, want_ref = m.group(1), m.group(2)

        dest.mkdir(parents=True, exist_ok=True)
        clone = run_git(["clone", "--quiet", url, str(dest)])
        if clone.returncode != 0:
            raise FetchError(f"git clone failed: {clone.stderr.strip()[:300]}")

        if want_ref:
            co = run_git(["checkout", "--quiet", want_ref], cwd=dest)
            if co.returncode != 0:
                raise FetchError(f"git checkout {want_ref} failed: {co.stderr.strip()[:200]}")

        sha = run_git(["rev-parse", "HEAD"], cwd=dest).stdout.strip()
        warnings = []
        if run_git(["lfs", "version"]).returncode == 0:
            warnings.append("git-lfs present; large files were not smudged during fetch (skip-smudge)")

        return FetchResult(
            input=ref, resolved_type="git", src_dir=str(dest),
            pin=Pin(kind="git_sha", value=sha),
            fetch_layer="git-clone", checksum_verified=False,
            warnings=warnings, metadata={"url": url, "ref": want_ref},
        )
