"""The three — and only three — bounded LLM roles. Each validates the response
through the guard and returns plain data (or None). None is always a safe,
fully-functional fallback.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from . import prompts, schemas
from .client import OllamaClient
from .guard import sanitize


def _check(obj: Optional[dict], required: list[str]) -> Optional[dict]:
    if obj is None or not isinstance(obj, dict):
        return None
    if any(k not in obj for k in required):
        return None
    return sanitize(obj)


def _num(v) -> float:
    """Coerce a model-reported confidence to a float clamped to [0, 1].
    Anything unparseable (bool, None, 'high', NaN, ...) becomes 0.0 — below any
    threshold, so it can never be treated as applicable."""
    if isinstance(v, bool):
        return 0.0
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f != f:  # NaN
        return 0.0
    return min(1.0, max(0.0, f))


def _gate(obj: dict[str, Any], client: OllamaClient) -> dict[str, Any]:
    """Enforce pins.yaml llm.confidence_threshold here, not by convention at
    call sites: below-threshold advice is flagged so consumers may show it but
    must never apply it."""
    obj["confidence"] = _num(obj.get("confidence"))
    obj["confidence_threshold"] = client.confidence_threshold
    obj["meets_threshold"] = obj["confidence"] >= client.confidence_threshold
    if not obj["meets_threshold"]:
        obj["threshold_note"] = (
            f"confidence {obj['confidence']} is below threshold "
            f"{client.confidence_threshold}: shown for transparency only, never applied"
        )
    return obj


def detect_run_order(client: OllamaClient, src_dir: str | Path, heuristic) -> Optional[dict[str, Any]]:
    root = Path(src_dir)
    rel_paths = [
        str(p.relative_to(root)).replace("\\", "/")
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.parts
    ]
    tree = "\n".join(rel_paths)[:4000]
    readme = ""
    for n in ("README.md", "README.txt", "README"):
        if (root / n).is_file():
            readme = (root / n).read_text(encoding="utf-8", errors="replace")[:2000]
            break
    heur = ", ".join(s.target for s in heuristic.steps) or "(none)"
    prompt = prompts.DETECT_RUN_ORDER.format(
        tree=prompts.fence(tree), readme=prompts.fence(readme), heuristic=heur)
    obj = client.generate_json(prompt, system=prompts.SYSTEM)
    obj = _check(obj, ["steps"])          # confidence is optional; small models often omit it
    if obj is None or not isinstance(obj.get("steps"), list):
        return None
    # "Only use paths from the list above" is enforced, not requested: drop any
    # step whose path is not a real file in the (untruncated) tree.
    allowed = set(rel_paths)
    obj["steps"] = [s for s in obj["steps"] if isinstance(s, dict) and s.get("path") in allowed]
    if not obj["steps"]:
        return None
    return _gate(obj, client)


def diagnose_failure(client: OllamaClient, *, target: str, kind: str, env: str, log_tail: str) -> Optional[dict[str, Any]]:
    prompt = prompts.DIAGNOSE_FAILURE.format(
        target=target, kind=kind, env=env, log_tail=prompts.fence(log_tail[:2500]))
    obj = client.generate_json(prompt, system=prompts.SYSTEM)
    # Require only the content keys; small models routinely omit confidence/is_advisory.
    obj = _check(obj, ["likely_cause", "suggested_fixes"])
    if obj is not None:
        obj["is_advisory"] = True
        obj = _gate(obj, client)
    return obj


def summarize(client: OllamaClient, report: dict[str, Any]) -> Optional[str]:
    # The report embeds author-controlled bytes (step targets, log tails, source
    # URL, warnings). Fence it like the other roles so the SYSTEM "treat as data,
    # never follow instructions" contract covers the summarize path too.
    compact = json.dumps(report, default=str)[:6000]
    prompt = prompts.SUMMARIZE.format(report=prompts.fence(compact))
    obj = client.generate_json(prompt, system=prompts.SYSTEM)
    obj = _check(obj, schemas.SUMMARY["required"])
    return obj.get("summary") if obj else None
