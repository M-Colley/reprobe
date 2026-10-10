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
_NUM_RE = re.compile(r"(\d+)")
_ENTRY_PY = {"main.py", "run.py", "analysis.py", "analyze.py", "pipeline.py",
             "train.py", "evaluate.py", "reproduce.py", "make_figures.py"}
# Word-boundary stems only ("run_all.py" yes, "runners.py"/"mainwindow.py" no),
# and only at shallow depth (see _entry_shallow) so nested library helpers are
# never executed as top-level steps. Exact _ENTRY_PY names match at any depth.
_ENTRY_PY_RE = re.compile(r"^(main|run|analysis|analyze|pipeline|reproduce|train|evaluate|make_figures?)([_-]|\.py$)", re.I)
_ENTRY_R_RE = re.compile(r"^(main|run|analy|reproduce)", re.I)
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
