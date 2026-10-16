"""Regressions for defects found by an adversarial review of the trust-boundary
code. Every case is the reviewer's reproduced input, so each of these once
produced the bad outcome named in its docstring."""

from __future__ import annotations

import json
import re
import time

import pytest
import yaml

from reprobe.config import load_config
from reprobe.envbuild import plan as plan_env
from reprobe.envbuild import sanitize as Z
from reprobe.models import DetectResult

BACKSLASH = chr(92)


def _plan(tmp_path, files, *, types=("python",), meta=None, **det):
    for rel, text in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    return plan_env(DetectResult(artifact_types=list(types), steps=[], **det), meta or {},
                    load_config(), tmp_path)


# --------------------------------------------------------------------------- #
# A committed archive reached pip: `evil-1.0.tar.gz` is a valid requirement NAME,
# but pip installs it as a local sdist when one is in its cwd — running setup.py
# while the network is open.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("line", [
    "evil-1.0.tar.gz",
    'evil-1.0.tar.gz ; python_version>="3"',        # survived: the suffix test saw the whole line
    "evil-1.0.tar.gz[x]",                           # survived: an extra hid the suffix
    "evil-1.0.tar.gz[x] ; sys_platform=='linux'",
    "evil.tbz", "evil.tbz2", "evil.txz", "evil.tlz", "evil.tar.lz", "evil.tar.lzma",
    "evil.tar.xz", "evil.tar.bz2", "evil.tar", "evil.zip", "evil.whl", "evil.egg",
    "EVIL.TAR.GZ",
])
def test_no_archive_spelling_survives_the_sanitiser(line):
    kept, dropped = Z.filter_requirement_lines([line])
    assert kept == [] and dropped and "archive" in dropped[0][1]


def test_pip_runs_from_a_directory_that_holds_only_the_sanitised_request(tmp_path):
    p = _plan(tmp_path, {"requirements.txt": "pandas\n"})
    cmd = next(c for c in p.install_commands if "-r /work/.reprobe_req/requirements.txt" in c)
    assert " && cd /work/.reprobe_req && " in cmd
    assert cmd.index("cd /work/.reprobe_req") < cmd.index("pip install")


def test_a_comment_ending_in_a_backslash_does_not_swallow_the_next_requirement(tmp_path):
    (tmp_path / "r.txt").write_text("# note" + BACKSLASH + "\nnumpy==1.26\n", encoding="utf-8")
    kept, _ = Z.filter_requirements(tmp_path / "r.txt", tmp_path)
    assert kept == ["numpy==1.26"]


def test_a_hash_token_ending_in_a_backslash_cannot_glue_the_next_line_on(tmp_path):
    kept, dropped = Z.filter_requirement_lines(["x --hash=sha256:aa" + BACKSLASH])
    assert kept == [] and dropped


def test_a_nul_in_an_include_path_is_a_drop_not_a_crash(tmp_path):
    (tmp_path / "r.txt").write_text("pandas\n-r a\x00b\n", encoding="utf-8")
    kept, dropped = Z.filter_requirements(tmp_path / "r.txt", tmp_path)
    assert kept == ["pandas"] and dropped


# --------------------------------------------------------------------------- #
# conda
# --------------------------------------------------------------------------- #

def test_a_conda_bracket_spec_cannot_name_a_channel_or_url():
    clean, dropped = Z.sanitize_conda_env({"channels": ["conda-forge"], "dependencies": [
        "numpy[channel=evil-user]", "scipy[url=https://evil.example/x.conda]", "pandas"]})
    assert clean["dependencies"] == ["pandas"] and len(dropped) == 2


def test_the_environment_name_is_not_carried_over():
    clean, _ = Z.sanitize_conda_env({"name": "../../../opt/x", "channels": ["conda-forge"],
                                     "dependencies": ["numpy"]})
    assert "name" not in clean


def test_a_yaml_alias_bomb_costs_nothing_on_the_host(tmp_path):
    """A shared-node alias tree is cheap to LOAD and exponential to str(): a 350-byte
    file took minutes. Only plain strings are ever read now."""
    levels = ["a: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    for i in range(1, 12):
        levels.append(f"b{i}: &a{i} [" + ", ".join([f"*a{i - 1}"] * 10) + "]")
    doc = "\n".join(levels) + "\ndependencies: [*a11]\nchannels: [*a11]\n"
    spec = yaml.safe_load(doc)
    t0 = time.monotonic()
    clean, dropped = Z.sanitize_conda_env(spec)
    assert time.monotonic() - t0 < 2.0
    assert clean["dependencies"] == [] and clean["channels"] == ["conda-forge"]


def test_non_string_yaml_values_are_dropped_by_type_not_stringified():
    clean, dropped = Z.sanitize_conda_env({"channels": [None, 5, ["x"]], "dependencies": [
        None, 7, {"pip": [None, 3, "pandas"]}, {"other": 1}]})
    assert clean["dependencies"] == [{"pip": ["pandas"]}]
    assert all(isinstance(r, tuple) and r[1] for r in dropped)


# --------------------------------------------------------------------------- #
# renv.lock: two JSON parsers can read a duplicate key differently
# --------------------------------------------------------------------------- #

def test_a_duplicate_key_lockfile_is_refused_not_read_twice():
    text = ('{"Packages": {"p": {"Source": "GitHub", "RemoteUsername": "evil"}, '
            '"p": {"Source": "Repository", "Repository": "CRAN"}}}')
    excluded, notes = Z.classify_renv_lock(text)
    assert excluded is None, "a refused lockfile must be an explicit signal, not an empty exclusion list"
    assert "duplicate key" in notes[0][1] and "nothing was restored" in notes[0][1]


def test_a_package_from_a_repository_the_lockfile_defined_is_excluded():
    lock = json.dumps({"Packages": {
        "a": {"Source": "Repository", "Repository": "CRAN"},
        "b": {"Source": "Repository", "Repository": "evilrepo"},
        "c": {"Source": "Bioconductor"}, "d": {"Source": "Repository"}}})
    excluded, notes = Z.classify_renv_lock(lock)
    assert excluded == ["b"]
    assert "evilrepo" in notes[0][1]


def test_a_duplicate_key_lockfile_is_not_restored_by_the_plan(tmp_path):
    (tmp_path / "renv.lock").write_text('{"Packages": {"p": {}, "p": {}}}', encoding="utf-8")
    p = plan_env(DetectResult(artifact_types=["r"], steps=[]), {}, load_config(), tmp_path)
    assert not any("renv::restore" in c for c in p.install_commands)


# --------------------------------------------------------------------------- #
# The planner reads author-NAMED files on the host
# --------------------------------------------------------------------------- #

def test_a_manifest_dependencies_path_cannot_read_a_host_file_into_the_report(tmp_path):
    outside = tmp_path / "chair-notes.txt"
    outside.write_text("my private API token is hunter2\nsecond line\n", encoding="utf-8")
    art = tmp_path / "artifact"
    art.mkdir()
    for dep in ("../chair-notes.txt", str(outside)):
        p = plan_env(DetectResult(artifact_types=["python"], steps=[]),
                     {"environment": {"dependencies": dep}}, load_config(), art)
        joined = "\n".join(p.warnings + p.install_commands)
        assert "hunter2" not in joined and "second line" not in joined
        assert any("outside the artifact" in w for w in p.warnings)


def test_a_symlinked_requirements_file_is_not_read(tmp_path):
    import os
    secret = tmp_path / "secret.txt"
    secret.write_text("hunter2-not-a-requirement\n", encoding="utf-8")
    art = tmp_path / "artifact"
    art.mkdir()
    try:
        os.symlink(secret, art / "requirements.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not creatable on this platform/user")
    p = plan_env(DetectResult(artifact_types=["python"], steps=[]), {}, load_config(), art)
    assert "hunter2" not in "\n".join(p.warnings + p.install_commands)


# --------------------------------------------------------------------------- #
# Detected installs: nothing a package ships may run while the network is open
# --------------------------------------------------------------------------- #

def test_detected_packages_are_downloaded_first_and_installed_in_one_step(tmp_path):
    """A wheel's .pth file executes at the NEXT interpreter start in its environment.
    Installing package by package gave an earlier squatted wheel a free pip process
    to run in; one install leaves no later process."""
    p = _plan(tmp_path, {}, py_packages=["shap", "umap-learn"])
    cmd = next(c for c in p.install_commands if "reprobe: imports the environment" in c)
    code = cmd[len("python -I -c '"):-1]
    assert code.index('run("download"') < code.index('run("install"')
    assert code.count('run("install"') == 1 and "--no-index" in code and "--find-links" in code
    assert 'os.chdir("/var/tmp/reprobe-pip")' in code
    assert "'" not in code and compile(code, "<install>", "exec")
    # the old shape: a pip INSTALL inside a loop over the packages
    assert not re.search(r'for [^\]]*run\("install"', code)
    assert any("downloaded first and installed in one step" in w for w in p.warnings)


def test_conda_builds_and_listings_run_from_a_clean_directory_and_home(tmp_path):
    p = _plan(tmp_path, {"environment.yml": "channels: [conda-forge]\ndependencies: [numpy]\n"})
    for c in p.install_commands:
        if "micromamba" in c:
            assert "cd /var/tmp/reprobe-home" in c and "HOME=/var/tmp/reprobe-home micromamba" in c


# --------------------------------------------------------------------------- #
# Reserved harness paths
# --------------------------------------------------------------------------- #

def test_the_install_phase_does_not_use_the_artifacts_directory_for_config(tmp_path):
    import reprobe.orchestrator as om

    seen = {}

    def fake_run(spec, limits, log_path, **kw):
        from pathlib import Path as _P

        from reprobe.models import RawRunOutput
        seen["cmd"] = spec.command[-1]
        _P(log_path).parent.mkdir(parents=True, exist_ok=True)
        _P(log_path).write_text("", encoding="utf-8")
        return RawRunOutput(exit_code=0, duration_s=0.1, image=spec.image, log_path=str(log_path))

    from reprobe.models import EnvPlan, Report
    plan = EnvPlan(image="img", install_commands=["true"], install_origins=["author-declared"])
    run, logs = tmp_path / "run", tmp_path / "logs"
    run.mkdir()
    logs.mkdir()
    orig = om.run_container
    om.run_container = fake_run
    try:
        om.Orchestrator(workroot=tmp_path)._install_phase(
            plan, run, logs, install=True, dry_run=False,
            report=Report(submission_id="x", harness_version="t", timestamp=""))
    finally:
        om.run_container = orig
    prep = seen["cmd"]
    assert "export HOME=/var/tmp/reprobe-home" in prep and "export HOME=/work" not in prep
    assert "PYTHONPATH" not in prep
    assert "R_MAKEVARS_USER=/dev/null" in prep and "PIP_CONFIG_FILE=/dev/null" in prep


def test_a_dataset_cannot_be_written_into_a_reserved_harness_path(tmp_path, monkeypatch):
    import reprobe.fetch.base as fbase
    from reprobe.models import Report
    from reprobe.orchestrator import Orchestrator

    monkeypatch.setattr(fbase, "download", lambda url, dest, **kw: (True, "downloaded"))
    rundir = tmp_path / "run"
    rundir.mkdir()
    report = Report(submission_id="x", harness_version="t", timestamp="")
    report.environment = {}
    meta = {"data": [
        {"path": ".reprobe_deps/sitecustomize.py", "source": "https://93.184.216.34/x"},
        {"path": ".REPROBE_Deps/sitecustomize.py", "source": "https://93.184.216.34/x"},
        {"path": "data/ok.csv", "source": "https://93.184.216.34/ok"}]}
    Orchestrator(workroot=tmp_path)._dataset_phase(meta, rundir, report, dry_run=False)
    status = {d["path"]: d["status"] for d in report.environment["datasets"]}
    assert status[".reprobe_deps/sitecustomize.py"] == "refused"
    assert status[".REPROBE_Deps/sitecustomize.py"] == "refused"
    assert status["data/ok.csv"] == "ok"


def test_the_reserved_path_filter_is_case_insensitive(tmp_path):
    """On a case-folding host (Windows, macOS) `.REPROBE_DEPS` IS `.reprobe_deps`."""
    from reprobe.models import Report
    from reprobe.orchestrator import Orchestrator

    src = tmp_path / "src"
    (src / ".REPROBE_DEPS").mkdir(parents=True)
    (src / ".REPROBE_DEPS" / "sitecustomize.py").write_text("print('x')\n", encoding="utf-8")
    (src / ".Reprobe_env").mkdir()
    (src / "run.py").write_text("print(1)\n", encoding="utf-8")
    run = tmp_path / "run"
    Orchestrator(workroot=tmp_path)._copy_run_tree(
        src, run, Report(submission_id="x", harness_version="t", timestamp=""))
    assert (run / "run.py").is_file()
    assert not any(c.name.casefold().startswith(".reprobe_") for c in run.iterdir())


# --------------------------------------------------------------------------- #
# Quadratic and cubic scans of author-controlled text (host-side)
# --------------------------------------------------------------------------- #

def _timed(fn, limit=2.0):
    t0 = time.monotonic()
    out = fn()
    elapsed = time.monotonic() - t0
    assert elapsed < limit, f"took {elapsed:.1f}s"
    return out


def test_unclosed_rmarkdown_fences_are_linear(tmp_path):
    """7 KB of unclosed ```{r} fences took twelve seconds (a cubic regex)."""
    from reprobe.detect import signatures as S
    (tmp_path / "a.Rmd").write_text("```{r}\n" * 1000, encoding="utf-8")
    _timed(lambda: S.scan_r_packages([tmp_path / "a.Rmd"]))


@pytest.mark.parametrize("text", [
    "library(" * 8000,                                   # 64 KB: 22 s
    "library(" * 100_000 + ")" * 100_000,                # deeply nested
    "install.packages(" * 20_000,
    "pacman::p_load(" * 20_000,
    '"' * 50_001,
], ids=["unclosed-library-calls", "deeply-nested-library", "unclosed-install-packages",
        "unclosed-p_load", "stray-quotes"])
def test_hostile_r_source_is_scanned_in_linear_time(tmp_path, text):
    from reprobe.detect import signatures as S
    (tmp_path / "a.R").write_text(text, encoding="utf-8")
    _timed(lambda: S.scan_r_packages([tmp_path / "a.R"]))


def test_ordinary_r_is_still_read_after_the_scan_was_bounded(tmp_path):
    from reprobe.detect import signatures as S
    (tmp_path / "a.R").write_text(
        "library(dplyr)\npacman::p_load(a1, b2)\ninstall.packages(c('lme4', 'brms'))\n"
        "# library(skipped)\n", encoding="utf-8")
    assert S.scan_r_packages([tmp_path / "a.R"]) == ["a1", "b2", "brms", "dplyr", "lme4", "pacman"]


def test_a_huge_readme_line_of_brackets_costs_nothing(tmp_path):
    """A 200 KB `http://x)))))…` README took half a minute in _clean_url."""
    from reprobe.fetch.data_source import referenced_deposits
    (tmp_path / "README.md").write_text("see http://x" + ")" * 200_000 + "\n"
                                        "data: https://osf.io/cwd6h/\n", encoding="utf-8")
    found = _timed(lambda: referenced_deposits(tmp_path))
    assert found == ["https://osf.io/cwd6h/"]


# --------------------------------------------------------------------------- #
# paper.py
# --------------------------------------------------------------------------- #

def test_a_cff_alias_bomb_costs_nothing(tmp_path):
    from reprobe import paper as P
    levels = ["a: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    for i in range(1, 12):
        levels.append(f"b{i}: &a{i} [" + ", ".join([f"*a{i - 1}"] * 10) + "]")
    (tmp_path / "CITATION.cff").write_text(
        "\n".join(levels) + "\ndoi: *a11\nidentifiers:\n  - {type: doi, value: *a11}\n", encoding="utf-8")
    assert _timed(lambda: P.find_doi(tmp_path)) is None


def test_the_pdf_childs_stderr_is_read_from_a_bounded_tail(tmp_path, monkeypatch):
    from reprobe import paper as P
    f = tmp_path / "paper.pdf"
    f.write_bytes(b"%PDF-1.4 not really")
    seen = {}
    real_run = P.subprocess.run

    def spy(argv, **kw):
        seen["stderr_is_pipe"] = kw.get("stderr") == P.subprocess.PIPE
        seen["capture"] = kw.get("capture_output")
        return real_run(argv, **kw)
    monkeypatch.setattr(P.subprocess, "run", spy)
    P.pdf_text(f)
    assert not seen["stderr_is_pipe"] and not seen["capture"]


# --------------------------------------------------------------------------- #
# The install log is third-party build output: bounded memory, whatever it holds
# --------------------------------------------------------------------------- #

def test_install_log_parsers_read_in_constant_memory(tmp_path):
    """A hostile build can print a gigabyte, or one gigabyte-long line. The parsers
    used to read_text() the whole file up to four times."""
    import tracemalloc

    import reprobe.orchestrator as om

    log = tmp_path / "install-python.log"
    with log.open("w", encoding="utf-8") as fh:
        fh.write("[reprobe-install] begin 0 author-declared\n")
        fh.write("x" * 30_000_000)                          # one 30 MB line, no newline
        fh.write("\n[reprobe-install] failed 0 author-declared exit 1\n")
        fh.write("[reprobe-install] begin 1 harness-record\npandas==2.2.0\n")
        fh.write("Temporary failure in name resolution\n")
    tracemalloc.start()
    failures = om._install_failures(log, [(0, "pip install -r x", "author-declared")])
    digest = om._hash_resolved_versions(tmp_path)
    sections = om._install_sections(log)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert [f["index"] for f in failures] == [0] and digest and 0 in sections
    assert peak < 5_000_000, f"peak {peak} bytes for a 30 MB log"


# --------------------------------------------------------------------------- #
# Network trouble belongs to the command whose own output shows it
# --------------------------------------------------------------------------- #

def _sections(tmp_path, text):
    import reprobe.orchestrator as om
    log = tmp_path / "install-python.log"
    log.write_text(text, encoding="utf-8")
    return om._install_sections(log), om._install_failures(log, [(0, "a", "author-declared"),
                                                                 (1, "b", "harness-detected")])


def test_a_retry_warning_from_one_command_does_not_excuse_anothers_failure(tmp_path):
    """A pip retry warning anywhere in the log tail used to mark the whole group as
    network trouble, which excused a genuinely broken requirements.txt."""
    from reprobe.report import badges
    _, failures = _sections(tmp_path, (
        "\n[reprobe-install] begin 0 author-declared\n"
        "ERROR: Could not find a version that satisfies the requirement nosuchpkg==9.9\n"
        "\n[reprobe-install] failed 0 author-declared exit 1\n"
        "\n[reprobe-install] begin 1 harness-detected\n"
        "WARNING: Retrying ... Temporary failure in name resolution\n"))
    assert failures[0]["network"] is False
    env = {"install_results": {"python": {"ok": False, "exit_code": 1, "timed_out": False,
                                          "failures": failures}}}
    assert "nosuchpkg" not in (badges.install_error(env) or "")
    assert badges.declared_env_error(env)                 # the author's finding stands


def test_a_declared_install_that_died_on_the_network_is_not_the_authors(tmp_path):
    from reprobe.report import badges
    _, failures = _sections(tmp_path, (
        "\n[reprobe-install] begin 0 author-declared\n"
        "WARNING: Retrying (Retry(total=0)) ... Max retries exceeded with url: /simple/pandas/\n"
        "ERROR: Could not install packages due to a ConnectionError\n"
        "\n[reprobe-install] failed 0 author-declared exit 1\n"))
    assert failures[0]["network"] is True
    env = {"install_results": {"python": {"ok": False, "exit_code": 1, "timed_out": False,
                                          "failures": failures, "network_error": True}}}
    assert badges.declared_env_error(env) is None
    assert "unreachable" in badges.install_error(env)


def test_the_network_cause_is_read_from_the_end_of_the_failing_section(tmp_path):
    lines = "".join(f"line {i}\n" for i in range(500))
    _, failures = _sections(tmp_path, (
        "\n[reprobe-install] begin 0 author-declared\nTemporary failure in name resolution\n"
        + lines + "ERROR: invalid requirement\n\n[reprobe-install] failed 0 author-declared exit 1\n"))
    assert failures[0]["network"] is False                # long past, not the cause of the failure


# --------------------------------------------------------------------------- #
# git clone has a disk budget
# --------------------------------------------------------------------------- #

def test_a_clone_that_grows_past_its_budget_is_stopped(tmp_path, monkeypatch):
    import subprocess as sp

    import reprobe.fetch.base as fb

    dest = tmp_path / "repo"
    dest.mkdir()
    (dest / "pack").write_bytes(b"0" * 4096)
    killed = []

    class Proc:
        def wait(self, timeout=None):
            if killed:
                return -9
            raise sp.TimeoutExpired("git", timeout)

        def kill(self):
            killed.append(True)

    monkeypatch.setattr(fb.subprocess, "Popen", lambda *a, **k: Proc())
    monkeypatch.setattr(fb, "_GIT_WATCH_EVERY_S", 0.0)
    with pytest.raises(fb.FetchError, match="passed"):
        fb.run_git(["clone", "--", "https://example.org/x.git", str(dest)], watch=dest, max_bytes=1024)
    assert killed


def test_a_clone_within_budget_returns_git_s_own_result(tmp_path):
    import shutil as sh

    import reprobe.fetch.base as fb
    if sh.which("git") is None:
        pytest.skip("needs git")
    r = fb.run_git(["--version"], watch=tmp_path, max_bytes=10**9)
    assert r.returncode == 0 and "git version" in r.stdout


def test_clones_are_run_with_the_budget(monkeypatch, tmp_path):
    from reprobe.fetch import git_host
    seen = {}

    def fake(args, cwd=None, timeout=600, **kw):
        if args and "clone" in args:
            seen.update(kw)
            import subprocess as sp
            return sp.CompletedProcess(args, 1, "", "fatal: nope")
        import subprocess as sp
        return sp.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(git_host, "run_git", fake)
    monkeypatch.setattr(git_host, "_public_probe", lambda url: "probe")
    from reprobe.fetch.base import FetchError
    with pytest.raises(FetchError):
        git_host.GitHostFetcher().fetch("https://github.com/a/b", tmp_path / "d")
    assert seen.get("max_bytes") and seen.get("watch") == tmp_path / "d"
