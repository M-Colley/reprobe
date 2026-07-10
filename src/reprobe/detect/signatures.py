"""Deterministic artifact detection. Runs first, always, with no code execution
and no LLM. The LLM (if enabled) only proposes an *alternative* ordering when
this heuristic is ambiguous — it never overrides these facts.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import DetectResult, RunStep

_SKIP_DIRS = {".git", ".hg", "node_modules", "__pycache__", ".ipynb_checkpoints",
              "Library", "Temp", "obj", "Build", "Builds", ".venv", "venv", "renv"}
_NUM_RE = re.compile(r"(\d+)")
_ENTRY_PY = {"main.py", "run.py", "analysis.py", "analyze.py", "pipeline.py",
             "train.py", "evaluate.py", "reproduce.py", "make_figures.py"}
_ENTRY_PY_RE = re.compile(r"^(main|run|analy|pipeline|reproduce|train|evaluate|fig)", re.I)


def _iter_files(root: Path):
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(root).parts          # skip tokens are repo-relative
        if not any(part in _SKIP_DIRS for part in rel_parts):
            yield p


def _readme_text(root: Path) -> str:
    for name in ("README.md", "README.rst", "README.txt", "README"):
        f = root / name
        if f.is_file():
            return f.read_text(encoding="utf-8", errors="replace")
    return ""


def _order_key(path: Path, root: Path, readme: str):
    name = path.name
    nums = _NUM_RE.search(name)
    num = int(nums.group(1)) if nums else 9999
    idx = readme.find(name)
    readme_idx = idx if idx >= 0 else 9999
    return (num, readme_idx, str(path.relative_to(root)).lower())


def _unity_projects(root: Path) -> list[Path]:
    projects = []
    for pv in root.rglob("ProjectSettings/ProjectVersion.txt"):
        if any(part in _SKIP_DIRS for part in pv.relative_to(root).parts):
            continue
        proj = pv.parent.parent
        if (proj / "Assets").is_dir():
            projects.append(proj)
    return projects


def scan(src_dir: str | Path) -> DetectResult:
    root = Path(src_dir).resolve()
    readme = _readme_text(root)
    files = list(_iter_files(root))

    def rel(p: Path) -> str:
        return str(p.relative_to(root)).replace("\\", "/")

    notebooks = [p for p in files if p.suffix == ".ipynb" and not p.name.endswith(".executed.ipynb")]
    rmds = [p for p in files if p.suffix.lower() == ".rmd"]
    r_scripts = [p for p in files if p.suffix == ".R"]
    py_files = [p for p in files if p.suffix == ".py"]
    unity = _unity_projects(root)

    notebooks.sort(key=lambda p: _order_key(p, root, readme))
    rmds.sort(key=lambda p: _order_key(p, root, readme))

    steps: list[RunStep] = []
    types: list[str] = []
    notes: list[str] = []
    flags: list[str] = []

    if notebooks:
        types.append("jupyter")
        for nb in notebooks:
            steps.append(RunStep(runner="jupyter", target=rel(nb), kind="jupyter"))
    if rmds:
        types.append("rmarkdown")
        for rmd in rmds:
            steps.append(RunStep(runner="rmarkdown", target=rel(rmd), kind="rmarkdown"))

    # R scripts: include as steps only when there are no notebooks/Rmd driving things,
    # or when they look like clear entry points.
    if r_scripts and not rmds:
        entry_r = [p for p in r_scripts if re.match(r"^(main|run|analy|reproduce)", p.stem, re.I)]
        chosen = entry_r or (r_scripts if not notebooks else [])
        if chosen:
            types.append("r")
            for p in sorted(chosen, key=lambda x: _order_key(x, root, readme)):
                steps.append(RunStep(runner="r", target=rel(p), kind="r"))

    # Python scripts: only when no notebooks (notebooks are the usual entry point).
    if py_files and not notebooks:
        entry = [p for p in py_files if p.name in _ENTRY_PY or _ENTRY_PY_RE.match(p.name)]
        top = [p for p in py_files if len(p.relative_to(root).parts) == 1]
        chosen = entry or top
        if chosen:
            types.append("python")
            for p in sorted(chosen, key=lambda x: _order_key(x, root, readme)):
                steps.append(RunStep(runner="python", target=rel(p), kind="python"))
        if not entry and len(top) > 3:
            notes.append("multiple top-level .py scripts and no clear entry point; ordering is best-effort")

    for proj in unity:
        types.append("unity")
        steps.append(RunStep(runner="unity", target=rel(proj) if proj != root else ".", kind="unity"))

    # repo2docker hints
    for hint in ("Dockerfile", "postBuild", "apt.txt", "start", "runtime.txt"):
        if (root / hint).is_file():
            flags.append("needs-repo2docker")
            notes.append(f"found {hint}: consider --allow-repo2docker for a faithful environment")
            break

    if not steps:
        notes.append("no runnable analyses detected by heuristic")

    return DetectResult(
        artifact_types=sorted(set(types)),
        steps=steps,
        run_plan_source="heuristic",
        flags=sorted(set(flags)),
        notes=notes,
    )


def is_ambiguous(result: DetectResult) -> bool:
    """Whether the LLM's alternative ordering is worth offering."""
    return any("best-effort" in n or "no clear entry" in n for n in result.notes)
