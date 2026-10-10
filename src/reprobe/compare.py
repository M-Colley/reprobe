"""Compare the outputs a re-run regenerated with the copies the authors committed.

Why this exists
---------------
A step that runs to completion shows the code *runs*. Whether it gives the *same
results* is what a Results Reproduced review asks, and the most direct evidence
an automated tool can offer is a file-by-file comparison: when a step rewrites a
file the authors committed (``results/table2.csv``, ``out/model_summary.txt``),
the committed copy is what the paper was written from and the regenerated copy
is what the code produces today.

The orchestrator copies the fetched tree ``srcdir`` into ``rundir`` and runs the
steps there; a committed file that the run rewrote is a *regenerated committed
output*. :func:`compare_outputs` compares each ``srcdir/<path>`` with
``rundir/<path>`` and returns one result per file; :func:`summarize` counts them.

What it does NOT claim
----------------------
A match is evidence for the human reviewer, never an automatic Results Reproduced
badge: the committed file may itself be stale, and the paper may quote numbers
from somewhere else. A mismatch is a finding to review, not a verdict: unseeded
random number generators, thread scheduling in parallel reductions, BLAS builds
and library version drift all move numbers legitimately. "not-compared" means
exactly that; nothing is guessed in either direction.

Status vocabulary
-----------------
identical     byte-for-byte the same.
equivalent    not byte-identical, but equal after the normalization documented for
              the file's kind below, with every number within tolerance.
differs       compared and found different beyond tolerance; ``detail`` says how and
              the extra keys carry the evidence (shapes, counts, largest differences,
              the first few differing locations).
not-compared  could not be judged: too large, unreadable or unparseable, a link,
              missing on one side, an unsafe path, or a format with no comparator.
              ``reason`` is a stable code and ``detail`` says why.

Numbers. Two floats match when ``|regenerated - committed| <= atol + rtol *
|committed|`` (``numpy.isclose`` semantics with the committed value as the
reference, used for every kind so a value pair gets the same verdict in a CSV, a
JSON file or a console log). NaN matches NaN; an infinity matches only itself.
Two *integers* must be equal: they are counts, sample sizes, seeds and IDs, which
have no rounding noise, and at rtol=1e-6 a tolerance would let n=1234568 pass for
n=1234567.

Kinds (chosen by file extension, never by sniffing content)
------------------------------------------------------------
tabular  .csv, .tsv, and .parquet when pyarrow>=14.0.1 is installed. Parsed with
         pandas. Column names *and order* must match (order is part of the output:
         scripts and readers address columns by position), and so must the row
         count. Cells are then compared one by one: numeric columns with the
         tolerance, other columns exactly, except that cells which parse as numbers
         on both sides (e.g. "0.023" in a p-value column that also holds "<0.001")
         use the tolerance. pandas' default missing markers ("", "NA", "NaN",
         "null", ...) all mean missing, and missing matches missing. When the first
         line is entirely numeric in both files, they are read as headerless
         (``numpy.savetxt`` output), so noise in row 1 is not mistaken for renamed
         columns. Row labels that pandas would turn into an index stay a compared
         column.
json     .json. Parsed with the stdlib. Object key order is ignored (JSON objects
         are unordered, RFC 8259). Keys, list lengths and order, and leaf types must
         match (``true`` is not ``1``). Numbers use the tolerance; other leaves must
         be equal.
text     .txt, .md, .log, .tex, .html, .htm, .Rout. Decoded as UTF-8 (no other
         encoding is guessed). Pass 1 removes line-ending differences (CRLF/CR to
         LF), trailing whitespace on each line, a leading BOM and trailing blank
         lines. If the texts still differ, pass 2 is number-aware: runs of
         whitespace are collapsed (console tables re-align when a value gains a
         minus sign or a digit), every numeric token is pulled out by regex, the
         remaining text must match exactly, and the numbers must match within
         tolerance. A regenerated R/Python console summary that differs only in the
         10th decimal is therefore equivalent, while a changed word, a changed count,
         or a value that moved in a printed digit is not. Base64 ``data:`` URIs
         (plots knitted into HTML) are masked first and flagged when they changed,
         because a re-rendered image is not evidence either way.
binary   everything else (images, PDF, .rds, .xlsx, ...): identical or not-compared.
         A differing PNG says nothing about the science (timestamps, compression and
         font rendering change the bytes), so a binary file is never "differs".

Known blind spot: a value that sits on a rounding boundary can print as 0.1234 in
one run and 0.1235 in the other while the underlying values agree within
tolerance. That shows up as "differs", with the evidence needed to dismiss it.

Robustness. This runs in the trusted host process on author-controlled files.
Links (symlinks, Windows junctions) are never followed, on either side and at any
path component. Paths must be relative and stay inside their tree. Sizes are
checked before anything is read, and pandas is handed bytes, never a path or a URL.
Every per-file failure becomes a not-compared result, so one bad file never breaks
the batch. pandas and numpy are imported only when a tabular file needs them.
"""

from __future__ import annotations

import difflib
import io
import json
import math
import os
import re
import stat
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

STATUSES = ("identical", "equivalent", "differs", "not-compared")

_TABULAR_SEP = {".csv": ",", ".tsv": "\t"}
_PARQUET = ".parquet"
_TEXT = frozenset({".txt", ".md", ".log", ".tex", ".html", ".htm", ".rout"})

# A results table this wide is a matrix dump; refusing it bounds pandas' per-column
# overhead on an author-controlled file.
_MAX_COLUMNS = 10_000
_MAX_EXAMPLES = 5       # differing locations quoted as evidence per file
_MAX_LISTED = 20        # column names / JSON paths listed per file
_SHOW = 80              # characters of a value or line quoted as evidence
# difflib is quadratic in the worst case. Past this many lines, count differing
# lines by position instead (an insertion then counts every later line).
_DIFFLIB_MAX_LINES = 5_000
# pyarrow before 14.0.1 deserializes (unpickles) extension types embedded in a
# Parquet file, which runs code from the file (CVE-2023-47248).
_MIN_PYARROW = (14, 0, 1)

_NUMBER = re.compile(r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)")
_INTEGER = re.compile(r"[-+]?\d+")
_DATA_URI = re.compile(r"data:[\w.+/-]+;base64,[A-Za-z0-9+/=]+")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_LINK_TAGS = frozenset(
    t for t in (getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0), getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0)) if t
)


class _Skip(Exception):
    """This file cannot be judged. ``reason`` is a stable machine-readable code."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------- #
#  Public API
# --------------------------------------------------------------------------- #

def compare_outputs(srcdir: Path, rundir: Path, regenerated: Iterable[str], *,
                    rtol: float = 1e-6, atol: float = 1e-12,
                    max_bytes: int = 50_000_000, max_files: int = 200) -> list[dict]:
    """Compare each regenerated committed output with the committed copy.

    ``regenerated`` holds relative POSIX paths (as ``runners.base.snapshot`` keys
    them) of files that exist in ``srcdir`` and that the run rewrote in ``rundir``.
    Returns one dict per path, sorted by path, with at least ``path``, ``kind``,
    ``status`` and ``detail`` (see the module docstring), plus ``reason`` when
    not-compared and kind-specific evidence otherwise. Every value is plain JSON.
    """
    if rtol < 0 or atol < 0:
        raise ValueError("rtol and atol must be non-negative")
    srcdir, rundir = Path(srcdir), Path(rundir)
    results: list[dict] = []
    wanted: dict[str, None] = {}
    for raw in regenerated:
        try:
            wanted.setdefault(_clean_rel(raw), None)
        except _Skip as skip:
            results.append(_not_compared(_display(raw), skip))

    # Spend the file budget where a comparison can say something: a binary file
    # can only come out identical or not-compared.
    order = sorted(wanted, key=lambda rel: (_kind(rel) == "binary", rel))
    for i, rel in enumerate(order):
        if i >= max(max_files, 0):
            results.append(_not_compared(rel, _Skip(
                "over-file-cap",
                f"the run regenerated {len(order):,} committed files and at most {max_files:,} are "
                f"compared (tabular, JSON and text files first)")))
            continue
        try:
            results.append(_compare_one(srcdir, rundir, rel, rtol, atol, max_bytes))
        except _Skip as skip:
            results.append(_not_compared(rel, skip))
        except Exception as exc:  # one bad file must never break the batch
            results.append(_not_compared(rel, _Skip("error", f"comparison failed ({_exc(exc)})")))
    results.sort(key=lambda r: r["path"])
    return results


def summarize(results: Iterable[dict]) -> dict:
    """Counts per status and a one-line headline for the report."""
    results = list(results)
    counts = {s: 0 for s in STATUSES}
    for r in results:
        status = r.get("status")
        counts[status if status in counts else "not-compared"] += 1
    total = len(results)
    if not total:
        headline = "The run regenerated none of the committed files, so there was nothing to compare."
    else:
        parts = [f"{counts['identical']:,} identical",
                 f"{counts['equivalent']:,} equivalent within tolerance",
                 f"{counts['differs']:,} {'differs' if counts['differs'] == 1 else 'differ'}",
                 f"{counts['not-compared']:,} not compared"]
        shown = [p for p, s in zip(parts, STATUSES, strict=True) if counts[s]]
        headline = (f"The run regenerated {_n(total, 'committed output file')}: "
                    + ", ".join(shown) + ".")
        if counts["differs"]:
            headline += " A difference is a finding to review, not a verdict."
    return {"total": total, "counts": counts, "headline": headline}


# --------------------------------------------------------------------------- #
#  Per-file driver, paths and reading
# --------------------------------------------------------------------------- #

def _compare_one(srcdir: Path, rundir: Path, rel: str, rtol: float, atol: float,
                 max_bytes: int) -> dict:
    kind = _kind(rel)
    suffix = PurePosixPath(rel).suffix.lower()
    # Locate (and size-check) both sides before reading either.
    com_path = _locate(srcdir, rel, "committed", max_bytes)
    reg_path = _locate(rundir, rel, "regenerated", max_bytes)
    com = _read(com_path, "committed", max_bytes)
    reg = _read(reg_path, "regenerated", max_bytes)
    base: dict[str, Any] = {"path": rel, "kind": kind,
                            "size_committed": len(com), "size_regenerated": len(reg)}
    if com == reg:
        return {**base, "status": "identical", "detail": "byte-identical to the committed file"}
    if kind == "binary":
        what = f"{suffix} files" if suffix else "files without an extension"
        raise _Skip("no-comparator",
                    f"the bytes differ, but there is no comparator for {what}; a byte difference in such "
                    f"a format (timestamps, compression, rendering) is not evidence either way")
    diffs = _Diffs(rtol, atol)
    compare = {"tabular": _compare_tabular, "json": _compare_json, "text": _compare_text}[kind]
    status, detail, evidence = compare(com, reg, suffix, diffs, max_bytes)
    return {**base, "status": status, "detail": detail, **evidence}


def _kind(rel: str) -> str:
    suffix = PurePosixPath(rel).suffix.lower()
    if suffix in _TABULAR_SEP or suffix == _PARQUET:
        return "tabular"
    if suffix == ".json":
        return "json"
    if suffix in _TEXT:
        return "text"
    return "binary"


def _clean_rel(raw: Any) -> str:
    """Validate a regenerated path. Unsafe paths are refused, not "cleaned":
    silently rewriting one could compare a different file under its name."""
    try:
        rel = os.fspath(raw)
    except TypeError:
        rel = None
    if not isinstance(rel, str) or not rel or "\x00" in rel:
        raise _Skip("unsafe-path", "not a usable relative path; refused")
    pieces = rel.split("/")
    # Paths arrive as relative POSIX strings; a backslash, drive or '..' means
    # something upstream is wrong (or hostile). ':' on Windows also names an
    # alternate data stream.
    if (rel.startswith("/") or "\\" in rel or PureWindowsPath(rel).drive or ".." in pieces
            or (os.name == "nt" and ":" in rel)):
        raise _Skip("unsafe-path", "the path is absolute or escapes the artifact tree; refused")
    parts = [p for p in pieces if p not in ("", ".")]
    if not parts:
        raise _Skip("unsafe-path", "the path names no file; refused")
    return "/".join(parts)


def _is_link(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", 0) in _LINK_TAGS


def _locate(root: Path, rel: str, side: str, max_bytes: int) -> Path:
    """lstat every component under root: a link anywhere on the way (not only the
    last component) could point the comparison outside the tree."""
    path = root
    st: os.stat_result | None = None
    for part in rel.split("/"):
        path = path / part
        try:
            st = os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            raise _Skip("missing", f"not present in the {side} tree")
        except OSError as exc:
            raise _Skip("unreadable", f"cannot inspect the {side} file ({_exc(exc)})")
        if _is_link(st):
            raise _Skip("symlink", f"the {side} path is or passes through a link; links are never followed")
    if st is None or not stat.S_ISREG(st.st_mode):
        raise _Skip("not-a-regular-file", f"the {side} path is not a regular file")
    # Belt and braces: with no link on the way this holds unless the tree is
    # being changed underneath us.
    if not path.resolve().is_relative_to(root.resolve()):
        raise _Skip("unsafe-path", f"the {side} path resolves outside its tree; refused")
    if st.st_size > max_bytes:
        raise _Skip("too-large", f"the {side} file is {st.st_size:,} bytes, over the {max_bytes:,}-byte "
                                 f"comparison limit")
    return path


def _read(path: Path, side: str, max_bytes: int) -> bytes:
    # O_NOFOLLOW / O_NONBLOCK close the window between lstat and open on POSIX
    # (a swapped-in link fails, a swapped-in FIFO cannot block). O_BINARY stops
    # Windows from translating CRLF, which would hide a real byte difference.
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0))
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise _Skip("unreadable", f"cannot open the {side} file ({_exc(exc)})")
    try:
        fh = os.fdopen(fd, "rb")
    except Exception:
        os.close(fd)
        raise
    with fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise _Skip("not-a-regular-file", f"the {side} path is not a regular file")
        try:
            data = fh.read(max_bytes + 1)
        except OSError as exc:
            raise _Skip("unreadable", f"cannot read the {side} file ({_exc(exc)})")
    if len(data) > max_bytes:  # grew after the size check
        raise _Skip("too-large", f"the {side} file is over the {max_bytes:,}-byte comparison limit")
    return data


def _load(side: str, fmt: str, parse: Callable[..., Any], *args: Any) -> Any:
    """Run a parser; any failure other than a deliberate skip is 'unparseable'."""
    try:
        return parse(*args)
    except _Skip:
        raise
    except Exception as exc:
        raise _Skip("unparseable", f"the {side} file could not be parsed as {fmt} ({_exc(exc)})")


# --------------------------------------------------------------------------- #
#  Shared evidence accumulator
# --------------------------------------------------------------------------- #

class _Diffs:
    """Per-value comparison results, shared by every comparator so that the
    evidence (counts, largest differences, first locations) reads the same."""

    def __init__(self, rtol: float, atol: float) -> None:
        self.rtol, self.atol = rtol, atol
        self.n_values = 0
        self.n_differing = 0
        self.max_abs: float | None = None
        self.max_rel: float | None = None
        self.examples: list[dict[str, str]] = []
        self.locations: list[str] = []

    def number(self, where: str, com: Any, reg: Any) -> None:
        if not (_is_number(com) and _is_number(reg)):
            self.other(where, _clip(str(com)), _clip(str(reg)), com == reg)
            return
        self.n_values += 1
        try:
            c, r = float(com), float(reg)
        except OverflowError:  # an integer too large for a float: exact comparison only
            same = com == reg
        else:
            if isinstance(com, int) and isinstance(reg, int):
                same = com == reg
            elif math.isnan(c) or math.isnan(r):
                same = math.isnan(c) and math.isnan(r)
            elif math.isinf(c) or math.isinf(r):
                same = c == r
            elif _integer_valued(c) and _integer_valued(r):
                # Two whole numbers are counts or IDs however a writer spelled them
                # (R prints 1.0 as 1): 1234567 vs 1234568.0 is a difference of one,
                # which a relative tolerance of 1e-6 would otherwise absorb
                same = c == r
            else:
                same = abs(r - c) <= self.atol + self.rtol * abs(c)
            if math.isfinite(c) and math.isfinite(r):
                diff = abs(r - c)
                self._magnitude(diff, diff / abs(c) if c else None)
        if not same:
            self._differ(where, _clip(repr(com)), _clip(repr(reg)))

    def other(self, where: str, com_shown: str, reg_shown: str, same: bool) -> None:
        self.n_values += 1
        if not same:
            self._differ(where, com_shown, reg_shown)

    def block(self, column: str, ok: Any, num: Any, fa: Any, fb: Any,
              show_a: Callable[[int], str], show_b: Callable[[int], str]) -> None:
        """A whole column at once (numpy arrays): ``ok`` per cell, ``num`` marks
        cells compared as numbers, ``fa``/``fb`` their float values."""
        import numpy as np

        self.n_values += int(len(ok))
        fin = num & np.isfinite(fa) & np.isfinite(fb)
        if fin.any():
            with np.errstate(over="ignore", invalid="ignore"):
                diff = np.abs(fb[fin] - fa[fin])
                ref = np.abs(fa[fin])
                nz = ref > 0
                self._magnitude(float(diff.max()), float((diff[nz] / ref[nz]).max()) if nz.any() else None)
        bad = np.flatnonzero(~ok)
        if len(bad):
            self.locations.append(column)
            for i in bad[: max(0, _MAX_EXAMPLES - len(self.examples))]:
                self.examples.append({"location": f"row {int(i) + 1}, column {column!r}",
                                      "committed": show_a(int(i)), "regenerated": show_b(int(i))})
            self.n_differing += int(len(bad))

    def _magnitude(self, abs_diff: float, rel_diff: float | None) -> None:
        """Track the largest differences over every finite numeric pair, matching
        or not: the size of the noise is evidence too. Relative to the committed
        value; pairs whose committed value is 0 have no relative difference."""
        if math.isfinite(abs_diff):
            self.max_abs = abs_diff if self.max_abs is None else max(self.max_abs, abs_diff)
        if rel_diff is not None and math.isfinite(rel_diff):
            self.max_rel = rel_diff if self.max_rel is None else max(self.max_rel, rel_diff)

    def _differ(self, where: str, com_shown: str, reg_shown: str) -> None:
        self.n_differing += 1
        if len(self.locations) < _MAX_LISTED:
            self.locations.append(where)
        if len(self.examples) < _MAX_EXAMPLES:
            self.examples.append({"location": where, "committed": com_shown, "regenerated": reg_shown})

    # -- reporting ------------------------------------------------------- #
    def tolerance(self) -> str:
        return f"rtol={self.rtol:g}, atol={self.atol:g}"

    def magnitude_text(self) -> str:
        if self.max_abs is None:
            return ""
        text = f"largest |diff| {self.max_abs:.3g}"
        if self.max_rel:
            text += f" (relative {self.max_rel:.3g})"
        return text

    def exact(self) -> bool:
        """No numeric difference at all, not even within tolerance."""
        return not self.max_abs

    def evidence(self, noun: str) -> dict[str, Any]:
        ev: dict[str, Any] = {f"n_{noun}_compared": self.n_values, f"n_{noun}_differing": self.n_differing}
        if self.max_abs is not None:
            ev["max_abs_diff"] = self.max_abs
        if self.max_rel is not None:
            ev["max_rel_diff"] = self.max_rel
        if self.examples:
            ev["examples"] = self.examples
        return ev


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# --------------------------------------------------------------------------- #
#  Tabular
# --------------------------------------------------------------------------- #

def _compare_tabular(com: bytes, reg: bytes, suffix: str, d: _Diffs,
                     max_bytes: int) -> tuple[str, str, dict]:
    try:
        import numpy as np
        import pandas as pd
    except ImportError as exc:
        raise _Skip("dependency-missing", f"pandas/numpy are not installed ({_exc(exc)})")
    empty = _empty_side(com, reg)
    if empty:
        return empty
    headerless = False
    if suffix == _PARQUET:
        fmt = "Parquet"
        a = _load("committed", fmt, _read_parquet, com, max_bytes)
        b = _load("regenerated", fmt, _read_parquet, reg, max_bytes)
    else:
        fmt = "TSV" if suffix == ".tsv" else "CSV"
        sep = _TABULAR_SEP[suffix]
        headerless = _numeric_first_line(com, sep) and _numeric_first_line(reg, sep)
        a = _load("committed", fmt, _read_delimited, pd, com, sep, headerless)
        b = _load("regenerated", fmt, _read_delimited, pd, reg, sep, headerless)
    for df in (a, b):
        # Row labels pandas inferred as an index (R's write.table header layout)
        # are results too; keep them as a compared column.
        if not isinstance(df.index, pd.RangeIndex):
            df.reset_index(inplace=True)

    cols_a, cols_b = [str(c) for c in a.columns], [str(c) for c in b.columns]
    ev: dict[str, Any] = {"shape_committed": [int(a.shape[0]), int(a.shape[1])],
                          "shape_regenerated": [int(b.shape[0]), int(b.shape[1])]}
    if headerless:
        ev["header_row"] = False
    set_a, set_b = set(cols_a), set(cols_b)
    added = [c for c in cols_b if c not in set_a]
    removed = [c for c in cols_a if c not in set_b]
    problems = []
    if added:
        ev["columns_added"] = added[:_MAX_LISTED]
        problems.append(f"{_n(len(added), 'column')} added ({_names(added)})")
    if removed:
        ev["columns_removed"] = removed[:_MAX_LISTED]
        problems.append(f"{_n(len(removed), 'column')} removed ({_names(removed)})")
    if not added and not removed and cols_a != cols_b:
        ev["column_order_changed"] = True
        problems.append("the same columns appear in a different order")
    same_rows = a.shape[0] == b.shape[0]
    if not same_rows:
        problems.append(f"the row count changed from {a.shape[0]:,} to {b.shape[0]:,}")

    shared = [c for c in cols_a if c in set_b]
    if same_rows:
        pos_a = {c: j for j, c in enumerate(cols_a)}
        pos_b = {c: j for j, c in enumerate(cols_b)}
        for c in shared:
            _compare_column(np, pd, a.iloc[:, pos_a[c]], b.iloc[:, pos_b[c]], c, d)
        ev.update(d.evidence("cells"))
        if d.locations:
            ev["columns_differing"] = d.locations[:_MAX_LISTED]

    if problems:
        detail = "; ".join(problems)
        if not same_rows:
            detail += "; cells were not compared because the rows cannot be aligned"
        elif shared and d.n_differing:
            detail += (f"; in the shared columns {d.n_differing:,} of {d.n_values:,} cells also differ "
                       f"beyond tolerance ({d.tolerance()})")
        elif shared:
            detail += f"; the {_n(len(shared), 'shared column')} agree within tolerance"
        return "differs", detail, ev
    shape = f"{a.shape[0]:,} rows x {a.shape[1]:,} columns"
    if d.n_differing:
        detail = (f"{d.n_differing:,} of {d.n_values:,} cells differ beyond tolerance ({d.tolerance()}) "
                  f"in {_n(len(d.locations), 'column')} ({_names(d.locations)})")
        mag = d.magnitude_text()
        return "differs", detail + (f"; {mag}" if mag else ""), ev
    if d.exact():
        how = "encoding or metadata" if fmt == "Parquet" else "formatting (quoting, number format, line endings)"
        return "equivalent", f"same shape ({shape}), columns and values; only the {how} differs", ev
    return "equivalent", (f"same shape ({shape}) and columns; all {d.n_values:,} cells agree within "
                          f"tolerance ({d.tolerance()}; {d.magnitude_text()})"), ev


def _read_delimited(pd: Any, data: bytes, sep: str, headerless: bool) -> Any:
    first = data.split(b"\n", 1)[0]
    if first.count(sep.encode()) + 1 > _MAX_COLUMNS:
        raise _Skip("too-many-columns", f"the file has more than {_MAX_COLUMNS:,} columns")
    # round_trip parsing so "same values" means the same doubles, not values
    # that merely survived a faster, slightly lossy float parser.
    df = pd.read_csv(io.BytesIO(data), sep=sep, header=None if headerless else 0, engine="c",
                     encoding="utf-8", low_memory=False, float_precision="round_trip")
    if df.shape[1] > _MAX_COLUMNS:
        raise _Skip("too-many-columns", f"the file has more than {_MAX_COLUMNS:,} columns")
    return df


def _read_parquet(data: bytes, max_bytes: int) -> Any:
    try:
        import pyarrow
        import pyarrow.parquet as pq
    except ImportError:
        raise _Skip("dependency-missing", "pyarrow is not installed, so Parquet files cannot be read")
    if _version_tuple(pyarrow.__version__) < _MIN_PYARROW:
        raise _Skip("dependency-missing",
                    f"pyarrow {pyarrow.__version__} can execute code embedded in an untrusted Parquet file "
                    f"(CVE-2023-47248); version 14.0.1 or later is needed to compare Parquet")
    pf = pq.ParquetFile(io.BytesIO(data))
    meta = pf.metadata
    if meta.num_columns > _MAX_COLUMNS:
        raise _Skip("too-many-columns", f"the file has more than {_MAX_COLUMNS:,} columns")
    # A compressed file within max_bytes can still expand enormously.
    expanded = sum(meta.row_group(i).total_byte_size for i in range(meta.num_row_groups))
    if expanded > max_bytes:
        raise _Skip("too-large", f"the file decompresses to {expanded:,} bytes, over the {max_bytes:,}-byte "
                                 f"comparison limit")
    return pf.read().to_pandas()


def _numeric_first_line(data: bytes, sep: str) -> bool:
    line = data.split(b"\n", 1)[0].decode("utf-8-sig", errors="replace").strip()
    if not line:
        return False
    for field in line.split(sep):
        try:
            float(field.strip().strip('"').strip())
        except ValueError:
            return False
    return True


def _compare_column(np: Any, pd: Any, ca: Any, cb: Any, name: str, d: _Diffs) -> None:
    from pandas.api import types as pdt

    na_a = ca.isna().to_numpy(dtype=bool)
    na_b = cb.isna().to_numpy(dtype=bool)
    oa = ca.to_numpy(dtype=object)
    ob = cb.to_numpy(dtype=object)

    def numeric(s: Any) -> bool:
        return pdt.is_numeric_dtype(s.dtype) and not pdt.is_bool_dtype(s.dtype)

    if numeric(ca) and numeric(cb):
        fa = ca.to_numpy(dtype="float64", na_value=np.nan)
        fb = cb.to_numpy(dtype="float64", na_value=np.nan)
        if pdt.is_integer_dtype(ca.dtype) and pdt.is_integer_dtype(cb.dtype):
            # Counts and IDs: exact, compared as Python ints so values beyond
            # 2**53 do not round together through float64.
            ok = (na_a & na_b) | (~na_a & ~na_b & (oa == ob))
        else:
            ok = np.isclose(fb, fa, rtol=d.rtol, atol=d.atol, equal_nan=True)
            # cells that are whole numbers on BOTH sides are counts/IDs: exact
            whole = _whole_cells(fa) & _whole_cells(fb)
            ok = np.where(whole, fa == fb, ok)
        num = np.ones(len(ok), dtype=bool)
    else:
        # Text, booleans, dates, or a column that is numeric on one side only
        # (one stray "NA*" or "<0.001" turns a whole column into strings).
        # Cells that are numbers on both sides use the tolerance (integers
        # exactly); every other cell must match as text.
        sa = np.array(["" if m else str(v) for v, m in zip(oa, na_a, strict=True)], dtype=object)
        sb = np.array(["" if m else str(v) for v, m in zip(ob, na_b, strict=True)], dtype=object)
        fa = pd.to_numeric(pd.Series(sa, dtype=object), errors="coerce").to_numpy(dtype="float64",
                                                                                  na_value=np.nan)
        fb = pd.to_numeric(pd.Series(sb, dtype=object), errors="coerce").to_numpy(dtype="float64",
                                                                                  na_value=np.nan)
        num = ~na_a & ~na_b & ~np.isnan(fa) & ~np.isnan(fb)
        ok = (na_a & na_b) | (~na_a & ~na_b & (sa == sb))
        if num.any():
            ok[num] = np.isclose(fb[num], fa[num], rtol=d.rtol, atol=d.atol, equal_nan=True)
            for i in np.flatnonzero(num):
                ta, tb = sa[i].strip(), sb[i].strip()
                if _INTEGER.fullmatch(ta) and _INTEGER.fullmatch(tb):
                    ok[i] = _parse_token(ta) == _parse_token(tb)  # integers are exact
                elif _integer_valued(float(fa[i])) and _integer_valued(float(fb[i])):
                    ok[i] = fa[i] == fb[i]                        # so are whole numbers

    def show(values: Any, missing: Any) -> Callable[[int], str]:
        return lambda i: "<missing>" if missing[i] else _show_cell(values[i])

    d.block(name, ok, num, fa, fb, show(oa, na_a), show(ob, na_b))


def _show_cell(v: Any) -> str:
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        try:
            v = v.item()  # numpy scalar -> Python, so floats print at full precision
        except Exception:
            pass
    return _clip(repr(v) if isinstance(v, float) else str(v))


# --------------------------------------------------------------------------- #
#  JSON
# --------------------------------------------------------------------------- #

def _compare_json(com: bytes, reg: bytes, suffix: str, d: _Diffs,
                  max_bytes: int) -> tuple[str, str, dict]:
    empty = _empty_side(com, reg)
    if empty:
        return empty
    a = _load("committed", "JSON", _parse_json, com)
    b = _load("regenerated", "JSON", _parse_json, reg)
    # Iterative walk: author-controlled nesting depth must not hit the recursion limit.
    stack: list[tuple[str, Any, Any]] = [("$", a, b)]
    while stack:
        path, x, y = stack.pop()
        if isinstance(x, dict) and isinstance(y, dict):
            children = []
            for key in sorted(set(x) | set(y)):
                p = _json_path(path, key)
                if key not in y:
                    d.other(p, _show_json(x[key]), "<absent>", False)
                elif key not in x:
                    d.other(p, "<absent>", _show_json(y[key]), False)
                else:
                    children.append((p, x[key], y[key]))
            stack.extend(reversed(children))
        elif isinstance(x, list) and isinstance(y, list):
            if len(x) != len(y):
                d.other(path, f"list of {len(x)}", f"list of {len(y)}", False)
            else:
                stack.extend(reversed([(f"{path}[{i}]", xi, yi) for i, (xi, yi) in enumerate(zip(x, y, strict=True))]))
        elif _is_number(x) and _is_number(y):
            d.number(path, x, y)
        else:
            d.other(path, _show_json(x), _show_json(y), type(x) is type(y) and x == y)

    ev = d.evidence("values")
    if d.locations:
        ev["differing_paths"] = d.locations[:_MAX_LISTED]
    if d.n_differing:
        detail = (f"{d.n_differing:,} of {d.n_values:,} JSON values differ beyond tolerance ({d.tolerance()}), "
                  f"e.g. at {', '.join(d.locations[:3])}")
        mag = d.magnitude_text()
        return "differs", detail + (f"; {mag}" if mag else ""), ev
    if d.exact():
        return "equivalent", "same JSON values; only formatting or key order differs", ev
    return "equivalent", (f"all {d.n_values:,} JSON values agree within tolerance "
                          f"({d.tolerance()}; {d.magnitude_text()})"), ev


def _parse_json(data: bytes) -> Any:
    return json.loads(data.decode("utf-8-sig"))


def _json_path(parent: str, key: str) -> str:
    return f"{parent}.{key}" if _IDENT.fullmatch(key) else f"{parent}[{json.dumps(key)}]"


def _show_json(v: Any) -> str:
    if isinstance(v, dict):
        return f"object with {_n(len(v), 'key')}"
    if isinstance(v, list):
        return f"list of {len(v)}"
    return _clip(json.dumps(v))


# --------------------------------------------------------------------------- #
#  Text
# --------------------------------------------------------------------------- #

def _compare_text(com: bytes, reg: bytes, suffix: str, d: _Diffs,
                  max_bytes: int) -> tuple[str, str, dict]:
    a, uris_a = _mask_data_uris(_decode_text(com, "committed"))
    b, uris_b = _mask_data_uris(_decode_text(reg, "regenerated"))
    la, lb = _normalize_lines(a), _normalize_lines(b)
    ev: dict[str, Any] = {"n_lines_committed": len(la), "n_lines_regenerated": len(lb)}
    caveat = ""
    if uris_a != uris_b:
        ev["embedded_data_differs"] = True
        caveat = ("; embedded base64 data (e.g. plots) differs byte-wise and was not compared, which is "
                  "not evidence either way")

    def embedded_data_blocks_equivalence() -> None:
        # The text may match to the last digit while the plots inside it differ — and
        # regenerated plots almost always differ byte-wise. "Equivalent" would then
        # vouch for figures nobody compared.
        if uris_a != uris_b:
            raise _Skip("embedded-data", "the text is equal, but the embedded base64 data (e.g. plots) "
                                         "differs and cannot be compared")

    if la == lb:
        embedded_data_blocks_equivalence()
        ev["normalization"] = "line-endings"
        what = ("the text is identical" if a == b
                else "equal after normalizing line endings, trailing whitespace and trailing blank lines")
        return "equivalent", what + caveat, ev

    # Number-aware pass: collapse whitespace runs, then split into alternating
    # [text, number, text, number, ..., text]; the text pieces must match exactly.
    pa = _NUMBER.split("\n".join(" ".join(line.split()) for line in la))
    pb = _NUMBER.split("\n".join(" ".join(line.split()) for line in lb))
    if len(pa) == len(pb) and pa[0::2] == pb[0::2]:
        line = 1
        for k in range(1, len(pa), 2):
            line += pa[k - 1].count("\n")
            d.number(f"line {line}", _parse_token(pa[k]), _parse_token(pb[k]))
        ev["normalization"] = "whitespace+numeric-tolerance"
        ev.update(d.evidence("numbers"))
        if not d.n_differing:
            embedded_data_blocks_equivalence()
            if not d.n_values:
                return "equivalent", "equal after collapsing runs of whitespace" + caveat, ev
            mag = d.magnitude_text()
            return "equivalent", (f"equal after collapsing whitespace, with all {_n(d.n_values, 'number')} "
                                  f"within tolerance ({d.tolerance()}" + (f"; {mag}" if mag else "")
                                  + ")" + caveat), ev
        ev["first_differing_line"] = int(d.locations[0].split()[-1])
        mag = d.magnitude_text()
        return "differs", (f"the text matches apart from numbers, but {d.n_differing:,} of {d.n_values:,} "
                           f"numbers differ beyond tolerance ({d.tolerance()}), first at "
                           f"{d.locations[0]}" + (f"; {mag}" if mag else "") + caveat), ev

    n_lines, first, example = _line_diff(la, lb)
    ev.update({"n_lines_differing": n_lines, "first_differing_line": first, "examples": [example]})
    return "differs", (f"{n_lines:,} of {len(la):,} lines differ beyond whitespace and numeric tolerance, "
                       f"first at line {first} of the committed file" + caveat), ev


def _decode_text(data: bytes, side: str) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _Skip("unparseable", f"the {side} file is not valid UTF-8 text (no other encoding is guessed)")


def _mask_data_uris(text: str) -> tuple[str, list[str]]:
    found = _DATA_URI.findall(text)
    return (_DATA_URI.sub("data:<embedded>", text), found) if found else (text, found)


def _normalize_lines(text: str) -> list[str]:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _parse_token(token: str) -> Any:
    if _INTEGER.fullmatch(token):
        try:
            return int(token)
        except ValueError:  # beyond int()'s digit limit: compared as a string
            return token
    return float(token)


def _line_diff(a: list[str], b: list[str]) -> tuple[int, int, dict[str, str]]:
    """(number of differing lines, first differing committed line (1-based), example)."""
    if max(len(a), len(b)) <= _DIFFLIB_MAX_LINES:
        ops = [op for op in difflib.SequenceMatcher(None, a, b).get_opcodes() if op[0] != "equal"]
        n = sum(max(i2 - i1, j2 - j1) for _, i1, i2, j1, j2 in ops)
        i, j = ops[0][1], ops[0][3]
    else:
        idx = [k for k in range(max(len(a), len(b))) if k >= len(a) or k >= len(b) or a[k] != b[k]]
        n, i, j = len(idx), idx[0], idx[0]
    example = {"location": f"line {i + 1}",
               "committed": _clip(a[i]) if i < len(a) else "<no line>",
               "regenerated": _clip(b[j]) if j < len(b) else "<no line>"}
    return n, i + 1, example


# --------------------------------------------------------------------------- #
#  Small helpers
# --------------------------------------------------------------------------- #

def _empty_side(com: bytes, reg: bytes) -> tuple[str, str, dict] | None:
    """Parsers reject empty input, but an output that became (or stopped being)
    empty is a clear difference, not something that cannot be judged."""
    ea, eb = not com.strip(), not reg.strip()
    if ea and eb:
        return "equivalent", "both files are empty apart from whitespace", {}
    if ea != eb:
        return "differs", f"the {'committed' if ea else 'regenerated'} file is empty and the other is not", {}
    return None


def _not_compared(path: str, skip: _Skip) -> dict:
    return {"path": path, "kind": _kind(path), "status": "not-compared",
            "reason": skip.reason, "detail": skip.detail}


def _display(raw: Any) -> str:
    try:
        text = os.fspath(raw)
    except TypeError:
        return repr(raw)
    return text if isinstance(text, str) else repr(text)


_EXACT_INT_LIMIT = 2.0 ** 53        # beyond this a float cannot tell neighbouring integers apart


def _integer_valued(x: float) -> bool:
    """A finite whole number a float represents exactly."""
    return math.isfinite(x) and abs(x) < _EXACT_INT_LIMIT and x == math.floor(x)


def _whole_cells(a: Any) -> Any:
    import numpy as np

    with np.errstate(invalid="ignore"):
        return np.isfinite(a) & (np.abs(a) < _EXACT_INT_LIMIT) & (a == np.floor(a))


def _exc(exc: BaseException) -> str:
    msg = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {msg[:160]}" if msg else type(exc).__name__


def _clip(text: str) -> str:
    return text if len(text) <= _SHOW else text[: _SHOW - 1] + "…"


def _n(count: int, noun: str) -> str:
    return f"{count:,} {noun}{'' if count == 1 else 's'}"


def _names(names: list[str]) -> str:
    shown = ", ".join(repr(c) for c in names[:5])
    return shown + (f" and {len(names) - 5} more" if len(names) > 5 else "")


def _version_tuple(version: str) -> tuple[int, ...]:
    out = []
    for part in version.split("."):
        m = re.match(r"\d+", part)
        if not m:
            break
        out.append(int(m.group()))
    return tuple(out)
