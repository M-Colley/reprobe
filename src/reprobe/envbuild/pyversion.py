"""Which Python an artifact's pinned requirements can install on.

The base image ships one Python (the newest the harness supports). An artifact
from 2022 that pins ``scikit-learn==1.1.2`` cannot install there: that release
publishes wheels up to CPython 3.10, and building it from source on 3.13 fails
(ACM-badged benchmark, EuroSys 2023). The authors, and the committee that
awarded the badge, ran it on the Python of its day.

The choice is made from what the artifact declares — ``.python-version``,
``runtime.txt``, ``requires-python`` — and from PyPI's record of which Pythons
each *exactly pinned* release ships wheels for. A release's files do not change
once published, so the same pins give the same choice on every run. Unpinned
requirements do not constrain it: pip resolves them on any Python.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename

#: Newest first; the first is the base image's own Python.
DEFAULT_CANDIDATES = ("3.13", "3.12", "3.11", "3.10", "3.9", "3.8", "3.7")
_PYPI = "https://pypi.org/pypi/{name}/{version}/json"
_TIMEOUT_S = 15


@dataclass
class Choice:
    version: str                                   # "3.10"
    reason: str                                    # one sentence for the report
    unsupported: list[str] = field(default_factory=list)   # pins no candidate satisfies
    # Upload time of the newest file among the pinned releases (ISO 8601, UTC): the
    # artifact is no older than this, so its unpinned dependencies are resolved as
    # of then — today's numpy 2 does not load under a 2022 pandas pin.
    cutoff: Optional[str] = None


@lru_cache(maxsize=1024)
def release_files(name: str, version: str) -> Optional[tuple]:
    """``(file names, requires_python, newest upload time)`` of one PyPI release,
    or None when PyPI cannot say (offline, unknown release). Tests replace this."""
    import requests
    try:
        r = requests.get(_PYPI.format(name=name, version=version), timeout=_TIMEOUT_S,
                         headers={"Accept": "application/json"})
        if r.status_code != 200:
            return None
        data = r.json()
    except (requests.RequestException, ValueError):
        return None
    urls = [u for u in data.get("urls") or [] if not u.get("yanked")]
    files = tuple(str(u.get("filename") or "") for u in urls)
    uploaded = max((str(u.get("upload_time_iso_8601") or "") for u in urls), default="") or None
    return files, (data.get("info") or {}).get("requires_python") or None, uploaded


def exact_pins(lines: Iterable[str]) -> list[tuple[str, str]]:
    """``(name, version)`` for each requirement pinned to one release (``==1.1.2``,
    not ``==1.*``), skipping lines whose markers exclude Linux CPython 3."""
    out = []
    for line in lines:
        try:
            req = Requirement(line.split(" #", 1)[0].strip())
        except InvalidRequirement:
            continue
        specs = list(req.specifier)
        if len(specs) != 1 or specs[0].operator not in ("==", "===") or "*" in specs[0].version:
            continue
        if req.marker and not req.marker.evaluate({"sys_platform": "linux", "platform_system": "Linux",
                                                    "os_name": "posix", "python_version": "3.10",
                                                    "implementation_name": "cpython"}):
            continue
        out.append((canonicalize_name(req.name), specs[0].version))
    return out


def open_names(lines: Iterable[str]) -> list[tuple[str, SpecifierSet]]:
    """``(name, specifier)`` of each requirement NOT pinned to one release."""
    pinned = {n for n, _ in exact_pins(lines)}
    out = []
    for line in lines:
        try:
            req = Requirement(line.split(" #", 1)[0].strip())
        except InvalidRequirement:
            continue
        if canonicalize_name(req.name) not in pinned:
            out.append((canonicalize_name(req.name), req.specifier))
    return out


@lru_cache(maxsize=1024)
def release_dates(name: str) -> Optional[tuple[tuple[str, str], ...]]:
    """``(version, first upload time)`` of each release of a PyPI project that has a
    file not yanked, or None when PyPI cannot say. Tests replace this."""
    import requests
    try:
        r = requests.get(f"https://pypi.org/pypi/{name}/json", timeout=_TIMEOUT_S,
                         headers={"Accept": "application/json"})
        if r.status_code != 200:
            return None
        data = r.json()
    except (requests.RequestException, ValueError):
        return None
    out = []
    for ver, files in (data.get("releases") or {}).items():
        times = [str(f.get("upload_time_iso_8601") or "") for f in files or [] if not f.get("yanked")]
        times = [t for t in times if t]
        if times:
            out.append((ver, min(times)))
    return tuple(out)


def as_of_version(name: str, cutoff: str, spec: Optional[SpecifierSet] = None) -> Optional[str]:
    """The newest final release of ``name`` first uploaded before ``cutoff`` (ISO
    8601) that ``spec`` allows: what ``pip install name`` gave on that day."""
    from packaging.version import InvalidVersion, Version
    best = None
    for ver, first in release_dates(name) or ():
        if first.replace("Z", "+00:00") >= cutoff.replace("Z", "+00:00"):
            continue
        try:
            v = Version(ver)
        except InvalidVersion:
            continue
        if v.is_prerelease or v.is_devrelease or (spec is not None and v not in spec):
            continue
        if best is None or v > best:
            best = v
    return str(best) if best is not None else None


def as_of_pins(names: Iterable[tuple[str, Optional[SpecifierSet]]], cutoff: str) -> list[tuple[str, str]]:
    """``(name, version)`` as of ``cutoff`` for each name PyPI knows; the rest are left out."""
    todo = list(dict.fromkeys((canonicalize_name(n), s) for n, s in names))
    with ThreadPoolExecutor(8) as ex:
        found = list(ex.map(lambda p: as_of_version(p[0], cutoff, p[1]), todo))
    return [(n, v) for (n, _), v in zip(todo, found, strict=True) if v]


def artifact_cutoff(date: Optional[str], min_age_days: int, today: Optional[str] = None) -> Optional[str]:
    """The as-of cutoff (UTC midnight after ``date``, YYYY-MM-DD) when the artifact is at
    least ``min_age_days`` old, else None."""
    from datetime import date as _date
    from datetime import timedelta
    if not date or not re.fullmatch(r"\d{4}-\d\d-\d\d", date[:10]):
        return None
    try:
        d = _date.fromisoformat(date[:10])
        now = _date.fromisoformat(today) if today else _date.today()
    except ValueError:
        return None
    if (now - d).days < min_age_days:
        return None
    return (d + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")


def _supports(files: tuple[str, ...], requires_python: Optional[str], minor: str) -> bool:
    """Whether a release installs on CPython ``minor`` (e.g. "3.10") on Linux x86-64.
    A release with compiled wheels but none for that Python is NOT supported: its
    sdist build is what failed. A pure or sdist-only release is, unless its
    requires-python excludes the version."""
    if requires_python:
        try:
            if not SpecifierSet(requires_python).contains(minor + ".0", prereleases=True):
                return False
        except InvalidSpecifier:
            pass
    want = int(minor.split(".")[1])
    wheels = [f for f in files if f.endswith(".whl")]
    if not wheels:
        return True
    for f in wheels:
        try:
            _, _, _, tags = parse_wheel_filename(f)
        except InvalidWheelFilename:
            continue
        for t in tags:
            if t.platform != "any" and not ("linux" in t.platform and "x86_64" in t.platform):
                continue
            if t.abi == "none" and t.interpreter in ("py3", f"py3{want}"):
                return True
            m = re.fullmatch(r"cp3(\d+)", t.interpreter)
            if m and (int(m.group(1)) == want or (t.abi == "abi3" and int(m.group(1)) <= want)):
                return True
    return False


def declared_python(src: Path, requires_python: Optional[str] = None) -> Optional[SpecifierSet]:
    """The Python the artifact says it needs: ``.python-version`` (pyenv),
    ``runtime.txt`` (``python-3.8.10``), or pyproject's ``requires-python``."""
    for name, rx in ((".python-version", r"^\s*(\d+\.\d+)"), ("runtime.txt", r"^\s*python-(\d+\.\d+)")):
        p = src / name
        if p.is_file() and p.stat().st_size < 4096:
            m = re.search(rx, p.read_text(encoding="utf-8", errors="replace"), re.M)
            if m:
                return SpecifierSet(f"=={m.group(1)}.*")
    if requires_python:
        try:
            return SpecifierSet(requires_python)
        except InvalidSpecifier:
            return None
    return None


def choose_python(pins: list[tuple[str, str]], declared: Optional[SpecifierSet] = None,
                  candidates: tuple[str, ...] = DEFAULT_CANDIDATES, force: bool = False,
                  soft: Iterable[tuple[str, str]] = ()) -> Optional[Choice]:
    """The newest candidate every exact pin installs on, or None when the base
    image's Python (``candidates[0]``) will do. When no candidate fits all pins,
    the one fitting most of them (newest on a tie), with the rest named. ``force``
    returns the base image's Python too (an as-of environment is always built).
    ``soft`` releases only break ties among the Pythons fitting most exact pins."""
    allowed = [c for c in candidates if declared is None or declared.contains(c + ".0", prereleases=True)]
    if not allowed:
        return None
    with ThreadPoolExecutor(8) as ex:
        soft = [p for p in dict.fromkeys(soft) if p not in pins]
        both = [*pins, *soft]
        meta = dict(zip(both, ex.map(lambda p: release_files(*p), both), strict=True))
    known = {p: meta[p] for p in pins if meta[p] is not None}
    weak = [meta[p] for p in soft if meta[p] is not None]
    fits = {c: [p for p, m in known.items() if _supports(m[0], m[1], c)] for c in allowed}
    sfit = {c: sum(_supports(m[0], m[1], c) for m in weak) for c in allowed}
    best = max(allowed, key=lambda c: (len(fits[c]), sfit[c], -allowed.index(c)))
    if best == candidates[0] and not force:
        return None
    misfit = [f"{n}=={v}" for (n, v) in known if (n, v) not in fits[best]]
    newer = allowed[:allowed.index(best)]
    blockers = sorted({f"{n}=={v}" for c in newer for (n, v) in known if (n, v) not in fits[c]})
    parts = []
    if len(allowed) < len(candidates):
        parts.append(f"the artifact declares Python {declared}")
    if blockers:
        parts.append(f"its pinned {', '.join(blockers[:4])}{' …' if len(blockers) > 4 else ''} "
                     "publish no wheels for Python "
                     + (f"{newer[-1]}–{newer[0]}" if len(newer) > 1 else newer[0]) + " (PyPI)")
    uploads = [m[2] for m in known.values() if len(m) > 2 and m[2]]
    return Choice(version=best, reason="; ".join(parts), unsupported=misfit,
                  cutoff=_day_after(max(uploads)) if uploads else None)


def _day_after(iso: str) -> Optional[str]:
    """The UTC midnight after an upload time: a margin for files a release added
    later that day, and a value pip's --uploaded-prior-to reads unambiguously."""
    from datetime import datetime, timedelta, timezone
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
    return (t + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
