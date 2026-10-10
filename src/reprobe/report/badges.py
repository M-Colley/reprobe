"""Declarative badge + verdict decisions. Rules come from config/badges.yaml so
a future chair tunes thresholds without code changes.

Stance: GRANT only Available (and only on an archival pin); PROPOSE Functional
as a candidate for a human; never auto-grant anything deeper. Harness-side
failures make NO statement about the artifact and are never folded into
artifact failures.
"""

from __future__ import annotations

from typing import Any, Optional

from ..models import DetectResult, FetchResult, RunResult

# badge status vocabulary: granted | candidate | not-met | not-evaluated
# verdict vocabulary: not-run | structural-only | runs | runs-with-warnings |
#   runs-with-failures | resource-limit | infra-error | nothing-executed
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
    install_error: Optional[str] = None,
    declared_env_error: Optional[str] = None,
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
        elif fetch.resolved_type == "osf" and (fetch.metadata or {}).get("doi"):
            # "none found" is false when the submitter handed us a DOI that
            # resolves: 10.17605/OSF.IO/<guid> exists, it simply points at
            # storage the depositor can still change. Saying it was not found
            # sends an author looking for a DOI they already have.
            notes.append("an OSF DOI resolves, but it points at project storage the depositor can "
                         "still change — register the project (or deposit in Zenodo) so the "
                         "identifier names fixed bytes")
        elif fetch.resolved_type == "osf":
            # A plain osf.io/<guid> project has no DOI at all — saying "an OSF DOI
            # resolves" told the author they already had one.
            notes.append("an OSF project is storage the depositor can still change and has no DOI — "
                         "register it (which mints a DOI for a frozen copy) or deposit in Zenodo")
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
        elif declared_env_error and not all(s.status == "pass" for s in primary) \
                and not any(_infra_error(s) for s in primary):
            # The artifact's OWN dependency declaration did not install. That is a
            # finding about the artifact, and the step failures follow from it.
            functional = "not-met"
            notes.append(declared_env_error)
        elif install_error and not all(s.status == "pass" for s in primary):
            # A step can only be judged against an environment the harness finished
            # building. Scoring it against a half-built one grades the artifact on
            # our failure — see install_error() for why this outranks the shape of
            # the step's own traceback.
            functional = "not-evaluated"
            notes.append(f"{install_error} — {_NO_STATEMENT}")
        elif not _artifact_failure(primary) and any(_infra_error(s) for s in primary):
            functional = "not-evaluated"
            notes.append(f"harness/infrastructure error — {_NO_STATEMENT}")
        elif not _artifact_failure(primary) and any(_budget(s) for s in primary):
            functional = "not-evaluated"
            notes.append(f"{_budget_note(primary)} — {_NO_STATEMENT} beyond that")
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

    if declared_env_error and functional == "candidate":
        notes.append(f"{declared_env_error}; the steps passed anyway, against the harness's base "
                     "image alone")

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
    return any(s.status == "fail" or (s.status == "error" and not _infra_error(s))
               for s in steps)


def _budget(s: RunResult) -> bool:
    # The sandbox's budget ran out (wall clock, memory cap, output or disk). That
    # says the analysis needs more than the review envelope, not that it is wrong:
    # PDRA_XAI_OS's CatBoost notebook is a working fit that takes longer than 90 min.
    return s.status in ("timeout", "resource-limit")


def _budget_note(steps: list[RunResult]) -> str:
    parts = []
    for s in steps:
        if s.status == "timeout":
            parts.append(f"{s.target}: stopped at the {s.diagnostics.get('timeout_s')}s time budget")
        elif s.status == "resource-limit":
            parts.append(f"{s.target}: stopped at the {s.diagnostics.get('limit')} cap "
                         f"({s.diagnostics.get('cap')})")
    return ("did not finish within the review sandbox's budget (" + "; ".join(parts)
            + ") — re-run with a larger budget (--timeout, or limits.yaml)")


def _unexecuted(steps: list[RunResult]) -> list[RunResult]:
    """Planned steps that produced no result about the code: no runner matched,
    or a host-only structural check failed. Invisible to a verdict computed from
    executed steps only, which then read "all declared steps ran"."""
    return [s for s in steps if not s.executed and s.status in ("skipped", "fail", "error")]


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
        # meta["license"] only exists for fetchers that return repository
        # metadata; a git clone returns none, so scoring on it alone marked every
        # git-sourced artifact unlicensed even with a LICENSE file at the root.
        # The file on disk is the stronger, always-available evidence.
        hits += bool(meta.get("license") or detect.license_file)
    if reu_cfg.get("reward_dependency_manifest", True):
        # For reusability this is the load-bearing one: code you cannot rebuild an
        # environment for is not reusable, whatever else it ships.
        wants += 1
        hits += bool(detect.dep_manifest)
    reusable = "yes" if wants and hits == wants else "partial" if hits else "no"

    return {
        "findable": bool(findable),
        "accessible": bool(accessible),
        "interoperable": interoperable,
        "reusable": reusable,
    }


#: Phrases a package manager prints when the network, not the package, failed.
_NETWORK_MARKERS = ("temporary failure in name resolution", "connection reset", "read timed out",
                    "connectionerror", "max retries exceeded", "could not resolve host",
                    "network is unreachable", "http error 50", "status code: 50")


def _failures(environment: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    results = (environment or {}).get("install_results") or {}
    return [(k, v) for k, v in sorted(results.items()) if isinstance(v, dict) and not v.get("ok")]


def _stopped_for_another_reason(row: dict[str, Any]) -> bool:
    """The install container ended for a reason that is not "a command failed": the
    clock, a memory/output/disk cap, the daemon, the network, or for no recorded
    reason at all. Whatever else the log shows, none of that is the author's doing."""
    return bool(row.get("timed_out") or row.get("error") or row.get("network_error")
                or row.get("limit_exceeded") or row.get("oom_killed") or row.get("unattributed"))


def declared_env_error(environment: dict[str, Any]) -> Optional[str]:
    """Why the artifact's OWN declared environment did not install, if it didn't.

    Distinct from install_error(): a requirements.txt naming `itertools` (stdlib,
    not on PyPI) or an unsatisfiable pin fails because of what the author wrote.
    Calling that an infrastructure error contradicted the same report's lint of
    the file, and made `batch --resume` retry it forever. Network trouble during
    the install is never pinned on the author."""
    why = []
    for k, v in _failures(environment):
        if _stopped_for_another_reason(v):
            continue
        for f in v.get("failures") or []:
            if f.get("origin") == "author-declared":
                what = f.get("what") or f"`{f.get('command', '')[:120]}`"
                why.append(f"{k}: {what} exited {f.get('exit_code')}")
    if not why:
        return None
    # "may", not "do": the steps ran without the environment, so their results are
    # unreliable — but whether each failure CAUSED by that is not something known
    return ("the artifact's declared environment does not install (" + "; ".join(why)
            + ") — the steps ran without it, so their results may follow from that")


def install_error(environment: dict[str, Any]) -> Optional[str]:
    """Why the harness's own dependency install did not complete, if it didn't.

    No author code runs in that container — it is reprobe installing what the
    manifest declares and what the import scan detected, under sanctioned egress.
    So when it is killed, the step that then dies on ModuleNotFoundError is
    reporting OUR missing install, and calling that runs-with-failures publishes a
    false statement about someone's artifact. The failure is also invisible from
    the step's own traceback, which looks exactly like an undeclared dependency —
    the very defect the install phase exists to distinguish it from."""
    why = []
    for k, v in _failures(environment):
        if v.get("timed_out"):
            why.append(f"{k}: killed at the install-phase wall-clock limit")
        elif v.get("error"):
            why.append(f"{k}: {v['error']}")
        elif v.get("limit_exceeded"):
            why.append(f"{k}: stopped by the {v['limit_exceeded']} cap")
        elif v.get("oom_killed"):
            why.append(f"{k}: killed at the memory cap")
        elif v.get("network_error"):
            why.append(f"{k}: the package index was unreachable mid-install (exit {v.get('exit_code')})")
        elif v.get("unattributed"):
            # non-zero exit and no command recorded as failing: the container (or its
            # shell) died, which no author file can explain
            why.append(f"{k}: the install container exited {v.get('exit_code')} with no failing "
                       "command recorded")
        else:
            failures = v.get("failures")
            if failures is None:
                # No per-command attribution (older plan): the whole group is ours.
                why.append(f"{k}: exited {v.get('exit_code')}")
                continue
            # ours is ours even when the author's own file failed as well
            ours = [f for f in failures if f.get("origin") not in ("author-declared", "harness-record")]
            why.extend(f"{k}: {f.get('what') or '`' + f.get('command', '')[:120] + '`'} "
                       f"exited {f.get('exit_code')}" for f in ours)
    if not why:
        return None
    return f"the harness's dependency install did not complete ({'; '.join(why)})"


def verdict(steps: list[RunResult], ran: bool,
            install_error: Optional[str] = None,
            declared_env_error: Optional[str] = None,
            dry_run: bool = False) -> dict[str, Any]:
    if dry_run:
        return {"overall": "not-run", "human_review_required": True,
                "note": f"dry run — no container was launched; {_NO_STATEMENT}"}
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
    elif any(_budget(s) for s in runnable):
        overall = "resource-limit"                # the sandbox's budget ran out, not the code
    elif all(s == "skipped" for s in statuses):
        overall = "nothing-executed"              # no runner matched any step
    else:
        overall = "runs-with-warnings"            # at least one pass/partial
    # An unfinished install outranks the shape of the failure it caused: the step
    # failed against an environment we never finished building. Steps that all
    # passed are left alone — the install phase evidently added nothing they
    # needed, so there is nothing to withdraw. The artifact's own declaration
    # failing is the opposite case: that IS the finding, so it is left standing.
    if install_error and not declared_env_error \
            and overall in ("runs-with-failures", "runs-with-warnings", "resource-limit"):
        overall = "infra-error"
    if overall == "runs" and declared_env_error:
        # every step passed, but only against the harness's base image: the
        # environment the artifact declared did not install. Not a clean run.
        overall = "runs-with-warnings"
    unrun = _unexecuted(steps)
    if overall == "runs" and unrun:
        overall = "runs-with-warnings"
    out: dict[str, Any] = {"overall": overall, "human_review_required": overall != "runs"}
    notes = []
    if overall == "infra-error":
        detail = f" ({install_error})" if install_error else ""
        notes.append(f"harness/infrastructure error{detail} — {_NO_STATEMENT}")
    elif overall == "nothing-executed":
        notes.append(f"no step was executed (no matching runner) — {_NO_STATEMENT}")
    elif overall == "resource-limit":
        # "no statement beyond that" would contradict a declared-environment
        # failure reported in the same note: that IS a statement about the artifact
        tail = "" if declared_env_error else f" — {_NO_STATEMENT} beyond that"
        notes.append(f"{_budget_note(runnable)}{tail}")
    if declared_env_error and overall != "infra-error":
        notes.append(declared_env_error)
        if overall == "runs-with-warnings" and all(s == "pass" for s in statuses):
            notes.append("the steps passed anyway — against the harness's base image alone, "
                         "which proves nothing about the environment the artifact declares")
        if install_error:
            notes.append(f"in addition, {install_error}")
    if unrun and overall not in ("infra-error", "nothing-executed"):
        notes.append(f"{len(unrun)} planned step(s) produced no result about the code: "
                     + ", ".join(f"{s.target} ({s.status}: "
                                 f"{s.diagnostics.get('reason') or 'structural check'})" for s in unrun))
    if notes:
        out["note"] = " · ".join(notes)
    return out
