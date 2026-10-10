"""Reports must render to md + html + json without a daemon or network."""

import json

from reprobe.models import Report, RunResult
from reprobe.report import dashboard, html, markdown


def _report():
    return Report(
        submission_id="sub-1", harness_version="reprobe 0.1.0", timestamp="2026-06-27T00:00:00Z",
        source={"input": "x", "resolved_type": "git", "pin": {"kind": "git_sha", "value": "abc"},
                "checksum_verified": False, "anonymized": False, "warnings": []},
        environment={"strategy": "pinned-base", "image": "img", "env_provenance": "harness-default", "warnings": []},
        steps=[RunResult(runner="python", target="a.py", status="pass", duration_s=1.2,
                         claims=["ran to completion"], not_verified=["results match the paper"])],
        badges={"acm": {"available": "candidate", "functional": "candidate",
                        "results_reproduced": "not-evaluated", "notes": []},
                "fair": {"findable": False, "accessible": True, "interoperable": "partial", "reusable": "partial"}},
        llm={"summary": "ran fine", "model": "gemma4:e4b"},
        verdict={"overall": "runs", "human_review_required": False},
    )


def test_markdown_html_render():
    r = _report()
    md = markdown.render(r)
    assert "reprobe report" in md and "What was NOT checked" in md
    h = html.render(r)
    assert "<html" in h and "Available" in h


def test_deterministic_diagnosis_renders_and_is_not_labelled_as_the_llm():
    """A timeout with no console output gets a harness-authored diagnosis. Both
    renderers must show it AND distinguish it from the LLM advisory — the whole
    point is that this one is not a guess."""
    from reprobe.orchestrator import _no_log_diagnosis

    step = RunResult(runner="jupyter", target="slow.ipynb", status="timeout",
                     duration_s=5400.0, diagnostics={"timeout_s": 5400})
    step.diagnostics["harness_diagnosis"] = _no_log_diagnosis(step)
    r = _report()
    r.steps = [step]

    md_out, h = markdown.render(r), html.render(r)
    for out in (md_out, h):
        assert "not an LLM guess" in out
        assert "--timeout 10800" in out          # doubled from the 5400s that expired
    assert "Harness diagnosis" in md_out
    assert "LLM diagnosis" not in md_out


def test_dashboard_render():
    out = dashboard.render([_report().model_dump(mode="json")])
    assert "batch" in out and "sub-1" in out


def test_report_json_roundtrip():
    r = _report()
    data = json.loads(json.dumps(r.model_dump(mode="json")))
    assert data["submission_id"] == "sub-1"


def test_markdown_feedback_for_authors_block():
    md = markdown.render(_report())
    assert "## Feedback for authors" in md
    # candidate/candidate report: both status sentences must be present. The
    # Available one is reason-neutral — "candidate" also covers an archival
    # deposit whose bytes could not be verified, which IS deposited.
    assert "could not be granted automatically" in md
    assert "not yet deposited" not in md
    assert "a human" in md and "Functional" in md


def test_dashboard_types_column_and_no_checksum_flag():
    rep = _report().model_dump(mode="json")
    rep["detect"] = {"artifact_types": ["python", "video"]}
    rep["badges"]["acm"]["notes"] = ["archival pin present but checksums not verified"]
    out = dashboard.render([rep])
    assert "python, video" in out
    assert "no-checksum" in out
    assert dashboard.triage_flags(rep) == ["no-checksum"]


def test_markdown_neutralizes_untrusted_html():
    r = _report()
    r.source["warnings"] = ["<script>alert(1)</script>"]
    r.not_verified = ["<img src=x onerror=alert(1)>"]
    md = markdown.render(r)
    assert "<script>" not in md and "&lt;script&gt;" in md
    assert "<img" not in md


def test_dry_run_and_budget_and_comparison_are_rendered_in_both_formats():
    from reprobe.models import RunResult
    r = _report()
    r.run_mode = "dry-run"
    r.steps = [RunResult(runner="jupyter", target="fit.ipynb", status="resource-limit",
                         diagnostics={"limit": "memory", "cap": "8g"})]
    r.results_comparison = {
        "rtol": 1e-6, "atol": 1e-9,
        "summary": {"total": 2, "counts": {"equivalent": 1, "differs": 1},
                    "headline": "2 regenerated: 1 equivalent, 1 differs"},
        "files": [{"path": "out/a.csv", "kind": "tabular", "status": "equivalent", "detail": "within tol"},
                  {"path": "out/b|x.csv", "kind": "tabular", "status": "differs", "detail": "3 cells"},
                  {"path": "z.bin", "kind": "binary", "status": "not-compared",
                   "reason": "over-file-cap", "detail": "cap"}],
    }
    md = markdown.render(r)
    assert "**Dry run**" in md
    assert "memory cap (8g)" in md
    assert "## Regenerated outputs vs the committed ones" in md
    assert "out/b&#124;x.csv" in md                     # a pipe cannot break the table
    assert "1 more file(s) past the per-run file cap" in md
    page = html.render(r)
    assert "Dry run" in page and "Regenerated outputs" in page and "memory cap (8g)" in page
    flags = dashboard.triage_flags(r.model_dump(mode="json"))
    assert {"dry-run", "resource-limit", "outputs-differ"} <= set(flags)
