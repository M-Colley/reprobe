"""The deposit fetchers' fetch() bodies: API response -> disk -> FetchResult.

Routing, id parsing and the shared guards are covered in test_fetch.py; what was
untested is what each fetcher DOES with a response — which is where a pin gets
claimed, a warning gets dropped, or an API-supplied filename reaches the disk.
These are also the modules most exposed to change outside this repo: a platform
can alter its JSON shape without notice, and the failure mode is quiet (a deposit
that fetches nothing still renders a clean-looking report).

Pure: no network. Every API call and download is stubbed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reprobe.fetch import anonymous_github as anon_mod
from reprobe.fetch import dataverse as dv_mod
from reprobe.fetch import dryad as dryad_mod
from reprobe.fetch import figshare as fig_mod
from reprobe.fetch import software_heritage as swh_mod
from reprobe.fetch import zenodo as zen_mod
from reprobe.fetch.base import FetchError


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _stub_download(module, monkeypatch, *, ok=True, note="downloaded", record=None):
    def fake(url, dest, **kw):
        if record is not None:
            record.append((url, str(dest), kw))
        if ok:
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_text("payload", encoding="utf-8")
        return ok, note
    monkeypatch.setattr(module, "download", fake)


# --- Zenodo -----------------------------------------------------------------

def _zenodo_payload(files, doi="10.5281/zenodo.42"):
    return {"doi": doi, "files": files, "metadata": {"title": "A dataset", "license": {"id": "cc-by"}}}


def test_zenodo_pins_the_doi_the_api_reports(tmp_path, monkeypatch):
    """The pin is what the Available badge attests, so it comes from the API —
    never from the id we happened to parse out of the user's URL."""
    files = [{"key": "a.csv", "checksum": "md5:1", "links": {"self": "https://z/a"}},
             {"key": "b.csv", "checksum": "md5:2", "links": {"download": "https://z/b"}}]
    monkeypatch.setattr(zen_mod, "get", lambda u, **k: _Resp(_zenodo_payload(files, doi="10.5281/zenodo.99")))
    _stub_download(zen_mod, monkeypatch)
    res = zen_mod.ZenodoFetcher().fetch("https://zenodo.org/records/42", tmp_path / "d")
    assert res.pin.kind == "version_doi" and res.pin.value == "10.5281/zenodo.99"
    assert res.resolved_type == "zenodo" and res.fetch_layer == "zenodo-api"
    assert res.metadata["record_id"] == "42" and res.metadata["title"] == "A dataset"
    assert (tmp_path / "d" / "a.csv").is_file() and (tmp_path / "d" / "b.csv").is_file()


def test_zenodo_a_file_with_no_link_is_warned_about_not_dropped(tmp_path, monkeypatch):
    """A deposit that silently contributes fewer files than it holds produces a
    report about an artifact the chair never saw."""
    files = [{"key": "present.csv", "links": {"self": "https://z/a"}}, {"key": "missing.csv"}]
    monkeypatch.setattr(zen_mod, "get", lambda u, **k: _Resp(_zenodo_payload(files)))
    _stub_download(zen_mod, monkeypatch)
    res = zen_mod.ZenodoFetcher().fetch("10.5281/zenodo.42", tmp_path / "d")
    assert any("no download link for missing.csv" in w for w in res.warnings)


def test_zenodo_a_traversing_filename_stays_inside_the_destination(tmp_path, monkeypatch):
    """The filename comes from the platform's JSON, i.e. from whoever uploaded it."""
    files = [{"key": "../../escaped.csv", "links": {"self": "https://z/a"}}]
    monkeypatch.setattr(zen_mod, "get", lambda u, **k: _Resp(_zenodo_payload(files)))
    seen: list = []
    _stub_download(zen_mod, monkeypatch, record=seen)
    dest = tmp_path / "d"
    zen_mod.ZenodoFetcher().fetch("10.5281/zenodo.42", dest)
    written = Path(seen[0][1]).resolve()
    assert dest.resolve() in written.parents, written
    assert not (tmp_path.parent / "escaped.csv").exists()


def test_zenodo_api_failure_names_the_platform(tmp_path, monkeypatch):
    monkeypatch.setattr(zen_mod, "get", lambda u, **k: _Resp({}, status=500))
    with pytest.raises(FetchError, match="Zenodo API error"):
        zen_mod.ZenodoFetcher().fetch("10.5281/zenodo.42", tmp_path / "d")


def test_zenodo_a_record_with_no_files_is_never_checksum_verified(tmp_path, monkeypatch):
    monkeypatch.setattr(zen_mod, "get", lambda u, **k: _Resp(_zenodo_payload([])))
    _stub_download(zen_mod, monkeypatch)
    res = zen_mod.ZenodoFetcher().fetch("10.5281/zenodo.42", tmp_path / "d")
    assert res.checksum_verified is False


# --- figshare ---------------------------------------------------------------

def test_figshare_unversioned_reference_says_it_took_latest(tmp_path, monkeypatch):
    """"Latest" is not a pin. A reader must not take an unversioned fetch for a
    stable one."""
    calls: list[str] = []

    def fake_get(url, **k):
        calls.append(url)
        return _Resp({"doi": "10.6084/m9.figshare.7", "title": "T", "files": []})
    monkeypatch.setattr(fig_mod, "get", fake_get)
    _stub_download(fig_mod, monkeypatch)
    res = fig_mod.FigshareFetcher().fetch("https://figshare.com/articles/dataset/x/7", tmp_path / "d")
    assert any("did not pin a figshare version" in w for w in res.warnings)
    assert calls == ["https://api.figshare.com/v2/articles/7"]


def test_figshare_versioned_reference_fetches_that_version_silently(tmp_path, monkeypatch):
    calls: list[str] = []

    def fake_get(url, **k):
        calls.append(url)
        return _Resp({"doi": "10.6084/m9.figshare.7.v2", "title": "T", "files": []})
    monkeypatch.setattr(fig_mod, "get", fake_get)
    _stub_download(fig_mod, monkeypatch)
    res = fig_mod.FigshareFetcher().fetch("10.6084/m9.figshare.7.v2", tmp_path / "d")
    assert calls == ["https://api.figshare.com/v2/articles/7/versions/2"]
    assert not any("did not pin" in w for w in res.warnings)
    assert res.metadata["version"] == "2"


def test_figshare_prefers_the_computed_md5_over_the_supplied_one(tmp_path, monkeypatch):
    """A depositor supplies one; the platform computes the other. Only the
    computed one is evidence."""
    files = [{"name": "a.csv", "download_url": "https://f/a",
              "computed_md5": "computed", "supplied_md5": "claimed"}]
    monkeypatch.setattr(fig_mod, "get", lambda u, **k: _Resp({"doi": "d", "files": files}))
    seen: list = []
    _stub_download(fig_mod, monkeypatch, record=seen)
    fig_mod.FigshareFetcher().fetch("10.6084/m9.figshare.7.v1", tmp_path / "d")
    assert seen[0][2]["expected_md5"] == "computed"


# --- Dryad ------------------------------------------------------------------

def test_dryad_encodes_the_doi_into_the_dataset_endpoint(tmp_path, monkeypatch):
    seen: list = []
    _stub_download(dryad_mod, monkeypatch, record=seen)
    res = dryad_mod.DryadFetcher().fetch("https://datadryad.org/stash/dataset/doi:10.5061/dryad.abc123",
                                         tmp_path / "d")
    assert "doi%3A10.5061%2Fdryad.abc123" in seen[0][0]
    assert res.pin.kind == "version_doi" and res.pin.value == "10.5061/dryad.abc123"
    assert any("latest published version" in w for w in res.warnings)


def test_dryad_download_failure_is_an_error_not_an_empty_success(tmp_path, monkeypatch):
    _stub_download(dryad_mod, monkeypatch, ok=False, note="404")
    with pytest.raises(FetchError, match="Dryad download failed"):
        dryad_mod.DryadFetcher().fetch("10.5061/dryad.abc123", tmp_path / "d")


# --- Dataverse --------------------------------------------------------------

def test_dataverse_downloads_from_the_host_in_the_reference(tmp_path, monkeypatch):
    """Any institution can run a Dataverse; hardcoding Harvard would fetch the
    wrong dataset (or nothing) for every other install."""
    seen: list = []
    _stub_download(dv_mod, monkeypatch, record=seen)
    res = dv_mod.DataverseFetcher().fetch(
        "https://darus.uni-stuttgart.de/dataset.xhtml?persistentId=doi:10.18419/darus-1", tmp_path / "d")
    assert seen[0][0].startswith("https://darus.uni-stuttgart.de/api/access/dataset/")
    assert res.pin.kind == "version_doi" and res.pin.value == "10.18419/darus-1"


def test_dataverse_a_handle_pid_is_not_claimed_as_a_doi(tmp_path, monkeypatch):
    """Pin kinds drive the Available badge, so a Handle must not be filed as a DOI."""
    _stub_download(dv_mod, monkeypatch)
    res = dv_mod.DataverseFetcher().fetch(
        "https://data.dataverse.org/dataset.xhtml?persistentId=hdl:1902.1/XYZ", tmp_path / "d")
    assert res.pin.kind == "none"
    assert res.pin.value == "hdl:1902.1/XYZ"


# --- Software Heritage ------------------------------------------------------

def test_swh_pins_the_swhid_even_when_the_bytes_cannot_be_cooked(tmp_path, monkeypatch):
    """The SWHID *is* the archival evidence. A vault that is still cooking must
    not cost the deposit its pin."""
    monkeypatch.setattr(swh_mod, "post", lambda u, **k: _Resp({}))
    monkeypatch.setattr(swh_mod, "get", lambda u, **k: _Resp({"status": "pending"}))
    monkeypatch.setattr(swh_mod.time, "sleep", lambda s: None)
    swhid = "swh:1:dir:" + "a" * 40
    res = swh_mod.SoftwareHeritageFetcher().fetch(swhid, tmp_path / "d")
    assert res.pin.kind == "swhid" and res.pin.value == swhid
    assert any("cooking in progress" in w for w in res.warnings)


def test_swh_a_non_directory_swhid_is_pinned_but_not_retrieved(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(swh_mod, "post", lambda u, **k: called.append(u))
    swhid = "swh:1:rev:" + "b" * 40
    res = swh_mod.SoftwareHeritageFetcher().fetch(swhid, tmp_path / "d")
    assert res.pin.value == swhid
    assert not called, "a revision SWHID was sent to the flat-directory vault"
    assert any("not a directory SWHID" in w for w in res.warnings)


def test_swh_vault_error_is_a_warning_not_a_failed_fetch(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("vault 503")
    monkeypatch.setattr(swh_mod, "post", boom)
    res = swh_mod.SoftwareHeritageFetcher().fetch("swh:1:dir:" + "c" * 40, tmp_path / "d")
    assert any("SWH vault error" in w for w in res.warnings)
    assert res.pin.kind == "swhid"


# --- anonymous.4open.science ------------------------------------------------

def _make_zip(path: Path, names=("a.py",)):
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        for n in names:
            z.writestr(n, "print('x')\n")


def test_anonymous_snapshot_is_flagged_and_never_pinned(tmp_path, monkeypatch):
    """A double-blind link expires and serves a snapshot, not history. Available
    must not rest on it."""
    def fake_download(url, dest, **kw):
        _make_zip(Path(dest))
        return True, "ok"
    monkeypatch.setattr(anon_mod, "download", fake_download)
    res = anon_mod.AnonymousGithubFetcher().fetch(
        "https://anonymous.4open.science/r/MyRepo-1a2b", tmp_path / "d")
    assert res.anonymized is True
    assert res.pin.kind == "none" and res.pin.value == "MyRepo-1a2b"
    assert any("no archival pin" in w for w in res.warnings)
    assert (tmp_path / "d" / "a.py").is_file()
    assert not (tmp_path / "d" / "_anon_repo.zip").exists(), "the snapshot zip was left in the tree"


def test_anonymous_download_failure_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(anon_mod, "download", lambda url, dest, **kw: (False, "410 gone"))
    with pytest.raises(FetchError, match="anonymous github download failed"):
        anon_mod.AnonymousGithubFetcher().fetch(
            "https://anonymous.4open.science/r/Gone-1", tmp_path / "d")


# --- OSF --------------------------------------------------------------------

from reprobe.fetch import osf as osf_mod


def _osf_router(routes):
    """Serve OSF API URLs from a prefix->payload table."""
    def fake_get(url, **k):
        for prefix, payload in routes.items():
            if url.startswith(prefix):
                return _Resp(payload)
        return _Resp({"data": []})
    return fake_get


def _file(name, download="https://osf/dl", md5=None):
    return {"attributes": {"kind": "file", "name": name,
                           "extra": {"hashes": {"md5": md5}} if md5 else {}},
            "links": {"download": download}}


def _folder(name, href):
    return {"attributes": {"kind": "folder", "name": name},
            "relationships": {"files": {"links": {"related": {"href": href}}}}}


def test_osf_walks_into_folders_and_keeps_the_tree(tmp_path, monkeypatch):
    """The common OSF deposit is folders of data, and a walk that only read the
    top level would fetch nothing while reporting success."""
    routes = {
        "https://api.osf.io/v2/guids/": {"data": {"type": "nodes"}},
        "https://api.osf.io/v2/nodes/abcd1/identifiers/": {"data": []},
        "https://api.osf.io/v2/nodes/abcd1/files/osfstorage/": {
            "data": [_file("top.csv"), _folder("Sub", "https://api.osf.io/v2/sub/")]},
        "https://api.osf.io/v2/sub/": {"data": [_file("inner.csv")]},
    }
    monkeypatch.setattr(osf_mod, "get", _osf_router(routes))
    _stub_download(osf_mod, monkeypatch)
    res = osf_mod.OSFFetcher().fetch("https://osf.io/abcd1", tmp_path / "d")
    assert (tmp_path / "d" / "top.csv").is_file()
    assert (tmp_path / "d" / "Sub" / "inner.csv").is_file()
    assert res.resolved_type == "osf"


def test_osf_follows_pagination(tmp_path, monkeypatch):
    """OSF pages at 10 items. Ignoring links.next silently truncates a deposit."""
    pages = {
        "https://api.osf.io/v2/guids/": {"data": {"type": "nodes"}},
        "https://api.osf.io/v2/nodes/abcd1/identifiers/": {"data": []},
        "https://api.osf.io/v2/nodes/abcd1/files/osfstorage/": {
            "data": [_file("p1.csv")], "links": {"next": "https://api.osf.io/v2/page2/"}},
        "https://api.osf.io/v2/page2/": {"data": [_file("p2.csv")], "links": {"next": None}},
    }
    monkeypatch.setattr(osf_mod, "get", _osf_router(pages))
    _stub_download(osf_mod, monkeypatch)
    osf_mod.OSFFetcher().fetch("https://osf.io/abcd1", tmp_path / "d")
    assert (tmp_path / "d" / "p1.csv").is_file() and (tmp_path / "d" / "p2.csv").is_file()


def test_osf_an_empty_deposit_says_so(tmp_path, monkeypatch):
    """"Fetched, zero files" and "fetched fine" must not render the same."""
    routes = {"https://api.osf.io/v2/guids/": {"data": {"type": "nodes"}},
              "https://api.osf.io/v2/nodes/abcd1/": {"data": []}}
    monkeypatch.setattr(osf_mod, "get", _osf_router(routes))
    _stub_download(osf_mod, monkeypatch)
    res = osf_mod.OSFFetcher().fetch("https://osf.io/abcd1", tmp_path / "d")
    assert any("no files found in osfstorage" in w for w in res.warnings)
    assert res.checksum_verified is False


def test_osf_a_view_only_token_is_carried_and_flagged(tmp_path, monkeypatch):
    """A double-blind review link must reach every API call, and the report must
    record that what was reviewed was an anonymized view."""
    seen_params: list = []

    def fake_get(url, **k):
        seen_params.append(k.get("params"))
        if url.startswith("https://api.osf.io/v2/guids/"):
            return _Resp({"data": {"type": "nodes"}})
        return _Resp({"data": []})
    monkeypatch.setattr(osf_mod, "get", fake_get)
    _stub_download(osf_mod, monkeypatch)
    res = osf_mod.OSFFetcher().fetch("https://osf.io/abcd1/?view_only=deadbeef", tmp_path / "d")
    assert res.anonymized is True
    assert all(p == {"view_only": "deadbeef"} for p in seen_params), seen_params


def test_osf_api_failure_is_an_error_not_an_empty_deposit(tmp_path, monkeypatch):
    def fake_get(url, **k):
        if "files/osfstorage" in url:
            return _Resp({}, status=500)
        return _Resp({"data": {"type": "nodes"}})
    monkeypatch.setattr(osf_mod, "get", fake_get)
    with pytest.raises(FetchError, match="OSF API error"):
        osf_mod.OSFFetcher().fetch("https://osf.io/abcd1", tmp_path / "d")


def test_osf_a_traversing_folder_name_stays_inside_the_destination(tmp_path, monkeypatch):
    routes = {
        "https://api.osf.io/v2/guids/": {"data": {"type": "nodes"}},
        "https://api.osf.io/v2/nodes/abcd1/identifiers/": {"data": []},
        "https://api.osf.io/v2/nodes/abcd1/files/osfstorage/": {
            "data": [_folder("../../escaped", "https://api.osf.io/v2/sub/")]},
        "https://api.osf.io/v2/sub/": {"data": [_file("inner.csv")]},
    }
    monkeypatch.setattr(osf_mod, "get", _osf_router(routes))
    seen: list = []
    _stub_download(osf_mod, monkeypatch, record=seen)
    dest = tmp_path / "d"
    osf_mod.OSFFetcher().fetch("https://osf.io/abcd1", dest)
    assert dest.resolve() in Path(seen[0][1]).resolve().parents
