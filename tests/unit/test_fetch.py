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
