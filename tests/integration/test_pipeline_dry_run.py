"""End-to-end pipeline test, daemon-free: the full orchestrator over the local
example fixture with --dry-run containers. Asserts the report artifacts are
written, sandbox flags reach docker argv, and no badge is ever granted for a
local (non-archival) pin."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import reprobe.orchestrator as orch_mod
from reprobe.orchestrator import Orchestrator

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "example-python"
BROKEN = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "broken-python"


@pytest.fixture()
def spy_run_container(monkeypatch):
    """Delegate to the real dry-run implementation while capturing every argv."""
    calls: list[dict] = []
    real = orch_mod.run_container

    def spy(spec, limits, log_path, **kw):
        raw = real(spec, limits, log_path, **kw)
        calls.append({"spec": spec, "argv": raw.argv_redacted, "kw": kw})
        return raw

    monkeypatch.setattr(orch_mod, "run_container", spy)
    return calls


def _run(tmp_path, ref, spy=None, **kw):
    o = Orchestrator(workroot=tmp_path)
    return o.run(str(ref), use_llm=False, dry_run=True, **kw)


def test_example_pipeline_writes_reports_and_never_grants(tmp_path, spy_run_container):
    report = _run(tmp_path, EXAMPLE)
    outdir = tmp_path / report.submission_id / "out"
    for name in ("report.json", "report.md", "report.html"):
        assert (outdir / name).is_file(), f"{name} not written"

    data = json.loads((outdir / "report.json").read_text(encoding="utf-8"))
    # a local path has no archival PID: Available must never be granted
    assert data["badges"]["acm"]["available"] != "granted"
    assert data["verdict"]["human_review_required"] is True
    # provenance records the pins so the run is re-checkable next year
    assert data["provenance"]["pins_year"] is not None
    assert "pins.yaml_sha256" in data["provenance"]


def test_run_phase_argv_is_hardened_and_install_is_not_readonly(tmp_path, spy_run_container):
    _run(tmp_path, EXAMPLE)
    assert spy_run_container, "no containers were launched"
    run_calls = [c for c in spy_run_container if c["spec"].network == "none"]
    install_calls = [c for c in spy_run_container if c["spec"].network == "egress"]
    assert run_calls, "no offline run-phase container"
    for c in run_calls:
        argv = c["argv"]
        assert "--network" in argv and "none" in argv
        assert "--read-only" in argv
        assert ["--cap-drop", "ALL"] == argv[argv.index("--cap-drop"):argv.index("--cap-drop") + 2]
    # the example declares no deps, so an install phase may legitimately not run
    for c in install_calls:
        assert "--read-only" not in c["argv"]


def test_failed_source_section_matches_success_shape():
    """A fetch failure must not under-populate report.source, or the report
    renderers blow up on missing keys (pin, resolved_type, ...). The failure
    shape must carry every key the success shape does."""
    from reprobe.models import FetchResult, Pin
    ok = orch_mod._source_section(FetchResult(
        input="u", resolved_type="git", src_dir="/x", pin=Pin(kind="git_sha", value="abc")))
    failed = orch_mod._failed_source_section("u", "boom")
    assert set(ok).issubset(set(failed)), f"missing keys: {set(ok) - set(failed)}"
    assert failed["pin"]["kind"] == "none" and failed["error"] == "boom"


def test_unresolvable_ref_yields_renderable_fetch_failed_report(tmp_path):
    # nonexistent local path: fails fast through the local fetcher, no network.
    report = _run(tmp_path, str(tmp_path / "does-not-exist-anywhere"))
    assert report.verdict["overall"] == "fetch-failed"
    assert report.verdict["human_review_required"] is True
    outdir = tmp_path / report.submission_id / "out"
    for name in ("report.json", "report.md", "report.html"):
        assert (outdir / name).is_file(), f"{name} not written on fetch failure"
    html_text = (outdir / "report.html").read_text(encoding="utf-8")
    assert "fetch failed" in html_text.lower()
    assert "<b>Badges</b>" not in html_text        # nothing fetched -> no badge chips


def test_broken_python_fixture_fails_cleanly(tmp_path, monkeypatch):
    """Dry-run fabricates exit 0, so force a nonzero exit to prove a failing
    step yields a clean FAIL verdict — never a traceback out of run()."""
    real = orch_mod.run_container

    def failing(spec, limits, log_path, **kw):
        raw = real(spec, limits, log_path, **kw)
        return raw.model_copy(update={"exit_code": 1})

    monkeypatch.setattr(orch_mod, "run_container", failing)
    report = _run(tmp_path, BROKEN)
    statuses = {s.status for s in report.steps if s.executed}
    assert statuses and statuses <= {"fail", "partial", "error"}
    assert report.verdict["overall"] in ("runs-with-failures", "infra-error")
    assert report.badges["acm"]["functional"] in ("not-met", "not-evaluated")
