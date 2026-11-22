"""--assisted: the LLM's proposed run order and arguments run after the verdict,
in the same sandbox, and are never counted in the verdict or a badge."""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path

from typer.testing import CliRunner

import reprobe.orchestrator as om
from reprobe.cli import app
from reprobe.llm import roles, schemas
from reprobe.models import RawRunOutput
from reprobe.orchestrator import Orchestrator

ROOT = Path(__file__).resolve().parents[2]
PROPOSAL = {"steps": [{"path": "tool/maian.py", "args": ["-c", "examples/a b.sol"],
                       "why": "README: python maian.py -c <contract>"}], "confidence": 0.8}


class _Client:
    """Duck-typed OllamaClient: the proposal for the run-order role, nothing else."""
    confidence_threshold = 0.6

    def status(self):
        return {"ok": True}

    def generate_json(self, prompt, *, system=None, schema=None):
        return dict(PROPOSAL) if schema is schemas.RUN_ORDER else None


def _artifact(tmp_path: Path) -> Path:
    art = tmp_path / "art"
    (art / "tool").mkdir(parents=True, exist_ok=True)
    (art / "examples").mkdir(exist_ok=True)
    (art / "tool" / "maian.py").write_text("import sys\nprint(sys.argv)\n", encoding="utf-8")
    (art / "tool" / "helpers.py").write_text("X = 1\n", encoding="utf-8")
    (art / "examples" / "a b.sol").write_text("contract A {}\n", encoding="utf-8")
    (art / "README.md").write_text("cd tool && python maian.py -c <contract>\n", encoding="utf-8")
    return art


def _run(tmp_path, monkeypatch, *, assisted, exit_code=0):
    commands = []

    def fake(spec, limits, log_path, **kw):
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text("ok\n", encoding="utf-8")
        if spec.network != "egress":
            commands.append(spec.command[-1])
        return RawRunOutput(exit_code=0 if spec.network == "egress" else exit_code,
                            duration_s=0.1, image=spec.image, log_path=str(log_path))

    monkeypatch.setattr(om, "run_container", fake)
    monkeypatch.setattr(om, "reap_orphans", lambda: [])
    monkeypatch.setattr(om, "_image_digest", lambda image: None)
    monkeypatch.setattr(om, "llm_from_config", lambda cfg: _Client())
    rep = Orchestrator(workroot=tmp_path / f"w{assisted}").run(str(_artifact(tmp_path)), assisted=assisted)
    return rep, commands


def test_the_proposal_runs_with_its_arguments_and_changes_no_verdict(tmp_path, monkeypatch):
    plain, _ = _run(tmp_path, monkeypatch, assisted=False)
    rep, commands = _run(tmp_path, monkeypatch, assisted=True)
    assert "assisted_run" not in plain.llm
    ar = rep.llm["assisted_run"]
    assert ar["counted_in_verdict"] is False and ar["status"] == "ran"
    assert [(s["target"], s["argv"], s["status"]) for s in ar["steps"]] == [
        ("tool/maian.py", ["-c", "examples/a b.sol"], "pass")]
    assert "1 of 1 step(s)" in ar["summary"]
    # passed to the script quoted, never through a shell string of the model's
    assert any(c.endswith("python 'tool/maian.py' '-c' 'examples/a b.sol'") for c in commands)
    # nothing the assisted run did reaches the verdict, the steps or the badges
    assert rep.steps == plain.steps == []
    assert rep.verdict == plain.verdict
    assert rep.badges == plain.badges
    assert "assisted run only" in " ".join(rep.environment.get("notes", []))
    md = (tmp_path / "wTrue" / rep.submission_id / "report" / "report.md").read_text(encoding="utf-8")
    assert "## Assisted run (LLM-proposed; not counted)" in md
    assert "tool/maian.py -c 'examples/a b.sol': **pass**" in md


def test_a_failed_assisted_step_is_reported_not_counted(tmp_path, monkeypatch):
    rep, _ = _run(tmp_path, monkeypatch, assisted=True, exit_code=1)
    ar = rep.llm["assisted_run"]
    assert ar["steps"][0]["status"] == "fail" and ar["steps"][0]["exit_code"] == 1
    assert "0 of 1 step(s)" in ar["summary"]
    assert rep.verdict["overall"] != "fail"


def test_proposed_arguments_are_bounded_and_screened(tmp_path):
    (tmp_path / "run.py").write_text("pass")
    client = types.SimpleNamespace(confidence_threshold=0.6, generate_json=lambda *a, **k: {
        "steps": [{"path": "run.py", "args": ["x" * 500] * 30}], "confidence": 0.9})
    out = roles.detect_run_order(client, tmp_path, types.SimpleNamespace(steps=[]))
    assert len(out["steps"][0]["args"]) == roles.MAX_ARGS
    assert {len(a) for a in out["steps"][0]["args"]} == {roles.MAX_ARG_LEN}
    client.generate_json = lambda *a, **k: {"steps": [{"path": "run.py", "args": ["$(rm -rf /)"]}],
                                            "confidence": 0.9}
    assert roles.detect_run_order(client, tmp_path, types.SimpleNamespace(steps=[])) is None


def test_assisted_needs_the_llm_and_a_run(tmp_path):
    for flag in ("--no-llm", "--no-run"):
        res = CliRunner().invoke(app, ["run", str(tmp_path), "--assisted", flag])
        assert res.exit_code == 2 and "--assisted" in res.output


def test_the_benchmark_comparison_lists_the_assisted_commands():
    spec = importlib.util.spec_from_file_location("bench_compare", ROOT / "benchmarks" / "compare.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rep = {"llm": {"assisted_run": {"steps": [
        {"target": "a.py", "argv": ["-n", "1"], "status": "pass"},
        {"target": "b.py", "argv": [], "status": "fail"}]}}}
    assert mod._assisted(rep) == "1/2: a.py -n 1 = pass; b.py = fail"
    assert mod._assisted({"llm": {"assisted_run": {"status": "no-proposal"}}}) == "no-proposal"
    assert mod._assisted({}) == ""
