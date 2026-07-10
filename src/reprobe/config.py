"""Configuration loading. All knobs live in ``config/*.yaml`` so the yearly
maintenance job is editing data, not code. Resolution order for the config dir:

1. ``--config-dir`` CLI flag (passed in explicitly)
2. ``$REPROBE_CONFIG_DIR``
3. ``<repo-root>/config``  (the repo this package was installed from)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _find_repo_config() -> Path:
    # src/reprobe/config.py -> repo root is three parents up
    here = Path(__file__).resolve()
    candidate = here.parents[2] / "config"
    return candidate


def resolve_config_dir(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("REPROBE_CONFIG_DIR")
    if env:
        return Path(env).resolve()
    return _find_repo_config()


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass
class Config:
    config_dir: Path
    pins: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    runners: dict[str, Any] = field(default_factory=dict)
    badges: dict[str, Any] = field(default_factory=dict)

    # -- convenience accessors -------------------------------------------- #
    def base_image(self, key: str | None) -> str | None:
        if not key:
            return None
        return (self.pins.get("base_images") or {}).get(key)

    def limits_for(self, runner_id: str) -> dict[str, Any]:
        base = dict(self.limits.get("defaults", {}))
        per = (self.limits.get("per_runner", {}) or {}).get(runner_id, {})
        base.update(per)
        return base

    @property
    def fetch_cfg(self) -> dict[str, Any]:
        return self.pins.get("fetch", {}) or {}

    @property
    def llm(self) -> dict[str, Any]:
        return self.pins.get("llm", {}) or {}

    @property
    def runner_rows(self) -> list[dict[str, Any]]:
        return self.runners.get("runners", []) or []


def load_config(config_dir: str | os.PathLike[str] | None = None) -> Config:
    cdir = resolve_config_dir(config_dir)
    return Config(
        config_dir=cdir,
        pins=_load_yaml(cdir / "pins.yaml"),
        limits=_load_yaml(cdir / "limits.yaml"),
        runners=_load_yaml(cdir / "runners.yaml"),
        badges=_load_yaml(cdir / "badges.yaml"),
    )
