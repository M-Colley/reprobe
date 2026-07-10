"""Bounded prompts for the three advisory LLM roles. The system prompt makes the
model's role explicit: it produces structured advice only; it has no ability to
run anything; uncertainty must be reported, not hidden.
"""

SYSTEM = (
    "You are an advisory assistant inside a reproducibility checker. You only "
    "produce structured JSON advice. You cannot run code, access the network, or "
    "change any decision. If unsure, say so via the confidence field. Never "
    "invent file paths that were not given to you."
)

DETECT_RUN_ORDER = """A research repository has these files (paths relative to repo root):
{tree}

README excerpt:
{readme}

The deterministic detector proposed this order:
{heuristic}

Propose the order in which the analysis files should be executed to reproduce
the results. Return JSON: {{"steps": [{{"path": "...", "why": "..."}}],
"confidence": 0.0-1.0, "uncertain": ["..."], "notes": "..."}}.
Only use paths from the list above."""

DIAGNOSE_FAILURE = """A reproduction step failed.

Step: {target}  ({kind})
Environment: {env}
Last lines of the log:
{log_tail}

Diagnose the most likely cause and suggest concrete fixes (e.g. a missing
package, a wrong path, a version mismatch). Return JSON:
{{"likely_cause": "...", "suggested_fixes": ["..."], "confidence": 0.0-1.0,
"is_advisory": true}}."""

SUMMARIZE = """Here is a finished reproducibility report (facts already computed by
the harness):
{report}

Write a short, plain-language paragraph for the Open Data chair summarizing what
was checked, what ran, and what was explicitly not verified. Do not invent
results or change any badge. Return JSON: {{"summary": "..."}}."""
