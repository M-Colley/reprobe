"""Human-readable Markdown report."""

from __future__ import annotations

import re

from ..models import Report

_BADGE = {"granted": "✅ granted", "candidate": "🟡 candidate (human review)",
          "not-met": "❌ not met", "not-evaluated": "— not evaluated"}


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
                 f"was checked: {src['error']}")
        for w in src.get("warnings", []) or []:
            L.append(f"  - ⚠️ {w}")
        L.append("")
        L.append("## Verdict")
        L.append(f"**{r.verdict.get('overall')}** · human review required: "
                 f"{r.verdict.get('human_review_required')}")
        L.append("")
        L.append("## What was NOT checked")
        for x in sorted(set(r.not_verified)):
            L.append(f"- {x}")
        L.append("")
        return "\n".join(L)
    L.append(f"- **Resolved:** {src.get('resolved_type')} · pin `{(src.get('pin') or {}).get('kind')}:"
             f"{(src.get('pin') or {}).get('value','')[:60]}`")
    L.append(f"- **Checksum verified:** {src.get('checksum_verified')} · **Anonymized:** {src.get('anonymized')}")
    for w in src.get("warnings", []) or []:
        L.append(f"  - ⚠️ {w}")
    L.append("")

    det_notes = (r.detect or {}).get("notes") or []
    if det_notes:
        L.append("## Detection")
        for n in det_notes:
            L.append(f"- ℹ️ {n}")
        L.append("")

    env = r.environment
    L.append("## Environment")
    L.append(f"- **Strategy:** {env.get('strategy')} ({env.get('env_provenance')})")
    L.append(f"- **Image:** `{env.get('image')}`")
    for key, label in (("base_image_digest", "Image digest"), ("resolved_deps_digest", "Deps digest")):
        if env.get(key):
            L.append(f"- **{label}:** `{env[key]}`")
    for w in env.get("warnings", []) or []:
        L.append(f"  - ⚠️ {w}")
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
        L.append(f"  - ℹ️ {n}")
    fair = (r.badges.get("fair") or {})
    L.append(f"- **FAIR:** findable={fair.get('findable')} · accessible={fair.get('accessible')} · "
             f"interoperable={fair.get('interoperable')} · reusable={fair.get('reusable')}")
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
                     f"{s.diagnostics['harness_error']}")
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
                     f"{adv.get('likely_cause', '')}")
            for fix in adv.get("suggested_fixes", []):
                L.append(f"  - 💡 {fix}")
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
            L.append(f"- {k}: {v}")
        L.append("")

    if r.llm.get("summary"):
        L.append("## Summary (LLM-advisory)")
        L.append(f"> {r.llm['summary']}")
        L.append(f"*— generated by {r.llm.get('model', 'local LLM (model not recorded)')}; "
                 "advisory only, not a verified fact._")
        L.append("")

    L.append("## What was NOT checked")
    nv = sorted({x for s in r.steps for x in s.not_verified} | set(r.not_verified))
    for x in nv:
        L.append(f"- {x}")
    L.append("")

    L.append("## Verdict")
    L.append(f"**{r.verdict.get('overall')}** · human review required: {r.verdict.get('human_review_required')}")
    if r.verdict.get("note"):
        L.append(f"- ℹ️ {r.verdict['note']}")
    L.append("")
    return "\n".join(L)


def _fence(text: str) -> str:
    # CommonMark: a closing fence must be >= the opener, so an opener longer
    # than any backtick run in the (untrusted) content cannot be closed by it.
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)
