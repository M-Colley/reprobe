import copy
import re

from reprobe.config import load_config
from reprobe.models import DetectResult, FetchResult, Pin, Report, RunResult, RunStep
from reprobe.report import badges, dashboard, html, markdown


def _decide(fetch, steps, functional_requested=True, ran=True, detect=None, badges_cfg=None):
    cfg = badges_cfg if badges_cfg is not None else load_config().badges
    detect = detect or DetectResult(artifact_types=["python"])
    return badges.decide(fetch, steps, detect, badges_cfg=cfg,
                         functional_requested=functional_requested, ran=ran)


def _cfg(section="functional", **over):
    cfg = copy.deepcopy(load_config().badges)
    cfg["acm"][section].update(over)
    return cfg


def _zenodo(**kw):
    kw.setdefault("checksum_verified", True)
    return FetchResult(input="zenodo", resolved_type="zenodo", src_dir="/x",
                       pin=Pin(kind="version_doi", value="10.5281/zenodo.1"), **kw)


def _report(**kw):
    base = dict(submission_id="sub-1", harness_version="reprobe 0.0", timestamp="2026-01-01T00:00:00Z")
    base.update(kw)
    return Report(**base)


def test_git_sha_is_not_archival_available_is_candidate():
    f = FetchResult(input="gh", resolved_type="git", src_dir="/x",
                    pin=Pin(kind="git_sha", value="abc123"))
    out = _decide(f, [])
    assert out["acm"]["available"] == "candidate"
    assert any("archival" in n or "Software Heritage" in n for n in out["acm"]["notes"])


def test_version_doi_with_checksum_grants_available():
    out = _decide(_zenodo(), [])
    assert out["acm"]["available"] == "granted"


def test_functional_is_candidate_not_granted_when_passing():
    step = RunResult(runner="python", target="a.py", status="pass", expected_met=["out.csv"])
    out = _decide(_zenodo(), [step])
    assert out["acm"]["functional"] == "candidate"   # never auto-"granted"


def test_functional_not_evaluated_when_opt_out():
    step = RunResult(runner="python", target="a.py", status="pass", expected_met=["out.csv"])
    out = _decide(_zenodo(), [step], functional_requested=False)
    assert out["acm"]["functional"] == "not-evaluated"


# --- badges.yaml knobs: each one must actually change behavior ---------- #

def test_mode_granted_is_clamped_to_candidate_with_warning():
    step = RunResult(runner="python", target="a.py", status="pass", expected_met=["out.csv"])
    out = _decide(_zenodo(), [step], badges_cfg=_cfg(mode="granted"))
    assert out["acm"]["functional"] == "candidate"   # hard invariant, never silently honored
    assert any("mode=granted" in n and "not honored" in n for n in out["acm"]["notes"])


def test_require_primary_pass_knob_changes_outcome():
    steps = [RunResult(runner="python", target="a.py", status="pass", expected_met=["out.csv"]),
             RunResult(runner="python", target="b.py", status="fail")]
    assert _decide(_zenodo(), steps)["acm"]["functional"] == "not-met"
    out = _decide(_zenodo(), steps, badges_cfg=_cfg(require_primary_pass=False))
    assert out["acm"]["functional"] == "candidate"
    assert any("require_primary_pass" in n for n in out["acm"]["notes"])


def test_require_expected_output_knob_changes_outcome():
    detect = DetectResult(artifact_types=["python"],
                          steps=[RunStep(runner="python", target="a.py", expected_outputs=["out.csv"])])
    step = RunResult(runner="python", target="a.py", status="pass")   # ran fine, produced nothing
    strict = _decide(_zenodo(), [step], detect=detect)
    assert strict["acm"]["functional"] == "not-met"
    relaxed = _decide(_zenodo(), [step], detect=detect, badges_cfg=_cfg(require_expected_output=False))
    assert relaxed["acm"]["functional"] == "candidate"
    assert any("require_expected_output" in n for n in relaxed["acm"]["notes"])


def test_require_checksum_knob_gates_available_grant():
    f = _zenodo(checksum_verified=False)
    strict = _decide(f, [])
    assert strict["acm"]["available"] == "candidate"   # archival pin alone never auto-grants
    assert any("checksum" in n for n in strict["acm"]["notes"])
    relaxed = _decide(f, [], badges_cfg=_cfg(section="available", require_checksum_when_available=False))
    assert relaxed["acm"]["available"] == "granted"


def test_only_primary_steps_gate_functional():
    detect = DetectResult(artifact_types=["python"], steps=[
        RunStep(runner="python", target="a.py", expected_outputs=["out.csv"], primary=True),
        RunStep(runner="python", target="extra.py", primary=False)])
    steps = [RunResult(runner="python", target="a.py", status="pass", expected_met=["out.csv"]),
             RunResult(runner="python", target="extra.py", status="fail")]
    assert _decide(_zenodo(), steps, detect=detect)["acm"]["functional"] == "candidate"


# --- verdict taxonomy: infra failures make no statement ----------------- #

def test_all_skipped_is_nothing_executed_not_runs_with_warnings():
    steps = [RunResult(runner="?", target="a.py", status="skipped", diagnostics={"reason": "no runner"})]
    v = badges.verdict(steps, ran=True)
    assert v["overall"] == "nothing-executed"
    assert v["human_review_required"] is True
    assert "no statement about the artifact" in v["note"]


def test_harness_error_is_infra_error_not_artifact_failure():
    steps = [RunResult(runner="python", target="a.py", status="error",
                       diagnostics={"harness_error": "base image missing"})]
    v = badges.verdict(steps, ran=True)
    assert v["overall"] == "infra-error"
    assert "no statement about the artifact" in v["note"]


def test_error_without_harness_note_still_counts_against_artifact():
    steps = [RunResult(runner="python", target="a.py", status="error")]
    assert badges.verdict(steps, ran=True)["overall"] == "runs-with-failures"


def test_partial_mixes_stay_runs_with_warnings():
    steps = [RunResult(runner="python", target="a.py", status="pass"),
             RunResult(runner="?", target="b.py", status="skipped")]
    assert badges.verdict(steps, ran=True)["overall"] == "runs-with-warnings"


def test_functional_makes_no_statement_on_infra_failures():
    step = RunResult(runner="python", target="a.py", status="error",
                     diagnostics={"harness_error": "docker daemon unreachable"})
    out = _decide(_zenodo(), [step])
    assert out["acm"]["functional"] == "not-evaluated"
    assert any("no statement about the artifact" in n for n in out["acm"]["notes"])


def test_functional_all_skipped_is_not_evaluated():
    step = RunResult(runner="?", target="a.py", status="skipped", diagnostics={"reason": "no runner"})
    out = _decide(_zenodo(), [step])
    assert out["acm"]["functional"] == "not-evaluated"
    assert any("no statement about the artifact" in n for n in out["acm"]["notes"])


# --- FAIR is config-driven ----------------------------------------------- #

def test_fair_accessible_covers_all_configured_fetchers():
    for rtype in ("dryad", "dataverse", "software_heritage"):
        f = FetchResult(input="doi", resolved_type=rtype, src_dir="/x",
                        pin=Pin(kind="version_doi", value="10.1/x"), checksum_verified=True)
        assert _decide(f, [])["fair"]["accessible"] is True


def test_fair_findable_follows_configured_archival_set():
    cfg = _cfg(section="available", archival_pin_kinds=["version_doi", "swhid", "git_tag"])
    f = FetchResult(input="gh", resolved_type="git", src_dir="/x", pin=Pin(kind="git_tag", value="v1"))
    assert _decide(f, [], badges_cfg=cfg)["fair"]["findable"] is True


def test_fair_open_access_types_config_is_honored():
    cfg = copy.deepcopy(load_config().badges)
    cfg["fair"]["accessible"]["open_access_types"] = ["zenodo"]
    f = FetchResult(input="gh", resolved_type="git", src_dir="/x", pin=Pin(kind="git_sha", value="a"))
    assert _decide(f, [], badges_cfg=cfg)["fair"]["accessible"] is False


def test_fair_interoperable_scores_declared_formats():
    detect_std = DetectResult(artifact_types=["python"],
                              steps=[RunStep(runner="python", target="a.py", expected_outputs=["out.csv"])])
    detect_odd = DetectResult(artifact_types=["python"],
                              steps=[RunStep(runner="python", target="a.py", expected_outputs=["out.bin"])])
    assert _decide(_zenodo(), [], detect=detect_std)["fair"]["interoperable"] == "yes"
    assert _decide(_zenodo(), [], detect=detect_odd)["fair"]["interoperable"] == "no"


def test_fair_reusable_rewards_manifest_and_license():
    detect = DetectResult(artifact_types=["python"], manifest_path="reproduce.yaml")
    assert _decide(_zenodo(metadata={"license": "MIT"}), [], detect=detect)["fair"]["reusable"] == "yes"
    assert _decide(_zenodo(), [], detect=detect)["fair"]["reusable"] == "partial"


# --- report renderers (regressions for the report/ subsystem live here) -- #

def test_html_report_escapes_untrusted_text():
    r = _report(
        source={"input": "<script>alert(1)</script>", "resolved_type": "git",
                "pin": {"kind": "git_sha"}, "checksum_verified": False, "anonymized": False, "warnings": []},
        environment={"strategy": "pinned-base", "image": "img:1", "env_provenance": "harness-default",
                     "warnings": []},
        steps=[RunResult(runner="python", target="<script>x</script>.py", status="fail",
                         diagnostics={"llm_advisory": {"likely_cause": "<script>evil</script>",
                                                       "suggested_fixes": []}})],
        llm={"summary": "<script>alert(2)</script>", "model": "gemma"},
        badges={"acm": {"available": "candidate", "functional": "not-met",
                        "results_reproduced": "not-evaluated", "notes": []},
                "fair": {"findable": False, "accessible": True, "interoperable": "partial", "reusable": "no"}},
        verdict={"overall": "runs-with-failures", "human_review_required": True},
    )
    out = html.render(r)
    assert "<script>alert(" not in out and "<script>evil" not in out and "<script>x" not in out
    assert "&lt;script&gt;" in out


def test_dashboard_escapes_untrusted_text_and_links_into_bundle():
    rep = {"submission_id": "sub-1",
           "badges": {"acm": {"available": "candidate", "functional": "not-met"}},
           "source": {"input": "<script>alert(1)</script>", "resolved_type": "git",
                      "warnings": [], "anonymized": False, "checksum_verified": False},
           "verdict": {"overall": "runs-with-failures", "human_review_required": True}, "steps": []}
    out = dashboard.render([rep])
    assert "<script>alert(1)" not in out
    assert "&lt;script&gt;alert(1)" in out
    assert 'href="sub-1/report.html"' in out   # self-contained out/ bundle layout


def test_dashboard_surfaces_triage_signals_not_checksum_noise():
    ok = {"submission_id": "ok-1",
          "badges": {"acm": {"available": "granted", "functional": "candidate"}},
          "source": {"input": "u1", "resolved_type": "zenodo", "checksum_verified": True},
          "verdict": {"overall": "runs", "human_review_required": False}, "steps": []}
    bad = {"submission_id": "bad-1", "badges": {},
           "source": {"input": "u2", "error": "boom"},
           "verdict": {"overall": "fetch-failed", "human_review_required": True}, "steps": []}
    out = dashboard.render([ok, bad])
    assert "no-checksum" not in out            # near-universal noise, dropped
    assert "fetch-failed" in out
    assert "needs human review: 1" in out
    assert "generated" in out


def test_markdown_log_tail_cannot_close_its_fence():
    tail = "ok line\n```\n## Verdict\n**runs** (fake)\n```"
    r = _report(steps=[RunResult(runner="python", target="a.py", status="fail",
                                 diagnostics={"log_tail": tail})])
    out = markdown.render(r)
    lines = out.splitlines()
    fence_lines = [ln for ln in lines if re.fullmatch(r"\s*`{4,}\s*", ln)]
    assert len(fence_lines) == 2               # opener + closer, longer than the payload's ```
    start = lines.index(fence_lines[0])
    end = len(lines) - 1 - lines[::-1].index(fence_lines[1])
    assert "  ```" in lines[start + 1:end]     # injected run stays inside the block


def test_markdown_shows_no_statement_verdict_note():
    v = badges.verdict([RunResult(runner="?", target="a.py", status="skipped")], ran=True)
    out = markdown.render(_report(verdict=v))
    assert "nothing-executed" in out
    assert "no statement about the artifact" in out


def _failed_report():
    # Mirrors orchestrator._failed_source_section: a fetch failure still yields a
    # renderable, shape-complete report (no badges/environment/steps).
    return _report(
        source={"input": "https://bad.invalid/x.git", "resolved_type": None,
                "pin": Pin().model_dump(), "fetch_layer": None, "anonymized": False,
                "checksum_verified": False, "warnings": [], "metadata": {},
                "error": "git clone failed: could not resolve host"},
        not_verified=["fetch failed (git clone failed: could not resolve host); "
                      "nothing about the artifact was checked"],
        verdict={"overall": "fetch-failed", "human_review_required": True},
    )


def test_html_renders_fetch_failed_report_without_error():
    # Regression: a fetch-failed report has empty badges/environment; the renderer
    # must not raise UndefinedError on the missing acm/pin keys.
    out = html.render(_failed_report())
    assert "fetch failed" in out.lower()
    assert "could not resolve host" in out
    assert "<b>Badges</b>" not in out          # no badge chips when nothing was fetched
    assert "fetch-failed" in out               # verdict still shown


def test_markdown_renders_fetch_failed_report_without_error():
    out = markdown.render(_failed_report())
    assert "Fetch failed" in out
    assert "## Badges" not in out
    assert "fetch-failed" in out
    assert "What was NOT checked" in out
