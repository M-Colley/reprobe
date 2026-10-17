"""Regressions for a review of whether reports say only true things.

Each case is a scenario in which a report once blamed the wrong party, vouched for
more than was checked, or described a limit other than the one enforced. The
surviving-mutant cases pin behaviour that had no test at all (a mutation harness
changed it and the suite stayed green)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import reprobe.orchestrator as om
from reprobe.config import load_config
from reprobe.models import DetectResult, EnvPlan, RawRunOutput, Report, RunResult
from reprobe.report import badges

# --------------------------------------------------------------------------- #
# The install phase: a container that dies must never be the artifact's fault
# --------------------------------------------------------------------------- #

PLAN_CMDS = ["python -I -m pip install -r /work/.reprobe_req/requirements.txt",
             "python -I -c 'reprobe: imports the environment does not provide'",
             "python -I -m pip freeze --path=/work/.reprobe_deps"]
PLAN_ORIGINS = ["author-declared", "harness-detected", "harness-record"]


def _install(tmp_path, monkeypatch, log_text, **raw_kw):
    def fake(spec, limits, log_path, **kw):
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(log_text, encoding="utf-8")
        raw_kw.setdefault("exit_code", 0)
        return RawRunOutput(duration_s=1.0, image=spec.image, log_path=str(log_path), **raw_kw)

    monkeypatch.setattr(om, "run_container", fake)
    run, logs = tmp_path / "run", tmp_path / "logs"
    run.mkdir(exist_ok=True)
    logs.mkdir(exist_ok=True)
    report = Report(submission_id="x", harness_version="t", timestamp="")
    plan = EnvPlan(image="img", install_commands=PLAN_CMDS, install_origins=PLAN_ORIGINS)
    om.Orchestrator(workroot=tmp_path)._install_phase(
        plan, run, logs, install=True, dry_run=False, report=report)
    return report


FAILED_0 = ("\n[reprobe-install] begin 0 author-declared\nERROR: No matching distribution\n"
            "\n[reprobe-install] failed 0 author-declared exit 1\n")


@pytest.mark.parametrize("raw_kw, expect", [
    ({"exit_code": 1}, "no failing command recorded"),                       # shell died, no marker
    ({"exit_code": 125}, "no failing command recorded"),                     # docker said so, no marker
    ({"exit_code": None, "limit_exceeded": "output"}, "stopped by the output cap"),
    ({"exit_code": 137, "oom_killed": True}, "killed at the memory cap"),
    ({"exit_code": None, "timed_out": True}, "wall-clock limit"),
])
def test_a_container_that_dies_without_a_failed_marker_is_ours(tmp_path, monkeypatch, raw_kw, expect):
    """Row `{ok: False, failures: []}` made install_error() return None and the step
    that then died on ModuleNotFoundError was reported runs-with-failures — while the
    same report said "their failures are ours and not the artifact's"."""
    report = _install(tmp_path, monkeypatch, "installing...\n", **raw_kw)
    env = report.environment
    row = env["install_results"]["python"]
    assert row["ok"] is False
    err = badges.install_error(env)
    assert err and expect in err
    assert badges.declared_env_error(env) is None
    steps = [RunResult(runner="python", target="main.py", status="fail")]
    v = badges.verdict(steps, True, err, None)
    assert v["overall"] == "infra-error" and "no statement about the artifact" in v["note"]


def test_a_failure_marked_after_the_container_was_stopped_is_still_not_the_authors(tmp_path, monkeypatch):
    # the author's file failed AND the container was then killed: the death is not theirs
    report = _install(tmp_path, monkeypatch, FAILED_0, exit_code=None, limit_exceeded="disk")
    assert badges.declared_env_error(report.environment) is None
    assert "disk cap" in badges.install_error(report.environment)


def test_a_failing_version_listing_alone_is_not_an_install_failure(tmp_path, monkeypatch):
    log = ("\n[reprobe-install] begin 2 harness-record\npip: error\n"
           "\n[reprobe-install] failed 2 harness-record exit 3\n")
    report = _install(tmp_path, monkeypatch, log, exit_code=3)
    assert report.environment["install_results"]["python"]["ok"] is True
    assert badges.install_error(report.environment) is None


def test_the_authors_failure_and_ours_are_both_reported(tmp_path, monkeypatch):
    log = (FAILED_0 + "\n[reprobe-install] begin 1 harness-detected\nboom\n"
           "\n[reprobe-install] failed 1 harness-detected exit 2\n")
    report = _install(tmp_path, monkeypatch, log, exit_code=1)
    env = report.environment
    assert badges.declared_env_error(env) and badges.install_error(env)
    steps = [RunResult(runner="python", target="main.py", status="fail")]
    v = badges.verdict(steps, True, badges.install_error(env), badges.declared_env_error(env))
    assert v["overall"] == "runs-with-failures"          # the author's finding stands
    assert "does not install" in v["note"] and "in addition" in v["note"]


def test_the_declared_environment_message_does_not_assert_causation(tmp_path, monkeypatch):
    report = _install(tmp_path, monkeypatch, FAILED_0, exit_code=1)
    msg = badges.declared_env_error(report.environment)
    assert "may follow from that" in msg and "follow from the declaration" not in msg


DETECTED_MISSING = ("\n[reprobe-install] begin 1 harness-detected\n"
                    "reprobe: pip could NOT install (reported, not faked): ['shap']\n")


def test_an_unreachable_index_during_the_import_install_is_ours(tmp_path, monkeypatch):
    """That command exits 0 by design (a mis-detected local module must not fail the
    phase); nothing parsed its report, so a network failure left ok=True and the
    step's ModuleNotFoundError became the artifact's verdict."""
    log = DETECTED_MISSING + "WARNING: Retrying ... Temporary failure in name resolution\n"
    report = _install(tmp_path, monkeypatch, log, exit_code=0)
    row = report.environment["install_results"]["python"]
    assert row["ok"] is False and row["network_error"] is True and row["incomplete"] == ["shap"]
    assert "unreachable" in badges.install_error(report.environment)


def test_pypi_simply_not_having_the_package_is_not_a_harness_failure(tmp_path, monkeypatch):
    report = _install(tmp_path, monkeypatch, DETECTED_MISSING + "ERROR: No matching distribution\n",
                      exit_code=0)
    row = report.environment["install_results"]["python"]
    assert row["ok"] is True and row["incomplete"] == ["shap"] and not row.get("network_error")


def test_a_failed_version_listing_is_not_hashed_as_resolved_versions(tmp_path):
    good = tmp_path / "good"
    bad = tmp_path / "bad"
    for d in (good, bad):
        d.mkdir()
    (good / "install-python.log").write_text(
        "\n[reprobe-install] begin 2 harness-record\npandas==2.2.0\n", encoding="utf-8")
    (bad / "install-python.log").write_text(
        "\n[reprobe-install] begin 2 harness-record\nERROR: pip crashed\n"
        "\n[reprobe-install] failed 2 harness-record exit 1\n", encoding="utf-8")
    assert om._hash_resolved_versions(good) is not None
    assert om._hash_resolved_versions(bad) is None


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_a_marker_survives_a_command_whose_last_output_has_no_newline(tmp_path):
    items = [(0, "printf 'no trailing newline'; exit 4", "author-declared"),
             (1, "printf 'also none'", "harness-record")]
    r = subprocess.run(["bash", "-c", "set -e; " + om._attributed_script(items)],
                       capture_output=True, text=True)
    log = tmp_path / "install-python.log"
    log.write_text(r.stdout, encoding="utf-8")
    assert [f["index"] for f in om._install_failures(log, items)] == [0]


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_each_install_command_stops_at_its_first_failure(tmp_path):
    """`set -e` inside the subshell: without it `false; echo after` would carry on and
    a failing install could be followed by a command that "succeeds"."""
    items = [(0, "echo first; false; echo SHOULD-NOT-RUN", "author-declared")]
    r = subprocess.run(["bash", "-c", "set -e; " + om._attributed_script(items)],
                       capture_output=True, text=True)
    assert "first" in r.stdout and "SHOULD-NOT-RUN" not in r.stdout and r.returncode == 1


# --------------------------------------------------------------------------- #
# decide() and verdict() must tell one story
# --------------------------------------------------------------------------- #

def _decide(steps, **kw):
    from reprobe.models import FetchResult, Pin
    fetch = FetchResult(input="x", resolved_type="git", src_dir="/x", pin=Pin(kind="none"))
    return badges.decide(fetch, steps, DetectResult(artifact_types=["python"]),
                         badges_cfg=load_config().badges, functional_requested=True, ran=True, **kw)


DECLARED = "the artifact's declared environment does not install (python: x exited 1) — the steps ran without it"


def test_an_infrastructure_error_outranks_a_declared_environment_failure_in_the_badge_too():
    steps = [RunResult(runner="python", target="m.py", status="error",
                       diagnostics={"harness_error": "docker-daemon-lost: x", "infra": True})]
    out = _decide(steps, declared_env_error=DECLARED)
    assert out["acm"]["functional"] == "not-evaluated"
    assert badges.verdict(steps, True, None, DECLARED)["overall"] == "infra-error"


def test_steps_that_passed_only_on_the_base_image_are_not_a_clean_run():
    steps = [RunResult(runner="python", target="m.py", status="pass")]
    v = badges.verdict(steps, True, None, DECLARED)
    assert v["overall"] == "runs-with-warnings" and v["human_review_required"]
    assert "base image alone" in v["note"]
    notes = _decide(steps, declared_env_error=DECLARED)["acm"]["notes"]
    assert any("passed anyway" in n for n in notes)


def test_the_dry_run_note_does_not_claim_containers_were_planned():
    v = badges.verdict([], True, dry_run=True)
    assert "planned" not in v["note"] and "no container was launched" in v["note"]


# --------------------------------------------------------------------------- #
# Regenerated outputs and collected artifacts
# --------------------------------------------------------------------------- #

def _orch(tmp_path):
    return om.Orchestrator(workroot=tmp_path)


def _trees(tmp_path):
    src, run = tmp_path / "src", tmp_path / "run"
    for d in (src, run):
        d.mkdir()
    (src / "out.csv").write_text("a\n1\n", encoding="utf-8")
    (run / "out.csv").write_text("a\n9\n", encoding="utf-8")
    return src, run


@pytest.mark.parametrize("status", ["fail", "timeout", "error", "resource-limit"])
def test_a_step_that_did_not_finish_regenerated_nothing(tmp_path, status):
    """A crashed step that truncated out.csv reported "regenerated with different
    values"."""
    src, run = _trees(tmp_path)
    report = Report(submission_id="x", harness_version="t", timestamp="")
    _orch(tmp_path)._regenerated_outputs(
        report, [RunResult(runner="python", target="m.py", status=status, artifacts=["out.csv"])], src, run)
    assert report.results_comparison is None


def test_a_step_that_finished_is_compared(tmp_path):
    src, run = _trees(tmp_path)
    report = Report(submission_id="x", harness_version="t", timestamp="")
    _orch(tmp_path)._regenerated_outputs(
        report, [RunResult(runner="python", target="m.py", status="pass", artifacts=["out.csv"])], src, run)
    assert [f["status"] for f in report.results_comparison["files"]] == ["differs"]


def test_a_file_that_is_not_a_committed_one_is_not_compared(tmp_path):
    src, run = _trees(tmp_path)
    (run / "new.csv").write_text("a\n1\n", encoding="utf-8")         # produced, never committed
    report = Report(submission_id="x", harness_version="t", timestamp="")
    _orch(tmp_path)._regenerated_outputs(
        report, [RunResult(runner="python", target="m.py", status="pass", artifacts=["new.csv"])], src, run)
    assert report.results_comparison is None


def test_declared_directories_and_globs_are_collected_and_read(tmp_path):
    run, out = tmp_path / "run", tmp_path / "out"
    (run / "results" / "sub").mkdir(parents=True)
    (run / "figures").mkdir()
    (run / "results" / "a.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    (run / "results" / "sub" / "b.csv").write_text("x,y\n3,4\n", encoding="utf-8")
    (run / "figures" / "f1.png").write_bytes(b"png")
    arts = ["results/a.csv", "results/sub/b.csv", "figures/f1.png", "elsewhere.txt"]
    res = RunResult(runner="python", target="m.py", status="pass", artifacts=arts,
                    expected_met=["results/", "figures/*.png"])
    _orch(tmp_path)._collect_artifacts([res], run, out)
    got = sorted(p.relative_to(out / "artifacts").as_posix() for p in (out / "artifacts").rglob("*")
                 if p.is_file())
    assert got == ["figures/f1.png", "results/a.csv", "results/sub/b.csv"]
    text = _orch(tmp_path)._produced_text([res], run)
    assert "produced file results/a.csv" in text and "produced file results/sub/b.csv" in text


def test_collection_never_follows_a_link_the_run_planted(tmp_path):
    import os
    secret = tmp_path / "host-secret.txt"
    secret.write_text("PRIVATE", encoding="utf-8")
    run, out = tmp_path / "run", tmp_path / "out"
    run.mkdir()
    try:
        os.symlink(secret, run / "x.executed.ipynb")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not creatable on this platform/user")
    res = RunResult(runner="jupyter", target="x.ipynb", status="pass", artifacts=["x.executed.ipynb"])
    _orch(tmp_path)._collect_artifacts([res], run, out)
    assert not list((out / "artifacts").rglob("*")) or not any(
        "PRIVATE" in p.read_text(encoding="utf-8", errors="replace")
        for p in (out / "artifacts").rglob("*") if p.is_file())


def test_a_literal_with_brackets_is_a_file_name_not_a_character_class():
    from reprobe.runners.base import match_expected
    assert match_expected(["data[1].csv"], ["data[1].csv"]) == ["data[1].csv"]
    # only when no file of that exact name exists is the bracket read as a character class
    assert match_expected(["data[12].csv"], ["data1.csv"]) == ["data[12].csv"]


def test_the_timeout_reported_is_the_one_enforced(tmp_path):
    from reprobe.runners.base import RunContext, snapshot
    from reprobe.runners.python_script import PythonScriptRunner
    cfg = load_config()
    limits = {**cfg.limits_for("python"), "timeout_s": 99_999, "max_timeout_s": 21_600}
    from reprobe.models import RunStep
    ctx = RunContext(step=RunStep(target="a.py", kind="python"), rundir=tmp_path, src_dir=tmp_path,
                     out_dir=tmp_path, image="img", config=cfg, limits=limits, pre_index=snapshot(tmp_path))
    res = PythonScriptRunner().interpret(RawRunOutput(exit_code=None, duration_s=3.0, timed_out=True), ctx)
    assert res.diagnostics["timeout_s"] == 21_600
    assert "21600s time budget" in badges.verdict([res], True)["note"]


def test_an_oom_flag_on_a_run_that_exited_zero_is_not_a_failure(tmp_path):
    """A child process was OOM-killed and the program carried on to a clean exit."""
    from reprobe.models import RunStep
    from reprobe.runners.base import RunContext, snapshot
    from reprobe.runners.python_script import PythonScriptRunner
    cfg = load_config()
    ctx = RunContext(step=RunStep(target="a.py", kind="python"), rundir=tmp_path, src_dir=tmp_path,
                     out_dir=tmp_path, image="img", config=cfg, limits=cfg.limits_for("python"),
                     pre_index=snapshot(tmp_path))
    res = PythonScriptRunner().interpret(RawRunOutput(exit_code=0, duration_s=1.0, oom_killed=True), ctx)
    assert res.status == "pass"


# --------------------------------------------------------------------------- #
# The threshold, SIGTERM, and the resume decisions
# --------------------------------------------------------------------------- #

def test_below_threshold_advice_never_reaches_the_authors_to_do_list():
    report = Report(submission_id="x", harness_version="t", timestamp="")
    res = RunResult(runner="python", target="a.py", status="fail", diagnostics={"llm_advisory": {
        "likely_cause": "a guess", "suggested_fixes": ["delete everything"],
        "meets_threshold": False, "threshold_note": "below 0.6"}})
    assert om._fix_list(report, [res]) == []
    res.diagnostics["llm_advisory"]["meets_threshold"] = True
    assert [i["fixes"] for i in om._fix_list(report, [res])] == [["delete everything"]]


def test_sigterm_unwinds_like_ctrl_c_so_a_container_is_stopped():
    from reprobe import cli
    with pytest.raises(SystemExit) as e:
        cli._exit_on_sigterm(15, None)
    assert e.value.code == 143


def test_nan_in_json_and_text_is_equal_to_nan_not_to_a_number(tmp_path):
    from reprobe.compare import compare_outputs
    src, run = tmp_path / "s", tmp_path / "r"
    src.mkdir()
    run.mkdir()
    for name, a, b in (("n.json", '{"x": NaN}', '{"x": NaN}'), ("m.json", '{"x": NaN}', '{"x": 1.0}')):
        (src / name).write_text(a, encoding="utf-8")
        (run / name).write_text(b, encoding="utf-8")
    res = {r["path"]: r["status"] for r in compare_outputs(src, run, ["n.json", "m.json"])}
    assert res["n.json"] in ("identical", "equivalent") and res["m.json"] == "differs"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_real_install_commands_survive_both_shell_layers_byte_for_byte(tmp_path):
    """Every install command runs as `bash -ec <quoted>` inside the container's own
    `bash -c`. The real commands carry single quotes, double quotes, `$`, brackets
    and base64; if either layer re-interpreted one character, the installer would
    run something else. Each command is printed back by the inner shell instead of
    executed, and must come back unchanged."""
    import shlex

    from reprobe.envbuild import sanitize
    from reprobe.envbuild.base import _cran_install_command, _py_detected_install_command
    real = [
        _py_detected_install_command(["shap", "ray[tune]", "umap-learn"], None),
        _py_detected_install_command(["shap"], "/work/.reprobe_env"),
        _cran_install_command(["lme4", "data.table"], "https://packagemanager.posit.co/cran/x"),
        sanitize.embed_file_command("pandas>=2\n$(touch pwned)\n'q'\n", "/work/.reprobe_req/r.txt"),
    ]
    for cmd in real:
        items = [(0, "printf '%s' " + shlex.quote(cmd), "author-declared")]
        r = subprocess.run(["bash", "-c", "set -e; " + om._attributed_script(items)],
                           capture_output=True, text=True, cwd=tmp_path)
        assert r.returncode == 0, r.stderr
        printed = r.stdout.split("[reprobe-install] begin 0 author-declared\n", 1)[1]
        assert printed == cmd
    assert not (tmp_path / "pwned").exists()


def test_the_generated_import_install_code_runs_and_does_what_it_says(monkeypatch, capsys):
    """Execute the very code the plan sends to the container, with its process calls
    intercepted: present modules are skipped, each missing one is DOWNLOADED (no
    install), ONE --no-index install follows, and what pip could not get is
    reported. A logic error here would otherwise first show up inside a container."""
    import os
    import sys

    from reprobe.envbuild.base import _py_detected_install_command
    cmd = _py_detected_install_command(["json-present-as-stdlib", "reprobe-absent-ok", "reprobe-absent-bad"], None)
    code = cmd[len("python -I -c '"):-1]
    # "json-present-as-stdlib" maps to module json_present_as_stdlib (absent) — use a
    # real present module instead by rewriting its pair to ("json", ...)
    code = code.replace('("json_present_as_stdlib", "json-present-as-stdlib")',
                        '("json", "json-present-as-stdlib")')
    calls: list[list[str]] = []

    def fake_call(argv, **kw):
        calls.append(list(argv))
        args = argv[argv.index("pip") + 1:]
        if args[0] == "download":
            return 0 if args[-1] == "reprobe-absent-ok" else 1
        return 0

    import subprocess as real_subprocess
    monkeypatch.setattr(real_subprocess, "call", fake_call)
    monkeypatch.setattr(os, "chdir", lambda p: None)
    monkeypatch.setattr(os, "makedirs", lambda p, exist_ok=False: None)
    saved_path = list(sys.path)
    try:
        exec(compile(code, "<install>", "exec"), {})
    finally:
        sys.path[:] = saved_path
    out = capsys.readouterr().out
    pip_args = [c[c.index("pip") + 1:] for c in calls]
    downloads = [a[-1] for a in pip_args if a[0] == "download"]
    installs = [a for a in pip_args if a[0] == "install"]
    assert downloads == ["reprobe-absent-bad", "reprobe-absent-ok"]   # json is present: not asked for
    assert len(installs) == 1 and "--no-index" in installs[0] and installs[0][-1] == "reprobe-absent-ok"
    assert "--target=/work/.reprobe_deps" in installs[0]
    assert "pip could NOT install (reported, not faked): ['reprobe-absent-bad']" in out


def test_an_activated_renv_project_is_restored_into_its_own_library(tmp_path):
    """Without activation renv restores into the first library on the path — the
    harness's — and a project whose activate.R then drops that library cannot
    see a single restored package; activate.R also loads renv itself from the
    project library, and offline it cannot download it. Verified against renv."""
    from reprobe.envbuild.base import _renv_activation, _renv_restore_command
    assert _renv_activation(tmp_path) is None
    (tmp_path / ".Rprofile").write_text('source("renv/activate.R")\n', encoding="utf-8")
    assert _renv_activation(tmp_path) == ""
    (tmp_path / "renv").mkdir()
    (tmp_path / "renv" / "activate.R").write_text(
        'local({\n\n  # the requested version of renv\n  version <- "1.2.3"\n', encoding="utf-8")
    assert _renv_activation(tmp_path) == "1.2.3"
    cmd = _renv_restore_command("r", [], "1.2.3")
    assert "library=lib" in cmd and 'v <- "1.2.3"' in cmd and 'renv::install(paste0("renv@", v)' in cmd
    assert "library=" not in _renv_restore_command("r", [], None)
    for c in (cmd, _renv_restore_command("r", ["x"], ""), _renv_restore_command("r", [], None)):
        assert "'" not in c.split("-e '", 1)[1][:-1]


def test_an_activate_version_that_is_not_a_plain_version_is_not_used(tmp_path):
    from reprobe.envbuild.base import _renv_activation
    (tmp_path / ".Rprofile").write_text('source("renv/activate.R")\n', encoding="utf-8")
    (tmp_path / "renv").mkdir()
    for bad in ('version <- "1.0; system(x)"\n', 'version <- "1.0\\" + 1"\n', 'version <- "dev"\n'):
        (tmp_path / "renv" / "activate.R").write_text(bad, encoding="utf-8")
        assert _renv_activation(tmp_path) == "", bad
