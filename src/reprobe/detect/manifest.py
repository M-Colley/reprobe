"""Author-facing manifest. Optional, but when present it removes all guesswork
(and the LLM) from the pipeline.

Reads ``autoui-repro.yml`` (our minimal convention, JSON-Schema'd in
schemas/autoui-repro.schema.json) or an existing CODECHECK ``codecheck.yml``,
and normalizes either into a DetectResult + environment hints.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

from ..models import DetectResult, RunStep

_AUTOUI_NAMES = ("autoui-repro.yml", "autoui-repro.yaml", ".autoui-repro.yml")
_CODECHECK_NAMES = ("codecheck.yml", "codecheck.yaml")

_KIND_BY_SUFFIX = {".ipynb": "jupyter", ".py": "python", ".r": "r", ".rmd": "rmarkdown"}


def find_manifest(src_dir: str | Path) -> Optional[tuple[Path, str]]:
    root = Path(src_dir)
    for name in _AUTOUI_NAMES:
        if (root / name).is_file():
            return root / name, "autoui"
    for name in _CODECHECK_NAMES:
        if (root / name).is_file():
            return root / name, "codecheck"
    return None


def _kind_for(target: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    return _KIND_BY_SUFFIX.get(Path(target).suffix.lower(), "custom")


def _steps_from_autoui(data: dict[str, Any]) -> list[RunStep]:
    steps: list[RunStep] = []
    run = (data.get("run") or {})
    expected = data.get("expected_outputs", []) or []
    for raw in run.get("steps", []) or []:
        if isinstance(raw, str):
            steps.append(RunStep(target=raw, kind=_kind_for(raw, None),
                                  runner=_kind_for(raw, None), expected_outputs=expected))
        elif isinstance(raw, dict):
            if "tool" in raw:                       # e.g. {tool: unity, project: prototype/, tier: compile}
                target = raw.get("project") or raw.get("path") or "."
                steps.append(RunStep(runner=raw["tool"], target=target, kind=raw["tool"],
                                     args={k: v for k, v in raw.items() if k not in ("tool", "project", "path")}))
            else:
                target = raw.get("path") or raw.get("file") or ""
                kind = _kind_for(target, raw.get("kind"))
                steps.append(RunStep(runner=raw.get("runner", kind), target=target, kind=kind,
                                     argv=[str(a) for a in (raw.get("args") or raw.get("argv") or [])],
                                     expected_outputs=raw.get("expected_outputs", expected),
                                     description=raw.get("description")))
    return steps


def _steps_from_codecheck(data: dict[str, Any]) -> list[RunStep]:
    # CODECHECK lists manifest outputs and a 'codecheck.yml: paper/manifest' of files;
    # the runnable bits are usually under 'manifest' (outputs) + a 'workflow'/script.
    steps: list[RunStep] = []
    expected = [m.get("file") for m in (data.get("manifest") or []) if isinstance(m, dict) and m.get("file")]
    # Best-effort: codecheck doesn't standardize the run command; fall back to scripts named in repository.
    return steps  # signatures.scan() will supply steps; we only lift expected_outputs


def load(src_dir: str | Path) -> Optional[tuple[DetectResult, dict[str, Any]]]:
    found = find_manifest(src_dir)
    if not found:
        return None
    path, kind = found
    data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace")) or {}
    rel = str(Path(path).name)

    if kind == "autoui":
        steps = _steps_from_autoui(data)
        env = data.get("environment", {}) or {}
        result = DetectResult(
            artifact_types=sorted({s.kind for s in steps}),
            steps=steps,
            manifest_path=rel,
            run_plan_source="manifest",
            notes=[f"using author manifest {rel}"],
        )
        meta = {
            "environment": env,
            "expected_outputs": data.get("expected_outputs", []) or [],
            "badges_claimed": data.get("badges_claimed", []) or [],
            "data": data.get("data", []) or [],
        }
        return result, meta

    # codecheck: lift expected outputs, let signatures supply the steps
    expected = [m.get("file") for m in (data.get("manifest") or []) if isinstance(m, dict) and m.get("file")]
    result = DetectResult(
        artifact_types=[], steps=[], manifest_path=rel, run_plan_source="heuristic",
        notes=[f"found {rel} (CODECHECK); lifting expected outputs, detecting run plan heuristically"],
    )
    return result, {"expected_outputs": expected, "environment": {}, "badges_claimed": [], "data": []}
