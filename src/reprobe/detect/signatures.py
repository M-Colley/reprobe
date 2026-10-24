"""Deterministic artifact detection. Runs first, always, with no code execution
and no LLM. The LLM (if enabled) only proposes an *alternative* ordering when
this heuristic is ambiguous — it never overrides these facts.
"""

from __future__ import annotations

import ast
import os
import re
import sys
import warnings
from functools import lru_cache
from pathlib import Path

from ..models import DetectResult, RunStep

_SKIP_DIRS = {".git", ".hg", "node_modules", "__pycache__", ".ipynb_checkpoints",
              "Library", "Temp", "obj", "Build", "Builds", ".venv", "venv", "renv",
              # Installed third-party code is never the artifact's own. A deposit
              # that vendors a site-packages tree (or a re-scanned run dir, which
              # carries the env the install phase built) otherwise contributes
              # thousands of files and every dependency of every dependency.
              "site-packages", "dist-packages",
              ".reprobe_env", ".reprobe_deps", ".reprobe_cache", ".reprobe_Rlib", ".reprobe_req"}
# The fetched tree is untrusted. Walk it without following symlinks (no loops,
# no escaping the fetch dir) and stop after a sane cap so a deposit with millions
# of files can't hang detection before any container is even used.
_MAX_SCAN_FILES = 200_000
# A STEP number is a leading one (`01_prep`, `step2_model`, `part-3`) — not any
# digits in the name: `plot_study2.R` is not step 2, and reading it so ran it
# before `03_model.R`.
_NUM_RE = re.compile(r"^(?:step|part|phase|stage|s)?[ _.-]?(\d+)(?!\d)", re.I)
_ENTRY_PY = {"main.py", "run.py", "analysis.py", "analyze.py", "pipeline.py",
             "train.py", "evaluate.py", "reproduce.py", "make_figures.py"}
# Word-boundary stems only ("run_all.py" yes, "runners.py"/"mainwindow.py" no),
# and only at shallow depth (see _entry_shallow) so nested library helpers are
# never executed as top-level steps. Exact _ENTRY_PY names match at any depth.
_ENTRY_PY_RE = re.compile(r"^(main|run|analysis|analyze|pipeline|reproduce|train|evaluate|make_figures?)([_-]|\.py$)", re.I)
# Word-bounded: `runtime_utils.R` and `analytic_helpers.R` are not entry points.
_ENTRY_R_RE = re.compile(r"^(main|run|run_?all|analysis|analyses|analyze|analyse|reproduce)([_\-. ]|$)",
                         re.I)
# Literate R sources whose code chunks are scanned for packages. .qmd is never a
# run step (there is no Quarto runner), but its R chunks are still code the
# artifact needs.
_R_LITERATE = (".rmd", ".qmd")
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
_R_NS_RE = re.compile(rf"""(?<![\w.])({_R_PKG}):::?(?=[A-Za-z.`"])""")   # pkg::fn / pkg:::fn
# Calls that LOAD a package, matched on text whose comments and string contents
# are blanked (see _r_views), so `# library(old)` and "x::y" inside a string
# are not code. The package arguments are then read from the same span of the
# comment-free text, where the quotes are still present.
_R_LOAD_CALL_RE = re.compile(
    r"(?<![\w.])(?:\w+::)?(library|require|requireNamespace|loadNamespace|attachNamespace|"
    r"p_load|p_load_gh|p_install|p_install_gh)\s*\(")
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
# Trailing "[extra]" is permitted so a distribution can carry the extra that
# actually provides the imported submodule (see _PY_SUBMODULE_TO_DIST).
_PY_DIST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9._,-]+\])?$")

# Fallback ONLY: source `ast` cannot parse (Python 2 `print x`, a notebook cell
# with a stray magic). What follows the module name has to look like Python, not
# English — prose in a docstring ("import the module first") once put "the" on
# the install list.
_PY_IMPORT_RE = re.compile(
    rf"(?m)^[ \t]*import[ \t]+({_PY_MOD}(?:\.{_PY_MOD})*(?:[ \t]*,[ \t]*{_PY_MOD}(?:\.{_PY_MOD})*)*)"
    rf"(?=[ \t]*(?:$|#)|[ \t]+as[ \t])")
_PY_FROM_RE = re.compile(rf"(?m)^[ \t]*from[ \t]+({_PY_MOD}(?:\.{_PY_MOD})*)[ \t]+import[ \t]")

#: Dotted import path -> the distribution WITH the extra that provides it.
#: An import scan reads `from ray import tune` as the distribution `ray`, and a
#: bare `pip install ray` then fails at runtime with the library's own advice —
#: "Can't import ray.tune as some dependencies are missing. Run `pip install
#: "ray[tune]"` to fix." The extra appears nowhere in the source, so it has to
#: be known here. Longest path wins, so ray.tune.schedulers still maps to
#: ray[tune]. Keep this list to cases whose upstream genuinely gates the
#: submodule behind an extra — guessing one that does not exist turns a working
#: install into a resolver error.
_PY_SUBMODULE_TO_DIST = {
    "ray.tune": "ray[tune]",
    "ray.train": "ray[train]",
    "ray.serve": "ray[serve]",
    "ray.rllib": "ray[rllib]",
    "ray.data": "ray[data]",
    "dask.dataframe": "dask[dataframe]",
    "dask.array": "dask[array]",
    "dask.distributed": "distributed",     # a separate distribution, not an extra
}

#: reprobe's own import-name -> PyPI-distribution decisions. They WIN over the
#: vendored pipreqs table (_pipreqs_mapping.MAPPING), which is ~1,150 community
#: entries and is missing several very common research libraries (Bio, skimage,
#: umap, tables) and carries `tables -> pytables`, a name that is not the PyPI
#: distribution. A name that is not a real distribution is worse than a failed
#: install: anyone can register it, and the install phase has network.
_PY_IMPORT_TO_DIST = {
    "cv2": "opencv-python", "sklearn": "scikit-learn", "skimage": "scikit-image",
    "PIL": "pillow", "yaml": "pyyaml", "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil", "dotenv": "python-dotenv", "serial": "pyserial",
    "OpenGL": "PyOpenGL", "Crypto": "pycryptodome", "fitz": "pymupdf",
    "mpl_toolkits": "matplotlib", "google": "protobuf", "attr": "attrs",
    "jwt": "pyjwt", "usb": "pyusb", "zmq": "pyzmq", "lxml": "lxml",
    "tables": "tables", "netCDF4": "netcdf4", "pyreadstat": "pyreadstat",
    "Bio": "biopython", "umap": "umap-learn", "sksurv": "scikit-survival",
    "pydicom": "pydicom", "nibabel": "nibabel", "mne": "mne",
}


@lru_cache(maxsize=1)
def _pipreqs_mapping() -> dict[str, str]:
    """The vendored pipreqs table as ``{import_name: distribution}``. On a
    duplicate import name the LAST line wins, exactly as pipreqs itself does."""
    from ._pipreqs_mapping import MAPPING

    out: dict[str, str] = {}
    for line in MAPPING.splitlines():
        mod, sep, dist = line.partition(":")
        if sep and mod.strip() and dist.strip():
            out[mod.strip()] = dist.strip()
    return out


def _canonical(dist: str) -> str:
    """PEP 503 normal form (lower case, runs of ``-_.`` as one ``-``). PyPI treats
    ``Requests`` and ``requests`` as one name; reports and the install phase's own
    presence check should too."""
    return re.sub(r"[-_.]+", "-", dist).lower()


def dist_for_import(mod: str) -> str:
    """The PyPI distribution that provides top-level import ``mod``, in canonical
    form: reprobe's decision, else pipreqs', else the module name itself (true for
    most)."""
    return _canonical(_PY_IMPORT_TO_DIST.get(mod) or _pipreqs_mapping().get(mod) or mod)


@lru_cache(maxsize=1)
def _import_for_dist() -> dict[str, str]:
    """distribution -> a top-level module to probe for presence (the install
    phase checks the import before asking pip)."""
    inv: dict[str, str] = {}
    for table in (_pipreqs_mapping(), _PY_IMPORT_TO_DIST):      # ours last: ours wins
        for mod, dist in table.items():
            inv[_canonical(dist)] = mod
    return inv


def py_module_for_dist(dist: str) -> str:
    return _import_for_dist().get(_canonical(dist)) or dist.replace("-", "_")


# Cell magics whose body is not Python at all: the whole cell is skipped.
_NON_PYTHON_CELL_MAGICS = frozenset({
    "bash", "sh", "script", "r", "ruby", "perl", "javascript", "js", "html", "latex",
    "markdown", "sql", "writefile", "file", "cython", "sx", "system", "svg", "pypy",
    "python2", "python3", "julia", "node", "powershell", "cmd", "capture_stdout"})


def _py_ipynb_cells(p: Path) -> list[str]:
    """Code cells of a PYTHON-kernel notebook (empty list for any other kernel),
    with IPython magics neutralised so `ast` can parse each cell on its own.

    Per cell, not concatenated: one cell with a syntax error then costs that cell
    only, and `%%bash` / `%%writefile` bodies never reach the Python parser."""
    import json
    try:
        data = json.loads(_read_head(p, 5_000_000))
    except (ValueError, OSError, RecursionError):
        return []
    if not isinstance(data, dict):
        return []
    meta = data.get("metadata") or {}
    ks = meta.get("kernelspec") or {}
    lang = str(ks.get("language", "")).lower()
    li = str((meta.get("language_info") or {}).get("name", "")).lower()
    name = str(ks.get("name", "")).lower()
    # An absent kernelspec is treated as Python: that is the overwhelming default
    # for .ipynb, and the R scanner already claims the ones that say "R".
    if lang not in ("", "python") and li not in ("", "python") and not name.startswith("py"):
        return []
    cells: list[str] = []
    for cell in (data.get("cells") or []):
        if not isinstance(cell, dict) or cell.get("cell_type") != "code":
            continue
        src = cell.get("source")
        text = "".join(src) if isinstance(src, list) else str(src or "")
        lines = text.split("\n")
        if lines and lines[0].lstrip().startswith("%%"):
            magic = lines[0].lstrip()[2:].split(None, 1)[0].lower() if lines[0].lstrip()[2:].strip() else ""
            if magic in _NON_PYTHON_CELL_MAGICS:
                continue
            lines = lines[1:]
        cleaned = []
        for ln in lines:
            stripped = ln.lstrip()
            if stripped.startswith(("%", "!")):
                cleaned.append(ln[:len(ln) - len(stripped)] + "pass")   # keep blocks well-formed
            else:
                cleaned.append(ln)
        cells.append("\n".join(cleaned))
    return cells


def notebook_language(p: Path) -> str:
    """A notebook's kernel language: "python", "r", or whatever else it declares
    ("julia", ...). An absent or unreadable kernelspec is Python, the default
    for .ipynb — the same rule the import scanners use."""
    import json
    try:
        data = json.loads(_read_head(p, 5_000_000))
    except (ValueError, OSError, RecursionError):
        return "python"
    meta = (data.get("metadata") or {}) if isinstance(data, dict) else {}
    ks = meta.get("kernelspec") or {}
    lang = str(ks.get("language", "")).lower()
    li = str((meta.get("language_info") or {}).get("name", "")).lower()
    name = str(ks.get("name", "")).lower()
    if lang == "r" or li == "r" or name.startswith("ir"):
        return "r"
    if lang in ("", "python") and li in ("", "python") or name.startswith("py"):
        return "python"
    return lang or li or name


def _py_ipynb_source(p: Path) -> str:
    """All code cells joined — kept for callers that want one blob."""
    return "\n".join(_py_ipynb_cells(p))


def _local_module_names(root: Path, files) -> set[str]:
    """Module names the deposit itself provides, so its own files are never
    mistaken for PyPI distributions. `import helper` in a repo shipping
    helper.py must not try to install a package called "helper" — a name anyone
    could have registered.

    Deliberately broad: every .py stem, and every directory ABOVE a .py file (at
    any depth: a `src/pkg/` layout, a namespace package with no __init__.py, a
    scripts folder that is on sys.path when a script runs). Over-excluding costs
    one honest ModuleNotFoundError; under-excluding is a supply-chain hole."""
    local: set[str] = set()
    for p in files:
        try:
            rel_parts = p.relative_to(root).parts
        except ValueError:
            continue
        if p.suffix == ".py":
            local.add(p.stem)
            local.update(rel_parts[:-1])
        elif p.suffix in (".so", ".pyd"):
            local.add(p.name.split(".", 1)[0])
    return local


#: Exception names that make `try: import x / except ...:` an OPTIONAL import.
_IMPORT_GUARDS = frozenset({"ImportError", "ModuleNotFoundError", "Exception", "BaseException"})


def _guards_import(handlers) -> bool:
    """True when a try's handlers catch a failed import (`except ImportError`,
    a tuple containing it, or a bare `except:`), i.e. the author wrote a fallback
    and the module is optional — installing it would change which branch runs."""
    for h in handlers:
        t = h.type
        if t is None:
            return True
        nodes = t.elts if isinstance(t, ast.Tuple) else [t]
        for n in nodes:
            name = n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) else ""
            if name in _IMPORT_GUARDS:
                return True
    return False


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or \
           (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


class _Imports(ast.NodeVisitor):
    """Absolute imports a module needs at run time. Never executes anything:
    `ast.parse` builds a tree and this reads it."""

    def __init__(self) -> None:
        self.paths: set[str] = set()      # full dotted paths, e.g. "ray.tune"

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self.paths.add(a.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level or not node.module:         # `from . import x` is the deposit's own
            return
        self.paths.add(node.module)
        for a in node.names:                      # `from ray import tune` -> ray.tune
            if a.name != "*":
                self.paths.add(f"{node.module}.{a.name}")

    def visit_Try(self, node: ast.Try) -> None:
        if not _guards_import(node.handlers):
            for stmt in node.body:
                self.visit(stmt)
        for stmt in [*node.handlers, *node.orelse, *node.finalbody]:
            self.visit(stmt)

    visit_TryStar = visit_Try

    def visit_If(self, node: ast.If) -> None:
        if not _is_type_checking(node.test):      # type-only imports are never executed
            for stmt in node.body:
                self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)


def _py_import_paths(text: str) -> set[str]:
    """Dotted import paths in ``text``: from the syntax tree when it parses, from
    the line-based fallback when it does not (Python 2 sources, odd cells)."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")       # invalid escapes etc. are not our business
            tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        paths: set[str] = set()
        for chunk in _PY_IMPORT_RE.findall(text):
            paths.update(x.strip() for x in chunk.split(","))
        paths.update(_PY_FROM_RE.findall(text))
        return paths
    v = _Imports()
    try:
        v.visit(tree)
    except RecursionError:
        return set()
    return v.paths


def scan_py_packages(root: Path, files) -> list[str]:
    """PyPI distributions the deposit's Python code imports.

    Excludes the standard library, the deposit's own modules, relative imports,
    optional (try/except ImportError) imports and type-checking-only imports.
    Returns sorted distribution names, each matching _PY_DIST_RE. The caller
    decides what to install; this only says what the code reaches for."""
    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    local = _local_module_names(root, files)
    paths: set[str] = set()
    for p in files:
        suf = p.suffix.lower()
        if suf == ".py":
            sources = [_read_head(p)]
        elif suf == ".ipynb":
            sources = _py_ipynb_cells(p)
        else:
            continue
        for text in sources:
            if text:
                paths |= _py_import_paths(text)
    dists: set[str] = set()
    for path in paths:
        top = path.split(".", 1)[0]
        if not re.fullmatch(_PY_MOD, top) or top in stdlib or top in local or top.startswith("_"):
            continue
        dist = dist_for_import(top)
        if _PY_DIST_RE.match(dist):
            dists.add(dist)
    dists |= _extra_dists(paths, local)
    # `ray[tune]` supersedes a bare `ray`: it is the same distribution, and
    # installing both would leave the run depending on which pip resolved last.
    superseded = {d.split("[", 1)[0] for d in dists if "[" in d}
    return sorted(d for d in dists if d not in superseded)


def _extra_dists(paths: set[str], local: frozenset[str] | set[str]) -> set[str]:
    """Distributions whose EXTRA a dotted import implies (ray.tune -> ray[tune]).
    Longest known prefix wins."""
    out: set[str] = set()
    for path in paths:
        parts = path.split(".")
        if parts[0] in local:            # the deposit's own package, not PyPI
            continue
        for n in range(len(parts), 1, -1):
            dist = _PY_SUBMODULE_TO_DIST.get(".".join(parts[:n]))
            if dist:
                out.add(dist)
                break
    return out


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


# One alternation for every R lexical form that can hide a '#': a quoted string
# (which may span lines), a backtick name, or a comment. Unterminated strings
# run to end of input rather than failing, so a hostile file with thousands of
# stray quotes costs one linear pass, not one scan to EOF per quote.
_R_LEX_RE = re.compile(r"""(?s)("(?:[^"\\]|\\.)*(?:"|\Z)|'(?:[^'\\]|\\.)*(?:'|\Z)|`[^`]*(?:`|\Z))|(#[^\n]*)""")


def _r_views(text: str) -> tuple[str, str]:
    """(clean, masked): the source with comments blanked, and the same with every
    string's CONTENT blanked too. Both keep the original length and newlines, so
    an index found in one is valid in the other.

    Patterns that name code (`pkg::fn`, `library(`) run on ``masked`` — a package
    mentioned inside a string or a comment is not a dependency — while argument
    values are read from ``clean``, where the quoted names are still visible."""
    def blank(m: re.Match) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))

    def clean_repl(m: re.Match) -> str:
        return m.group(1) if m.group(1) is not None else blank(m)

    def masked_repl(m: re.Match) -> str:
        if m.group(1) is None:
            return blank(m)
        lit = m.group(1)
        return lit[0] + re.sub(r"[^\n]", " ", lit[1:-1]) + (lit[-1] if len(lit) > 1 else "")

    return _R_LEX_RE.sub(clean_repl, text), _R_LEX_RE.sub(masked_repl, text)


def _strip_r_comments(text: str) -> str:
    """The source with `# …` comments removed — but not a '#' inside a string, so
    `install.packages("pkg#1")` and `url <- "http://x/#frag"` are left alone."""
    return _r_views(text)[0]


_R_FENCE_OPEN_RE = re.compile(r"^`{3,}[ \t]*\{[ \t]*[rR][ ,}]")
_R_FENCE_CLOSE_RE = re.compile(r"^`{3,}[ \t]*$")
_R_INLINE_RE = re.compile(r"`r[ \t]+([^`\n]+)`")


def _r_literate_code(text: str) -> str:
    """The R code inside an R Markdown / Quarto document: its ```{r} chunks and
    inline `r …` spans, never its prose. Scanning the whole file read a sentence
    like "call library(foo) first" in the narrative as a dependency.

    One pass over the lines. (A lazy multi-line regex for the fence pair was cubic
    on a file of unclosed fences: 7 KB took twelve seconds.) A chunk with no closing
    fence runs to the end of the file, as it does for knitr."""
    out: list[str] = []
    in_chunk = False
    for line in text.splitlines():
        stripped = line.strip()
        if in_chunk:
            if _R_FENCE_CLOSE_RE.match(stripped):
                in_chunk = False
            else:
                out.append(line)
        elif _R_FENCE_OPEN_RE.match(stripped):
            in_chunk = True
    out.extend(_R_INLINE_RE.findall(text))
    return "\n".join(out)


#: Longest argument list read as a package-loading call. `library(dplyr)` and a
#: `p_load()` of fifty packages are far under it; a megabyte of nested `library(`
#: is not code anyone wrote.
_R_MAX_CALL_ARGS = 4000
#: Characters of call arguments examined per file, in total.
_R_ARG_BUDGET = 2_000_000


def _paren_pairs(masked: str) -> dict[int, int]:
    """``{index of '(' : index of its ')'}`` for the whole text in ONE linear pass.

    ``masked`` has string contents blanked, so a bracket inside a string never
    counts. An unclosed '(' has no entry. (Searching forward from every call for
    its closing bracket was quadratic: 64 KB of ``library(`` took 22 seconds.)"""
    pairs: dict[int, int] = {}
    stack: list[int] = []
    for i, ch in enumerate(masked):
        if ch == "(":
            stack.append(i)
        elif ch == ")" and stack:
            pairs[stack.pop()] = i
    return pairs


def _split_args(args: str) -> list[str]:
    """Top-level, comma-separated arguments of an R argument list (strings and
    nested brackets respected)."""
    out, cur, depth, quote = [], [], 0, ""
    for ch in args:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


_R_NAMED_ARG_RE = re.compile(r"^([A-Za-z._][A-Za-z0-9._]*)\s*=(?!=)\s*(.*)$", re.S)
_R_STRING_ARG_RE = re.compile(r"""^(['"])(.+?)\1$""")


def _r_loaded_packages(clean: str, masked: str) -> set[str]:
    """Package names a script LOADS: library()/require()/requireNamespace()/
    loadNamespace() and pacman's p_load()/p_install().

    - `library(pkg, character.only = TRUE)` takes pkg from a VARIABLE; the
      literal text is not a package name, so a bare name there is skipped (a
      quoted one is still a package).
    - `p_load(a, b, "c", install = FALSE)` names every positional argument;
      options (`install=`, `update=`) are not packages.
    - Only the first argument (or `package=`) of library/require is a package;
      the rest are options (`quietly = TRUE`)."""
    found: set[str] = set()
    pairs = _paren_pairs(masked)
    budget = _R_ARG_BUDGET
    for m in _R_LOAD_CALL_RE.finditer(masked):
        fn = m.group(1)
        open_idx = m.end() - 1
        close = pairs.get(open_idx)
        if close is None or close - open_idx > _R_MAX_CALL_ARGS:
            continue                                # unclosed, or not a real call
        budget -= close - open_idx
        if budget < 0:
            break
        args = _split_args(clean[open_idx + 1:close])
        positional, named = [], {}
        for a in args:
            nm = _R_NAMED_ARG_RE.match(a)
            if nm:
                named[nm.group(1)] = nm.group(2).strip()
            elif a:
                positional.append(a)
        char_only = named.get("character.only", "").upper() in ("TRUE", "T")
        if fn in ("p_load", "p_load_gh", "p_install", "p_install_gh"):
            cands = positional
            # p_load_gh("user/repo") names a GitHub repo, not a CRAN package
            if fn.endswith("_gh"):
                continue
        else:
            first = positional[0] if positional else named.get("package", "")
            cands = [first] if first else []
        for a in cands:
            sm = _R_STRING_ARG_RE.match(a)
            if sm:
                name = sm.group(2)
            elif char_only or not re.fullmatch(_R_PKG, a):
                continue                            # a variable / an expression, not a name
            else:
                name = a
            if re.fullmatch(_R_PKG, name):
                found.add(name)
    return found


_R_VEC_ASSIGN_RE = re.compile(r"(?<![\w.$@])([A-Za-z.][\w.]*)\s*(?:<-|=)\s*c\s*\(")
_R_APPLY_RE = re.compile(r"(?<![\w.$@])(?:\w+::)?(?:lapply|sapply|vapply|walk|map)\s*\(")
_R_FOR_IN_RE = re.compile(r"(?<![\w.])for\s*\(\s*[A-Za-z.][\w.]*\s+in\s+")
_R_LOADERS = {"library", "require", "requireNamespace", "loadNamespace"}


def _r_string_vector(clean: str, masked: str, open_idx: int, pairs: dict[int, int]) -> list[str]:
    """The string literals of ``c(...)`` whose '(' is at ``open_idx``."""
    close = pairs.get(open_idx)
    if close is None or close - open_idx > _R_MAX_CALL_ARGS:
        return []
    out = []
    for a in _split_args(clean[open_idx + 1:close]):
        sm = _R_STRING_ARG_RE.match(a.strip())
        if sm and re.fullmatch(_R_PKG, sm.group(2)):
            out.append(sm.group(2))
    return out


def _r_indirect_packages(clean: str, masked: str) -> set[str]:
    """Packages loaded through a character vector rather than by name — the
    idioms research scripts use to load a list at once:

        pkgs <- c("lme4", "emmeans"); invisible(lapply(pkgs, library, character.only = TRUE))
        sapply(c("car", "psych"), require, character.only = TRUE)
        for (p in pkgs) library(p, character.only = TRUE)
        pacman::p_load(char = pkgs)

    Only vectors of string literals count, and only when they reach a loader;
    a vector of file names that is never loaded names no package."""
    pairs = _paren_pairs(masked)
    vectors: dict[str, list[str]] = {}
    for m in _R_VEC_ASSIGN_RE.finditer(masked):
        vals = _r_string_vector(clean, masked, m.end() - 1, pairs)
        if vals:
            vectors.setdefault(m.group(1), []).extend(vals)

    def resolve(arg: str) -> list[str]:
        arg = arg.strip()
        if arg in vectors:
            return vectors[arg]
        cm = re.match(r"c\s*\(", arg)
        if cm:
            inner_open = clean.find(arg)          # locate this literal in the text
            if inner_open >= 0:
                return _r_string_vector(clean, masked, inner_open + cm.end() - 1, pairs)
        return []

    found: set[str] = set()
    for m in _R_APPLY_RE.finditer(masked):
        close = pairs.get(m.end() - 1)
        if close is None:
            continue
        args = _split_args(clean[m.end():close])
        if len(args) < 2:
            continue
        fun = args[1].split("=", 1)[-1].strip() if args[1].strip().startswith("FUN") else args[1].strip()
        if fun.split("::")[-1] in _R_LOADERS:
            found.update(resolve(args[0]))
    char_only_load = re.search(r"character\.only\s*=\s*(?:TRUE|T)\b", masked) is not None
    if char_only_load:
        for m in _R_FOR_IN_RE.finditer(masked):
            rest = clean[m.end():m.end() + 200]
            name = re.match(r"([A-Za-z.][\w.]*)\s*\)", rest)
            if name:
                found.update(vectors.get(name.group(1), []))
            elif rest.startswith("c"):
                found.update(resolve(rest[:rest.find(")") + 1]))
    for m in re.finditer(r"(?<![\w.])(?:pacman::)?p_load\s*\(", masked):
        close = pairs.get(m.end() - 1)
        if close is None:
            continue
        for a in _split_args(clean[m.end():close]):
            nm = _R_NAMED_ARG_RE.match(a)
            if nm and nm.group(1) == "char":
                found.update(resolve(nm.group(2)))
    return found


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
    clean, masked = _r_views(text)
    pairs = _paren_pairs(masked)
    pkgs: set[str] = set()
    frontier = []
    budget = _R_ARG_BUDGET
    for m in re.finditer(r"(?<![\w.])install\.packages\s*\(", masked):
        close = pairs.get(m.end() - 1)
        if close is None or close - m.end() > 50 * _R_MAX_CALL_ARGS:   # a long c("a", "b", …) is real
            continue
        budget -= close - m.end()
        if budget < 0:
            break
        frontier.append(_first_arg(clean[m.end():close]))
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
    sources (.R, and the code chunks of .Rmd/.qmd), R-kernel notebooks, and
    DESCRIPTION. Returns a sorted list with base/recommended packages removed;
    each name matches _R_PKG_NAME_RE.

    Comments and string contents are not code: `# library(old)` and
    `"foo.bar::baz"` name no dependency."""
    found: set[str] = set()
    for p in files:
        suf = p.suffix.lower()
        if p.name == "DESCRIPTION":
            found |= _description_packages(_read_head(p))
            continue
        if suf == ".r":
            text = _read_head(p)
        elif suf in _R_LITERATE:
            text = _r_literate_code(_read_head(p))
        elif suf == ".ipynb":
            text = _r_ipynb_source(p)
        else:
            continue
        if not text:
            continue
        clean, masked = _r_views(text)
        found |= _r_loaded_packages(clean, masked)
        found |= _r_indirect_packages(clean, masked)
        found.update(_R_NS_RE.findall(masked))
        if suf in (".r", *_R_LITERATE):
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
_STAGE_MODEL_RE = re.compile(
    r"(?:^|[_\-. ])(?:train|training|fit|fitting|model|models|modelling|modeling|estimate|"
    r"simulate|simulation)(?:[_\-. ]|$)", re.IGNORECASE)
_STAGE_EVAL_RE = re.compile(
    r"(?:^|[_\-. ])(?:eval|evaluate|evaluation|test|testing|validate|validation|predict|"
    r"inference)(?:[_\-. ]|$)", re.IGNORECASE)
_STAGE_LATE_RE = re.compile(
    r"(?:^|[_\-. ])(?:analyse|analyze|analysis|aggregate|aggregated|combine|combined|"
    r"consensus|summar(?:y|ise|ize|ised|ized)|compare|comparison|report|plot|plots|"
    r"figure|figures|visuali[sz]e|visuali[sz]ation|results?|tables?)(?:[_\-. ]|$)",
    re.IGNORECASE)


def _stage_rank(name: str) -> int:
    """0 = data prep, 1 = model fitting/training, 2 = unclassified,
    3 = evaluation, 4 = downstream aggregation/reporting.

    A repo whose files carry no numeric prefix and whose README does not order
    them falls back to alphabetical, which silently puts an aggregator first when
    its name sorts early (`analyse_combined_*.ipynb` before `PDRA_*.ipynb`). That
    is worse than cosmetic: the aggregator then reads the *committed* outputs of
    steps that have not re-run yet, so it can pass on stale data and turn a
    broken pipeline green. Ranking by name is a guess, so it ranks BELOW both the
    numeric prefix and README order — it only decides otherwise-arbitrary ties."""
    if _STAGE_LATE_RE.search(name):
        return 4
    if _STAGE_EVAL_RE.search(name):
        return 3
    if _STAGE_EARLY_RE.search(name):
        return 0
    if _STAGE_MODEL_RE.search(name):
        return 1
    return 2


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


_R_SOURCE_RE = re.compile(r"""(?<![\w.])(?:sys\.)?source\s*\(\s*(?P<arg>[^)]*)\)""")
_R_STR_RE = re.compile(r"""["']([^"']+)["']""")


def _sourced_r_files(root: Path, r_scripts: list[Path], docs: tuple[Path, ...] = ()) -> set[Path]:
    """R scripts that another script loads with ``source("…")`` (a literal path, or
    ``here::here("dir", "x.R")`` / ``file.path("dir", "x.R")``), resolved against the
    repository root and against the sourcing script's own directory. Comments and
    strings elsewhere are ignored (``_r_views``)."""
    by_path = {q.resolve(): q for q in r_scripts}
    found: set[Path] = set()
    for p in [*r_scripts, *docs]:                   # .Rmd/.qmd chunks source helpers too
        text = _read_head(p) if p.suffix.lower() == ".r" else _r_literate_code(_read_head(p))
        clean, masked = _r_views(text)
        for m in _R_SOURCE_RE.finditer(masked):
            parts = _R_STR_RE.findall(clean[m.start("arg"):m.end("arg")])
            if not parts:
                continue
            rel_path = "/".join(parts) if len(parts) > 1 else parts[0]
            for base in (root, p.parent, root / r_working_dir(p, root)):
                try:
                    cand = (base / rel_path).resolve()
                except (OSError, ValueError):
                    continue
                if cand in by_path and by_path[cand] != p:
                    found.add(by_path[cand])
    return found


_KNIT_CHILD_RE = re.compile(r"""(?:\bchild\s*=\s*(?:c\s*\(\s*)?|knit_child\s*\(\s*)["']([^"']+\.[Rr]md)["']""")
#: Directories of vendored third-party code: never the artifact's own steps.
_THIRD_PARTY_DIRS = {"third_party", "thirdparty", "third-party", "vendor", "vendored",
                     "external", "extern", "externals", "submodules"}


def r_working_dir(script: Path, root: Path) -> str:
    """Where an R script runs from: the nearest folder between it and the root
    that holds an RStudio project (`*.Rproj`) — RStudio's working directory, and
    so the one the author's relative paths were written against — else the
    root. "" means the root."""
    try:
        rel = script.relative_to(root)
    except ValueError:
        return ""
    for i in range(len(rel.parts) - 1, 0, -1):
        d = root.joinpath(*rel.parts[:i])
        if any(d.glob("*.Rproj")):
            return "/".join(rel.parts[:i])
    return ""


_PY_PROJECT_FILES = ("pyproject.toml", "setup.py", "setup.cfg")


def own_package_root(root: Path) -> str | None:
    """Where the artifact's own Python package lives, if it ships one: "src" for
    a src layout, "" for packages at the root, None when there is none. A script
    doing `from mypkg import x` relies on `pip install -e .`; the harness never
    runs the project's build (its backend is the artifact's code, and the install
    phase has network), so it puts this directory on PYTHONPATH instead."""
    if not any((root / f).is_file() for f in _PY_PROJECT_FILES):
        return None

    def has_pkg(d: Path) -> bool:
        try:
            return any((c / "__init__.py").is_file() for c in d.iterdir()
                       if c.is_dir() and c.name not in ("tests", "test", "docs"))
        except OSError:
            return False
    if (root / "src").is_dir() and has_pkg(root / "src"):
        return "src"
    return "" if has_pkg(root) else None


def _knit_children(rmds: list[Path]) -> set[Path]:
    """R Markdown files another document includes as a knitr child (a chunk
    option ``child=`` or ``knit_child()``). Rendered on their own they lack the
    parent's objects and fail — a failure that is not the artifact's."""
    by_path = {q.resolve(): q for q in rmds}
    out: set[Path] = set()
    for p in rmds:
        for m in _KNIT_CHILD_RE.finditer(_read_head(p)):
            try:
                cand = (p.parent / m.group(1)).resolve()
            except (OSError, ValueError):
                continue
            if cand in by_path and by_path[cand] != p:
                out.add(by_path[cand])
    return out


def _bookdown_dirs(rmds: list[Path]) -> set[Path]:
    """Directories holding a bookdown book: a `_bookdown.yml`, or an index.Rmd
    whose YAML names a bookdown output. Its chapters are rendered together by
    bookdown::render_book, never one by one."""
    dirs = {p.parent for p in rmds if (p.parent / "_bookdown.yml").is_file()}
    for p in rmds:
        if p.name.lower() == "index.rmd" and "bookdown::" in _read_head(p)[:4000]:
            dirs.add(p.parent)
    return dirs


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

    def third_party(p: Path) -> bool:
        return any(part.lower() in _THIRD_PARTY_DIRS for part in p.relative_to(root).parts[:-1])

    def depth(p: Path) -> int:
        return len(p.relative_to(root).parts)

    def numbered(p: Path) -> bool:
        return _NUM_RE.search(p.name) is not None

    qmds = [p for p in files if p.suffix.lower() == ".qmd"]
    vendored = [p for p in notebooks + rmds + r_scripts + py_files if third_party(p)]
    notebooks = [p for p in notebooks if not third_party(p)]
    # knitr children and bookdown chapters are rendered as part of their parent
    children = _knit_children(rmds)
    books = _bookdown_dirs(rmds)
    chapters = [p for p in rmds if p.parent in books]
    rmds = [p for p in rmds if not third_party(p) and p not in children and p.parent not in books]

    # R helpers that other scripts (or R Markdown / Quarto chunks) source() are
    # not steps of their own (running them standalone executes them twice, and
    # adds trivial "passes"); nor is the R/ of an R package, whose functions are
    # loaded, not run.
    sourced = _sourced_r_files(root, r_scripts, tuple(rmds + qmds + chapters + list(children)))
    is_pkg = (root / "DESCRIPTION").is_file()
    r_candidates = [p for p in r_scripts if p not in sourced and not third_party(p)
                    and not (is_pkg and p.relative_to(root).parts[0] == "R")]
    # Python files inside a package (a directory with __init__.py) are modules: run
    # as a script, `from . import x` fails with an ImportError blamed on the artifact.
    py_candidates = [p for p in py_files if not (p.parent / "__init__.py").is_file()
                     and not third_party(p)]

    planned: list[tuple[Path, str]] = []          # (path, runner), ordered below
    unscheduled: dict[str, list[str]] = {}
    if notebooks:
        types.append("jupyter")
        planned += [(nb, "jupyter") for nb in notebooks]
    if rmds:
        types.append("rmarkdown")
        planned += [(rmd, "rmarkdown") for rmd in rmds]

    # A numbered script is a step of a pipeline wherever the rest of it lives:
    # `01_clean_data.py` before `02_analysis.ipynb` is not optional preparation,
    # and an entry-named `analysis_plots.R` does not replace `01_prep.R`.
    # (One directory deep at most: numbered files further down are fixtures.)
    # R scripts: entry-named and numbered scripts always run; the full set only
    # when nothing else is known to drive the analysis.
    if r_scripts:
        types.append("r")
        entry_r = [p for p in r_candidates if _ENTRY_R_RE.match(p.stem) and _entry_shallow(p, root)]
        numbered_r = [p for p in r_candidates if numbered(p) and depth(p) <= 2]
        # numbered scripts are ADDED to what runs; they never stand in for the rest
        driven = notebooks or rmds or entry_r
        chosen = list(dict.fromkeys(entry_r + numbered_r)) if driven else r_candidates
        planned += [(p, "r") for p in chosen]
        unscheduled["R"] = [rel(p) for p in r_candidates if p not in chosen]
        if sourced:
            notes.append(f"{len(sourced)} R file(s) are source()d by other scripts or documents and "
                         "run as part of them, not as steps of their own: "
                         + ", ".join(sorted(rel(p) for p in sourced)[:8]))

    # Python scripts: entry-named and numbered scripts always run; otherwise the
    # top-level scripts, unless notebooks drive the analysis.
    if py_files:
        entry = [p for p in py_candidates
                 if p.name in _ENTRY_PY or (_entry_shallow(p, root) and _ENTRY_PY_RE.match(p.name))]
        numbered_py = [p for p in py_candidates if numbered(p) and depth(p) <= 2]
        top = [p for p in py_candidates if depth(p) == 1]
        driven = notebooks or entry
        chosen = list(dict.fromkeys((entry if driven else top) + numbered_py))
        if chosen or py_candidates:
            types.append("python")
        planned += [(p, "python") for p in chosen]
        if not driven and len(top) > 3:
            notes.append("multiple top-level .py scripts and no clear entry point; ordering is best-effort")
        # Scripts one directory down are named too: `scripts/01_x.py` and friends
        # were dropped without a word when the plan came from elsewhere.
        unscheduled["Python"] = [rel(p) for p in py_candidates if p not in chosen and depth(p) <= 2]

    # ONE order across languages: `01_preprocess.py` runs before `02_model.R`.
    # Without step numbers, README order, then the stage a name suggests (prep,
    # model, other, evaluation, reporting) decide — before the language does:
    # grouping by runner first ran `results.ipynb` before `train.py`. At equal
    # stage an R Markdown document goes last: it renders what the scripts made.
    group = {"jupyter": 0, "python": 1, "r": 2, "rmarkdown": 3}

    def plan_key(pr: tuple[Path, str]):
        k = _order_key(pr[0], root, readme)
        return (k[0], k[1], k[2], group[pr[1]], k[3])

    planned.sort(key=plan_key)
    pkg_root = own_package_root(root)
    for path, runner in planned:
        args: dict = {}
        if runner == "jupyter":
            args["language"] = notebook_language(path)
        if pkg_root is not None and (runner == "python" or args.get("language") == "python"):
            args["pythonpath"] = pkg_root
        elif runner == "r" and (wd := r_working_dir(path, root)):
            args["cwd"] = wd
        steps.append(RunStep(runner=runner, target=rel(path), kind=runner, args=args))

    # Say so when the run order is a pure guess. Multi-step pipelines are order-
    # sensitive in a way that does not announce itself: a downstream step run too
    # early silently consumes the committed outputs of steps that have not re-run,
    # so it can pass on stale data. A reviewer must be told the order was inferred.
    if len(planned) > 1 and not any(numbered(p) for p, _ in planned) \
            and not any(p.name in readme for p, _ in planned):
        notes.append(
            "run order is inferred, not declared: no numeric filename prefixes and the README does not "
            "reference these files by name. The order follows what the names suggest (data "
            "preparation, model fitting, evaluation, then analysis/plots/reports last). If the real "
            "order differs, declare `run: steps:` in autoui-repro.yml — a mis-ordered aggregation "
            "step can read stale committed outputs and pass.")

    # Never drop a script silently: a verdict of `runs` with 2 of 3 scripts never
    # executed would be read as "the analysis runs".
    for lang, names in unscheduled.items():
        if names:
            why = ("notebooks/R Markdown assumed to drive the analysis" if (notebooks or rmds)
                   else "neither entry-named nor numbered, and other scripts were found to run the "
                        "analysis")
            notes.append(f"{len(names)} {lang} script(s) present but NOT run ({why}): "
                         + ", ".join(names[:8]) + (" …" if len(names) > 8 else "")
                         + ". If they are part of the analysis, declare `run: steps:` in "
                           "autoui-repro.yml")
    if pkg_root is not None and any(s.args.get("pythonpath") is not None for s in steps):
        notes.append(f"the artifact's own Python package ({pkg_root or 'repository root'}) was put on "
                     "PYTHONPATH, as `pip install -e .` would make it importable — without building "
                     "or installing the project")
    if qmds:
        notes.append(f"{len(qmds)} Quarto document(s) present but NOT run (reprobe has no Quarto "
                     "renderer; their R packages were still installed): "
                     + ", ".join(rel(p) for p in qmds[:8]))
    if children:
        notes.append(f"{len(children)} R Markdown file(s) are knitr children of another document and "
                     "render as part of it: " + ", ".join(sorted(rel(p) for p in children)[:8]))
    if books:
        notes.append("bookdown book(s) in " + ", ".join(sorted(rel(d) or "." for d in books))
                     + " NOT rendered: chapters only render together, through "
                       "bookdown::render_book, which reprobe does not run")
    if vendored:
        notes.append(f"{len(vendored)} file(s) under third-party directories were not considered as "
                     "steps (vendored code is not the artifact's own)")

    for proj in unity:
        types.append("unity")
        steps.append(RunStep(runner="unity", target=rel(proj) if proj != root else ".", kind="unity"))

    # repo2docker hints (repo root or binder/, per repo2docker conventions)
    # (environment.yml is not among them: the install phase builds it.)
    for hint in ("Dockerfile", "postBuild", "apt.txt", "start", "runtime.txt"):
        if any((base / hint).is_file() for base in (root, root / "binder")):
            flags.append("needs-repo2docker")
            notes.append(f"found {hint}: the artifact declares system-level setup that the pinned "
                         "base image may lack, and reprobe does not apply it")
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
                   if p.suffix.lower() in (".r", *_R_LITERATE, ".ipynb") or p.name == "DESCRIPTION"]
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
    return any("best-effort" in n or "no clear entry" in n or "NOT run" in n
               for n in result.notes)
