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
              "Library", "Temp", "obj", "Build", "Builds", ".venv", "venv", "renv",
              # Installed third-party code is never the artifact's own. A deposit
              # that vendors a site-packages tree (or a re-scanned run dir, which
              # carries the env the install phase built) otherwise contributes
              # thousands of files and every dependency of every dependency.
              "site-packages", "dist-packages",
              ".reprobe_env", ".reprobe_deps", ".reprobe_cache", ".reprobe_Rlib"}
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

# Any file here counts as the artifact declaring *something* about its
# dependencies. Presence is all that is checked — reprobe installs only a few of
# them; the point is to tell "declared, and we skipped it" (which the env planner
# warns about per-file) apart from "declared nothing at all". Shared with
# envbuild so the two never drift.
DEP_MANIFESTS = (
    "requirements.txt", "requirements.in", "Pipfile", "Pipfile.lock",
    "pyproject.toml", "setup.py", "setup.cfg", "poetry.lock", "uv.lock",
    "environment.yml", "environment.yaml", "conda-lock.yml",
    "binder/environment.yml", "binder/requirements.txt",
    "renv.lock", "install.R", "DESCRIPTION",
)

# Root-level license filenames, matched case-insensitively on the stem so
# LICENSE, License.md, LICENCE.txt and COPYING all count.
_LICENSE_STEMS = {"license", "licence", "copying", "copyright"}

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


# --------------------------------------------------------------------------- #
# Python imports the artifact uses but never declares.
#
# The R side has done this since 0.1: library()/require() calls are scanned and
# the CRAN-available subset is installed in the sanctioned egress phase. Python
# had no counterpart, so the same defect produced two different verdicts — an R
# artifact calling library(shap) without declaring it ran, while the Python one
# died on ModuleNotFoundError. Worse, a Python artifact could *appear* to pass by
# silently borrowing a package from the harness base image, which is exactly the
# over-claim the report elsewhere works to prevent.
#
# Names are validated to a strict charset so a token discovered in an untrusted
# deposit can never carry a shell metacharacter into the later `bash -c`.
# --------------------------------------------------------------------------- #
_PY_MOD = r"[A-Za-z_][A-Za-z0-9_]*"
_PY_DIST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# What follows the module name has to look like Python, not English. Prose in a
# docstring ("import the module first") matched a bare `import\s+(\w+)` and put
# "the" on the install list, so require a real statement ending: end of line, a
# comma, ` as `, a dotted path, or a trailing comment.
_PY_IMPORT_RE = re.compile(rf"(?m)^[ \t]*import[ \t]+({_PY_MOD})(?=[ \t]*(?:$|[,#.])|[ \t]+as[ \t])")
_PY_FROM_RE = re.compile(rf"(?m)^[ \t]*from[ \t]+({_PY_MOD})(?:\.{_PY_MOD})*[ \t]+import[ \t]")

#: Import name -> PyPI distribution, for the cases where they differ. Unlisted
#: names are assumed to match, which is true for the overwhelming majority.
_PY_IMPORT_TO_DIST = {
    "cv2": "opencv-python", "sklearn": "scikit-learn", "skimage": "scikit-image",
    "PIL": "pillow", "yaml": "pyyaml", "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil", "dotenv": "python-dotenv", "serial": "pyserial",
    "OpenGL": "PyOpenGL", "Crypto": "pycryptodome", "fitz": "pymupdf",
    "mpl_toolkits": "matplotlib", "google": "protobuf", "attr": "attrs",
    "jwt": "pyjwt", "usb": "pyusb", "zmq": "pyzmq", "lxml": "lxml",
    "tables": "pytables", "netCDF4": "netcdf4", "pyreadstat": "pyreadstat",
}


def _py_ipynb_source(p: Path) -> str:
    """Concatenated code cells of a PYTHON-kernel notebook, else ""."""
    import json
    try:
        data = json.loads(_read_head(p, 5_000_000))
    except (ValueError, OSError, RecursionError):
        return ""
    if not isinstance(data, dict):
        return ""
    meta = data.get("metadata") or {}
    ks = meta.get("kernelspec") or {}
    lang = str(ks.get("language", "")).lower()
    li = str((meta.get("language_info") or {}).get("name", "")).lower()
    name = str(ks.get("name", "")).lower()
    # An absent kernelspec is treated as Python: that is the overwhelming default
    # for .ipynb, and the R scanner already claims the ones that say "R".
    if lang not in ("", "python") and li not in ("", "python") and not name.startswith("py"):
        return ""
    out: list[str] = []
    for cell in (data.get("cells") or []):
        if isinstance(cell, dict) and cell.get("cell_type") == "code":
            src = cell.get("source")
            out.append("".join(src) if isinstance(src, list) else str(src or ""))
    return "\n".join(out)


def _local_module_names(root: Path, files) -> set[str]:
    """Module names the deposit itself provides, so its own files are never
    mistaken for PyPI distributions. `import helper` in a repo shipping
    helper.py must not try to install a package called "helper"."""
    local: set[str] = set()
    for p in files:
        try:
            rel_parts = p.relative_to(root).parts
        except ValueError:
            continue
        if p.suffix == ".py":
            local.add(p.stem)
            if len(rel_parts) > 1:
                local.add(rel_parts[0])          # a package directory
        if p.name == "__init__.py" and len(rel_parts) > 1:
            local.add(rel_parts[-2])
    return local


def scan_py_packages(root: Path, files) -> list[str]:
    """PyPI distributions the deposit's Python code imports.

    Excludes the standard library, the deposit's own modules, and relative
    imports. Returns sorted distribution names, each matching _PY_DIST_RE. The
    caller decides what to install; this only says what the code reaches for."""
    import sys

    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    local = _local_module_names(root, files)
    found: set[str] = set()
    for p in files:
        suf = p.suffix.lower()
        if suf == ".py":
            text = _read_head(p)
        elif suf == ".ipynb":
            text = _py_ipynb_source(p)
        else:
            continue
        if not text:
            continue
        for rx in (_PY_IMPORT_RE, _PY_FROM_RE):
            found.update(rx.findall(text))
    dists = set()
    for mod in found:
        if mod in stdlib or mod in local or mod.startswith("_"):
            continue
        dist = _PY_IMPORT_TO_DIST.get(mod, mod)
        if _PY_DIST_RE.match(dist):
            dists.add(dist)
    return sorted(dists)


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


# Pipeline-stage hints, used ONLY to break ties the author left unordered.
# Word-boundary anchored so "cleanup_utils" matches but "unclean" does not.
_STAGE_EARLY_RE = re.compile(
    r"(?:^|[_\-. ])(?:prep|prepare|preprocess|preprocessing|preproc|clean|cleaning|"
    r"load|loader|download|fetch|ingest|import|extract|setup|build_dataset)(?:[_\-. ]|$)",
    re.IGNORECASE)
_STAGE_LATE_RE = re.compile(
    r"(?:^|[_\-. ])(?:analyse|analyze|analysis|aggregate|aggregated|combine|combined|"
    r"consensus|summar(?:y|ise|ize|ised|ized)|compare|comparison|report|plot|plots|"
    r"figure|figures|visuali[sz]e|visuali[sz]ation)(?:[_\-. ]|$)",
    re.IGNORECASE)


def _stage_rank(name: str) -> int:
    """0 = data prep, 1 = unclassified, 2 = downstream aggregation/reporting.

    A repo whose files carry no numeric prefix and whose README does not order
    them falls back to alphabetical, which silently puts an aggregator first when
    its name sorts early (`analyse_combined_*.ipynb` before `PDRA_*.ipynb`). That
    is worse than cosmetic: the aggregator then reads the *committed* outputs of
    steps that have not re-run yet, so it can pass on stale data and turn a
    broken pipeline green. Ranking by name is a guess, so it ranks BELOW both the
    numeric prefix and README order — it only decides otherwise-arbitrary ties."""
    if _STAGE_LATE_RE.search(name):
        return 2
    if _STAGE_EARLY_RE.search(name):
        return 0
    return 1


def _order_key(path: Path, root: Path, readme: str):
    name = path.name
    nums = _NUM_RE.search(name)
    num = int(nums.group(1)) if nums else 9999
    idx = readme.find(name)
    readme_idx = idx if idx >= 0 else 9999
    return (num, readme_idx, _stage_rank(path.stem), str(path.relative_to(root)).lower())


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

    # Say so when the run order is a pure guess. Multi-step pipelines are order-
    # sensitive in a way that does not announce itself: a downstream step run too
    # early silently consumes the committed outputs of steps that have not re-run,
    # so it can pass on stale data. A reviewer must be told the order was inferred.
    ordered = notebooks + rmds
    if len(ordered) > 1 and not any(_NUM_RE.search(p.name) for p in ordered) \
            and not any(p.name in readme for p in ordered):
        notes.append(
            "run order is inferred, not declared: no numeric filename prefixes and the README does not "
            "reference these files by name. Names that look downstream (analyse/combine/summary/"
            "compare/plot) were moved last; everything else is alphabetical. If the real order differs, "
            "declare `steps:` in .reprobe.yaml — a mis-ordered aggregation step can read stale committed "
            "outputs and pass.")

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
    py_packages = scan_py_packages(root, files)

    # FAIR inputs that were previously only reachable via fetcher metadata. Most
    # fetchers (git clone in particular) return no license field at all, so a repo
    # shipping a plain LICENSE file scored as if it had none.
    try:
        root_entries = sorted(root.iterdir())
    except OSError:
        root_entries = []
    license_file = next((rel(p) for p in root_entries
                         if p.is_file() and p.stem.lower() in _LICENSE_STEMS), None)
    dep_manifest = next((m for m in DEP_MANIFESTS if (root / m).is_file()), None)

    return DetectResult(
        artifact_types=sorted(set(types) | set(inventory)),
        inventory=inventory,
        steps=steps,
        license_file=license_file,
        dep_manifest=dep_manifest,
        run_plan_source="heuristic",
        flags=sorted(set(flags)),
        notes=notes,
        r_packages=r_packages,
        py_packages=py_packages,
    )


def is_ambiguous(result: DetectResult) -> bool:
    """Whether the LLM's alternative ordering is worth offering."""
    return any("best-effort" in n or "no clear entry" in n or "not scheduled" in n
               for n in result.notes)
