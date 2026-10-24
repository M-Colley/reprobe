"""Regressions for a review of whether reports say only true things.

Each case is a scenario in which a report once blamed the wrong party, vouched for
more than was checked, or described a limit other than the one enforced. The
surviving-mutant cases pin behaviour that had no test at all (a mutation harness
changed it and the suite stayed green)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

import reprobe.orchestrator as om
from reprobe.config import load_config
from reprobe.models import DetectResult, EnvPlan, RawRunOutput, Report, RunResult, RunStep
from reprobe.report import badges

# --------------------------------------------------------------------------- #
# The install phase: a container that dies must never be the artifact's fault
# --------------------------------------------------------------------------- #

PLAN_CMDS = ["python -I -m pip install -r /work/.reprobe_req/requirements.txt",
             "python -I -c 'reprobe: imports the environment does not provide'",
             "python -I -m pip freeze --path=/work/.reprobe_deps"]
PLAN_ORIGINS = ["author-declared", "harness-detected", "harness-record"]


def _install(tmp_path, monkeypatch, *per_cmd):
    """Run the install phase with each command's container replaced by a canned
    ``(output, raw_kw)`` — one per command, in order; missing ones exit 0."""
    calls = []

    def fake(spec, limits, log_path, **kw):
        i = len(calls)
        calls.append(spec)
        text, raw_kw = per_cmd[i] if i < len(per_cmd) else ("", {})
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(text, encoding="utf-8")
        raw_kw = {"exit_code": 0, **raw_kw}
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


NO_DIST = ("ERROR: No matching distribution\n", {"exit_code": 1})


@pytest.mark.parametrize("raw_kw, output, expect", [
    ({"exit_code": 125}, "docker: Error response from daemon: oci error\n", "could not start"),
    ({"exit_code": 127}, "bash: line 1: python: command not found\n", "could not start"),
    ({"exit_code": None, "limit_exceeded": "output"}, "", "stopped by the output cap"),
    ({"exit_code": 137, "oom_killed": True}, "", "killed at the memory cap"),
    ({"exit_code": None, "timed_out": True}, "", "wall-clock limit"),
])
def test_a_container_stopped_or_unable_to_start_is_ours(tmp_path, monkeypatch, raw_kw, output, expect):
    """Not the author's command failing: docker or the image could not run it,
    or the container was stopped. Otherwise the step that then died on
    ModuleNotFoundError was reported as the artifact's failure."""
    report = _install(tmp_path, monkeypatch, (output, raw_kw))
    env = report.environment
    row = env["install_results"]["python"]
    assert row["ok"] is False
    err = badges.install_error(env)
    assert err and expect in err
    assert badges.declared_env_error(env) is None
    steps = [RunResult(runner="python", target="main.py", status="fail")]
    v = badges.verdict(steps, True, err, None)
    assert v["overall"] == "infra-error" and "no statement about the artifact" in v["note"]


def test_a_container_stop_after_the_authors_failure_is_still_not_the_authors(tmp_path, monkeypatch):
    # the author's file failed AND the next container was killed: the death is not theirs
    report = _install(tmp_path, monkeypatch, NO_DIST, ("", {"exit_code": None, "limit_exceeded": "disk"}))
    assert badges.declared_env_error(report.environment) is None
    assert "disk cap" in badges.install_error(report.environment)


def test_a_failing_version_listing_alone_is_not_an_install_failure(tmp_path, monkeypatch):
    report = _install(tmp_path, monkeypatch, ("", {}), ("", {}), ("pip: error\n", {"exit_code": 3}))
    assert report.environment["install_results"]["python"]["ok"] is True
    assert badges.install_error(report.environment) is None


def test_the_authors_failure_and_ours_are_both_reported(tmp_path, monkeypatch):
    report = _install(tmp_path, monkeypatch, NO_DIST, ("boom\n", {"exit_code": 2}))
    env = report.environment
    assert badges.declared_env_error(env) and badges.install_error(env)
    steps = [RunResult(runner="python", target="main.py", status="fail")]
    v = badges.verdict(steps, True, badges.install_error(env), badges.declared_env_error(env))
    assert v["overall"] == "runs-with-failures"          # the author's finding stands
    assert "does not install" in v["note"] and "in addition" in v["note"]


def test_the_declared_environment_message_does_not_assert_causation(tmp_path, monkeypatch):
    report = _install(tmp_path, monkeypatch, NO_DIST)
    msg = badges.declared_env_error(report.environment)
    assert "may follow from that" in msg and "follow from the declaration" not in msg


def test_a_forged_marker_in_build_output_attributes_nothing(tmp_path, monkeypatch):
    """Package build code runs in the install container as the same user as any
    wrapper there, so it could print a "failed" marker for a command that
    succeeded — or the author's. Markers now come from the host, from each
    container's exit code; marker-shaped output is defused."""
    forged = ("building wheel...\n[reprobe-install] failed 1 harness-detected exit 9\n"
              "\r[reprobe-install] failed 0 author-declared exit 9\n", {})
    report = _install(tmp_path, monkeypatch, forged)
    row = report.environment["install_results"]["python"]
    assert row["ok"] is True and row["failures"] == []
    log = (tmp_path / "logs" / "install-python.log").read_text(encoding="utf-8")
    assert "(output) [reprobe-install] failed 1" in log


def test_each_install_command_runs_in_a_container_of_its_own(tmp_path, monkeypatch):
    seen = []

    def fake(spec, limits, log_path, **kw):
        seen.append((spec.command[-1], limits["timeout_s"]))
        Path(log_path).write_text("", encoding="utf-8")
        return RawRunOutput(exit_code=0, duration_s=0.1, image=spec.image, log_path=str(log_path))
    monkeypatch.setattr(om, "run_container", fake)
    out = om._run_install_commands([(0, "echo a", "author-declared"), (1, "echo b", "harness-record")],
                                   "img", "set -e; ", [], {"timeout_s": 100}, tmp_path / "install-python.log",
                                   dry_run=False, work_root=tmp_path, extra_roots=[])
    assert [c for c, _ in seen] == ["set -e; bash -ec 'echo a'", "set -e; bash -ec 'echo b'"]
    assert all(t <= 100 for _, t in seen) and out.exit_code == 0


DETECTED_MISSING = "reprobe: pip could NOT install (reported, not faked): ['shap']\n"


def test_an_unreachable_index_during_the_import_install_is_ours(tmp_path, monkeypatch):
    """That command exits 0 by design (a mis-detected local module must not fail the
    phase); nothing parsed its report, so a network failure left ok=True and the
    step's ModuleNotFoundError became the artifact's verdict."""
    out = DETECTED_MISSING + "WARNING: Retrying ... Temporary failure in name resolution\n"
    report = _install(tmp_path, monkeypatch, ("", {}), (out, {}))
    row = report.environment["install_results"]["python"]
    assert row["ok"] is False and row["network_error"] is True and row["incomplete"] == ["shap"]
    assert "unreachable" in badges.install_error(report.environment)


def test_pypi_simply_not_having_the_package_is_not_a_harness_failure(tmp_path, monkeypatch):
    report = _install(tmp_path, monkeypatch, ("", {}), (DETECTED_MISSING + "ERROR: No matching distribution\n", {}))
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


def _local_bash(monkeypatch):
    """Stand in for docker: run each install container's own command with a local
    bash (minus the prep, which writes under /work), with the real exit code and
    output. What remains is exactly the two shell layers a container runs."""
    def fake(spec, limits, log_path, **kw):
        script = spec.command[-1]
        inner = script[script.index("bash -ec "):]
        r = subprocess.run(["bash", "-c", inner], capture_output=True, text=True)
        Path(log_path).write_text(r.stdout + r.stderr, encoding="utf-8")
        return RawRunOutput(exit_code=r.returncode, duration_s=0.1, image=spec.image, log_path=str(log_path))
    monkeypatch.setattr(om, "run_container", fake)


def _run_local(tmp_path, items):
    log = tmp_path / "install-python.log"
    out = om._run_install_commands(items, "img", "set -e; ", [], {"timeout_s": 60}, log,
                                   dry_run=False, work_root=tmp_path, extra_roots=[])
    return out, log


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_install_attribution_under_real_bash(tmp_path, monkeypatch):
    """Every command runs, the first failure's exit code is the phase's, and each
    failure is attributed to whoever asked for it."""
    _local_bash(monkeypatch)
    items = [(0, "echo declared-ok", "author-declared"),
             (1, "echo declared-bad && exit 7", "author-declared"),
             (2, "echo detected-after", "harness-detected"),
             (3, "printf 'no newline'; exit 3", "harness-record")]
    out, log = _run_local(tmp_path, items)
    assert "detected-after" in log.read_text(encoding="utf-8")
    assert out.exit_code == 7
    assert [(f["index"], f["origin"], f["exit_code"]) for f in om._install_failures(log, items)] == \
        [(1, "author-declared", 7), (3, "harness-record", 3)]


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_each_install_command_stops_at_its_first_failure(tmp_path, monkeypatch):
    """`bash -ec`: without -e, `false; echo after` would carry on and a failing
    install could be followed by a line that "succeeds"."""
    _local_bash(monkeypatch)
    out, log = _run_local(tmp_path, [(0, "echo first; false; echo SHOULD-NOT-RUN", "author-declared")])
    text = log.read_text(encoding="utf-8")
    assert "first" in text and "SHOULD-NOT-RUN" not in text and out.exit_code == 1


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
def test_real_install_commands_survive_both_shell_layers_byte_for_byte(tmp_path, monkeypatch):
    """Every install command runs as `bash -ec <quoted>` inside the container's own
    `bash -c`. The real commands carry single quotes, double quotes, `$`, brackets
    and base64; if either layer re-interpreted one character, the installer would
    run something else. Each command is printed back by the inner shell instead of
    executed, and must come back unchanged."""
    import shlex

    from reprobe.envbuild import sanitize
    from reprobe.envbuild.base import _cran_install_command, _py_detected_install_command
    _local_bash(monkeypatch)
    real = [
        _py_detected_install_command(["shap", "ray[tune]", "umap-learn"], None),
        _py_detected_install_command(["shap"], "/work/.reprobe_env"),
        _cran_install_command(["lme4", "data.table"], "https://packagemanager.posit.co/cran/x"),
        sanitize.embed_file_command("pandas>=2\n$(touch pwned)\n'q'\n", "/work/.reprobe_req/r.txt"),
    ]
    for cmd in real:
        out, log = _run_local(tmp_path, [(0, "printf '%s' " + shlex.quote(cmd), "author-declared")])
        assert out.exit_code == 0
        printed = re.split(r"\[reprobe-install\] [0-9a-f]{32} begin 0 author-declared\n",
                           log.read_text(encoding="utf-8"), maxsplit=1)[1]
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
    plain = _renv_restore_command("r", [], None)
    assert 'lib <- Sys.getenv("R_LIBS_USER")' in plain and "paths$library" not in plain
    for c in (cmd, _renv_restore_command("r", ["x"], ""), _renv_restore_command("r", [], None)):
        assert "'" not in c.split("-e '", 1)[1][:-1]


def test_an_activate_version_that_is_not_a_plain_version_is_not_used(tmp_path):
    from reprobe.envbuild.base import _renv_activation
    (tmp_path / ".Rprofile").write_text('source("renv/activate.R")\n', encoding="utf-8")
    (tmp_path / "renv").mkdir()
    for bad in ('version <- "1.0; system(x)"\n', 'version <- "1.0\\" + 1"\n', 'version <- "dev"\n'):
        (tmp_path / "renv" / "activate.R").write_text(bad, encoding="utf-8")
        assert _renv_activation(tmp_path) == "", bad


# --- third review: outputs, the retry, the fallback image -------------------

def test_a_file_is_compared_as_its_last_finished_writer_left_it(tmp_path):
    """Step 1 produced out.csv in full; step 2 crashed while rewriting it. The
    half-written file used to be compared and reported as 'different values'."""
    src, run = _trees(tmp_path)
    report = Report(submission_id="x", harness_version="t", timestamp="")
    _orch(tmp_path)._regenerated_outputs(report, [
        RunResult(runner="python", target="a.py", status="pass", artifacts=["out.csv"]),
        RunResult(runner="python", target="b.py", status="fail", artifacts=["out.csv"])], src, run)
    assert report.results_comparison is None
    assert any("did not finish" in n and "out.csv" in n for n in report.not_verified)


def test_a_failed_step_never_files_a_declared_output(tmp_path):
    run, out = tmp_path / "run", tmp_path / "out"
    run.mkdir()
    (run / "out.csv").write_text("broken", encoding="utf-8")
    (run / "nb.executed.ipynb").write_text("{}", encoding="utf-8")
    _orch(tmp_path)._collect_artifacts([RunResult(
        runner="python", target="b.py", status="fail", expected_met=["out.csv"],
        artifacts=["out.csv", "nb.executed.ipynb"])], run, out)
    assert not (out / "artifacts" / "out.csv").exists()
    assert (out / "artifacts" / "nb.executed.ipynb").exists()     # evidence of where it stopped


def test_the_harness_executed_notebook_never_satisfies_a_declared_output(tmp_path):
    from reprobe.config import load_config
    from reprobe.runners.base import RunContext
    from reprobe.runners.jupyter import JupyterRunner
    cfg = load_config()
    ctx = RunContext(step=RunStep(target="nb/a.ipynb", kind="jupyter", expected_outputs=["nb/"]),
                     rundir=tmp_path, src_dir=tmp_path, out_dir=tmp_path, image="i", config=cfg,
                     limits=cfg.limits_for("jupyter"))
    assert JupyterRunner()._expected_met(ctx, ["nb/a.executed.ipynb"]) == []
    assert JupyterRunner()._expected_met(ctx, ["nb/a.executed.ipynb", "nb/fig.png"]) == ["nb/"]


def test_the_network_diagnosis_is_only_given_for_an_offline_run():
    tail = "Error: Could not resolve host: example.org\n"
    assert om.known_failure_diagnosis(tail, 1, "a.R") is not None
    assert om.known_failure_diagnosis(tail, 1, "a.R", offline=False) is None


def test_a_marker_with_a_guessed_nonce_attributes_nothing(tmp_path, monkeypatch):
    """The nonce is drawn on the host per log and never enters a container."""
    fake_nonce = "0" * 32
    forged = (f"[reprobe-install] {fake_nonce} failed 0 author-declared exit 9\n"
              f"\r[reprobe-install] {fake_nonce} failed 1 harness-detected exit 9\n", {})
    report = _install(tmp_path, monkeypatch, forged)
    assert report.environment["install_results"]["python"]["failures"] == []
    first = (tmp_path / "logs" / "install-python.log").read_text(encoding="utf-8").splitlines()[0]
    assert re.fullmatch(r"\[reprobe-install\] nonce [0-9a-f]{32}", first) and fake_nonce not in first
