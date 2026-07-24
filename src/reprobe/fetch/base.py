"""Fetcher contract + shared helpers. A fetcher resolves an input (URL / DOI /
path) to a local source tree and an archival *pin* (the thing that makes the
Available badge meaningful: a version DOI, a commit SHA, or a SWHID — never a
moving tag)."""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import os
import socket
import subprocess
import tarfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Optional, Protocol, runtime_checkable
from urllib.parse import urljoin, urlparse

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
    env["GIT_LFS_SKIP_SMUDGE"] = "1"        # don't pull large LFS blobs during clone/checkout
    env["GIT_TERMINAL_PROMPT"] = "0"        # never hang on a credential prompt
    env["GIT_CONFIG_NOSYSTEM"] = "1"        # ignore host /etc/gitconfig (no injected helpers/transports)
    # Restrict git to network transports we actually fetch over. This blocks the
    # `ext::`/`fd::` remote-helper transports (arbitrary host command execution)
    # and `file::` (local exfiltration) even if a crafted ref reaches `git clone`
    # — clone runs on the TRUSTED host, outside every container sandbox, so an
    # unfenced transport here is a full sandbox escape. Defence in depth with the
    # ref validation in git_host.py.
    env["GIT_ALLOW_PROTOCOL"] = "http:https:git:ssh"
    try:
        return subprocess.run(["git", *args], cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise FetchError("git is not installed or not on PATH — see the chair runbook prerequisites")
    except subprocess.TimeoutExpired:
        raise FetchError(f"git {args[0]} timed out after {timeout}s "
                         f"(large repo or slow network); re-run or clone manually")


# ---------------------------------------------------------------------------
# Resource bounds. A malicious deposit (a huge file or a decompression bomb) is
# handled on the TRUSTED host, outside the container caps, so it could exhaust
# host disk/memory and abort a whole batch season. These are generous ceilings
# that still bound a bomb (which is TB–PB scale); a chair can lower them here.
# ---------------------------------------------------------------------------
_MAX_DOWNLOAD_BYTES = 20 * 1024**3       # per streamed file
_MAX_EXTRACT_BYTES = 50 * 1024**3        # total declared-uncompressed per archive
_MAX_ARCHIVE_MEMBERS = 500_000
_MAX_LFS_TOTAL_BYTES = 20 * 1024**3      # aggregate declared LFS payload per repo (git-lfs
                                         # bypasses download()'s per-file cap; bound it here)


# ---------------------------------------------------------------------------
# SSRF defense for author-influenced download/LFS endpoints. Fetch runs on the
# TRUSTED host with no container isolation, so an author-declared URL (or an LFS
# endpoint from a committed .lfsconfig) must never be allowed to reach an
# internal/link-local/metadata address (169.254.169.254, RFC1918, loopback).
# Match on the resolved IP(s), not a substring — see dataverse.py for the same
# host-based (not substring) discipline.
# ---------------------------------------------------------------------------

def is_public_host(host: str) -> bool:
    """True only if ``host`` (a hostname or IP literal) resolves entirely to
    public, routable addresses. Any private/loopback/link-local/reserved/
    multicast/unspecified address — or a resolution failure — returns False."""
    if not host:
        return False
    host = host.strip("[]")                     # unwrap an IPv6 literal
    try:
        candidates = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except (OSError, UnicodeError):
            return False
        candidates = []
        for info in infos:
            try:
                candidates.append(ipaddress.ip_address(info[4][0]))
            except (ValueError, IndexError):
                continue
        if not candidates:
            return False
    for ip in candidates:
        # Normalize embedded IPv4 (::ffff:169.254.169.254, 6to4) to its v4 form
        # so an internal address can't hide behind an IPv6 wrapper.
        mapped = getattr(ip, "ipv4_mapped", None) or getattr(ip, "sixtofour", None)
        if mapped is not None:
            ip = mapped
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def assert_safe_url(url: str) -> str:
    """Validate an untrusted (author-declared) download URL: http(s) only, host
    must be public (SSRF guard). Returns the hostname. Raises FetchError."""
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise FetchError(f"refusing non-http(s) URL: {url!r}")
    host = p.hostname or ""
    if not is_public_host(host):
        raise FetchError(f"refusing URL to a non-public/internal host: {host or url!r}")
    return host


def _resolve_public(host: str, port: int):
    """Resolve ``host`` to its TCP addrinfos and require EVERY result to be a
    public IP. Returns the addrinfo list (to pin); raises FetchError otherwise.
    One resolution, so validation and the pinned connect can't disagree."""
    if not host:
        raise FetchError("empty host")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as e:
        raise FetchError(f"cannot resolve host {host!r}: {e}")
    if not infos:
        raise FetchError(f"host {host!r} did not resolve")
    for ai in infos:
        ip = ipaddress.ip_address(ai[4][0])
        mapped = getattr(ip, "ipv4_mapped", None) or getattr(ip, "sixtofour", None)
        if mapped is not None:
            ip = mapped
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise FetchError(f"refusing URL to a non-public/internal host: {host}")
    return infos


@contextlib.contextmanager
def _pinned_dns(host: str, infos):
    """Force ``socket.getaddrinfo(host, ...)`` to return the already-validated
    ``infos`` for the duration, so the name can't be re-resolved to a different
    (internal) address between validation and connect — closing DNS-rebinding.
    Fetch is single-threaded, so a process-global swap for one connect is safe;
    the hostname stays in the URL, so TLS SNI / cert verification are unaffected."""
    real = socket.getaddrinfo

    def patched(h, port, family=0, type=0, proto=0, flags=0):
        return infos if h == host else real(h, port, family, type, proto, flags)

    socket.getaddrinfo = patched
    try:
        yield
    finally:
        socket.getaddrinfo = real


def guard_zip(z: "zipfile.ZipFile") -> None:
    """Refuse a zip whose member count or declared uncompressed size is bomb-shaped,
    BEFORE extracting it. Raises FetchError."""
    infos = z.infolist()
    if len(infos) > _MAX_ARCHIVE_MEMBERS:
        raise FetchError(f"archive has too many members ({len(infos)} > {_MAX_ARCHIVE_MEMBERS})")
    total = sum(i.file_size for i in infos)
    if total > _MAX_EXTRACT_BYTES:
        raise FetchError(f"archive uncompressed size {total} exceeds {_MAX_EXTRACT_BYTES}-byte cap (decompression bomb?)")


def _guard_tar_members(members: list[tarfile.TarInfo]) -> None:
    if len(members) > _MAX_ARCHIVE_MEMBERS:
        raise ValueError(f"archive has too many members ({len(members)} > {_MAX_ARCHIVE_MEMBERS})")
    total = sum(m.size for m in members)
    if total > _MAX_EXTRACT_BYTES:
        raise ValueError(f"archive uncompressed size {total} exceeds {_MAX_EXTRACT_BYTES}-byte cap (decompression bomb?)")


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
    members = t.getmembers()
    _guard_tar_members(members)             # reject bombs before writing anything
    try:
        t.extractall(dest, members=members, filter="data")   # sanitizes; 3.12+ and 3.11.4+
        return
    except TypeError:
        pass                                # 3.11.0–3.11.3: no filter= kwarg, validate by hand
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
                guard_zip(z)                # reject a decompression bomb first
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

def _open_stream(url: str, params: dict | None, timeout: int, restrict_public: bool):
    """Open a streaming GET. With ``restrict_public`` (untrusted author URL),
    follow redirects MANUALLY and, on every hop, resolve+validate the host to a
    public IP and PIN it for the connect — so a validated public host can neither
    302 to an internal address nor DNS-rebind to one. Otherwise use the session's
    normal redirect handling (trusted platform)."""
    if not restrict_public:
        return _SESSION.get(url, params=params, stream=True, timeout=timeout)
    cur = url
    for _ in range(6):
        p = urlparse(cur)
        if p.scheme not in ("http", "https"):
            raise FetchError(f"refusing non-http(s) URL: {cur!r}")
        port = p.port or (443 if p.scheme == "https" else 80)
        infos = _resolve_public(p.hostname or "", port)   # raises on non-public
        with _pinned_dns(p.hostname or "", infos):
            r = _SESSION.get(cur, params=params, stream=True, timeout=timeout, allow_redirects=False)
        params = None                           # query params apply to the first hop only
        if getattr(r, "is_redirect", False) or getattr(r, "is_permanent_redirect", False):
            loc = r.headers.get("Location")
            r.close()
            if not loc:
                raise FetchError("redirect without a Location header")
            cur = urljoin(cur, loc)
            continue
        return r
    raise FetchError("too many redirects")


def download(url: str, dest: Path, *, expected_md5: str | None = None,
             params: dict | None = None, timeout: int = 300,
             max_bytes: int = _MAX_DOWNLOAD_BYTES,
             restrict_public: bool = False) -> tuple[bool, str]:
    """Stream a file to dest; verify md5 if provided. Returns (ok, note).

    Aborts (and removes the partial file) if the response exceeds ``max_bytes`` —
    an untrusted platform could otherwise stream unbounded bytes onto the host.
    Pass ``restrict_public=True`` for an author-declared URL to SSRF-guard every
    redirect hop."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.md5()
    total = 0
    over = False
    try:
        with _open_stream(url, params, timeout, restrict_public) as r:
            r.raise_for_status()
            clen = r.headers.get("Content-Length")
            if clen and clen.isdigit() and int(clen) > max_bytes:
                return False, f"refusing oversized download ({int(clen)} bytes > {max_bytes}-byte cap)"
            with dest.open("wb") as fh:
                for chunk in r.iter_content(65536):
                    total += len(chunk)
                    if total > max_bytes:
                        over = True
                        break
                    fh.write(chunk)
                    h.update(chunk)
    except Exception as e:
        return False, f"download failed: {e}"
    if over:
        dest.unlink(missing_ok=True)
        return False, f"download exceeded {max_bytes}-byte cap (possible archive bomb)"
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
