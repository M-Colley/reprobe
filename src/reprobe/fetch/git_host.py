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
from .base import _MAX_LFS_TOTAL_BYTES, FetchError, assert_safe_url, run_git

_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org")


def _lfs_tracked_files(dest: Path) -> tuple[bool, list[str]]:
    """(query_succeeded, LFS-tracked paths) for the checked-out ref.

    This is the ONLY sound test for "does this repo use LFS". ``git lfs version``
    merely proves the git-lfs CLI is installed on the *host* — using it as the
    gate made every fetch on an LFS-equipped machine warn about unsmudged data
    even for repos with no LFS at all."""
    res = run_git(["lfs", "ls-files", "-n"], cwd=dest)
    if res.returncode != 0:
        return (False, [])
    return (True, [ln.strip() for ln in res.stdout.splitlines() if ln.strip()])


def _declares_lfs_filter(dest: Path) -> bool:
    """Whether any committed .gitattributes routes files through the lfs filter —
    used only when ``git lfs ls-files`` could not run, so "we don't know" is
    never silently reported as "there was nothing to fetch"."""
    for ga in list(dest.rglob(".gitattributes"))[:50]:
        try:
            if "filter=lfs" in ga.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            continue
    return False


def _lfs_declared_bytes(dest: Path, files: list[str]) -> tuple[int, int, int]:
    """(total_bytes, sized_count, unsized_count) from the repo's LFS pointers.
    git-lfs bypasses download()'s streaming cap, so bound the payload using the
    committed pointer ``size`` fields — authoritative because git-lfs verifies
    each object's oid after download, so a lie-small size can't smuggle a larger
    object through. ``unsized`` is reported separately so a size-accounting
    failure is never mistaken for "no data"."""
    total = sized = unsized = 0
    for rel in files:
        try:
            head = (dest / rel).read_text(encoding="utf-8", errors="replace")[:2048]
        except OSError:
            unsized += 1
            continue
        m = re.search(r"(?m)^\s*size\s+(\d+)\s*$", head)
        if m:
            total += int(m.group(1))
            sized += 1
        else:
            unsized += 1
    return (total, sized, unsized)


def _pull_lfs(dest: Path, warnings: list[str], files: list[str]) -> None:
    """Opt-in, hardened git-lfs smudge on the trusted host.

    The committed ``.lfsconfig`` is UNTRUSTED, and git-lfs honors many endpoint-
    and exec-setting keys from it (``lfs.url``/``lfs.pushurl``,
    ``remote.<n>.lfsurl``, ``customtransfer``/``standalonetransferagent``, and on
    old git-lfs ``credential.helper``/``core.*``). Rather than denylist keys, we
    NEUTRALIZE the whole file and let git-lfs derive the endpoint from the
    already-validated origin clone URL — then cap the payload and pull via
    ``run_git`` (so GIT_ALLOW_PROTOCOL / GIT_CONFIG_NOSYSTEM / no-credential-prompt
    all hold). Residual: git-lfs still contacts the origin LFS server's batch API,
    whose object hrefs it trusts; only run --allow-lfs on repos whose git host you
    trust."""
    cfg = dest / ".lfsconfig"
    if cfg.is_file():
        try:
            cfg.rename(cfg.with_name(".lfsconfig.reprobe-disabled"))
            warnings.append("git-lfs: ignored the repo's committed .lfsconfig "
                            "(untrusted LFS endpoint/agent config)")
        except OSError:
            warnings.append("git-lfs NOT pulled: could not neutralize the repo .lfsconfig")
            return
    # Defense in depth: the endpoint git-lfs will use derives from the origin
    # clone URL; refuse an http(s) origin that resolves to a non-public host.
    origin = run_git(["config", "--get", "remote.origin.url"], cwd=dest).stdout.strip()
    if origin.lower().startswith(("http://", "https://")):
        try:
            assert_safe_url(origin)
        except FetchError:
            warnings.append(f"git-lfs NOT pulled: origin resolves to a non-public host ({origin})")
            return
    total, sized, unsized = _lfs_declared_bytes(dest, files)
    if sized == 0:
        # Never report a size-accounting failure as "there was no data".
        warnings.append(f"git-lfs NOT pulled: none of the {len(files)} tracked file(s) had a "
                        "readable pointer size, so the download could not be bounded")
        return
    if unsized:
        warnings.append(f"git-lfs: {unsized} tracked file(s) had no readable pointer size and are "
                        "not counted toward the size cap")
    if total > _MAX_LFS_TOTAL_BYTES:
        warnings.append(f"git-lfs NOT pulled: declared LFS payload {total} bytes exceeds the "
                        f"{_MAX_LFS_TOTAL_BYTES}-byte cap (fetch the data manually)")
        return
    pull = run_git(["lfs", "pull"], cwd=dest, timeout=1800)
    if pull.returncode == 0:
        warnings.append(f"git-lfs: pulled {sized} file(s) (~{total} bytes; LFS objects are "
                        "content-addressed, so this data is reproducibly pinned by the commit)")
    else:
        warnings.append(f"git-lfs pull failed: {pull.stderr.strip()[:200]}")


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


def _reject_unsafe_clone_url(url: str) -> None:
    """Refuse a clone URL that could be read as a git option or a dangerous
    transport. `git clone` runs on the trusted host (outside any sandbox), so a
    ref like ``ext::sh -c '…' #x.git`` — accepted by can_handle because it ends
    in ``.git`` — would otherwise reach git's ext remote-helper and execute a
    command on the host. Only vetted network URL shapes are allowed through."""
    if url.startswith("-"):
        raise FetchError(f"refusing git ref that looks like an option: {url!r}")
    if "::" in url:
        raise FetchError(f"refusing git ref with a transport marker '::' (e.g. ext::/fd::): {url!r}")
    if not (url.lower().startswith(("http://", "https://", "git://", "ssh://")) or url.startswith("git@")):
        raise FetchError(
            f"refusing git ref {url!r}: only http(s)://, git://, ssh:// or git@host:path URLs are cloned")


def _reject_unsafe_ref(want_ref: str) -> None:
    """A checkout ref must never look like an option (argument injection into
    ``git checkout``); legitimate branch/tag/commit refs never start with '-'."""
    if want_ref.startswith("-"):
        raise FetchError(f"refusing git checkout ref that looks like an option: {want_ref!r}")


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

    def fetch(self, ref: str, dest: Path, *, allow_lfs: bool = False) -> FetchResult:
        url, want_ref = ref, None
        m = re.match(r"^(.*?)(?:@([^@/]+))$", ref)
        # only treat trailing @ref as a pin when it's not part of an scp-like git@ URL
        if m and not ref.startswith("git@") and "/" not in (m.group(2) or ""):
            url, want_ref = m.group(1), m.group(2)
        url, browser_ref = normalize_browser_url(url, self.hosts)
        want_ref = want_ref or browser_ref

        # Validate BEFORE touching git: the ref is untrusted submitter input.
        _reject_unsafe_clone_url(url)
        if want_ref:
            _reject_unsafe_ref(want_ref)

        dest.mkdir(parents=True, exist_ok=True)
        # `--` ends option parsing so a URL/dest can never be read as a flag.
        clone = run_git(["clone", "--quiet", "--", url, str(dest)])
        if clone.returncode != 0:
            raise FetchError(f"git clone failed: {clone.stderr.strip()[:300]}")

        if want_ref:
            co = run_git(["checkout", "--quiet", want_ref], cwd=dest)
            if co.returncode != 0:
                raise FetchError(f"git checkout {want_ref} failed: {co.stderr.strip()[:200]}")

        sha = run_git(["rev-parse", "HEAD"], cwd=dest).stdout.strip()
        warnings = []
        # Ask whether THIS REPO tracks files in LFS. The old gate was
        # `git lfs version`, which only proves the CLI exists on the host, so
        # every fetch on an LFS-equipped machine claimed data was left unsmudged
        # — even for repos (the common case) with no LFS at all.
        lfs_ok, lfs_files = _lfs_tracked_files(dest)
        if lfs_files:
            if allow_lfs:
                _pull_lfs(dest, warnings, lfs_files)
            else:
                warnings.append(f"{len(lfs_files)} git-lfs-tracked file(s) were not fetched "
                                "(skip-smudge) — re-run with --allow-lfs to pull them")
        elif not lfs_ok and _declares_lfs_filter(dest):
            # couldn't ask git-lfs, but the repo does route files through it
            warnings.append("repo declares a git-lfs filter but `git lfs ls-files` failed "
                            "(git-lfs installed?); LFS data was NOT fetched")

        return FetchResult(
            input=ref, resolved_type="git", src_dir=str(dest),
            pin=Pin(kind="git_sha", value=sha),
            fetch_layer="git-clone", checksum_verified=False,
            warnings=warnings, metadata={"url": url, "ref": want_ref},
        )
