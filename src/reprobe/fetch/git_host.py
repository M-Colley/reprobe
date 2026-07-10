"""Git host fetcher (GitHub / GitLab / Bitbucket / Codeberg / any .git URL).

Clones and pins the exact commit SHA. Authors overwhelmingly paste browser URLs
(…/tree/<branch>, …/blob/…, …/releases/tag/…), so known-host URLs are normalized
to a clone URL + checkout ref before cloning. An optional ``...@<ref>`` suffix
also checks out a specific commit or tag (then re-pins to the resolved SHA)."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from ..models import FetchResult, Pin
from .base import Fetcher, FetchError, run_git

_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org")


def normalize_browser_url(ref: str, hosts: tuple[str, ...] = _HOSTS) -> tuple[str, str | None]:
    """Translate a pasted browser URL on a known host into (clone_url, want_ref).

    Strips query/fragment; translates /tree/<ref>, /blob/<ref>/…, /commit/<sha>,
    /releases/tag/<tag> (and GitLab's /-/ variants, so nested groups survive).
    Unknown hosts and non-http refs pass through verbatim."""
    if not ref.startswith(("http://", "https://")):
        return ref, None
    p = urlparse(ref)
    host = p.netloc.lower()
    if host not in hosts:
        return ref, None
    path = p.path.strip("/")
    base = f"{p.scheme}://{p.netloc}"
    if "/-/" in path:                                   # GitLab browser URLs
        repo, _, rest = path.partition("/-/")
        return f"{base}/{repo}", _ref_from_segments(rest.split("/"))
    segs = [s for s in path.split("/") if s]
    if len(segs) < 2:
        return ref, None                                # not a repo path; let git report it
    want = _ref_from_segments(segs[2:])
    if "gitlab" in host and want is None:
        return f"{base}/{path}", None                   # nested group, no /-/ marker
    return f"{base}/{'/'.join(segs[:2])}", want


def _ref_from_segments(segs: list[str]) -> str | None:
    if not segs:
        return None
    if segs[0] in ("tree", "blob", "commit", "raw", "tags") and len(segs) > 1:
        return segs[1]
    if segs[0] == "releases" and len(segs) > 2 and segs[1] == "tag":
        return segs[2]
    return None


class GitHostFetcher:
    name = "git"

    def __init__(self, extra_hosts: tuple[str, ...] = ()):
        # chair-supplied institutional hosts (config/pins.yaml fetch.extra_git_hosts)
        self.hosts = _HOSTS + tuple(h.lower() for h in extra_hosts)

    def can_handle(self, ref: str) -> bool:
        r = ref.lower()
        if r.endswith(".git"):
            return True
        if "anonymous.4open.science" in r:
            return False
        return any(h in r for h in self.hosts) and r.startswith(("http://", "https://", "git@"))

    def fetch(self, ref: str, dest: Path) -> FetchResult:
        url, want_ref = ref, None
        m = re.match(r"^(.*?)(?:@([^@/]+))$", ref)
        # only treat trailing @ref as a pin when it's not part of an scp-like git@ URL
        if m and not ref.startswith("git@") and "/" not in (m.group(2) or ""):
            url, want_ref = m.group(1), m.group(2)
        url, browser_ref = normalize_browser_url(url, self.hosts)
        want_ref = want_ref or browser_ref

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
