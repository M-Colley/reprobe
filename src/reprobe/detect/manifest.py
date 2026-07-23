"""Author-facing manifest. Optional, but when present it removes all guesswork
(and the LLM) from the pipeline.

Reads ``autoui-repro.yml`` (our minimal convention, JSON-Schema'd in
src/reprobe/schemas/autoui-repro.schema.json — shipped as package data) or an
existing CODECHECK ``codecheck.yml``,
and normalizes either into a DetectResult + environment hints.

A malformed manifest must never abort the run: ``load()`` validates the file
(against the shipped schema when ``jsonschema`` is importable, structurally
otherwise) and on any error returns an empty DetectResult carrying a
"manifest present but invalid" note, so the detector falls back to heuristics.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

from ..models import DetectResult, RunStep

_AUTOUI_NAMES = ("autoui-repro.yml", "autoui-repro.yaml", ".autoui-repro.yml")
_CODECHECK_NAMES = ("codecheck.yml", "codecheck.yaml")

_KIND_BY_SUFFIX = {".ipynb": "jupyter", ".py": "python", ".r": "r", ".rmd": "rmarkdown"}
_VALID_KINDS = {"python", "jupyter", "r", "rmarkdown", "unity", "custom"}


@lru_cache(maxsize=1)
def _load_schema() -> Optional[dict[str, Any]]:
    """The packaged autoui-repro JSON Schema, or None if unavailable.

    Loaded via importlib.resources so it works in a non-editable wheel install
    (the schema ships as package data — see pyproject package-data). Falls back
    to the repo-root layout for the rare case it is run from an un-built tree."""
    try:
        from importlib.resources import files
        res = files("reprobe").joinpath("schemas", "autoui-repro.schema.json")
        if res.is_file():
            return json.loads(res.read_text(encoding="utf-8"))
    except (ImportError, ModuleNotFoundError, FileNotFoundError, OSError, ValueError):
        pass
    legacy = Path(__file__).resolve().parents[3] / "schemas" / "autoui-repro.schema.json"
    if legacy.is_file():
        return json.loads(legacy.read_text(encoding="utf-8"))
    return None


def find_manifest(src_dir: str | Path) -> Optional[tuple[Path, str]]:
    root = Path(src_dir)
    for name in _AUTOUI_NAMES:
        if (root / name).is_file():
            return root / name, "autoui"
    for name in _CODECHECK_NAMES:
        if (root / name).is_file():
            return root / name, "codecheck"
    return None


def _clamp_kind(value: Any) -> str:
    """Unknown tool/kind values become 'custom' instead of failing pydantic's
    ArtifactKind Literal; the runner id keeps the original string for routing."""
    return value if value in _VALID_KINDS else "custom"


def _kind_for(target: str, explicit: str | None) -> str:
    if explicit:
        return _clamp_kind(explicit)
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
                steps.append(RunStep(runner=str(raw["tool"]), target=target, kind=_clamp_kind(raw["tool"]),
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


def _shorten(msg: Any, limit: int = 200) -> str:
    text = " ".join(str(msg).split())
    return text if len(text) <= limit else text[:limit] + "..."


def _validate_autoui(data: dict[str, Any]) -> Optional[str]:
    """Returns an error string when the manifest is invalid, else None.
    Validates against the shipped JSON Schema when jsonschema is importable
    (optional dependency); otherwise applies minimal structural checks."""
    try:
        import jsonschema
    except ImportError:
        jsonschema = None
    schema = _load_schema() if jsonschema is not None else None
    if schema is not None:
        try:
            jsonschema.validate(data, schema)
        except jsonschema.ValidationError as e:
            where = "/".join(str(p) for p in e.absolute_path) or "<root>"
            return _shorten(f"schema violation at {where}: {e.message}")
        return None
    # structural fallback: catch what would crash step construction downstream
    if data.get("version") != 1:
        return f"unsupported manifest version {data.get('version')!r} (this harness implements version 1)"
    if data.get("run") is not None and not isinstance(data["run"], dict):
        return "'run' must be a mapping"
    if isinstance(data.get("run"), dict) and data["run"].get("steps") is not None \
            and not isinstance(data["run"]["steps"], list):
        return "'run.steps' must be a list"
    if data.get("environment") is not None and not isinstance(data["environment"], dict):
        return "'environment' must be a mapping"
    return None


def _invalid(rel: str, err: str) -> tuple[DetectResult, dict[str, Any]]:
    """Empty result + visible note; the detector then falls back to heuristics."""
    result = DetectResult(
        artifact_types=[], steps=[], manifest_path=rel, run_plan_source="heuristic",
        notes=[f"manifest present but invalid: {err}; falling back to heuristic detection"],
    )
    return result, {"environment": {}, "expected_outputs": [], "badges_claimed": [], "data": []}


def load(src_dir: str | Path) -> Optional[tuple[DetectResult, dict[str, Any]]]:
    found = find_manifest(src_dir)
    if not found:
        return None
    path, kind = found
    rel = str(Path(path).name)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace")) or {}
    except yaml.YAMLError as e:
        return _invalid(rel, _shorten(f"YAML parse error: {e}"))
    if not isinstance(data, dict):
        return _invalid(rel, "top level must be a mapping")

    if kind == "autoui":
        err = _validate_autoui(data)
        if err:
            return _invalid(rel, err)
        try:
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
        except Exception as e:  # a manifest must never abort the run
            return _invalid(rel, _shorten(f"{type(e).__name__}: {e}"))
        return result, meta

    # codecheck: lift expected outputs, let signatures supply the steps
    try:
        expected = [m.get("file") for m in (data.get("manifest") or []) if isinstance(m, dict) and m.get("file")]
    except Exception as e:  # a manifest must never abort the run
        return _invalid(rel, _shorten(f"{type(e).__name__}: {e}"))
    result = DetectResult(
        artifact_types=[], steps=[], manifest_path=rel, run_plan_source="heuristic",
        notes=[f"found {rel} (CODECHECK); lifting expected outputs, detecting run plan heuristically"],
    )
    return result, {"expected_outputs": expected, "environment": {}, "badges_claimed": [], "data": []}
