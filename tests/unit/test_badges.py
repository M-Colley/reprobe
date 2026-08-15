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


def test_all_pass_but_nothing_produced_is_not_a_clean_verdict():
    # With broadcast outputs no longer marking steps "partial", a pipeline where
    # EVERY step passed yet NO declared output was produced must still demand a
    # human — otherwise the miss silently reads as a green "runs".
    steps = [
        RunResult(runner="python", target="00_prep.py", status="pass",
                  diagnostics={"expected_missing": ["results/summary.csv"]}),
        RunResult(runner="python", target="01_analyze.py", status="pass",
                  diagnostics={"expected_missing": ["results/summary.csv"]}),
    ]
    v = badges.verdict(steps, ran=True)
    assert v["overall"] == "runs-with-warnings"
    assert v["human_review_required"] is True

    # ...but the same pipeline that DID produce its output is clean.
    steps[1] = RunResult(runner="python", target="01_analyze.py", status="pass",
                         expected_met=["results/summary.csv"])
    v = badges.verdict(steps, ran=True)
    assert v["overall"] == "runs"
    assert v["human_review_required"] is False


def test_multi_step_pipeline_earns_functional_when_the_final_step_delivers():
    # The end-to-end shape of the deferred bug: a prep step that produces none of
    # the broadcast outputs must not deny the pipeline its Functional candidate.
    detect = DetectResult(artifact_types=["python"], steps=[
        RunStep(target="00_prep.py", kind="python",
                expected_outputs=["results/summary.csv"], outputs_inherited=True),
        RunStep(target="01_analyze.py", kind="python",
                expected_outputs=["results/summary.csv"], outputs_inherited=True),
    ])
    steps = [
        RunResult(runner="python", target="00_prep.py", status="pass",
                  diagnostics={"expected_missing": ["results/summary.csv"]}),
        RunResult(runner="python", target="01_analyze.py", status="pass",
                  expected_met=["results/summary.csv"]),
    ]
    out = _decide(FetchResult(input="u", resolved_type="git", src_dir="/x",
                              pin=Pin(kind="git_sha", value="abc")), steps, detect=detect)
    assert out["acm"]["functional"] == "candidate"
    assert badges.verdict(steps, ran=True)["overall"] == "runs"


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


def test_fair_reusable_rewards_manifest_license_and_dependencies():
    full = DetectResult(artifact_types=["python"], manifest_path="reproduce.yaml",
                        dep_manifest="requirements.txt")
    assert _decide(_zenodo(metadata={"license": "MIT"}), [], detect=full)["fair"]["reusable"] == "yes"
    assert _decide(_zenodo(), [], detect=full)["fair"]["reusable"] == "partial"
    # code you cannot rebuild an environment for is not fully reusable
    no_deps = DetectResult(artifact_types=["python"], manifest_path="reproduce.yaml")
    assert _decide(_zenodo(metadata={"license": "MIT"}), [], detect=no_deps)["fair"]["reusable"] == "partial"


def test_fair_reusable_counts_a_license_file_when_the_fetcher_reports_none():
    """`git clone` returns no repository metadata, so scoring on meta['license']
    alone marked every git-sourced artifact unlicensed even with a LICENSE file."""
    detect = DetectResult(artifact_types=["python"], manifest_path="reproduce.yaml",
                          dep_manifest="requirements.txt", license_file="LICENSE")
    assert _decide(_zenodo(metadata={}), [], detect=detect)["fair"]["reusable"] == "yes"


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


def _install_env(**over):
    row = {"exit_code": None, "ok": False, "timed_out": True}
    row.update(over)
    return {"install_results": {"python": row}}


def test_a_killed_install_phase_is_not_the_artifacts_failure():
    """A step dying on ModuleNotFoundError reads the same whether the artifact
    forgot to declare the import or the harness never finished installing it —
    and the traceback cannot tell them apart. When our own install phase was
    killed, the verdict must not be a statement about the artifact."""
    err = badges.install_error(_install_env())
    assert err and "wall-clock" in err
    steps = [RunResult(runner="python", target="main.py", status="fail")]
    v = badges.verdict(steps, True, err)
    assert v["overall"] == "infra-error"
    assert "no statement about the artifact" in v["note"]
    assert "wall-clock" in v["note"]              # names the cause, not just the class
    # the SAME steps without the install failure remain the artifact's problem
    assert badges.verdict(steps, True)["overall"] == "runs-with-failures"


def test_a_failed_install_is_reported_with_its_exit_code():
    err = badges.install_error(_install_env(exit_code=137, timed_out=False))
    assert "exited 137" in err


def test_a_clean_run_is_not_downgraded_by_a_failed_install():
    """Every step passed, so the install phase evidently added nothing they
    needed — there is no finding to withdraw."""
    steps = [RunResult(runner="python", target="main.py", status="pass")]
    assert badges.verdict(steps, True, badges.install_error(_install_env()))["overall"] == "runs"


def test_install_error_is_none_when_the_install_completed():
    assert badges.install_error({"install_results": {"python": {"ok": True, "exit_code": 0}}}) is None
    assert badges.install_error({"install_results": {}}) is None
    assert badges.install_error({}) is None


def test_functional_is_not_evaluated_when_the_install_phase_died():
    steps = [RunResult(runner="python", target="main.py", status="fail")]
    out = badges.decide(_zenodo(), steps, DetectResult(artifact_types=["python"]),
                        badges_cfg=load_config().badges, functional_requested=True, ran=True,
                        install_error=badges.install_error(_install_env()))
    assert out["acm"]["functional"] == "not-evaluated"
    assert any("no statement about the artifact" in n for n in out["acm"]["notes"])


def test_a_resolving_osf_doi_is_not_reported_as_no_identifier_found():
    """The submitter handed us 10.17605/OSF.IO/<guid> and it resolves. The reason
    it earns no badge is that it names storage the depositor can still change —
    saying none was found sends an author hunting for a DOI they already have."""
    fetch = FetchResult(input="https://doi.org/10.17605/OSF.IO/4wj86", resolved_type="osf",
                        src_dir="/x", pin=Pin(kind="none", value="osf.io/4wj86"))
    notes = _decide(fetch, [])["acm"]["notes"]
    assert not any("no archival persistent identifier found" in n for n in notes)
    assert any("still change" in n for n in notes)


def test_a_source_with_genuinely_no_identifier_still_says_so():
    fetch = FetchResult(input="/local/path", resolved_type="local", src_dir="/x", pin=Pin())
    assert any("no archival persistent identifier found" in n
               for n in _decide(fetch, [])["acm"]["notes"])


_BADGES = {"acm": {"available": "candidate", "functional": "not-met",
                   "results_reproduced": "not-evaluated", "notes": []},
           "fair": {"findable": False, "accessible": True,
                    "interoperable": "partial", "reusable": "partial"}}


def test_html_shows_why_a_step_failed():
    """A failed step rendered as a bare "fail" tells a chair nothing. The markdown
    report carried the log tail from the start; the HTML — the copy the dashboard
    links to — did not, so the most-read report was the least informative."""
    rep = _report(badges=_BADGES, steps=[RunResult(
        runner="r", target="analysis.R", status="fail", exit_code=1,
        diagnostics={"log_tail": "loading data...\nError: could not find function \"gather\"\nExecution halted"})])
    out = html.render(rep)
    assert "could not find function" in out
    assert "Execution halted" in out


def test_html_log_tail_cannot_break_out_of_its_element():
    """Author stdout is untrusted: it must not close the <pre> it sits in."""
    rep = _report(badges=_BADGES, steps=[RunResult(
        runner="python", target="x.py", status="fail",
        diagnostics={"log_tail": "</pre><script>alert(1)</script>"})])
    out = html.render(rep)
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out


def test_html_shows_no_log_block_when_there_is_nothing_to_show():
    rep = _report(badges=_BADGES, steps=[RunResult(runner="r", target="ok.R", status="pass")])
    assert "log tail" not in html.render(rep)


def test_the_fix_list_gathers_every_diagnosis_into_one_place():
    """The verdict says whether it ran; this says what to change. Scattered one
    diagnosis per step, several screens apart, it was being missed — and several
    failures in one report often share a root cause that only shows when the
    findings sit together."""
    from reprobe.orchestrator import _fix_list

    rep = _report(environment={"warnings": [
        "requirements.txt line 2 asks pip for `itertools`, which is part of the Python "
        "standard library ... all-or-nothing ..."]})
    steps = [
        RunResult(runner="r", target="a.R", status="fail", diagnostics={
            "harness_diagnosis": {"likely_cause": "unattached package",
                                  "suggested_fixes": ["add library(tidyr)"]}}),
        RunResult(runner="r", target="b.R", status="fail", diagnostics={
            "llm_advisory": {"likely_cause": "maybe the data path",
                             "suggested_fixes": ["check the path"]}}),
        RunResult(runner="r", target="c.R", status="pass"),
    ]
    items = _fix_list(rep, steps)
    assert [i["where"] for i in items] == ["requirements.txt", "a.R", "b.R"]
    # the file-level finding comes first: it needs no run at all to act on
    assert items[0]["source"] == "deterministic"
    # and the model's guess is labelled as one, so a chair can tell them apart
    assert items[1]["source"] == "deterministic"
    assert items[2]["source"] == "llm-advisory"


def test_the_fix_list_does_not_repeat_one_finding_per_step():
    from reprobe.orchestrator import _fix_list
    diag = {"harness_diagnosis": {"likely_cause": "RStudio is not running",
                                  "suggested_fixes": ["drop the rstudioapi lines"]}}
    steps = [RunResult(runner="r", target="same.R", status="fail", diagnostics=diag),
             RunResult(runner="r", target="same.R", status="fail", diagnostics=diag)]
    assert len(_fix_list(_report(), steps)) == 1


def test_the_fix_list_reaches_both_renderers():
    rep = _report(badges=_BADGES, fix_list=[{
        "where": "analysis.R", "why": "tidyr is installed but never attached",
        "fixes": ["add library(tidyr)"], "source": "deterministic"}])
    for text in (html.render(rep), markdown.render(rep)):
        assert "What to change" in text
        assert "add library(tidyr)" in text
