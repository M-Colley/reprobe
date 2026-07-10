"""Runner discovery.

Built-ins are imported directly (so the harness works from a source checkout
without ``pip install``). Third-party runners shipped as separate packages are
merged in via the ``reprobe.runners`` entry-point group — adding an artifact
type (Julia, MATLAB, Node/Playwright) needs no core edit.
"""

from __future__ import annotations

from typing import Optional

from .base import Runner
from .python_script import PythonScriptRunner
from .jupyter import JupyterRunner
from .r_script import RScriptRunner
from .rmarkdown import RMarkdownRunner
from .unity import UnityRunner

_BUILTINS: list[type] = [
    PythonScriptRunner,
    JupyterRunner,
    RScriptRunner,
    RMarkdownRunner,
    UnityRunner,
]


def _load_entry_point_runners() -> list[type]:
    out: list[type] = []
    try:
        from importlib.metadata import entry_points
        eps = entry_points()
        group = eps.select(group="reprobe.runners") if hasattr(eps, "select") else eps.get("reprobe.runners", [])  # type: ignore
        builtin_modules = {c.__module__ for c in _BUILTINS}
        for ep in group:
            try:
                cls = ep.load()
                if cls.__module__ not in builtin_modules:   # avoid double-registering built-ins
                    out.append(cls)
            except Exception:
                continue
    except Exception:
        pass
    return out


def load_runners(enabled_ids: Optional[set[str]] = None) -> dict[str, Runner]:
    classes = list(_BUILTINS) + _load_entry_point_runners()
    runners: dict[str, Runner] = {}
    for cls in classes:
        inst = cls()
        if enabled_ids is not None and inst.id not in enabled_ids:
            continue
        runners[inst.id] = inst
    return runners


def runner_for(step, runners: dict[str, Runner]) -> Optional[Runner]:
    # explicit runner id wins, then capability match
    if step.runner and step.runner in runners:
        return runners[step.runner]
    for r in runners.values():
        if r.can_handle(step):
            return r
    return None
