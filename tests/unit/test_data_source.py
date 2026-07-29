"""Secondary data sources — the "code in git, data on OSF" artifact.

Pure: no network and no Docker. Fetching is exercised through a local directory
(the LocalFetcher) or a stubbed download, never a live deposit.
"""

import pytest

from reprobe.detect.manifest import declared_data_sources
from reprobe.fetch.base import FetchError
from reprobe.fetch.data_source import (DirectUrlFetcher, _clean_url, _filename_for,
                                       fetch_data_source, merge_into, parse_ref,
                                       referenced_deposits)
from reprobe.fetch.osf import OSFFetcher, guid_of
from reprobe.fetch.registry import select

_BUNDLE = ("https://files.de-1.osf.io/v1/resources/cwd6h/providers/osfstorage/"
           "67f8cb0d76707cc9d48b805d/?zip=")


# --------------------------------------------------------------------------- #
# ref parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("spec,url,into", [
    ("https://osf.io/cwd6h", "https://osf.io/cwd6h", ""),
    ("https://osf.io/cwd6h::data", "https://osf.io/cwd6h", "data"),
    ("https://osf.io/cwd6h::data/raw/", "https://osf.io/cwd6h", "data/raw"),
    (_BUNDLE + "::dataset", _BUNDLE, "dataset"),
    # "::" also occurs inside an IPv6 literal — don't mangle one into a subdir
    ("http://[::1]/x", "http://[::1]/x", ""),
])
def test_parse_ref(spec, url, into):
    assert parse_ref(spec) == (url, into)


# --------------------------------------------------------------------------- #
# OSF: can_handle must not claim what fetch() cannot resolve
# --------------------------------------------------------------------------- #
def test_osf_declines_file_server_bundle_links():
    """OSF's own UI hands out files.de-1.osf.io/v1/resources/<guid>/... bundle
    links, and READMEs paste them. "osf.io" is a substring of that host, so the
    OSF fetcher claimed them and then died on "could not parse OSF guid" —
    turning a perfectly downloadable archive into an unfetchable source."""
    assert guid_of(_BUNDLE) is None
    assert OSFFetcher().can_handle(_BUNDLE) is False
    assert select(_BUNDLE) is None          # falls through to the direct fetcher
    # the project form still routes to OSF
    assert guid_of("https://osf.io/cwd6h/") == "cwd6h"
    assert OSFFetcher().can_handle("https://osf.io/cwd6h/") is True


@pytest.mark.parametrize("ref", ["https://osf.io/download/abcd1234/", "https://osf.io/files/"])
def test_osf_declines_route_segments_that_are_not_guids(ref):
    assert OSFFetcher().can_handle(ref) is False


# --------------------------------------------------------------------------- #
# direct URL fetcher
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url,expected", [
    (_BUNDLE, "data.zip"),                                   # names no file; payload is a zip
    ("https://example.org/logs.zip", "logs.zip"),
    ("https://example.org/a/b/table.csv", "table.csv"),
    ("https://example.org/opaque", "data.bin"),
])
def test_filename_for(url, expected):
    assert _filename_for(url) == expected


def test_direct_url_refuses_internal_hosts(tmp_path):
    # the SSRF guard is the reason a data URL may be author-supplied at all
    with pytest.raises(FetchError):
        DirectUrlFetcher().fetch("http://127.0.0.1/secrets.zip", tmp_path)


def test_direct_url_is_not_a_primary_fetcher():
    """A bare URL as a *submission* must still fail with the supported-sources
    list — a submission needs a pin, and a URL has none."""
    assert select("https://example.org/data.zip") is None


def test_data_source_falls_back_to_direct_download(tmp_path, monkeypatch):
    import reprobe.fetch.data_source as ds

    def fake_download(url, dest, **kw):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"col\n1\n")
        return True, "ok"

    monkeypatch.setattr(ds, "download", fake_download)
    monkeypatch.setattr(ds, "assert_safe_url", lambda url: "example.org")
    fr = fetch_data_source("https://example.org/table.csv", tmp_path)

    assert fr.resolved_type == "direct-url"
    assert fr.pin.kind == "none"           # a URL is not an archival pin, ever
    assert fr.checksum_verified is False
    assert any("no platform checksum" in w for w in fr.warnings)
    assert (tmp_path / "table.csv").read_text() == "col\n1\n"


def test_local_path_data_source_uses_the_normal_registry(tmp_path):
    """Platform fetchers are tried first, so every source a submission can use
    is also usable as a data source — here a local directory."""
    deposit = tmp_path / "deposit"
    (deposit / "sub").mkdir(parents=True)
    (deposit / "sub" / "x.csv").write_text("a\n", encoding="utf-8")
    fr = fetch_data_source(str(deposit), tmp_path / "staged")
    assert fr.resolved_type == "local"
    assert (tmp_path / "staged" / "sub" / "x.csv").is_file()


# --------------------------------------------------------------------------- #
# merging
# --------------------------------------------------------------------------- #
def test_merge_copies_nested_tree(tmp_path):
    src, dest = tmp_path / "s", tmp_path / "d"
    (src / "Study Data").mkdir(parents=True)
    (src / "Study Data" / "p01.csv").write_text("x", encoding="utf-8")
    (src / "top.txt").write_text("y", encoding="utf-8")
    dest.mkdir()

    copied, collisions = merge_into(src, dest)

    assert sorted(copied) == ["Study Data/p01.csv", "top.txt"] and collisions == []
    assert (dest / "Study Data" / "p01.csv").read_text() == "x"


def test_merge_into_subdir(tmp_path):
    src, dest = tmp_path / "s", tmp_path / "d"
    src.mkdir()
    (src / "p01.csv").write_text("x", encoding="utf-8")
    dest.mkdir()
    copied, _ = merge_into(src, dest, "dataset/raw")
    assert copied == ["dataset/raw/p01.csv"]
    assert (dest / "dataset" / "raw" / "p01.csv").is_file()


def test_merge_never_overwrites_the_artifacts_own_files(tmp_path):
    """A deposit that could replace a script would mean the code reviewed is not
    the code submitted. Collisions are skipped and named, never resolved."""
    src, dest = tmp_path / "s", tmp_path / "d"
    src.mkdir(), dest.mkdir()
    (src / "main.py").write_text("print('from the deposit')", encoding="utf-8")
    (src / "new.csv").write_text("data", encoding="utf-8")
    (dest / "main.py").write_text("print('from the repo')", encoding="utf-8")

    copied, collisions = merge_into(src, dest)

    assert copied == ["new.csv"] and collisions == ["main.py"]
    assert (dest / "main.py").read_text() == "print('from the repo')"


def test_merge_cannot_escape_the_tree(tmp_path):
    """Containment is by normalization, not by exception: safe_join drops '..'
    so a traversal lands inside the tree instead of beside it."""
    src, dest = tmp_path / "s", tmp_path / "d"
    src.mkdir(), dest.mkdir()
    (src / "ok.csv").write_text("x", encoding="utf-8")

    copied, _ = merge_into(src, dest, "../../escaped")

    assert copied == ["escaped/ok.csv"]
    assert (dest / "escaped" / "ok.csv").is_file()
    assert not (tmp_path / "escaped").exists(), "data escaped the artifact tree"


# --------------------------------------------------------------------------- #
# prose-only data links
# --------------------------------------------------------------------------- #
def test_readme_deposit_links_are_found_and_demarkdowned(tmp_path):
    (tmp_path / "README.md").write_text(
        "1. Download participant logs ([OSF](%s))\n"
        "2. See also https://osf.io/cwd6h/files/osfstorage.\n"
        "3. Unrelated: https://github.com/ciao-group/PerceivedRisk\n" % _BUNDLE,
        encoding="utf-8")
    found = referenced_deposits(tmp_path)
    assert found[0] == _BUNDLE, "markdown ')' was kept as part of the URL"
    assert "https://osf.io/cwd6h/files/osfstorage" in found
    assert not any("github.com" in u for u in found)   # code host, not a deposit


def test_clean_url_keeps_balanced_brackets():
    assert _clean_url("https://en.wikipedia.org/wiki/Foo_(bar)") == \
        "https://en.wikipedia.org/wiki/Foo_(bar)"


def test_no_deposit_links_means_no_hint(tmp_path):
    (tmp_path / "README.md").write_text("run main.py\n", encoding="utf-8")
    assert referenced_deposits(tmp_path) == []


# --------------------------------------------------------------------------- #
# manifest declaration
# --------------------------------------------------------------------------- #
def test_manifest_declares_data_sources(tmp_path):
    (tmp_path / "autoui-repro.yml").write_text(
        "version: 1\n"
        "data_sources:\n"
        "  - source: https://osf.io/cwd6h\n"
        "    into: dataset\n"
        "  - source: https://zenodo.org/records/1\n",
        encoding="utf-8")
    assert declared_data_sources(tmp_path) == [
        "https://osf.io/cwd6h::dataset", "https://zenodo.org/records/1"]


def test_dot_reprobe_yaml_is_read(tmp_path):
    """The report tells authors to "declare `steps:` in .reprobe.yaml"; reading
    only autoui-repro.yml made that advice produce a file nothing ever loads."""
    (tmp_path / ".reprobe.yaml").write_text(
        "version: 1\ndata_sources:\n  - source: https://osf.io/cwd6h\n", encoding="utf-8")
    assert declared_data_sources(tmp_path) == ["https://osf.io/cwd6h"]


def test_malformed_manifest_never_aborts_the_run(tmp_path):
    (tmp_path / "autoui-repro.yml").write_text("version: 1\ndata_sources: [[[\n", encoding="utf-8")
    assert declared_data_sources(tmp_path) == []


def test_no_manifest_means_no_declared_sources(tmp_path):
    assert declared_data_sources(tmp_path) == []
