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


def _num(v, default=None):
    return v if isinstance(v, (int, float)) else default


def detect_run_order(client: OllamaClient, src_dir: str | Path, heuristic) -> Optional[dict[str, Any]]:
    root = Path(src_dir)
    tree = "\n".join(
        str(p.relative_to(root)).replace("\\", "/")
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.parts
    )[:4000]
    readme = ""
    for n in ("README.md", "README.txt", "README"):
        if (root / n).is_file():
            readme = (root / n).read_text(encoding="utf-8", errors="replace")[:2000]
            break
    heur = ", ".join(s.target for s in heuristic.steps) or "(none)"
    prompt = prompts.DETECT_RUN_ORDER.format(tree=tree, readme=readme, heuristic=heur)
    obj = client.generate_json(prompt, system=prompts.SYSTEM)
    obj = _check(obj, ["steps"])          # confidence is optional; small models often omit it
    if obj is not None:
        obj.setdefault("confidence", _num(obj.get("confidence")))
    return obj


def diagnose_failure(client: OllamaClient, *, target: str, kind: str, env: str, log_tail: str) -> Optional[dict[str, Any]]:
    prompt = prompts.DIAGNOSE_FAILURE.format(target=target, kind=kind, env=env, log_tail=log_tail[:2500])
    obj = client.generate_json(prompt, system=prompts.SYSTEM)
    # Require only the content keys; small models routinely omit confidence/is_advisory.
    obj = _check(obj, ["likely_cause", "suggested_fixes"])
    if obj is not None:
        obj["is_advisory"] = True
        obj["confidence"] = _num(obj.get("confidence"))
    return obj


def summarize(client: OllamaClient, report: dict[str, Any]) -> Optional[str]:
    compact = json.dumps(report, default=str)[:6000]
    prompt = prompts.SUMMARIZE.format(report=compact)
    obj = client.generate_json(prompt, system=prompts.SYSTEM)
    obj = _check(obj, schemas.SUMMARY["required"])
    return obj.get("summary") if obj else None
