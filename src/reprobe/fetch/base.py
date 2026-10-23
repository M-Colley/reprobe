"""Fetcher contract + shared helpers. A fetcher resolves an input (URL / DOI /
path) to a local source tree and an archival *pin* (the thing that makes the
Available badge meaningful: a version DOI, a commit SHA, or a SWHID — never a
moving tag)."""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import json
import os
import socket
import struct
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Optional, Protocol, runtime_checkable
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import parse_url as _urllib3_parse_url
from urllib3.util.retry import Retry

from ..models import FetchResult


@runtime_checkable
class Fetcher(Protocol):
    name: str
    def can_handle(self, ref: str) -> bool: ...
    def fetch(self, ref: str, dest: Path) -> FetchResult: ...


class FetchError(RuntimeError):
    pass


class RefusedSource(FetchError):
    """An author-declared reference that is not fetched at all (a local path,
    an ssh/scp git URL, a non-http(s) scheme, an internal host). A FetchError, so
    every caller that already reports a failed source reports this one too."""


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
#: For URLs an author chose. Not trust_env: an HTTP(S)_PROXY from the environment
#: would resolve the host itself (the DNS pin then guards nothing — the proxy can
#: be rebound to an internal address), and ~/.netrc would hand the chair's stored
#: credentials to whatever host the author named.
_DIRECT = _make_session()
_DIRECT.trust_env = False


def get(url: str, **kwargs) -> requests.Response:
    """GET via the shared retrying session. All fetcher API calls go through here."""
    return _SESSION.get(url, **kwargs)


def post(url: str, **kwargs) -> requests.Response:
    """POST via the shared retrying session."""
    return _SESSION.post(url, **kwargs)


# Inherited GIT_* variables that carry no credentials or config: trust roots for
# a chair behind a TLS-inspecting proxy. Every other GIT_* is dropped (below).
_GIT_ENV_KEEP = ("GIT_SSL_CAINFO", "GIT_SSL_CAPATH")


def run_git(args: list[str], cwd: Optional[Path] = None, timeout: int = 600, *,
            watch: Optional[Path] = None, max_bytes: Optional[int] = None) -> subprocess.CompletedProcess:
    # git runs on the TRUSTED host against an untrusted remote, so it must not
    # act as the chair: no credential helper, no ~/.gitconfig (helpers,
    # url.insteadOf rewrites, http.extraHeader tokens, core.hooksPath), no
    # inherited GIT_CONFIG_PARAMETERS / GIT_CONFIG_COUNT injection, no
    # GIT_ASKPASS / GIT_SSH_COMMAND program. A host asking for credentials then
    # gets none, which _clone_failure reports as "not publicly accessible".
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith("GIT_") or k.upper() in _GIT_ENV_KEEP}
    env.pop("SSH_ASKPASS", None)            # git's fallback prompt program
    env["GIT_LFS_SKIP_SMUDGE"] = "1"        # don't pull large LFS blobs during clone/checkout
    env["GIT_TERMINAL_PROMPT"] = "0"        # never hang on a credential prompt
    env["GIT_CONFIG_NOSYSTEM"] = "1"        # ignore host /etc/gitconfig (no injected helpers/transports)
    env["GIT_CONFIG_GLOBAL"] = os.devnull   # ...and the chair's ~/.gitconfig (git >= 2.32)
    # Restrict git to network transports we actually fetch over. This blocks the
    # `ext::`/`fd::` remote-helper transports (arbitrary host command execution)
    # and `file::` (local exfiltration) even if a crafted ref reaches `git clone`
    # — clone runs on the TRUSTED host, outside every container sandbox, so an
    # unfenced transport here is a full sandbox escape. Defence in depth with the
    # ref validation in git_host.py.
    env["GIT_ALLOW_PROTOCOL"] = "http:https:git:ssh"
    # credential.helper= empties the helper list (beats any config git still
    # reads). core.symlinks=false checks a committed symlink out as a plain file
    # holding its target text, so a link to ~/.ssh in the repo never becomes a
    # live path that host-side readers (detection, the paper finder, the LLM
    # prompt) would follow. Set on every call: a later `checkout` reads the
    # repo's own config, not the clone's -c.
    argv = ["git", "-c", "credential.helper=", "-c", "core.symlinks=false", *args]
    if watch is not None and max_bytes:
        return _run_git_watched(argv, cwd, env, timeout, watch, max_bytes, args[0])
    try:
        return subprocess.run(argv, cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise FetchError("git is not installed or not on PATH — see the chair runbook prerequisites")
    except subprocess.TimeoutExpired:
        raise FetchError(f"git {args[0]} timed out after {timeout}s "
                         f"(large repo or slow network); re-run or clone manually")


def _dir_bytes_over(root: Path, cap: int) -> bool:
    """True once the files under ``root`` pass ``cap`` bytes (links not followed;
    stops counting as soon as the answer is yes)."""
    total = 0
    stack = [root]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
                    if total > cap:
                        return True
        except OSError:
            continue
    return False


_GIT_WATCH_EVERY_S = 2.0


def _run_git_watched(argv: list[str], cwd: Optional[Path], env: dict, timeout: int,
                     watch: Path, max_bytes: int, verb: str) -> subprocess.CompletedProcess:
    """``git`` with a disk budget on ``watch``.

    A clone writes onto the TRUSTED host, outside every container cap, and its
    size is the author's choice — a repository of tens of gigabytes (or a crafted
    pack) would fill the disk a whole review season runs on. git has no size
    limit of its own, so the destination is measured while it grows and the
    process is killed past ``max_bytes``. Output goes to temp files, not pipes,
    so a chatty git can never deadlock the wait."""
    import tempfile

    start = time.monotonic()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=out, stderr=err)
        except FileNotFoundError:
            raise FetchError("git is not installed or not on PATH — see the chair runbook prerequisites")
        stopped = ""
        while True:
            try:
                code = proc.wait(timeout=_GIT_WATCH_EVERY_S)
                break
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() - start > timeout:
                stopped = f"git {verb} timed out after {timeout}s (large repo or slow network); " \
                          "re-run or clone manually"
            elif watch.exists() and _dir_bytes_over(watch, max_bytes):
                stopped = (f"git {verb} stopped: the repository passed {max_bytes // 1024**3} GiB on disk, "
                           "the cap for a fetch on the review host")
            if stopped:
                proc.kill()
                proc.wait(timeout=30)
                raise FetchError(stopped)
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(argv, code, out.read().decode("utf-8", "replace"),
                                           err.read().decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# Resource bounds. A malicious deposit (a huge file or a decompression bomb) is
# handled on the TRUSTED host, outside the container caps, so it could exhaust
# host disk/memory and abort a whole batch season. These are generous ceilings
# that still bound a bomb (which is TB–PB scale); a chair can lower them here.
# ---------------------------------------------------------------------------
_MAX_DOWNLOAD_BYTES = 20 * 1024**3       # per streamed file
_MAX_CLONE_BYTES = 20 * 1024**3          # a git clone, working tree + .git
_MAX_EXTRACT_BYTES = 50 * 1024**3        # total declared-uncompressed per archive
_MAX_ARCHIVE_MEMBERS = 500_000
_MAX_LFS_TOTAL_BYTES = 20 * 1024**3      # aggregate declared LFS payload per repo (git-lfs
                                         # bypasses download()'s per-file cap; bound it here)
_MAX_DOWNLOAD_SECONDS = 4 * 3600         # wall clock per streamed file: `timeout` is per read,
                                         # so a server trickling a byte per read never trips it
_MAX_JSON_BYTES = 64 * 1024**2           # an API document from a submitter-named host


# ---------------------------------------------------------------------------
# SSRF defense for author-influenced download/LFS endpoints. Fetch runs on the
# TRUSTED host with no container isolation, so an author-declared URL (or an LFS
# endpoint from a committed .lfsconfig) must never be allowed to reach an
# internal/link-local/metadata address (169.254.169.254, RFC1918, loopback).
# Match on the resolved IP(s), not a substring — see dataverse.py for the same
# host-based (not substring) discipline.
# ---------------------------------------------------------------------------

_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _is_internal_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for any address a fetch on the trusted host must not reach.

    ``is_global`` is the allowlist: it is False for RFC1918, loopback,
    link-local (169.254.169.254), CGNAT 100.64.0.0/10 (Alibaba's metadata
    service at 100.100.100.200, Tailscale), ULA fc00::/7 and the documentation
    and benchmark ranges — ``is_private`` alone misses CGNAT. The explicit checks
    cover what it gets wrong: multicast and deprecated site-local fec0::/10 are
    "global" to ``ipaddress``, and its tables have shifted between releases."""
    if ip.version == 6:
        # An internal IPv4 must not ride in on an IPv6 wrapper: mapped
        # (::ffff:a.b.c.d), 6to4 (2002::/16) or NAT64 (64:ff9b::/96 — and a
        # NAT64 wrapper of a PUBLIC v4 is fine, it is how IPv6-only hosts reach it).
        inner = ip.ipv4_mapped or ip.sixtofour
        if inner is None and ip in _NAT64:
            inner = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if inner is not None:
            return _is_internal_ip(inner)
    return (not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
            or bool(getattr(ip, "is_site_local", False)))


def _addr_is_internal(addr: str) -> bool:
    try:
        return _is_internal_ip(ipaddress.ip_address(addr))
    except ValueError:
        return True                             # unparseable: never trust it


def is_public_host(host: str) -> bool:
    """True only if ``host`` (a hostname or IP literal) resolves entirely to
    public, routable addresses. Any internal address (see ``_is_internal_ip``)
    — or a resolution failure — returns False."""
    if not host:
        return False
    host = host.strip("[]")                     # unwrap an IPv6 literal
    try:
        candidates = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except (OSError, UnicodeError):
            return False
        candidates = [str(info[4][0]) for info in infos if info and len(info) > 4 and info[4]]
        if not candidates:
            return False
    return not any(_addr_is_internal(a) for a in candidates)


def _url_parts(url: str) -> tuple[str, str, Optional[int]]:
    """``(scheme, host, port)`` of an http(s) URL, or FetchError.

    The host that was VALIDATED must be the host a request will CONNECT to, and
    two URL parsers are involved: ``urlparse`` here, and urllib3 inside
    ``requests``. They disagree on some strings — ``http://127.0.0.1:80`` + a
    backslash + ``@8.8.8.8/`` is host 8.8.8.8 to urlparse and 127.0.0.1 to urllib3 — so a guard that
    validated one and fetched with the other was bypassable. Strings that could
    be read two ways are refused outright rather than interpreted:

    * a backslash, whitespace or a control character anywhere in the URL;
    * a non-ASCII host (urllib3 resolves its IDNA form, so a pinned address for
      the unicode spelling would never be applied; use the ``xn--`` form);
    * any scheme but http/https;
    * a host or port the two parsers do not agree on."""
    if not isinstance(url, str) or not url:
        raise FetchError("refusing an empty URL")
    if any(c == "\\" or ord(c) <= 32 or ord(c) == 127 for c in url):
        raise FetchError(f"refusing URL with a backslash, whitespace or control character: {url!r}")
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        port = p.port
        u3 = _urllib3_parse_url(url)
    except Exception as e:                       # noqa: BLE001 - any parse failure is a refusal
        raise FetchError(f"refusing malformed URL {url!r}: {e}")
    if p.scheme not in ("http", "https"):
        raise FetchError(f"refusing non-http(s) URL: {url!r}")
    if not host.isascii():
        raise FetchError(f"refusing non-ASCII host {host!r} (use its xn-- form): {url!r}")
    if (u3.host or "").strip("[]").lower() != host or (u3.port or None) != (port or None):
        raise FetchError(f"refusing URL that parses to different hosts in different parsers: {url!r}")
    return p.scheme, host, port


def assert_safe_url(url: str) -> str:
    """Validate an untrusted (author-declared) download URL: http(s) only, host
    must be public (SSRF guard). Returns the hostname. Raises FetchError."""
    _, host, _ = _url_parts(url)
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
        if _addr_is_internal(str(ai[4][0])):
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
    # urllib3 strips a trailing dot from the host before it resolves it, and the
    # URL's host is lower-cased: compare the way it will actually be asked.
    want = host.rstrip(".").lower()

    def patched(h, port, family=0, type=0, proto=0, flags=0):
        if isinstance(h, str) and h.rstrip(".").lower() == want:
            return infos
        return real(h, port, family, type, proto, flags)

    socket.getaddrinfo = patched
    try:
        yield
    finally:
        socket.getaddrinfo = real


def guard_zip(z: zipfile.ZipFile) -> None:
    """Refuse a zip whose member count or declared uncompressed size is bomb-shaped,
    BEFORE extracting it. Raises FetchError."""
    infos = z.infolist()
    if len(infos) > _MAX_ARCHIVE_MEMBERS:
        raise FetchError(f"archive has too many members ({len(infos)} > {_MAX_ARCHIVE_MEMBERS})")
    total = sum(i.file_size for i in infos)
    if total > _MAX_EXTRACT_BYTES:
        raise FetchError(f"archive uncompressed size {total} exceeds {_MAX_EXTRACT_BYTES}-byte cap (decompression bomb?)")


def zip_entry_count(path: Path) -> int | None:
    """Entries a zip declares in its end-of-central-directory record (zip64
    included), read WITHOUT parsing the central directory — which ZipFile() does
    in full on open, so a small file declaring millions of entries exhausts host
    memory before guard_zip can count them. None when unreadable (ZipFile then
    reports the error itself)."""
    end = getattr(zipfile, "_EndRecData", None)        # the stdlib's own EOCD reader
    if end is None:
        return None
    try:
        with open(path, "rb") as fp:
            rec = end(fp)
    except (OSError, struct.error, ValueError):
        return None
    return int(rec[zipfile._ECD_ENTRIES_TOTAL]) if rec else None


def _tar_members(t: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """The archive's members, read one header at a time and refused as soon as
    the count or the declared size passes its cap — getmembers() would read
    every header into memory first."""
    members: list[tarfile.TarInfo] = []
    total = 0
    while (m := t.next()) is not None:
        members.append(m)
        total += m.size
        if len(members) > _MAX_ARCHIVE_MEMBERS:
            raise ValueError(f"archive has too many members (> {_MAX_ARCHIVE_MEMBERS})")
        if total > _MAX_EXTRACT_BYTES:
            raise ValueError(f"archive uncompressed size exceeds {_MAX_EXTRACT_BYTES}-byte cap "
                             "(decompression bomb?)")
    return members


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


# Containment for tar members is the stdlib 'data' filter (3.11.4+), not a
# hand-rolled check — the old lexical fallback was walked out of by a symlink
# chain. Before these releases the filter itself could be escaped through link
# members (CVE-2025-4517 / -4330 / -4138, CVE-2024-12718), so on those
# interpreters link members are dropped rather than trusted to it.
_TAR_FILTER_SOUND = (sys.version_info >= (3, 13, 4)
                     or (3, 12, 11) <= sys.version_info < (3, 13)
                     or (3, 11, 13) <= sys.version_info < (3, 12))


def _extract_tar(t: tarfile.TarFile, dest: Path, warnings: list[str]) -> None:
    if not hasattr(tarfile, "data_filter"):
        raise FetchError("tar extraction needs Python >= 3.11.4 (tarfile filter='data')")
    members = _tar_members(t)              # reject bombs before writing anything
    if not _TAR_FILTER_SOUND:
        links = [m for m in members if m.issym() or m.islnk()]
        if links:
            members = [m for m in members if not (m.issym() or m.islnk())]
            warnings.append(
                f"skipped {len(links)} link member(s) in the archive: this Python's tar "
                "'data' filter predates the CVE-2025-4517 fix (upgrade to 3.12.11+ / "
                "3.13.4+ to extract links)")
    t.extractall(dest, members=members, filter="data")


def maybe_unzip(dest: Path, warnings: list[str]) -> None:
    """If the deposit is a single .zip/.tar.gz archive, extract it in place."""
    zips = list(dest.glob("*.zip"))
    tars = list(dest.glob("*.tar.gz")) + list(dest.glob("*.tgz"))
    try:
        if len(zips) == 1 and not tars:
            n = zip_entry_count(zips[0])
            if n is not None and n > _MAX_ARCHIVE_MEMBERS:
                raise FetchError(f"archive has too many members ({n} > {_MAX_ARCHIVE_MEMBERS})")
            with zipfile.ZipFile(zips[0]) as z:
                guard_zip(z)                # reject a decompression bomb first
                z.extractall(dest)          # zipfile sanitizes member paths itself
        elif len(tars) == 1 and not zips:
            with tarfile.open(tars[0]) as t:
                _extract_tar(t, dest, warnings)
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
        scheme, host, explicit_port = _url_parts(cur)         # every hop, not only the first
        port = explicit_port or (443 if scheme == "https" else 80)
        infos = _resolve_public(host, port)                   # raises on non-public
        with _pinned_dns(host, infos):
            r = _DIRECT.get(cur, params=params, stream=True, timeout=timeout, allow_redirects=False)
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


def _body_chunks(r, size: int = 65536):
    """Yield the response body as it arrives.

    ``iter_content`` blocks until a whole chunk is in, so a server trickling one
    byte per read holds it open indefinitely without ever tripping the per-read
    timeout. urllib3 2.x ``read1`` returns whatever is available, which lets the
    caller's wall-clock check run between reads; older urllib3 falls back."""
    read1 = getattr(getattr(r, "raw", None), "read1", None)
    if read1 is None:
        yield from r.iter_content(size)
        return
    while True:
        chunk = read1(size, decode_content=True)
        if not chunk:
            return
        yield chunk


def probe_status(url: str, *, timeout: int = 20) -> int:
    """HTTP status an unauthenticated GET of ``url`` ends on, following
    redirects only through public hosts (the per-hop guard ``download`` uses).

    Following redirects with the plain session would let a public URL bounce the
    probe to an internal address and echo that service's status code into a
    report — a port scan of the chair's network. Raises FetchError when refused."""
    with _open_stream(url, None, timeout, restrict_public=True) as r:
        return int(r.status_code)


def get_json(url: str, *, params: dict | None = None, timeout: int = 60,
             restrict_public: bool = False, max_bytes: int = _MAX_JSON_BYTES):
    """GET and parse a JSON document under a byte cap. ``restrict_public`` is
    for an API whose HOST came from the submission (a Dataverse install), so
    every redirect hop is SSRF-checked and DNS-pinned like a download."""
    buf = bytearray()
    with _open_stream(url, params, timeout, restrict_public) as r:
        r.raise_for_status()
        for chunk in _body_chunks(r):
            buf += chunk
            if len(buf) > max_bytes:
                raise FetchError(f"API response from {urlparse(url).hostname} exceeds "
                                 f"the {max_bytes}-byte cap")
    return json.loads(bytes(buf))


# Platform spellings of a digest algorithm -> hashlib name. Anything else
# (UNF, an unknown name) is "no usable checksum", never a silent pass.
_HASH_NAMES = {"md5": "md5", "sha1": "sha1", "sha224": "sha224", "sha256": "sha256",
               "sha384": "sha384", "sha512": "sha512"}


def hash_algorithm(name: object) -> str | None:
    """hashlib name for a platform's algorithm label ("MD5", "SHA-256", "sha-1")."""
    key = str(name or "").strip().lower().replace("-", "").replace("_", "")
    return _HASH_NAMES.get(key)


def download(url: str, dest: Path, *, expected_md5: str | None = None,
             expected_digest: tuple[str, str] | None = None,
             params: dict | None = None, timeout: int = 300,
             max_bytes: int = _MAX_DOWNLOAD_BYTES,
             max_seconds: float = _MAX_DOWNLOAD_SECONDS,
             restrict_public: bool = False) -> tuple[bool, str]:
    """Stream a file to dest; verify a checksum if provided. Returns (ok, note).

    ``expected_digest`` is ``(hashlib_name, hexdigest)`` (see ``hash_algorithm``);
    ``expected_md5`` is the older md5-only form ("md5:" prefix tolerated).
    Aborts (and removes the partial file) past ``max_bytes`` or ``max_seconds``
    of wall clock — an untrusted platform could otherwise stream unbounded bytes
    onto the host, or hold the fetch open forever by trickling them. Pass
    ``restrict_public=True`` for an author-declared URL to SSRF-guard every
    redirect hop."""
    algo, want = None, None
    if expected_digest and expected_digest[0] and expected_digest[1]:
        algo, want = expected_digest[0], str(expected_digest[1]).strip().lower()
    elif expected_md5:
        algo, want = "md5", expected_md5.strip().lower().replace("md5:", "")
    try:
        h = hashlib.new(algo or "md5")
    except ValueError:
        return False, f"download failed: unsupported checksum algorithm {algo!r}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max_seconds
    total = 0
    stopped = ""
    try:
        with _open_stream(url, params, timeout, restrict_public) as r:
            r.raise_for_status()
            clen = r.headers.get("Content-Length")
            if clen and clen.isdigit() and int(clen) > max_bytes:
                return False, f"refusing oversized download ({int(clen)} bytes > {max_bytes}-byte cap)"
            with dest.open("wb") as fh:
                for chunk in _body_chunks(r):
                    total += len(chunk)
                    if total > max_bytes:
                        stopped = f"download exceeded {max_bytes}-byte cap (possible archive bomb)"
                        break
                    if time.monotonic() > deadline:
                        stopped = (f"download exceeded the {int(max_seconds)}s wall-clock limit "
                                   "(server too slow, or trickling bytes)")
                        break
                    fh.write(chunk)
                    h.update(chunk)
    except Exception as e:
        return False, f"download failed: {e}"
    if stopped:
        dest.unlink(missing_ok=True)
        return False, stopped
    if want:
        if h.hexdigest() != want:
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
