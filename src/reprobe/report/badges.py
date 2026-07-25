"""Declarative badge + verdict decisions. Rules come from config/badges.yaml so
a future chair tunes thresholds without code changes.

Stance: GRANT only Available (and only on an archival pin); PROPOSE Functional
as a candidate for a human; never auto-grant anything deeper. Harness-side
failures make NO statement about the artifact and are never folded into
artifact failures.
"""

from __future__ import annotations

from typing import Any

from ..models import DetectResult, FetchResult, RunResult

# badge status vocabulary: granted | candidate | not-met | not-evaluated
# verdict vocabulary: not-run | structural-only | runs | runs-with-warnings |
#   runs-with-failures | infra-error | nothing-executed
#   (fetch-failed is set by the orchestrator before any step exists)

_NO_STATEMENT = "no statement about the artifact"

# fallback only — config/badges.yaml fair.accessible.open_access_types wins
_OPEN_ACCESS_TYPES = ["zenodo", "git", "osf", "figshare", "dryad", "dataverse",
                      "software_heritage", "local"]


def decide(
    fetch: FetchResult,
    steps: list[RunResult],
    detect: DetectResult,
    *,
    badges_cfg: dict[str, Any],
    functional_requested: bool,
    ran: bool,
) -> dict[str, Any]:
    acm_cfg = (badges_cfg.get("acm") or {})
    avail_cfg = acm_cfg.get("available", {}) or {}
    func_cfg = acm_cfg.get("functional", {}) or {}

    archival = set(avail_cfg.get("archival_pin_kinds", ["version_doi", "swhid"]))

    # --- Available (no code execution) ---------------------------------
    notes: list[str] = []
    if fetch.pin.kind in archival:
        if avail_cfg.get("require_checksum_when_available", True) and not fetch.checksum_verified:
            available = "candidate"
            notes.append("archival pin present but checksums not verified")
        else:
            available = "granted"
    else:
        available = "candidate"
        if fetch.pin.kind == "git_sha":
            notes.append("pinned to a git commit, but not archival — deposit in Zenodo or archive via "
                         "Software Heritage to earn Artifact Available")
        elif fetch.anonymized:
            notes.append("anonymized review snapshot — needs a durable archival deposit before publication")
        else:
            notes.append("no archival persistent identifier found")

    # --- Functional (opt-in; candidate only) ---------------------------
    primary = _primary_steps(steps, detect)
    if not functional_requested or not ran or not primary:
        functional = "not-evaluated"
    else:
        mode = str(func_cfg.get("mode") or "candidate")
        if mode != "candidate":
            notes.append(f"badges.yaml functional.mode={mode} is not honored — Functional is never "
                         "auto-granted; reporting at most candidate")
        if all(s.status == "skipped" for s in primary):
            functional = "not-evaluated"
            notes.append(f"no step was executed (no matching runner) — {_NO_STATEMENT}")
        elif not _artifact_failure(primary) and any(_infra_error(s) for s in primary):
            functional = "not-evaluated"
            notes.append(f"harness/infrastructure error — {_NO_STATEMENT}")
        else:
            all_pass = all(s.status == "pass" for s in primary)
            passed = all_pass if func_cfg.get("require_primary_pass", True) \
                else any(s.status == "pass" for s in primary)
            produced = any(s.expected_met for s in primary)
            need_output = func_cfg.get("require_expected_output", True)
            if passed and (produced or not need_output or not _any_declared(detect)):
                functional = "candidate"          # always candidate -> a human confirms
                if not all_pass:
                    notes.append("not all steps passed (require_primary_pass: false) — review the failing steps")
                if not produced and _any_declared(detect):
                    notes.append("declared expected outputs were not produced (require_expected_output: false)")
            elif passed and need_output:
                functional = "not-met"
                notes.append("steps ran but no declared expected output was produced")
            else:
                functional = "not-met"

    # Pipeline-level completeness: per-step status stays honest for multi-step
    # pipelines (the manifest's outputs are broadcast onto every step), but the
    # human confirmer must see anything declared that NO step ever produced.
    if ran and functional_requested:
        declared = {o for d in detect.steps for o in d.expected_outputs}
        produced = {o for s in steps for o in s.expected_met}
        missing = sorted(declared - produced)
        if missing:
            notes.append(f"{len(missing)} declared output(s) never produced by any step: "
                         + ", ".join(missing))

    acm = {
        "available": available,
        "functional": functional,
        "results_reproduced": "not-evaluated",
        "notes": notes,
    }
    fair = _fair(fetch, detect, fair_cfg=(badges_cfg.get("fair") or {}), archival=archival)
    return {"acm": acm, "fair": fair}


def _primary_steps(steps: list[RunResult], detect: DetectResult) -> list[RunResult]:
    runnable = [s for s in steps if s.executed]   # host-only tiers (Unity T0) are structural, not functional
    primary_targets = {d.target for d in detect.steps if getattr(d, "primary", True)}
    gated = [s for s in runnable if s.target in primary_targets]
    return gated or runnable   # defensive: if targets don't line up, gate on everything


def _infra_error(s: RunResult) -> bool:
    # Runners flag harness-side failures (image missing, daemon down, ...) via
    # status "error" + diagnostics.harness_error; absent field = author code failed.
    diags = s.diagnostics if isinstance(s.diagnostics, dict) else {}
    return s.status == "error" and bool(diags.get("harness_error"))


def _artifact_failure(steps: list[RunResult]) -> bool:
    return any(s.status in ("fail", "timeout") or (s.status == "error" and not _infra_error(s))
               for s in steps)


def _any_declared(detect: DetectResult) -> bool:
    return any(s.expected_outputs for s in detect.steps)


def _fair(fetch: FetchResult, detect: DetectResult, *,
          fair_cfg: dict[str, Any], archival: set[str]) -> dict[str, Any]:
    find_cfg = fair_cfg.get("findable", {}) or {}
    acc_cfg = fair_cfg.get("accessible", {}) or {}
    inter_cfg = fair_cfg.get("interoperable", {}) or {}
    reu_cfg = fair_cfg.get("reusable", {}) or {}
    meta = fetch.metadata or {}

    if find_cfg.get("require_persistent_id", True):
        findable = fetch.pin.kind in archival    # same set as ACM Available — one config knob
    else:
        findable = fetch.pin.kind != "none"

    open_types = set(acc_cfg.get("open_access_types", _OPEN_ACCESS_TYPES))
    open_ok = fetch.resolved_type in open_types or bool(meta.get("openly_accessible"))
    if not acc_cfg.get("require_open_or_documented_access", True):
        open_ok = True                            # fetch succeeded, so it was retrievable
    accessible = open_ok and not fetch.anonymized

    formats = {str(f).lower().lstrip(".") for f in inter_cfg.get("prefer_standard_formats", [])}
    declared = [o for s in detect.steps for o in s.expected_outputs]
    if not declared or not formats:
        interoperable = "partial"                 # nothing declared to judge against
    else:
        std = sum(1 for o in declared if o.lower().rsplit(".", 1)[-1] in formats)
        interoperable = "yes" if std == len(declared) else "partial" if std else "no"

    hits = wants = 0
    if reu_cfg.get("reward_manifest", True):
        wants += 1
        hits += bool(detect.manifest_path)
    if reu_cfg.get("reward_open_license", True):
        wants += 1
        hits += bool(meta.get("license"))
    reusable = "yes" if wants and hits == wants else "partial" if hits else "no"

    return {
        "findable": bool(findable),
        "accessible": bool(accessible),
        "interoperable": interoperable,
        "reusable": reusable,
    }


def verdict(steps: list[RunResult], ran: bool) -> dict[str, Any]:
    if not ran:
        return {"overall": "not-run", "human_review_required": True}
    runnable = [s for s in steps if s.executed]
    statuses = [s.status for s in runnable]
    if not runnable:
        overall = "structural-only"
    elif all(s == "pass" for s in statuses):
        # Every step ran clean — but if outputs were declared and NOT ONE was
        # produced, the run is not clean. Steps carrying only broadcast outputs
        # are no longer marked "partial" individually (that denied healthy
        # pipelines the Functional candidate), so without this the pipeline-wide
        # miss would silently read as a green "runs" with no human review.
        declared = any(s.expected_met or s.diagnostics.get("expected_missing") for s in runnable)
        produced = any(s.expected_met for s in runnable)
        overall = "runs" if (produced or not declared) else "runs-with-warnings"
    elif _artifact_failure(runnable):
        overall = "runs-with-failures"
    elif any(_infra_error(s) for s in runnable):
        overall = "infra-error"                   # the harness failed, not the artifact
    elif all(s == "skipped" for s in statuses):
        overall = "nothing-executed"              # no runner matched any step
    else:
        overall = "runs-with-warnings"            # at least one pass/partial
    out: dict[str, Any] = {"overall": overall, "human_review_required": overall != "runs"}
    if overall == "infra-error":
        out["note"] = f"harness/infrastructure error — {_NO_STATEMENT}"
    elif overall == "nothing-executed":
        out["note"] = f"no step was executed (no matching runner) — {_NO_STATEMENT}"
    return out
