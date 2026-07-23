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
    # candidate/candidate report: both status sentences must be present
    assert "not yet deposited under an archival persistent identifier" in md
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
