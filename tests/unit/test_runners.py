"""Runner commands, interpretation, and discovery — all daemon-free.

Covers: shell quoting (the rmarkdown injection/mangling fix), writable-HOME
exports, papermill/nbconvert cwd agreement, launcher-exit-code attribution,
the expected-output freshness + missing-set recording, unity host-side
containment, and config-driven plugin loading.
"""

from __future__ import annotations

import os
import sys
import types

import pytest

from reprobe.config import load_config
from reprobe.models import RawRunOutput, RunStep
from reprobe.runners.base import BaseRunner, RunContext, _q, snapshot
from reprobe.runners.jupyter import JupyterRunner
from reprobe.runners.python_script import PythonScriptRunner
from reprobe.runners.r_script import RScriptRunner
from reprobe.runners.registry import RunnerLoadError, load_runners
from reprobe.runners.rmarkdown import RMarkdownRunner
from reprobe.runners.unity import UnityRunner


def _ctx(tmp_path, step, **kw):
    cfg = load_config()
    defaults = dict(step=step, rundir=tmp_path, src_dir=tmp_path, out_dir=tmp_path,
                    image="img", config=cfg, limits=cfg.limits_for("python"),
                    pre_index=snapshot(tmp_path))
    defaults.update(kw)
    return RunContext(**defaults)


def _bash(cmd: list[str]) -> str:
    assert cmd[:2] == ["bash", "-c"] and len(cmd) == 3
    return cmd[2]


# --------------------------------------------------------------------------- #
# Shell quoting (base._q) and command construction
# --------------------------------------------------------------------------- #
def test_q_escapes_apostrophe_instead_of_stripping():
    assert _q("John's analysis.Rmd") == "'John'\\''s analysis.Rmd'"
    assert _q("plain.txt") == "'plain.txt'"


def test_rmarkdown_uses_commandargs_and_preserves_hostile_paths(tmp_path):
    hostile = "x$(touch /tmp/pwned).Rmd"
    cmd = _bash(RMarkdownRunner().build_command(_ctx(tmp_path, RunStep(target=hostile, kind="rmarkdown"))))
    assert "commandArgs(TRUE)" in cmd
    assert _q(hostile) in cmd                      # single-quoted -> $() is inert
    assert 'Rscript -e "' not in cmd               # never double-quoted (shell-active)
    # apostrophes survive instead of being deleted (the old bug made a false FAIL)
    cmd2 = _bash(RMarkdownRunner().build_command(_ctx(tmp_path, RunStep(target="John's analysis.Rmd", kind="rmarkdown"))))
    assert "John'\\''s analysis.Rmd" in cmd2
    assert _q("/work/.") in cmd2                   # root-level doc renders into /work


def test_python_and_r_quote_target_and_argv(tmp_path):
    step = RunStep(target="run.py", kind="python", argv=["--n", "it's"])
    cmd = _bash(PythonScriptRunner().build_command(_ctx(tmp_path, step)))
    assert "python 'run.py' '--n' 'it'\\''s'" in cmd
    rcmd = _bash(RScriptRunner().build_command(_ctx(tmp_path, RunStep(target="a b.R", kind="r"))))
    assert "Rscript 'a b.R'" in rcmd


def test_python_and_jupyter_export_writable_home(tmp_path):
    for runner, step in [(PythonScriptRunner(), RunStep(target="run.py", kind="python")),
                         (JupyterRunner(), RunStep(target="nb.ipynb", kind="jupyter"))]:
        cmd = _bash(runner.build_command(_ctx(tmp_path, step)))
        assert "export HOME=/work XDG_CACHE_HOME=/work/.reprobe_cache MPLCONFIGDIR=/tmp;" in cmd


def test_jupyter_papermill_runs_kernel_in_notebook_dir(tmp_path):
    cmd = _bash(JupyterRunner().build_command(_ctx(tmp_path, RunStep(target="analysis/figs.ipynb", kind="jupyter"))))
    assert ("papermill --no-progress-bar --log-output --cwd 'analysis' "
            "'analysis/figs.ipynb' 'analysis/figs.executed.ipynb'") in cmd
    assert "nbconvert --to notebook --execute --output 'figs.executed'" in cmd
    # root-level notebook: parent is "."
    cmd = _bash(JupyterRunner().build_command(_ctx(tmp_path, RunStep(target="nb.ipynb", kind="jupyter"))))
    assert "--cwd '.'" in cmd


def test_jupyter_asks_papermill_to_log_cell_boundaries(tmp_path):
    """Without --log-output papermill emits one line for an entire run, so a
    timed-out notebook leaves nothing to say which cell stalled."""
    cmd = _bash(JupyterRunner().build_command(_ctx(tmp_path, RunStep(target="nb.ipynb", kind="jupyter"))))
    assert "--log-output" in cmd


# --------------------------------------------------------------------------- #
# interpret(): status attribution + expected-output honesty
# --------------------------------------------------------------------------- #
def _raw(exit_code=0, **kw):
    return RawRunOutput(exit_code=exit_code, duration_s=0.1, **kw)


@pytest.mark.parametrize("code", [125, 126, 127])
def test_launcher_exit_codes_are_harness_errors_not_author_fails(tmp_path, code):
    ctx = _ctx(tmp_path, RunStep(target="run.py", kind="python"))
    res = PythonScriptRunner().interpret(_raw(exit_code=code), ctx)
    assert res.status == "error"
    assert "could not start" in res.diagnostics["harness_error"]
    assert "log_tail" in res.diagnostics


def test_timeout_keeps_the_log_tail_and_the_partial_output(tmp_path):
    """A timeout used to report neither, which made it indistinguishable from a
    harness bug and left the LLM diagnoser with an empty string to explain."""
    log = tmp_path / "step.log"
    log.write_text("Executing Cell 12---\n[reprobe] hard timeout after 1800s\n", encoding="utf-8")
    ctx = _ctx(tmp_path, RunStep(target="slow.ipynb", kind="jupyter"))
    # papermill checkpoints after every cell, so a partial notebook exists on disk
    (tmp_path / "slow.executed.ipynb").write_text("{}", encoding="utf-8")

    res = JupyterRunner().interpret(
        _raw(exit_code=None, timed_out=True, log_path=str(log)), ctx)

    assert res.status == "timeout"
    assert "Executing Cell 12" in res.diagnostics["log_tail"]
    assert "slow.executed.ipynb" in res.artifacts
    # a timeout still claims nothing about the science
    assert res.claims == []
    assert "results match the paper" in res.not_verified


def test_raw_error_maps_to_error_and_exit_1_stays_fail(tmp_path):
    ctx = _ctx(tmp_path, RunStep(target="run.py", kind="python"))
    res = PythonScriptRunner().interpret(_raw(exit_code=None, error="image missing"), ctx)
    assert res.status == "error" and res.diagnostics["harness_error"] == "image missing"
    res = PythonScriptRunner().interpret(_raw(exit_code=1), ctx)
    assert res.status == "fail" and "harness_error" not in res.diagnostics


def test_subset_of_declared_outputs_passes_but_records_missing(tmp_path):
    step = RunStep(target="run.py", kind="python", expected_outputs=["a.csv", "b.csv"])
    ctx = _ctx(tmp_path, step)                     # pre_index taken while dir is empty
    (tmp_path / "a.csv").write_text("x", encoding="utf-8")
    res = PythonScriptRunner().interpret(_raw(), ctx)
    assert res.status == "pass"
    assert res.expected_met == ["a.csv"]
    assert res.diagnostics["expected_missing"] == ["b.csv"]


def test_zero_declared_outputs_is_partial(tmp_path):
    step = RunStep(target="run.py", kind="python", expected_outputs=["a.csv"])
    res = PythonScriptRunner().interpret(_raw(), _ctx(tmp_path, step))
    assert res.status == "partial"
    assert res.diagnostics["expected_missing"] == ["a.csv"]


def test_inherited_outputs_do_not_make_a_prep_step_partial(tmp_path):
    # A multi-step pipeline broadcasts the manifest's outputs onto EVERY step, so
    # a prep step that legitimately produces none of the final artifacts must
    # stay "pass" — marking it "partial" denied the whole pipeline the Functional
    # candidate. The missing set is still recorded (never over-claim).
    step = RunStep(target="00_prep.py", kind="python",
                   expected_outputs=["a.csv"], outputs_inherited=True)
    res = PythonScriptRunner().interpret(_raw(), _ctx(tmp_path, step))
    assert res.status == "pass"
    assert res.diagnostics["expected_missing"] == ["a.csv"]


def test_committed_but_unchanged_output_does_not_count(tmp_path):
    # freshness rule: only files (re)created by THIS run may satisfy a declared
    # output — a figure committed to the repo must not inflate the signal.
    out = tmp_path / "fig.png"
    out.write_text("old", encoding="utf-8")
    step = RunStep(target="run.py", kind="python", expected_outputs=["fig.png"])
    ctx = _ctx(tmp_path, step)                     # snapshot AFTER the file exists
    res = PythonScriptRunner().interpret(_raw(), ctx)
    assert res.status == "partial" and res.expected_met == []
    os.utime(out, (out.stat().st_atime, out.stat().st_mtime + 10))   # the run "re-creates" it
    res = PythonScriptRunner().interpret(_raw(), ctx)
    assert res.status == "pass" and res.expected_met == ["fig.png"]
    assert "expected_missing" not in res.diagnostics


# --------------------------------------------------------------------------- #
# Unity host-side containment
# --------------------------------------------------------------------------- #
def _unity_ctx(tmp_path, target):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    return _ctx(tmp_path, RunStep(runner="unity", target=target, kind="unity"), src_dir=src)


@pytest.mark.parametrize("target", ["../..", "../outside", "a/../../b"])
def test_unity_rejects_traversal_targets(tmp_path, target):
    res = UnityRunner().interpret(None, _unity_ctx(tmp_path, target))
    assert res.status == "error"
    assert "escapes the source directory" in res.diagnostics["harness_error"]


def test_unity_rejects_absolute_targets(tmp_path):
    outside = str((tmp_path / "elsewhere").resolve())   # pathlib join: absolute replaces the base
    res = UnityRunner().interpret(None, _unity_ctx(tmp_path, outside))
    assert res.status == "error"


def test_unity_contained_project_still_passes(tmp_path):
    ctx = _unity_ctx(tmp_path, "proto")
    proj = ctx.src_dir / "proto"
    (proj / "Assets" / "Scenes").mkdir(parents=True)
    (proj / "Assets" / "Scenes" / "main.unity").write_text("", encoding="utf-8")
    (proj / "ProjectSettings").mkdir()
    (proj / "ProjectSettings" / "ProjectVersion.txt").write_text(
        "m_EditorVersion: 2021.3.5f1\n", encoding="utf-8")
    res = UnityRunner().interpret(None, ctx)
    assert res.status == "pass"
    assert res.diagnostics["version_detected"] == "2021.3.5f1"
    assert res.diagnostics["scene_count"] == 1
    assert "scan_truncated" not in res.diagnostics


def test_unity_is_host_only_by_contract(tmp_path):
    r = UnityRunner()
    assert r.host_only and r.container_spec(_unity_ctx(tmp_path, ".")) is None
    assert not PythonScriptRunner().host_only


# --------------------------------------------------------------------------- #
# Registry: config rows drive plugin loading
# --------------------------------------------------------------------------- #
def test_default_config_rows_load_all_builtin_runners():
    runners = load_runners()
    assert {"python", "jupyter", "r", "rmarkdown", "unity"} <= set(runners)
    assert runners["rmarkdown"].image_key == "r"    # default_image honored


def test_fake_dotted_path_row_loads_and_default_image_overrides():
    mod = types.ModuleType("fake_reprobe_plugin")

    class FakeRunner(BaseRunner):
        id = "fake"
        display_name = "Fake"
        handles_types = frozenset({"custom"})

    mod.FakeRunner = FakeRunner
    sys.modules["fake_reprobe_plugin"] = mod
    try:
        rows = [{"id": "fake", "plugin": "fake_reprobe_plugin:FakeRunner",
                 "default_image": "weird", "enabled": True}]
        runners = load_runners(enabled_ids={"fake"}, rows=rows)
        assert set(runners) == {"fake"}
        assert runners["fake"].image_key == "weird"
    finally:
        del sys.modules["fake_reprobe_plugin"]


@pytest.mark.parametrize("plugin,fragment", [
    ("no_such_module_xyz:JuliaRunner", "cannot import plugin module"),
    ("reprobe.runners.python_script:NoSuchClass", "has no class"),
    ("not-a-dotted-path", "must be 'package.module:ClassName'"),
])
def test_broken_plugin_row_raises_naming_the_row(plugin, fragment):
    rows = [{"id": "julia", "plugin": plugin, "enabled": True}]
    with pytest.raises(RunnerLoadError, match="julia") as exc:
        load_runners(rows=rows)
    assert fragment in str(exc.value)


def test_disabled_row_is_not_imported():
    rows = [{"id": "julia", "plugin": "no_such_module_xyz:JuliaRunner", "enabled": False}]
    runners = load_runners(enabled_ids={"julia"}, rows=rows)   # no raise, no runner
    assert "julia" not in runners


def test_tail_reads_bounded_window(tmp_path):
    # A huge author-controlled log must be tailed without loading it all into RAM.
    from reprobe.runners.base import _TAIL_CAP_BYTES, _tail
    log = tmp_path / "big.log"
    with log.open("w", encoding="utf-8") as fh:
        for i in range(200_000):
            fh.write(f"line {i}\n")
    assert log.stat().st_size > _TAIL_CAP_BYTES        # confirm we exceeded the window
    out = _tail(str(log), n=5)
    lines = out.splitlines()
    assert len(lines) == 5
    assert lines[-1] == "line 199999"                  # last real line preserved
    assert len(out) < _TAIL_CAP_BYTES                  # only a small tail returned


def test_tail_missing_or_empty(tmp_path):
    from reprobe.runners.base import _tail
    assert _tail(None) == ""
    assert _tail(str(tmp_path / "nope.log")) == ""
    empty = tmp_path / "e.log"; empty.write_text("")
    assert _tail(str(empty)) == ""
