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

def _dryad_api(monkeypatch, files, *, version=3):
    """The v2 API as Dryad serves it: dataset record -> version -> file listing."""
    calls: list[str] = []

    def fake_get(url, **kw):
        calls.append(url)
        if url.endswith("/files"):
            return _Resp({"_embedded": {"stash:files": files}, "_links": {}})
        return _Resp({"versionNumber": version,
                      "_links": {"stash:version": {"href": "/api/v2/versions/77"}}})
    monkeypatch.setattr(dryad_mod, "get", fake_get)
    return calls


def _dryad_file(path="a.csv", digest="abc", digest_type="md5"):
    return {"path": path, "digest": digest, "digestType": digest_type,
            "_links": {"stash:download": {"href": f"/api/v2/files/1/download/{path}"}}}


def test_dryad_encodes_the_doi_into_the_dataset_endpoint(tmp_path, monkeypatch):
    calls = _dryad_api(monkeypatch, [_dryad_file()])
    seen: list = []
    _stub_download(dryad_mod, monkeypatch, record=seen)
    res = dryad_mod.DryadFetcher().fetch("https://datadryad.org/stash/dataset/doi:10.5061/dryad.abc123",
                                         tmp_path / "d")
    assert "doi%3A10.5061%2Fdryad.abc123" in calls[0]
    assert res.pin.kind == "version_doi" and res.pin.value == "10.5061/dryad.abc123"
    assert any("latest published version" in w for w in res.warnings)
    assert res.metadata["version"] == 3 and res.metadata["version_id"] == "77"


def test_dryad_verifies_the_digest_dryad_records(tmp_path, monkeypatch):
    """A Dryad DOI names the dataset, not its bytes. The per-file digest is the
    evidence, so it is passed to the download and only a verified set counts."""
    _dryad_api(monkeypatch, [_dryad_file(digest="deadbeef", digest_type="sha-256")])
    seen: list = []
    _stub_download(dryad_mod, monkeypatch, record=seen, note="checksum verified")
    res = dryad_mod.DryadFetcher().fetch("10.5061/dryad.abc123", tmp_path / "d")
    assert seen[0][2]["expected_digest"] == ("sha256", "deadbeef")
    assert res.checksum_verified is True


def test_dryad_unverifiable_files_do_not_verify_the_deposit(tmp_path, monkeypatch):
    f = _dryad_file()
    f.pop("digest")
    _dryad_api(monkeypatch, [f])
    _stub_download(dryad_mod, monkeypatch, note="downloaded (no checksum provided)")
    res = dryad_mod.DryadFetcher().fetch("10.5061/dryad.abc123", tmp_path / "d")
    assert res.checksum_verified is False


def test_dryad_download_failure_is_an_error_not_an_empty_success(tmp_path, monkeypatch):
    _dryad_api(monkeypatch, [_dryad_file()])
    _stub_download(dryad_mod, monkeypatch, ok=False, note="404")
    with pytest.raises(FetchError, match="Dryad download failed"):
        dryad_mod.DryadFetcher().fetch("10.5061/dryad.abc123", tmp_path / "d")


# --- Dataverse --------------------------------------------------------------

def _dv_api(monkeypatch, *, files=None, version_state=(1, 0)):
    """The native API's dataset-version record, plus doi.org's handle answer."""
    seen_get: list = []
    entry = {"label": "a.csv", "dataFile": {"id": 5, "checksum": {"type": "MD5", "value": "abc"}}}

    def fake_get_json(url, **kw):
        seen_get.append((url, kw))
        return {"data": {"versionNumber": version_state[0], "versionMinorNumber": version_state[1],
                         "files": [entry] if files is None else files}}
    monkeypatch.setattr(dv_mod, "get_json", fake_get_json)
    return seen_get


def test_dataverse_downloads_from_the_host_in_the_reference(tmp_path, monkeypatch):
    """Any institution can run a Dataverse; hardcoding Harvard would fetch the
    wrong dataset (or nothing) for every other install."""
    seen_get = _dv_api(monkeypatch)
    monkeypatch.setattr(dv_mod, "assert_safe_url", lambda url: None)
    monkeypatch.setattr(dv_mod, "resolve_doi", lambda doi: "https://darus.uni-stuttgart.de/dataset.xhtml")
    seen: list = []
    _stub_download(dv_mod, monkeypatch, record=seen, note="checksum verified")
    res = dv_mod.DataverseFetcher().fetch(
        "https://darus.uni-stuttgart.de/dataset.xhtml?persistentId=doi:10.18419/darus-1", tmp_path / "d")
    assert seen_get[0][0].startswith("https://darus.uni-stuttgart.de/api/datasets/")
    assert seen[0][0].startswith("https://darus.uni-stuttgart.de/api/access/datafile/5")
    assert seen[0][2]["restrict_public"] is True             # every request is SSRF-guarded
    assert seen[0][2]["expected_digest"] == ("md5", "abc")
    assert res.pin.kind == "version_doi" and res.pin.value == "10.18419/darus-1"
    assert res.checksum_verified is True


def test_dataverse_a_doi_that_points_elsewhere_is_not_an_archival_pin(tmp_path, monkeypatch):
    """`dataverse.<anything>` can serve its own bytes and verify them against its
    own API under someone else's DOI. The pin is only archival when doi.org says
    the DOI belongs to THIS install."""
    _dv_api(monkeypatch)
    monkeypatch.setattr(dv_mod, "assert_safe_url", lambda url: None)
    monkeypatch.setattr(dv_mod, "resolve_doi", lambda doi: "https://elsewhere.example/dataset")
    _stub_download(dv_mod, monkeypatch)
    res = dv_mod.DataverseFetcher().fetch(
        "https://dataverse.evil.example/dataset.xhtml?persistentId=doi:10.18419/darus-1", tmp_path / "d")
    assert res.pin.kind == "none"
    assert any("does not resolve to" in w for w in res.warnings)


def test_dataverse_a_handle_pid_is_not_claimed_as_a_doi(tmp_path, monkeypatch):
    """Pin kinds drive the Available badge, so a Handle must not be filed as a DOI."""
    _dv_api(monkeypatch)
    monkeypatch.setattr(dv_mod, "assert_safe_url", lambda url: None)
    _stub_download(dv_mod, monkeypatch)
    res = dv_mod.DataverseFetcher().fetch(
        "https://data.dataverse.org/dataset.xhtml?persistentId=hdl:1902.1/XYZ", tmp_path / "d")
    assert res.pin.kind == "none"
    assert res.pin.value == "hdl:1902.1/XYZ"


def test_dataverse_honours_an_explicit_version(tmp_path, monkeypatch):
    seen_get = _dv_api(monkeypatch, version_state=(2, 1))
    monkeypatch.setattr(dv_mod, "assert_safe_url", lambda url: None)
    monkeypatch.setattr(dv_mod, "resolve_doi", lambda doi: None)
    _stub_download(dv_mod, monkeypatch)
    res = dv_mod.DataverseFetcher().fetch(
        "https://data.dataverse.org/dataset.xhtml?persistentId=doi:10.7910/DVN/ABC123&version=2.1", tmp_path / "d")
    assert "/versions/2.1" in seen_get[0][0]
    assert res.metadata["version"] == "2.1"


def test_dataverse_refuses_an_internal_host_before_any_request(tmp_path, monkeypatch):
    seen_get = _dv_api(monkeypatch)
    with pytest.raises(FetchError):
        dv_mod.DataverseFetcher().fetch(
            "http://dataverse@169.254.169.254/dataset.xhtml?persistentId=doi:10.7/x", tmp_path / "d")
    assert not seen_get


def test_dataverse_does_not_match_a_userinfo_or_suffix_trick():
    f = dv_mod.DataverseFetcher(extra_hosts=("darus.uni-stuttgart.de",))
    assert not f.can_handle("http://dataverse@169.254.169.254/dataset.xhtml?persistentId=doi:10.7/x")
    assert not f.can_handle("https://darus.uni-stuttgart.de.evil.example/dataset.xhtml?persistentId=doi:1/x")
    assert f.can_handle("https://darus.uni-stuttgart.de/dataset.xhtml?persistentId=doi:10.7/x")


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
    assert res.checksum_verified is False


def test_swh_a_snapshot_swhid_is_pinned_but_not_retrieved(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(swh_mod, "post", lambda u, **k: called.append(u))
    swhid = "swh:1:snp:" + "b" * 40
    res = swh_mod.SoftwareHeritageFetcher().fetch(swhid, tmp_path / "d")
    assert res.pin.value == swhid
    assert not called, "a snapshot SWHID was sent to the flat-directory vault"
    assert any("not a directory SWHID" in w for w in res.warnings)


def test_swh_a_revision_is_resolved_to_its_directory_before_the_vault(tmp_path, monkeypatch):
    directory = "c" * 40
    posted: list = []
    monkeypatch.setattr(swh_mod, "get", lambda u, **k: _Resp({"directory": directory}))
    monkeypatch.setattr(swh_mod, "post", lambda u, **k: posted.append(u) or _Resp({}))
    monkeypatch.setattr(swh_mod.time, "sleep", lambda s: None)
    res = swh_mod.SoftwareHeritageFetcher().fetch("swh:1:rev:" + "b" * 40, tmp_path / "d")
    assert posted and f"swh:1:dir:{directory}" in posted[0]
    assert res.metadata["directory"] == f"swh:1:dir:{directory}"
    assert any("revision object itself was not re-hashed" in w for w in res.warnings)


def test_swh_an_unresolvable_revision_keeps_its_pin_and_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr(swh_mod, "get", lambda u, **k: (_ for _ in ()).throw(RuntimeError("503")))
    posted: list = []
    monkeypatch.setattr(swh_mod, "post", lambda u, **k: posted.append(u))
    res = swh_mod.SoftwareHeritageFetcher().fetch("swh:1:rev:" + "b" * 40, tmp_path / "d")
    assert res.pin.kind == "swhid" and not posted
    assert any("could not resolve" in w for w in res.warnings)


def _fake_swh_model(monkeypatch, computed):
    """Stand-in for swh.model.from_disk, so the tests need neither the package nor
    the network."""
    import sys
    import types

    class _Node:
        def swhid(self):
            return computed

    class _Directory:
        @staticmethod
        def from_disk(path, max_content_length=None):
            return _Node()

    mod = types.ModuleType("swh.model.from_disk")
    mod.Directory = _Directory
    for name in ("swh", "swh.model"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "swh.model.from_disk", mod)


def test_swh_verification_compares_the_tree_hash_to_the_swhid(tmp_path, monkeypatch):
    want = "swh:1:dir:" + "a" * 40
    root = tmp_path / "tree"
    root.mkdir()
    meta: dict = {}
    _fake_swh_model(monkeypatch, want)
    assert swh_mod.verify_directory(root, want, [], meta) is True
    assert meta["swhid_verification"] == "verified"

    warnings: list[str] = []
    _fake_swh_model(monkeypatch, "swh:1:dir:" + "f" * 40)
    assert swh_mod.verify_directory(root, want, warnings, meta) is False
    assert meta["swhid_verification"] == "mismatch"
    assert any("SWHID MISMATCH" in w for w in warnings)


def test_swh_verification_agrees_with_gits_own_tree_hash(tmp_path):
    """A SWHID directory id is, by specification, the git tree hash of the
    directory. Git is the independent oracle: the reference implementation
    (`reprobe[swh]`) must reproduce what git says, and disagree with anything else."""
    pytest.importorskip("swh.model.from_disk")
    import shutil

    from reprobe.fetch.base import run_git

    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / "a.txt").write_text("hi\n", encoding="utf-8")
    (repo / "sub" / "b.py").write_text("x = 1\n", encoding="utf-8")
    assert run_git(["init", "-q", "."], cwd=repo).returncode == 0
    assert run_git(["-c", "core.autocrlf=false", "add", "-A"], cwd=repo).returncode == 0
    tree = run_git(["write-tree"], cwd=repo).stdout.strip()
    assert len(tree) == 40
    clean = tmp_path / "clean"
    shutil.copytree(repo, clean, ignore=shutil.ignore_patterns(".git"))

    meta: dict = {}
    assert swh_mod.verify_directory(clean, f"swh:1:dir:{tree}", [], meta) is True
    assert meta["swhid_verification"] == "verified"
    assert swh_mod.verify_directory(clean, "swh:1:dir:" + "0" * 40, [], {}) is False


def test_swh_without_the_optional_package_never_claims_verification(tmp_path, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "swh.model.from_disk", None)      # import raises ImportError
    warnings: list[str] = []
    meta: dict = {}
    assert swh_mod.verify_directory(tmp_path, "swh:1:dir:" + "a" * 40, warnings, meta) is False
    assert "reprobe[swh]" in meta["swhid_verification"]
    assert any("NOT checked" in w for w in warnings)


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
