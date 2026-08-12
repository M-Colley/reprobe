"""Human-readable Markdown report."""

from __future__ import annotations

import re
from typing import Any

from ..models import Report

_BADGE = {"granted": "✅ granted", "candidate": "🟡 candidate (human review)",
          "not-met": "❌ not met", "not-evaluated": "— not evaluated"}


def _ml(s: Any) -> str:
    """Neutralize raw HTML in an untrusted free-text value (error text, warnings,
    notes, LLM output) so a crafted author string cannot inject an HTML tag if
    the Markdown is later viewed through a permissive renderer. Values the caller
    wraps in `inline code` already render literally and are left as-is."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render(r: Report) -> str:
    L: list[str] = []
    L.append(f"# reprobe report — {r.submission_id}")
    L.append("")
    L.append(f"*{r.harness_version} · {r.timestamp}*")
    L.append("")

    src = r.source
    L.append("## Source")
    L.append(f"- **Input:** `{src.get('input')}`")
    if src.get("error"):
        L.append(f"- ⚠️ **Fetch failed** — no code was run and nothing about the artifact "
                 f"was checked: {_ml(src['error'])}")
        for w in src.get("warnings", []) or []:
            L.append(f"  - ⚠️ {_ml(w)}")
        L.append("")
        L.append("## Verdict")
        L.append(f"**{r.verdict.get('overall')}** · human review required: "
                 f"{r.verdict.get('human_review_required')}")
        L.append("")
        L.append("## What was NOT checked")
        for x in sorted(set(r.not_verified)):
            L.append(f"- {_ml(x)}")
        L.append("")
        return "\n".join(L)
    L.append(f"- **Resolved:** {src.get('resolved_type')} · pin `{(src.get('pin') or {}).get('kind')}:"
             f"{(src.get('pin') or {}).get('value','')[:60]}`")
    L.append(f"- **Checksum verified:** {src.get('checksum_verified')} · **Anonymized:** {src.get('anonymized')}")
    for w in src.get("warnings", []) or []:
        L.append(f"  - ⚠️ {_ml(w)}")
    for d in src.get("data_sources", []) or []:
        pin = (d.get("pin") or {}).get("kind", "none")
        if d.get("status") == "ok":
            L.append(f"- **Data source:** `{_ml(str(d.get('input')))}` → "
                     f"`{d.get('into')}` · {d.get('resolved_type')} · pin `{pin}` · "
                     f"{d.get('files')} file(s)")
            for c in d.get("collisions") or []:
                L.append(f"  - ⚠️ not overwritten (the artifact already ships it): `{_ml(str(c))}`")
        else:
            # `error` when a fetch was attempted and failed; `detail` when the
            # deposit was only probed (the code half failed first, so nothing was
            # fetched) — that detail carries the embargo date and is the whole
            # point of probing.
            why = d.get("error") or d.get("detail") or ""
            L.append(f"- **Data source:** `{_ml(str(d.get('input')))}` — "
                     f"**{d.get('status')}**: {_ml(str(why))}")
    L.append("")

    det = r.detect or {}
    det_notes = det.get("notes") or []
    inventory = det.get("inventory") or {}
    types = det.get("artifact_types") or []
    if det_notes or inventory or types:
        L.append("## Detection")
        if types:
            L.append(f"- **Artifact types:** {', '.join(types)}")
        if inventory:
            L.append("- **Non-code files:** "
                     + " · ".join(f"{t} ×{n}" for t, n in sorted(inventory.items())))
        for n in det_notes:
            L.append(f"- ℹ️ {_ml(n)}")
        L.append("")

    env = r.environment
    L.append("## Environment")
    L.append(f"- **Strategy:** {env.get('strategy')} ({env.get('env_provenance')})")
    L.append(f"- **Image:** `{env.get('image')}`")
    for key, label in (("base_image_digest", "Image digest"), ("resolved_deps_digest", "Deps digest")):
        if env.get(key):
            L.append(f"- **{label}:** `{env[key]}`")
    for w in env.get("warnings", []) or []:
        L.append(f"  - ⚠️ {_ml(w)}")
    # Phase disclosures (dependency install, dataset download, runtime egress)
    # live here. They were previously written to the report but rendered by
    # nothing, hiding the "--allow-net downgrades badge confidence" statement
    # from the human-readable reports it exists to warn.
    for n in env.get("notes", []) or []:
        L.append(f"  - ℹ️ {_ml(n)}")
    L.append("")

    prov = getattr(r, "provenance", None)
    if prov:
        L.append("## Provenance")
        for k, v in prov.items():
            L.append(f"- **{k}:** `{v}`")
        L.append("")

    L.append("## Badges")
    acm = (r.badges.get("acm") or {})
    L.append(f"- **ACM Artifact Available:** {_BADGE.get(acm.get('available'), acm.get('available'))}")
    L.append(f"- **ACM Artifacts Evaluated – Functional:** {_BADGE.get(acm.get('functional'), acm.get('functional'))}")
    L.append(f"- **ACM Results Reproduced:** {_BADGE.get(acm.get('results_reproduced'), acm.get('results_reproduced'))}")
    for n in acm.get("notes", []) or []:
        L.append(f"  - ℹ️ {_ml(n)}")
    fair = (r.badges.get("fair") or {})
    L.append(f"- **FAIR:** findable={fair.get('findable')} · accessible={fair.get('accessible')} · "
             f"interoperable={fair.get('interoperable')} · reusable={fair.get('reusable')}")
    L.append("")

    # Copy-pasteable block for the chair's reply to the authors: every line
    # restates a machine-checked fact from above — no new claims.
    L.append("## Feedback for authors")
    fb: list[str] = []
    avail_line = {
        "granted": "The artifact is archivally deposited and was retrieved and verified — the ACM "
                   "*Artifact Available* criteria are met.",
        "candidate": "The artifact could be retrieved, but is not yet deposited under an archival "
                     "persistent identifier — the notes below say exactly what is still needed for "
                     "the *Available* badge.",
    }.get(acm.get("available"))
    if avail_line:
        fb.append(avail_line)
    func_line = {
        "candidate": "All declared analysis steps ran to completion in the harness sandbox; a human "
                     "reviewer will confirm the *Functional* badge.",
        "not-met": "The automated run did not complete as declared — see the step table in the full "
                   "report for what failed.",
    }.get(acm.get("functional"))
    if func_line:
        fb.append(func_line)
    fb += [str(n) for n in acm.get("notes") or []]
    fb += [str(w) for w in src.get("warnings") or []]
    for line in fb:
        L.append(f"- {_ml(line)}")
    if not fb:
        L.append("- Nothing actionable — see the full report above.")
    L.append("")

    L.append("## Steps")
    if not r.steps:
        L.append("_No runnable steps were executed._")
    for s in r.steps:
        head = f"### `{s.target}` — {s.status.upper()}"
        if s.tier_reached:
            head += f" (tier: {s.tier_reached})"
        L.append(head)
        L.append(f"- runner: `{s.runner}` · exit: {s.exit_code} · {s.duration_s}s")
        if s.diagnostics.get("harness_error"):
            L.append(f"- ⚠️ **harness error** — no statement about the artifact: "
                     f"{_ml(s.diagnostics['harness_error'])}")
        if s.claims:
            L.append("- **Verified claims:**")
            for c in s.claims:
                L.append(f"  - ✓ {c}")
        if s.not_verified:
            L.append("- **NOT verified:**")
            for c in s.not_verified:
                L.append(f"  - ✗ {c}")
        if s.expected_met:
            L.append(f"- **Expected outputs produced:** {', '.join(s.expected_met)}")
        adv = s.diagnostics.get("llm_advisory")
        if adv:
            L.append(f"- **LLM diagnosis** _(advisory, {r.llm.get('model', 'local LLM (model not recorded)')})_: "
                     f"{_ml(adv.get('likely_cause', ''))}")
            for fix in adv.get("suggested_fixes", []):
                L.append(f"  - 💡 {_ml(fix)}")
        hdiag = s.diagnostics.get("harness_diagnosis")
        if hdiag:
            # Deliberately labelled differently from the LLM advisory: this one is
            # a fact about the run, not a guess about its cause.
            L.append(f"- **Harness diagnosis** _(deterministic, not an LLM guess)_: "
                     f"{_ml(hdiag.get('likely_cause', ''))}")
            for fix in hdiag.get("suggested_fixes", []):
                L.append(f"  - 💡 {_ml(fix)}")
        if s.diagnostics.get("log_tail"):
            tail = str(s.diagnostics["log_tail"]).splitlines()[-15:]
            fence = _fence("\n".join(tail))   # author stdout must not close the fence
            L.append("- **Log tail:**")
            L.append(f"  {fence}")
            for line in tail:
                L.append(f"  {line}")
            L.append(f"  {fence}")
        L.append("")

    if r.unity:
        L.append("## Unity")
        for k, v in r.unity.items():
            L.append(f"- {_ml(k)}: {_ml(v)}")
        L.append("")

    rc = r.llm.get("results_check") or {}
    if rc:
        L.append("## Results vs the paper (LLM-advisory)")
        L.append(f"- **Paper:** {_ml(rc.get('ref', '—'))} ({_ml(rc.get('source', '—'))})")
        if rc.get("coverage"):
            L.append(f"- **Coverage:** {_ml(rc['coverage'])}")
        if rc.get("status") != "compared":
            L.append(f"- _not compared: {_ml(rc.get('detail', rc.get('status', 'unknown')))}_")
        else:
            counts = rc.get("counts") or {}
            L.append("- **Claims:** " + " · ".join(f"{v} {k}" for k, v in sorted(counts.items())))
            if rc.get("overall"):
                L.append(f"- **Overall:** {_ml(rc['overall'])}")
            L.append("")
            L.append("| Claim | Paper | Reproduced | Verdict |")
            L.append("|---|---|---|---|")
            _mark = {"match": "✓ match", "mismatch": "✗ MISMATCH",
                     "unclear": "? unclear", "not-reported": "– not reported"}
            for c in rc.get("claims", []):
                L.append(f"| {_ml(c.get('claim', ''))} | {_ml(c.get('paper_value', '—'))} "
                         f"| {_ml(c.get('produced_value', '—'))} "
                         f"| {_mark.get(c.get('verdict'), c.get('verdict', '?'))} |")
        for w in rc.get("warnings", []) or []:
            L.append(f"  - ⚠️ {_ml(w)}")
        L.append("")
        L.append("*Advisory only: this comparison is produced by a local LLM and is NOT a "
                 "verified fact. It never grants a badge — confirming that results match the "
                 "paper remains a human judgement.*")
        L.append("")

    if r.llm.get("summary"):
        L.append("## Summary (LLM-advisory)")
        L.append(f"> {_ml(r.llm['summary'])}")
        L.append(f"*— generated by {r.llm.get('model', 'local LLM (model not recorded)')}; "
                 "advisory only, not a verified fact._")
        L.append("")

    L.append("## What was NOT checked")
    nv = sorted({x for s in r.steps for x in s.not_verified} | set(r.not_verified))
    for x in nv:
        L.append(f"- {_ml(x)}")
    L.append("")

    L.append("## Verdict")
    L.append(f"**{r.verdict.get('overall')}** · human review required: {r.verdict.get('human_review_required')}")
    if r.verdict.get("note"):
        L.append(f"- ℹ️ {_ml(r.verdict['note'])}")
    L.append("")
    return "\n".join(L)


def _fence(text: str) -> str:
    # CommonMark: a closing fence must be >= the opener, so an opener longer
    # than any backtick run in the (untrusted) content cannot be closed by it.
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)
