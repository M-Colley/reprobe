"""The four — and only four — bounded LLM roles. Each validates the response
through the guard and returns plain data (or None). None is always a safe,
fully-functional fallback.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Optional

from jsonschema import Draft202012Validator

from . import prompts, schemas
from .client import OllamaClient
from .guard import sanitize


def _valid(obj: Any, schema: dict[str, Any]) -> bool:
    return isinstance(obj, dict) and not any(Draft202012Validator(schema).iter_errors(obj))


def _check(obj: Optional[dict], schema: dict[str, Any]) -> Optional[dict]:
    """Schema-valid and guard-clean, or None (= no advice, the safe fallback).
    An unparseable confidence ("high") is coerced to 0.0 first rather than
    voiding the advice: below threshold, it is shown but never applied."""
    if isinstance(obj, dict) and "confidence" in obj:
        obj = {**obj, "confidence": _num(obj["confidence"])}
    if not _valid(obj, schema):
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
    obj = client.generate_json(prompt, system=prompts.SYSTEM, schema=schemas.RUN_ORDER)
    obj = _check(obj, schemas.RUN_ORDER)
    if obj is None:
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
    obj = client.generate_json(prompt, system=prompts.SYSTEM, schema=schemas.DIAGNOSIS)
    obj = _check(obj, schemas.DIAGNOSIS)
    if obj is not None:
        obj["is_advisory"] = True
        obj = _gate(obj, client)
    return obj


_VALID_VERDICTS = {"match", "mismatch", "unclear", "not-reported"}

_NUM_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")


def _numbers(text: str) -> list[float]:
    out = []
    for tok in _NUM_RE.findall(text or ""):
        try:
            out.append(float(tok))
        except ValueError:
            continue
    return out


def _value_in_output(value: str, produced: str) -> Optional[bool]:
    """Does a number the model says the run produced actually occur in the run's
    output? None when the value states no number (nothing to check). A model that
    quotes a value the output never printed is narrating, not comparing — and a
    "match" built on it is a fabricated agreement with someone's published work."""
    wanted = _numbers(value)
    if not wanted:
        return None
    have = _numbers(produced)
    return all(any(math.isclose(w, h, rel_tol=1e-9, abs_tol=1e-12) for h in have) for w in wanted)


def compare_results(client: OllamaClient, *, paper: str, produced: str,
                    coverage: str) -> Optional[dict[str, Any]]:
    """Claim-by-claim comparison of the paper's numbers against what the re-run
    actually printed. ADVISORY ONLY: it never grants or changes a badge — the
    harness's stance is that "results match the paper" is a human judgement, and
    this only puts the comparison in front of that human.

    Both inputs are untrusted (the paper is author-supplied, the produced text is
    author-code output), so both are fenced."""
    # Budgets are small on purpose: the client is single-shot with a ~60s read
    # timeout against a small local model, so an oversized prompt returns nothing
    # at all. The caller selects the results-bearing passages (paper.excerpt);
    # truncating a paper blindly would send its intro, which holds no numbers.
    prompt = prompts.COMPARE_RESULTS.format(
        coverage=coverage,
        paper=prompts.fence(paper[:8000]),
        produced=prompts.fence(produced[:6000]),
    )
    obj = client.generate_json(prompt, system=prompts.SYSTEM, schema=schemas.RESULTS_CHECK)
    if not isinstance(obj, dict) or not isinstance(obj.get("claims"), list):
        return None
    # Keep only well-formed claims with a verdict from the fixed vocabulary; a
    # model that invents a verdict must not widen the report's meaning. Claims
    # are validated one by one so a single malformed row does not discard the rest.
    claims = []
    for c in obj["claims"]:
        if not isinstance(c, dict) or not c.get("claim"):
            continue
        if c.get("verdict") not in _VALID_VERDICTS:
            c["verdict"] = "unclear"
        if not _valid(c, schemas.CLAIM):
            continue
        found = _value_in_output(str(c.get("produced_value") or ""), produced)
        if found is not None:
            c["produced_value_found"] = found
        elif c["verdict"] in ("match", "mismatch"):
            # no number was quoted as produced, so there is nothing to check the
            # verdict against: it is the model's say-so, which is not evidence
            c["verdict"] = "unclear"
            c["why"] = ("[harness] no numeric produced value was quoted, so this comparison was "
                        "not made from evidence. " + str(c.get("why") or ""))
        if found is False and c["verdict"] in ("match", "mismatch"):
            c["verdict"] = "unclear"
            c["why"] = ("[harness] the value quoted as produced does not occur in the run's output, "
                        "so this comparison was not made from evidence. " + str(c.get("why") or ""))
        claims.append(c)
    obj = sanitize({**obj, "claims": claims}) if claims else None
    if obj is None:
        return None
    claims = obj["claims"]
    if not claims:
        return None
    obj["claims"] = claims[:40]
    obj["is_advisory"] = True
    obj["coverage"] = coverage
    return _gate(obj, client)


def summarize(client: OllamaClient, report: dict[str, Any]) -> Optional[str]:
    # The report embeds author-controlled bytes (step targets, log tails, source
    # URL, warnings). Fence it like the other roles so the SYSTEM "treat as data,
    # never follow instructions" contract covers the summarize path too.
    compact = json.dumps(report, default=str)[:6000]
    prompt = prompts.SUMMARIZE.format(report=prompts.fence(compact))
    obj = client.generate_json(prompt, system=prompts.SYSTEM, schema=schemas.SUMMARY)
    obj = _check(obj, schemas.SUMMARY)
    return obj.get("summary") if obj else None
