"""Declarative badge + verdict decisions. Rules come from config/badges.yaml so
a future chair tunes thresholds without code changes.

Stance: GRANT only Available (and only on an archival pin); PROPOSE Functional
as a candidate for a human; never auto-grant anything deeper.
"""

from __future__ import annotations

from typing import Any

from ..models import DetectResult, FetchResult, RunResult

# status vocabulary: granted | candidate | not-met | not-evaluated


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
    notes = []
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
    primary = [s for s in steps if s.runner != "unity"]   # unity T0 is structural, not functional
    declared_outputs = any(s.expected_met or _declared(detect, s) for s in primary)
    if not functional_requested or not ran:
        functional = "not-evaluated"
    elif not primary:
        functional = "not-evaluated"
    else:
        all_pass = all(s.status == "pass" for s in primary)
        produced = any(s.expected_met for s in primary)
        need_output = func_cfg.get("require_expected_output", True)
        if all_pass and (produced or not _any_declared(detect)):
            functional = "candidate"          # mode is candidate -> a human confirms
        elif all_pass and need_output:
            functional = "not-met"
            notes.append("steps ran but no declared expected output was produced")
        else:
            functional = "not-met"

    acm = {
        "available": available,
        "functional": functional,
        "results_reproduced": "not-evaluated",
        "notes": notes,
    }
    fair = _fair(fetch, detect)
    return {"acm": acm, "fair": fair}


def _declared(detect: DetectResult, step: RunResult) -> bool:
    return False  # placeholder for per-step expected lookup (kept simple)


def _any_declared(detect: DetectResult) -> bool:
    return any(s.expected_outputs for s in detect.steps)


def _fair(fetch: FetchResult, detect: DetectResult) -> dict[str, Any]:
    findable = fetch.pin.kind in ("version_doi", "swhid")
    accessible = fetch.resolved_type in ("zenodo", "git", "osf", "figshare", "local") and not fetch.anonymized
    interoperable = "partial"
    reusable = "partial" if detect.manifest_path else "no"
    return {
        "findable": bool(findable),
        "accessible": bool(accessible),
        "interoperable": interoperable,
        "reusable": reusable,
    }


def verdict(steps: list[RunResult], ran: bool) -> dict[str, Any]:
    if not ran:
        return {"overall": "not-run", "human_review_required": True}
    runnable = [s for s in steps if s.runner != "unity"]
    statuses = [s.status for s in runnable]
    if not runnable:
        overall = "structural-only"
    elif all(s == "pass" for s in statuses):
        overall = "runs"
    elif any(s in ("fail", "error", "timeout") for s in statuses):
        overall = "runs-with-failures"
    else:
        overall = "runs-with-warnings"
    return {"overall": overall, "human_review_required": overall != "runs"}
