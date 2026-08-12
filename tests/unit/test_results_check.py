"""The advisory paper-claims comparison.

This is the one part of the harness that writes a statement about someone's
PUBLISHED work — "N paper claim(s) look inconsistent with the re-run" — and every
other test runs with ``use_llm=False``, so none of it was ever executed in CI.
No model is needed to test it: the comparison is a pure function of what the
model returned, and that is exactly the part that must not go wrong.

Pure: no network, no Docker, no Ollama.
"""

from __future__ import annotations

import pytest

import reprobe.paper as paper_mod
from reprobe.llm import roles as llm_roles
from reprobe.models import FetchResult, Pin, Report, RunResult
from reprobe.orchestrator import Orchestrator
from reprobe.paper import Paper


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    """An orchestrator, a report, and one step that printed something."""
    work = tmp_path / "w"
    (work / "run").mkdir(parents=True)
    log = work / "run" / "step.log"
    log.write_text("$ docker run ...\n\naccuracy = 0.81\n", encoding="utf-8")
    step = RunResult(runner="python", target="main.py", status="pass",
                     log_path=str(log), executed=True)
    report = Report(submission_id="s", harness_version="t", timestamp="")
    fetch = FetchResult(input="u", resolved_type="git", src_dir=str(tmp_path),
                        pin=Pin(kind="git_sha", value="abc"))
    o = Orchestrator(workroot=tmp_path)
    # run() sets this per submission; these tests call the phase directly.
    o._merged_data_paths = set()
    return {"o": o, "report": report, "steps": [step],
            "src": tmp_path, "work": work, "fetch": fetch}


def _check(ctx, **kw):
    ctx["o"]._results_check(ctx["report"], ctx["steps"], ctx["src"], ctx["work"],
                            kw.get("manifest", {}), ctx["fetch"], object())
    return ctx["report"]


def test_a_paper_that_cannot_be_located_is_reported_not_guessed(ctx, monkeypatch):
    monkeypatch.setattr(paper_mod, "locate", lambda *a, **k: None)
    rep = _check(ctx)
    assert rep.llm["results_check"]["status"] == "no-paper"
    assert "no paper found" in rep.llm["results_check"]["detail"]


def test_a_crash_while_locating_the_paper_never_breaks_the_run(ctx, monkeypatch):
    """The comparison is advisory. A PDF that explodes the parser must cost the
    report its comparison, not the whole run."""
    def boom(*a, **k):
        raise ValueError("corrupt xref table")
    monkeypatch.setattr(paper_mod, "locate", boom)
    rep = _check(ctx)
    assert rep.llm["results_check"]["status"] == "error"
    assert "ValueError" in rep.llm["results_check"]["detail"]


def test_an_unreadable_paper_says_so_instead_of_comparing_nothing(ctx, monkeypatch):
    """A scanned PDF yields no text. Reporting "no mismatches" off an empty
    comparison would read as agreement with the paper."""
    monkeypatch.setattr(paper_mod, "locate",
                        lambda *a, **k: Paper(source="repo-pdf", ref="p.pdf", text="   ",
                                              coverage="none"))
    called = []
    monkeypatch.setattr(llm_roles, "compare_results",
                        lambda *a, **k: called.append(1))
    rep = _check(ctx)
    assert rep.llm["results_check"]["status"] == "not-compared"
    assert not called, "the model was asked to compare an empty paper"
    assert any("were NOT compared" in n for n in rep.not_verified)


def test_a_model_that_returns_nothing_is_not_read_as_agreement(ctx, monkeypatch):
    monkeypatch.setattr(paper_mod, "locate",
                        lambda *a, **k: Paper(source="repo-pdf", ref="p.pdf",
                                              text="accuracy of 0.92", coverage="full text"))
    monkeypatch.setattr(llm_roles, "compare_results", lambda *a, **k: None)
    rep = _check(ctx)
    assert rep.llm["results_check"]["status"] == "not-compared"
    assert "no usable comparison" in rep.llm["results_check"]["detail"]


def test_mismatched_claims_are_counted_and_surfaced_for_a_human(ctx, monkeypatch):
    monkeypatch.setattr(paper_mod, "locate",
                        lambda *a, **k: Paper(source="repo-pdf", ref="p.pdf",
                                              text="accuracy of 0.92", coverage="full text"))
    monkeypatch.setattr(llm_roles, "compare_results", lambda *a, **k: {"claims": [
        {"claim": "accuracy", "paper_value": "0.92", "produced_value": "0.81", "verdict": "mismatch"},
        {"claim": "n", "paper_value": "35", "produced_value": "35", "verdict": "match"},
        {"claim": "f1", "paper_value": "0.7", "produced_value": "-", "verdict": "not-reported"},
    ]})
    rep = _check(ctx)
    section = rep.llm["results_check"]
    assert section["status"] == "compared"
    assert section["counts"] == {"mismatch": 1, "match": 1, "not-reported": 1}
    assert any("1 paper claim(s) look inconsistent" in n for n in rep.not_verified)
    # the wording must keep it advisory — this is a statement about published work
    assert any("a human must confirm" in n for n in rep.not_verified)


def test_agreement_with_the_paper_adds_no_caveat(ctx, monkeypatch):
    monkeypatch.setattr(paper_mod, "locate",
                        lambda *a, **k: Paper(source="repo-pdf", ref="p.pdf",
                                              text="accuracy of 0.81", coverage="full text"))
    monkeypatch.setattr(llm_roles, "compare_results", lambda *a, **k: {"claims": [
        {"claim": "accuracy", "paper_value": "0.81", "produced_value": "0.81", "verdict": "match"}]})
    rep = _check(ctx)
    assert rep.llm["results_check"]["counts"] == {"match": 1}
    assert not any("inconsistent" in n for n in rep.not_verified)


def test_the_comparison_never_moves_results_reproduced(tmp_path, monkeypatch):
    """The invariant the whole feature rests on: "results match the paper" is a
    human judgement. A model agreeing with every claim must still leave the badge
    at not-evaluated, or the harness would be granting on an LLM's say-so."""
    from reprobe.models import DetectResult
    from reprobe.report import badges

    out = badges.decide(
        FetchResult(input="u", resolved_type="zenodo", src_dir="/x",
                    pin=Pin(kind="version_doi", value="10.5281/zenodo.1"), checksum_verified=True),
        [RunResult(runner="python", target="main.py", status="pass")],
        DetectResult(artifact_types=["python"]),
        badges_cfg=__import__("reprobe.config", fromlist=["load_config"]).load_config().badges,
        functional_requested=True, ran=True)
    assert out["acm"]["results_reproduced"] == "not-evaluated"


def test_produced_text_prefers_declared_outputs_over_console_noise(ctx, tmp_path):
    """A declared .csv is the author saying "these are my numbers"; the console is
    a fallback. Sending the model the wrong one wastes a small context window."""
    rundir = ctx["work"] / "run"
    (rundir / "results").mkdir(parents=True, exist_ok=True)
    (rundir / "results" / "summary.csv").write_text("metric,value\naccuracy,0.81\n", encoding="utf-8")
    ctx["steps"][0].expected_met = ["results/summary.csv"]
    text = ctx["o"]._produced_text(ctx["steps"], rundir)
    assert text.index("produced file results/summary.csv") < text.index("console output")
    assert "accuracy,0.81" in text


def test_produced_text_skips_a_file_too_large_to_fence(ctx):
    rundir = ctx["work"] / "run"
    (rundir / "big.csv").write_text("x" * 200_001, encoding="utf-8")
    ctx["steps"][0].expected_met = ["big.csv"]
    text = ctx["o"]._produced_text(ctx["steps"], rundir)
    assert "produced file big.csv" not in text
