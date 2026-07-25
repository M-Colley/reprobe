"""Deterministic artifact detection. Runs first, always, with no code execution
and no LLM. The LLM (if enabled) only proposes an *alternative* ordering when
this heuristic is ambiguous — it never overrides these facts.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..models import DetectResult, RunStep

_SKIP_DIRS = {".git", ".hg", "node_modules", "__pycache__", ".ipynb_checkpoints",
              "Library", "Temp", "obj", "Build", "Builds", ".venv", "venv", "renv"}
# The fetched tree is untrusted. Walk it without following symlinks (no loops,
# no escaping the fetch dir) and stop after a sane cap so a deposit with millions
# of files can't hang detection before any container is even used.
_MAX_SCAN_FILES = 200_000
_NUM_RE = re.compile(r"(\d+)")
_ENTRY_PY = {"main.py", "run.py", "analysis.py", "analyze.py", "pipeline.py",
             "train.py", "evaluate.py", "reproduce.py", "make_figures.py"}
# Word-boundary stems only ("run_all.py" yes, "runners.py"/"mainwindow.py" no),
# and only at shallow depth (see _entry_shallow) so nested library helpers are
# never executed as top-level steps. Exact _ENTRY_PY names match at any depth.
_ENTRY_PY_RE = re.compile(r"^(main|run|analysis|analyze|pipeline|reproduce|train|evaluate|make_figures?)([_-]|\.py$)", re.I)
_ENTRY_R_RE = re.compile(r"^(main|run|analy|reproduce)", re.I)
_SCRIPT_DIRS = {"scripts", "script", "code", "src", "analysis", "analyses", "bin", "r"}

# --------------------------------------------------------------------------- #
# R dependency discovery (static, no execution). We parse library()/require()/
# requireNamespace()/pkg:: usages out of R sources + DESCRIPTION Imports/Depends
# so the env planner can install the CRAN-available subset in the sanctioned
# egress phase. Names are validated to a strict charset so a discovered token
# can never carry a shell/R metacharacter into the later `bash -c` install.
# --------------------------------------------------------------------------- #
_R_PKG = r"[A-Za-z][A-Za-z0-9.]*[A-Za-z0-9]"          # >=2 chars, no trailing dot
_R_PKG_NAME_RE = re.compile(rf"^{_R_PKG}$")
_R_LIB_RE = re.compile(rf"""(?:library|require)\s*\(\s*['"]?({_R_PKG})['"]?""")
_R_REQNS_RE = re.compile(rf"""requireNamespace\s*\(\s*['"]({_R_PKG})['"]""")
_R_NS_RE = re.compile(rf"""(?<![\w.])({_R_PKG}):::?""")   # pkg::fn / pkg:::fn
_R_SCAN_READ_CAP = 1_000_000                          # bytes read per file for scanning
# base + recommended packages ship with every R; never install from CRAN.
_R_BASE_PKGS = frozenset({
    "base", "compiler", "datasets", "grDevices", "graphics", "grid", "methods",
    "parallel", "splines", "stats", "stats4", "tcltk", "tools", "translations",
    "utils",
    "boot", "class", "cluster", "codetools", "foreign", "KernSmooth", "lattice",
    "MASS", "Matrix", "mgcv", "nlme", "nnet", "rpart", "spatial", "survival",
})


def _read_head(p: Path, cap: int = _R_SCAN_READ_CAP) -> str:
    try:
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(cap)
    except OSError:
        return ""


def _r_ipynb_source(p: Path) -> str:
    """Concatenated code cells of an R-kernel notebook, else "" (never scan a
    Python notebook for R packages)."""
    import json
    try:
        data = json.loads(_read_head(p, 5_000_000))
    except (ValueError, OSError, RecursionError):
        # untrusted deposit: malformed, unreadable, OR deeply-nested JSON (a
        # RecursionError bomb far under the byte cap) must never crash detection.
        return ""
    if not isinstance(data, dict):
        return ""
    meta = data.get("metadata") or {}
    ks = meta.get("kernelspec") or {}
    lang = str(ks.get("language", "")).lower()
    name = str(ks.get("name", "")).lower()
    li = str((meta.get("language_info") or {}).get("name", "")).lower()
    if not (lang == "r" or li == "r" or name.startswith("ir")):
        return ""
    out: list[str] = []
    for cell in data.get("cells", []) or []:
        if isinstance(cell, dict) and cell.get("cell_type") == "code":
            src = cell.get("source")
            out.append("".join(src) if isinstance(src, list) else str(src or ""))
    return "\n".join(out)


_R_QUOTED_PKG_RE = re.compile(rf"""['"]({_R_PKG})['"]""")
_R_IDENT_RE = re.compile(r"[A-Za-z._][A-Za-z0-9._]*")
# names that appear in the install expression itself, never a user variable
_R_NOT_A_VAR = frozenset({"c", "install.packages", "installed.packages", "rownames",
                          "setdiff", "unique", "character", "repos", "lib", "dependencies",
                          "requireNamespace", "suppressWarnings", "Sys.getenv"})
_R_RESOLVE_DEPTH = 4


def _strip_r_comments(text: str) -> str:
    """Drop `# …` comments (naive, line-based) so package-list `c(...)` vectors
    whose entries carry trailing comments — a common style — parse cleanly."""
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _balanced(text: str, open_idx: int) -> str:
    """Text inside the parentheses that open at ``text[open_idx]``."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i]
    return text[open_idx + 1:]


def _first_arg(args: str) -> str:
    """The first top-level argument of an R argument list."""
    depth, cur = 0, []
    for ch in args:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "," and depth == 0:
            break
        cur.append(ch)
    return "".join(cur).strip()


def _assignment_rhs(text: str, sym: str) -> str | None:
    """The right-hand side of ``sym <- …`` / ``sym = …`` (balanced across lines)."""
    m = re.search(rf"(?m)^\s*{re.escape(sym)}\s*(?:<<-|<-|=)\s*", text)
    if not m:
        return None
    depth, out = 0, []
    for ch in text[m.end():]:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "\n" and depth <= 0:
            break
        out.append(ch)
    return "".join(out)


def _declared_install_packages(text: str) -> set[str]:
    """Package names an author lists for installation, e.g. a setup.R's
    ``pkgs <- c("a","b",…); install.packages(pkgs)``. This is a very common way
    to declare the FULL dependency set that ``library()``/``require()`` calls
    never reveal — packages loaded on demand inside other packages (FSA, Hmisc,
    rstatix, …) are named only here.

    Only names reachable from an actual ``install.packages()`` argument are
    returned: we take its first argument and, when that is a variable, follow the
    assignment chain (``install.packages(missing)`` -> ``missing <- pkgs[…]`` ->
    ``pkgs <- c(…)``). Harvesting every ``c(...)`` in the file instead would drag
    in unrelated character vectors — factor levels like ``c("car","boot")`` are
    real CRAN names and would be installed for nothing."""
    if "install.packages" not in text:
        return set()
    clean = _strip_r_comments(text)
    pkgs: set[str] = set()
    frontier = [_first_arg(_balanced(clean, m.end() - 1))
                for m in re.finditer(r"(?<![\w.])install\.packages\s*\(", clean)]
    seen: set[str] = set()
    for _ in range(_R_RESOLVE_DEPTH):
        if not frontier:
            break
        nxt: list[str] = []
        for expr in frontier:
            pkgs.update(_R_QUOTED_PKG_RE.findall(expr))
            for sym in _R_IDENT_RE.findall(expr):
                if sym in _R_NOT_A_VAR or sym in seen:
                    continue
                seen.add(sym)
                rhs = _assignment_rhs(clean, sym)
                if rhs:
                    nxt.append(rhs)
        frontier = nxt
    return pkgs


def _description_packages(text: str) -> set[str]:
    pkgs: set[str] = set()
    for field in ("Imports", "Depends"):
        m = re.search(rf"(?im)^{field}\s*:(.*?)(?=^\S|\Z)", text, re.S)
        if not m:
            continue
        for tok in m.group(1).split(","):
            name = tok.strip().split("(")[0].strip()
            name = name.split()[0] if name else ""
            if name and name != "R":
                pkgs.add(name)
    return pkgs


def scan_r_packages(files) -> list[str]:
    """Statically discover required R package names across the deposit's R
    sources (.R/.Rmd), R-kernel notebooks, and DESCRIPTION. Returns a sorted list
    with base/recommended packages removed; each name matches _R_PKG_NAME_RE."""
    found: set[str] = set()
    for p in files:
        suf = p.suffix.lower()
        if p.name == "DESCRIPTION":
            found |= _description_packages(_read_head(p))
            continue
        if suf in (".r", ".rmd"):
            text = _read_head(p)
        elif suf == ".ipynb":
            text = _r_ipynb_source(p)
        else:
            continue
        if not text:
            continue
        for rx in (_R_LIB_RE, _R_REQNS_RE, _R_NS_RE):
            found.update(rx.findall(text))
        if suf in (".r", ".rmd"):
            found |= _declared_install_packages(text)   # setup.R-style install lists
    return sorted(n for n in found if _R_PKG_NAME_RE.match(n) and n not in _R_BASE_PKGS)

# Non-code artifact categories (the AutoUI/CHI submission form's Video, Audio,
# Datasets, Other). Classification is advisory: it feeds artifact_types and the
# report inventory so a media/data-only deposit reads as what it is rather than
# "(none)" — it never schedules a run step and never affects badge decisions.
# Ubiquitous repo noise (.md, .txt, .json, .yml, images) is deliberately absent.
_NONCODE_TYPES = {
    "video": {"mp4", "mov", "avi", "mkv", "webm", "m4v", "mpg", "mpeg"},
    "audio": {"wav", "mp3", "flac", "ogg", "oga", "m4a", "aac", "opus"},
    "dataset": {"csv", "tsv", "parquet", "feather", "arrow", "xlsx", "xls",
                "sav", "dta", "rds", "rdata", "h5", "hdf5", "nc",
                "sqlite", "sqlite3", "jsonl", "ndjson"},
    "document": {"pdf", "doc", "docx", "ppt", "pptx", "tex"},
    "3d-model": {"stl", "step", "stp", "obj", "fbx", "blend", "3mf",
                 "iges", "igs", "ply", "gltf", "glb"},
}
_EXT_TO_NONCODE = {ext: t for t, exts in _NONCODE_TYPES.items() for ext in exts}


def _iter_files(root: Path):
    seen = 0
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]   # prune, never descend
        for f in files:
            p = Path(dirpath) / f
            if p.is_symlink():                               # don't inventory/run symlinked files
                continue
            seen += 1
            if seen > _MAX_SCAN_FILES:
                return
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


def _entry_shallow(p: Path, root: Path) -> bool:
    """Entry-point *prefix* matches only count at the repo root or one level
    down inside a conventional script directory."""
    parts = p.relative_to(root).parts
    return len(parts) == 1 or (len(parts) == 2 and parts[0].lower() in _SCRIPT_DIRS)


def _unity_projects(root: Path) -> list[Path]:
    projects = []
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        d = Path(dirpath)
        if d.name == "ProjectSettings" and "ProjectVersion.txt" in files:
            proj = d.parent
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
    r_scripts = [p for p in files if p.suffix.lower() == ".r"]
    py_files = [p for p in files if p.suffix == ".py"]
    unity = _unity_projects(root)

    # Non-code inventory. Unity project trees are excluded: their Assets are
    # engine content (audio clips, models), not standalone research artifacts.
    unity_prefixes = [p.relative_to(root).parts for p in unity]
    inventory: dict[str, int] = {}
    for p in files:
        parts = p.relative_to(root).parts
        if any(parts[:len(u)] == u for u in unity_prefixes):
            continue
        cat = _EXT_TO_NONCODE.get(p.suffix.lower().lstrip("."))
        if cat:
            inventory[cat] = inventory.get(cat, 0) + 1

    notebooks.sort(key=lambda p: _order_key(p, root, readme))
    rmds.sort(key=lambda p: _order_key(p, root, readme))

    steps: list[RunStep] = []
    types: list[str] = []
    notes: list[str] = []
    flags: list[str] = []

    if len(files) >= _MAX_SCAN_FILES:
        notes.append(f"file scan stopped at {_MAX_SCAN_FILES} files; detection is best-effort on this deposit")

    if notebooks:
        types.append("jupyter")
        for nb in notebooks:
            steps.append(RunStep(runner="jupyter", target=rel(nb), kind="jupyter"))
    if rmds:
        types.append("rmarkdown")
        for rmd in rmds:
            steps.append(RunStep(runner="rmarkdown", target=rel(rmd), kind="rmarkdown"))

    # R scripts: entry-named scripts always run; the full set only when no
    # notebooks/Rmd drive the analysis. Suppressed scripts still register the
    # language (so renv restore runs) and leave a note in the report.
    if r_scripts:
        entry_r = [p for p in r_scripts if _ENTRY_R_RE.match(p.stem) and _entry_shallow(p, root)]
        would_run = entry_r or r_scripts
        chosen = entry_r if (notebooks or rmds) else would_run
        if chosen:
            types.append("r")
            for p in sorted(chosen, key=lambda x: _order_key(x, root, readme)):
                steps.append(RunStep(runner="r", target=rel(p), kind="r"))
        suppressed = len(would_run) - len(chosen)
        if suppressed:
            types.append("r")
            notes.append(f"{suppressed} R script(s) present but not scheduled "
                         "(notebooks/R Markdown assumed to drive the analysis)")

    # Python scripts: entry-named scripts always run; otherwise notebooks are
    # assumed to be the entry point and plain scripts are noted, not run.
    if py_files:
        entry = [p for p in py_files
                 if p.name in _ENTRY_PY or (_entry_shallow(p, root) and _ENTRY_PY_RE.match(p.name))]
        top = [p for p in py_files if len(p.relative_to(root).parts) == 1]
        would_run = entry or top
        chosen = entry if notebooks else would_run
        if chosen:
            types.append("python")
            for p in sorted(chosen, key=lambda x: _order_key(x, root, readme)):
                steps.append(RunStep(runner="python", target=rel(p), kind="python"))
        if not entry and not notebooks and len(top) > 3:
            notes.append("multiple top-level .py scripts and no clear entry point; ordering is best-effort")
        suppressed = len(would_run) - len(chosen)
        if suppressed:
            types.append("python")
            notes.append(f"{suppressed} Python script(s) present but not scheduled "
                         "(notebooks assumed to drive the analysis)")

    for proj in unity:
        types.append("unity")
        steps.append(RunStep(runner="unity", target=rel(proj) if proj != root else ".", kind="unity"))

    # repo2docker hints (repo root or binder/, per repo2docker conventions)
    for hint in ("Dockerfile", "postBuild", "apt.txt", "start", "runtime.txt",
                 "environment.yml", "environment.yaml"):
        if any((base / hint).is_file() for base in (root, root / "binder")):
            flags.append("needs-repo2docker")
            notes.append(f"found {hint}: consider --allow-repo2docker for a faithful environment")
            break

    if len(steps) > 12:
        notes.append(f"{len(steps)} steps detected; ordering is best-effort")
    if not steps:
        if inventory:
            inv = ", ".join(f"{n} {t}" for t, n in sorted(inventory.items()))
            notes.append(f"no runnable analyses detected; deposit contains non-code artifacts "
                         f"({inv}) — the Available badge does not require execution")
        else:
            notes.append("no runnable analyses detected by heuristic")

    r_pkg_files = [p for p in files
                   if p.suffix.lower() in (".r", ".rmd", ".ipynb") or p.name == "DESCRIPTION"]
    r_packages = scan_r_packages(r_pkg_files)

    return DetectResult(
        artifact_types=sorted(set(types) | set(inventory)),
        inventory=inventory,
        steps=steps,
        run_plan_source="heuristic",
        flags=sorted(set(flags)),
        notes=notes,
        r_packages=r_packages,
    )


def is_ambiguous(result: DetectResult) -> bool:
    """Whether the LLM's alternative ordering is worth offering."""
    return any("best-effort" in n or "no clear entry" in n or "not scheduled" in n
               for n in result.notes)
