"""Runner discovery.

``config/runners.yaml`` is the source of truth: each enabled row's ``plugin:``
dotted path (``package.module:ClassName``) is imported here, so adding an
artifact type (Julia, MATLAB, Node/Playwright) is a config row + an importable
package — no core edit. A row's ``default_image:`` overrides the class's
``image_key``. Rows without a ``plugin:`` merely enable/disable runners shipped
via the ``reprobe.runners`` entry-point group (the secondary mechanism).

Load failures are never swallowed: a broken ``plugin:`` row raises
``RunnerLoadError`` naming the row, and entry-point failures are appended to
the caller-supplied ``errors`` list (and logged) instead of vanishing into an
unexplained "skipped: no runner" a year later.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Optional

from .base import Runner

log = logging.getLogger(__name__)


class RunnerLoadError(Exception):
    """A runners.yaml row declares a plugin that cannot be loaded."""


def _import_plugin(row: dict[str, Any]) -> type:
    path = str(row.get("plugin") or "")
    mod_name, _, cls_name = path.partition(":")
    if not mod_name or not cls_name:
        raise RunnerLoadError(
            f"runners.yaml row id={row.get('id')!r}: plugin must be 'package.module:ClassName', got {path!r}")
    try:
        module = importlib.import_module(mod_name)
    except Exception as e:
        raise RunnerLoadError(
            f"runners.yaml row id={row.get('id')!r}: cannot import plugin module {mod_name!r}: {e!r}") from e
    try:
        return getattr(module, cls_name)
    except AttributeError as e:
        raise RunnerLoadError(
            f"runners.yaml row id={row.get('id')!r}: module {mod_name!r} has no class {cls_name!r}") from e


def _load_entry_point_runners(known_modules: set[str], errors: list[str]) -> list[type]:
    out: list[type] = []
    try:
        from importlib.metadata import entry_points
        eps = entry_points()
        group = eps.select(group="reprobe.runners") if hasattr(eps, "select") else eps.get("reprobe.runners", [])  # type: ignore
        for ep in group:
            try:
                cls = ep.load()
                if cls.__module__ not in known_modules:   # avoid double-registering row plugins
                    out.append(cls)
            except Exception as e:
                errors.append(f"entry point {ep.name!r} failed to load: {e!r}")
    except Exception as e:
        errors.append(f"entry point scan failed: {e!r}")
    return out


def load_runners(enabled_ids: Optional[set[str]] = None,
                 rows: Optional[list[dict[str, Any]]] = None,
                 errors: Optional[list[str]] = None) -> dict[str, Runner]:
    """Instantiate runners from config rows (+ entry points).

    ``rows`` defaults to the resolved ``config/runners.yaml``; pass
    ``Config.runner_rows`` to honor an explicit ``--config-dir``. ``errors``,
    if given, receives human-readable entry-point load failures.
    """
    if rows is None:
        from ..config import load_config
        rows = load_config().runner_rows
    if errors is None:
        errors = []
    classes = [_import_plugin(row) for row in rows
               if row.get("enabled", True) and row.get("plugin")]
    classes += _load_entry_point_runners({c.__module__ for c in classes}, errors)
    for msg in errors:
        log.warning("runner discovery: %s", msg)

    runners: dict[str, Runner] = {}
    for cls in classes:
        inst = cls()
        if enabled_ids is not None and inst.id not in enabled_ids:
            continue
        runners[inst.id] = inst
    for row in rows:                       # default_image overrides the class's image_key
        if row.get("default_image") and row.get("id") in runners:
            runners[row["id"]].image_key = row["default_image"]
    return runners


def runner_for(step, runners: dict[str, Runner]) -> Optional[Runner]:
    # explicit runner id wins, then capability match
    if step.runner and step.runner in runners:
        return runners[step.runner]
    for r in runners.values():
        if r.can_handle(step):
            return r
    return None
