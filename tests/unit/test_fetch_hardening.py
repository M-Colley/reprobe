"""The fetch layer runs on the TRUSTED host against bytes and references an author
chose. These pin the defences added after a review of that boundary: who may name
what, which addresses are reachable, and what git is allowed to inherit from the
chair. Offline: IP literals, monkeypatched transports, tmp_path trees."""

from __future__ import annotations

import os

import pytest

import reprobe.fetch.base as base
from reprobe.fetch import doi as doi_mod
from reprobe.fetch import registry
from reprobe.fetch.base import FetchError, RefusedSource
from reprobe.fetch.data_source import fetch_data_source, merge_into

# --------------------------------------------------------------------------- #
# Which addresses a fetch may reach
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("addr", [
    "100.100.100.200",                  # Alibaba Cloud metadata — CGNAT, not RFC1918
    "100.64.0.1", "100.127.255.254",    # CGNAT range edges (Tailscale lives here)
    "169.254.169.254",                  # AWS/GCP/Azure metadata
    "10.0.0.1", "172.16.5.5", "192.168.0.1", "127.0.0.1", "0.0.0.0",
    "::1", "fe80::1", "fc00::1", "fd12:3456::1",
    "::ffff:169.254.169.254",           # v4-mapped
    "2002:a9fe:a9fe::1",                # 6to4 wrapping 169.254.169.254
    "64:ff9b::a9fe:a9fe",               # NAT64 wrapping 169.254.169.254
    "224.0.0.1", "ff02::1",             # multicast
])
def test_internal_and_special_addresses_are_never_public(addr):
    assert base.is_public_host(addr) is False


@pytest.mark.parametrize("addr", [
    "8.8.8.8", "93.184.216.34", "2606:4700:4700::1111",
    "64:ff9b::808:808",                 # NAT64 of a PUBLIC v4: how IPv6-only hosts reach it
])
def test_public_addresses_stay_reachable(addr):
    assert base.is_public_host(addr) is True


def test_a_url_with_a_userinfo_trick_is_judged_by_its_real_host():
    # urlparse().hostname drops "user@"; the host a request would reach is the IP.
    with pytest.raises(FetchError):
        base.assert_safe_url("http://dataverse@169.254.169.254/x")
    with pytest.raises(FetchError):
        base.assert_safe_url("http://93.184.216.34@10.0.0.1/x")


def test_probe_status_refuses_an_internal_start_without_connecting(monkeypatch):
    def boom(*a, **k):                  # a request here would be the port scan
        raise AssertionError("connected to an internal host")
    for sess in (base._SESSION, base._DIRECT):
        monkeypatch.setattr(sess, "get", boom, raising=False)
    with pytest.raises(FetchError):
        base.probe_status("http://10.0.0.5/admin")


# --------------------------------------------------------------------------- #
# What an author may name in a manifest (untrusted) vs what the operator may
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("ref", [
    "/home/chair/.ssh", "C:\\Users\\chair\\.ssh", "..", ".", "work", "~/.aws",
    "file:///etc/passwd",
    "git@github.com:a/b.git",           # would clone with the chair's SSH keys
    "ssh://git@host/a/b.git", "git://host/a/b.git", "ftp://host/x",
    "http://169.254.169.254/latest/meta-data", "https://100.100.100.200/x",
    "http://10.0.0.5/data.zip", "",
])
def test_untrusted_refs_that_reach_the_host_are_refused(ref):
    with pytest.raises(RefusedSource):
        registry.check_untrusted_ref(ref)


@pytest.mark.parametrize("ref", [
    "https://93.184.216.34/data.zip",
    "10.5281/zenodo.1234567",
    "https://doi.org/10.5061/dryad.abc123",
    "swh:1:dir:" + "a" * 40,
])
def test_untrusted_refs_that_use_a_public_api_are_admitted(ref):
    assert registry.check_untrusted_ref(ref) == ref


def test_untrusted_selection_skips_the_local_path_fetcher(tmp_path):
    # LocalFetcher is a catch-all for any existing path; for an author's source it
    # must not even be a candidate, whatever the string looks like.
    assert registry.select(str(tmp_path), untrusted=False) is not None
    assert registry.select(str(tmp_path), untrusted=True) is None


def test_fetch_data_source_untrusted_never_reads_a_host_directory(tmp_path):
    secret = tmp_path / "host-secret"
    secret.mkdir()
    (secret / "id_rsa").write_text("PRIVATE", encoding="utf-8")
    stage = tmp_path / "stage"
    with pytest.raises(RefusedSource):
        fetch_data_source(str(secret), stage, untrusted=True)
    assert not stage.exists() or not any(stage.rglob("id_rsa"))
    # the operator's own --data may name it
    res = fetch_data_source(str(secret), stage, untrusted=False)
    assert (stage / "id_rsa").is_file() and res.resolved_type == "local"


def test_a_doi_whose_registrant_points_inside_is_refused(monkeypatch):
    """The landing page a DOI names is whatever its registrant registered — for a
    self-minted DOI that is author-controlled, so it gets the untrusted rules."""
    monkeypatch.setattr(registry, "resolve_doi", lambda doi: "http://10.0.0.5/data.zip")
    with pytest.raises(RefusedSource, match="not fetched"):
        registry.fetch("10.9999/internal.1", "unused")


def test_resolving_a_doi_never_fetches_the_landing_page(monkeypatch):
    """doi.org's handle API returns the target as JSON. Following the redirect
    chain instead would fetch the registrant's page from the trusted host."""
    seen = {}

    class R:
        status_code = 200

        @staticmethod
        def json():
            return {"responseCode": 1,
                    "values": [{"type": "URL", "data": {"value": "https://example.org/landing"}}]}

    def fake_get(url, **kw):
        seen.update(url=url, **kw)
        return R()
    monkeypatch.setattr(doi_mod, "get", fake_get)
    assert doi_mod.resolve_doi("doi:10.5281/zenodo.1") == "https://example.org/landing"
    assert seen["url"] == "https://doi.org/api/handles/10.5281/zenodo.1"
    assert seen["allow_redirects"] is False


# --------------------------------------------------------------------------- #
# git runs as the host, against an untrusted remote — it must not be the chair
# --------------------------------------------------------------------------- #

def test_git_inherits_nothing_that_would_authenticate_as_the_chair(monkeypatch):
    for k, v in {"GIT_ASKPASS": "/bin/steal", "GIT_SSH_COMMAND": "ssh -i ~/.ssh/id",
                 "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
                 "GIT_SSL_CAINFO": "/corp/ca.pem", "SSH_ASKPASS": "/bin/steal2"}.items():
        monkeypatch.setenv(k, v)
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, env=kw["env"])

        class P:
            returncode, stdout, stderr = 0, "", ""
        return P()
    monkeypatch.setattr(base.subprocess, "run", fake_run)
    base.run_git(["clone", "--", "https://example.org/a.git", "x"])

    env, argv = seen["env"], seen["argv"]
    assert argv[:5] == ["git", "-c", "credential.helper=", "-c", "core.symlinks=false"]
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ALLOW_PROTOCOL"] == "http:https:git:ssh"
    for leaked in ("GIT_ASKPASS", "GIT_SSH_COMMAND", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
                   "SSH_ASKPASS"):
        assert leaked not in env, f"{leaked} reached git"
    assert env["GIT_SSL_CAINFO"] == "/corp/ca.pem"        # a trust root, not a credential


# --------------------------------------------------------------------------- #
# Links in a fetched deposit are never followed
# --------------------------------------------------------------------------- #

def _try_symlink(link, target, directory=False):
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not creatable on this platform/user")


def test_merge_into_does_not_copy_what_a_link_points_at(tmp_path):
    secret = tmp_path / "host-secret"
    secret.mkdir()
    (secret / "id_rsa").write_text("PRIVATE", encoding="utf-8")
    deposit, tree = tmp_path / "deposit", tmp_path / "tree"
    deposit.mkdir()
    tree.mkdir()
    (deposit / "ok.csv").write_text("a\n", encoding="utf-8")
    _try_symlink(deposit / "leak", secret, directory=True)
    _try_symlink(deposit / "stolen.txt", secret / "id_rsa")

    skipped: list[str] = []
    copied, collisions = merge_into(deposit, tree, "dataset", skipped=skipped)
    assert copied == ["dataset/ok.csv"] and not collisions
    assert sorted(skipped) == ["leak", "stolen.txt"]
    assert not list(tree.rglob("id_rsa")) and not (tree / "dataset" / "stolen.txt").exists()


def test_merge_into_will_not_write_through_a_link_the_artifact_committed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    deposit, tree = tmp_path / "deposit", tmp_path / "tree"
    (deposit / "sub").mkdir(parents=True)
    tree.mkdir()
    (deposit / "sub" / "x.csv").write_text("a\n", encoding="utf-8")
    _try_symlink(tree / "sub", outside, directory=True)     # the CODE source's own link

    copied, collisions = merge_into(deposit, tree, "")
    assert not copied and collisions == ["sub/x.csv"]
    assert not (outside / "x.csv").exists(), "a deposit wrote through the artifact's symlink"


# --------------------------------------------------------------------------- #
# The validated host must be the host that is connected to
# --------------------------------------------------------------------------- #

_BACKSLASH = chr(92)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:80" + _BACKSLASH + "@8.8.8.8/x",    # urlparse: 8.8.8.8, urllib3: 127.0.0.1
    "http://8.8.8.8" + _BACKSLASH + ".evil.example/x",
    "http://8.8.8.8/a b", "http://8.8.8.8/\x00", "http://8.8.8.8/\r\nHost: x", "http://8.8.8.8/\t",
    "https://bücher.example/x",                      # urllib3 resolves the IDNA form
    "http://8.8.8.8:99999/x", "ftp://8.8.8.8/x", "", "http://",
])
def test_urls_that_two_parsers_could_read_differently_are_refused(url):
    with pytest.raises(FetchError):
        base.assert_safe_url(url)


def test_the_backslash_bypass_cannot_reach_a_local_server(monkeypatch):
    """Reproduced from the review: assert_safe_url passed and the download fetched
    a local server's bytes into the run tree."""
    called = []
    for sess in (base._SESSION, base._DIRECT):
        monkeypatch.setattr(sess, "get", lambda *a, **k: called.append(a) or None, raising=False)
    ok, note = base.download("http://127.0.0.1:80" + _BACKSLASH + "@8.8.8.8/x", __import__("pathlib").Path("x"),
                             restrict_public=True)
    assert ok is False and not called


def test_every_redirect_hop_gets_the_same_strict_check(monkeypatch):
    class Hop:
        is_redirect = True
        is_permanent_redirect = False
        headers = {"Location": "http://127.0.0.1:80" + _BACKSLASH + "@8.8.8.8/x"}

        @staticmethod
        def close():
            pass

    monkeypatch.setattr(base, "_resolve_public", lambda h, p: [(2, 1, 6, "", ("8.8.8.8", p))])
    monkeypatch.setattr(base._DIRECT, "get", lambda *a, **k: Hop(), raising=False)
    with pytest.raises(FetchError, match="backslash"):
        base._open_stream("http://93.184.216.34/x", None, 5, restrict_public=True)


@pytest.mark.parametrize("asked", ["example.com", "EXAMPLE.com", "example.com."])
def test_the_dns_pin_applies_however_urllib3_spells_the_host(asked):
    """urllib3 strips a trailing dot before resolving; a pin keyed on the exact
    string would be skipped and the name re-resolved (DNS rebinding)."""
    import socket
    pinned = [(2, 1, 6, "", ("93.184.216.34", 443))]
    with base._pinned_dns("example.com.", pinned):
        assert socket.getaddrinfo(asked, 443) == pinned
    assert socket.getaddrinfo is not None


def test_a_workroot_inside_the_artifact_is_not_copied_into_it(tmp_path):
    """`reprobe run .` with the default --workroot work: copytree walked into its
    own output, and every earlier run's work dir became part of the artifact."""
    from reprobe.fetch.local import LocalFetcher
    art = tmp_path / "repo"
    (art / "work" / "old-sub" / "src").mkdir(parents=True)
    (art / "work" / "old-sub" / "src" / "stale.py").write_text("x = 1\n", encoding="utf-8")
    (art / "analysis.py").write_text("print(1)\n", encoding="utf-8")
    dest = art / "work" / "this-sub" / "src"

    res = LocalFetcher().fetch(str(art), dest)
    assert sorted(p.name for p in dest.iterdir()) == ["analysis.py"]
    assert any("work/` was left out" in w for w in res.warnings)


def test_a_workroot_outside_the_artifact_changes_nothing(tmp_path):
    from reprobe.fetch.local import LocalFetcher
    art = tmp_path / "repo"
    (art / "work").mkdir(parents=True)                 # the author's own "work" dir
    (art / "work" / "notes.txt").write_text("n\n", encoding="utf-8")
    res = LocalFetcher().fetch(str(art), tmp_path / "wk" / "sid" / "src")
    assert (tmp_path / "wk" / "sid" / "src" / "work" / "notes.txt").is_file()
    assert not any("left out" in w for w in res.warnings)


# --- third review: the trust boundary ---------------------------------------

@pytest.mark.parametrize("packages", [
    {"evil pkg": {"Package": "evil pkg", "Source": "GitHub", "RemoteRepo": "x/y"}},
    {"praise": {"Package": "evilpkg", "Source": "Repository", "Repository": "CRAN"}},
])
def test_an_renv_record_that_could_slip_past_the_exclusion_refuses_the_lockfile(packages):
    """Exclusion is by key. A key R cannot name, or one that disagrees with the
    record's own Package, would have been restored from wherever it says — with
    network — so the whole lockfile is refused."""
    import json

    from reprobe.envbuild.sanitize import classify_renv_lock
    excluded, notes = classify_renv_lock(json.dumps({"Packages": packages}))
    assert excluded is None and "refused" in notes[0][1]


def test_renv_restores_from_a_clean_project_with_a_named_library():
    """The artifact's renv/ folder (settings.json, a profile switching the
    lockfile) must not steer a restore that runs with network."""
    from reprobe.envbuild.base import _renv_restore_command
    for activation in (None, "", "1.2.3"):
        cmd = _renv_restore_command("https://r", [], activation)
        assert 'renv::restore(project=p, library=lib' in cmd
        assert 'file.path(tempdir(), "reprobe-renv")' in cmd
        assert "project=getwd()," not in cmd.replace("paths$library(project=getwd())", "")


def test_checking_out_the_submitted_ref_is_size_capped_like_the_clone(tmp_path, monkeypatch):
    import reprobe.fetch.git_host as gh
    calls = []

    class R:
        returncode, stdout, stderr = 0, "", ""

    def fake(args, cwd=None, timeout=600, **kw):
        calls.append((args[0], kw))
        return R()
    monkeypatch.setattr(gh, "run_git", fake)
    monkeypatch.setattr(gh, "_lfs_tracked_files", lambda dest: (True, []))
    try:
        gh.GitHostFetcher().fetch("https://github.com/a/b/tree/v1.0", tmp_path / "d")
    except Exception:
        pass                                   # later steps need a real repo; only the calls matter
    by = {a: kw for a, kw in calls}
    assert by["clone"].get("max_bytes") and by["checkout"].get("max_bytes") == by["clone"]["max_bytes"]


def test_a_zip_declaring_too_many_entries_is_refused_before_it_is_opened(tmp_path, monkeypatch):
    import zipfile
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for i in range(5):
            zf.writestr(f"f{i}.txt", "x")
    assert base.zip_entry_count(z) == 5
    monkeypatch.setattr(base, "_MAX_ARCHIVE_MEMBERS", 3)
    opened = []
    monkeypatch.setattr(zipfile, "ZipFile", lambda *a, **k: opened.append(a) or (_ for _ in ()).throw(AssertionError))
    warnings: list[str] = []
    base.maybe_unzip(tmp_path, warnings)
    assert not opened and "too many members" in warnings[0]


def test_tar_headers_are_counted_as_they_are_read(tmp_path, monkeypatch):
    import io
    import tarfile
    t = tmp_path / "a.tar.gz"
    with tarfile.open(t, "w:gz") as tf:
        for i in range(10):
            info = tarfile.TarInfo(f"f{i}")
            tf.addfile(info, io.BytesIO(b""))
    monkeypatch.setattr(base, "_MAX_ARCHIVE_MEMBERS", 3)
    read = []
    real_next = tarfile.TarFile.next

    def counting(self):
        m = real_next(self)
        read.append(m)
        return m
    monkeypatch.setattr(tarfile.TarFile, "next", counting)
    warnings: list[str] = []
    base.maybe_unzip(tmp_path, warnings)
    assert "too many members" in warnings[0]
    # open() reads the first header itself; then it stops at the first one past the cap
    assert {m.name for m in read if m} == {"f0", "f1", "f2", "f3"}
    assert not (tmp_path / "f0").exists()


@pytest.mark.parametrize("module, call", [
    ("figshare", "download"), ("zenodo", "download"), ("osf", "download"),
    ("dryad", "download"), ("software_heritage", "download"), ("anonymous_github", "download"),
])
def test_downloads_from_platform_api_urls_guard_every_hop(module, call):
    """The URL comes from an API response — figshare link-only files, an
    absolute Dryad href — so the per-hop public-address check applies."""
    import importlib
    import inspect
    src = inspect.getsource(importlib.import_module(f"reprobe.fetch.{module}"))
    calls = [ln for ln in src.splitlines() if f"{call}(" in ln and "def " not in ln
             and "import" not in ln and "record_download" not in ln]
    assert calls
    body = src[src.index(calls[0].strip()):]
    assert "restrict_public=True" in body[:400], (module, calls[0])
