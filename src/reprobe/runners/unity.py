"""Unity runner — T0 *structural* tier only (no Unity licence required).

This tier runs HOST-SIDE with zero code execution: it confirms the artifact is
a Unity project, reads the required editor version, and sanity-checks the
project layout. It reports the *tier it reached* and is explicit about what a
structural check can NOT verify.

T1 (compile) and T2 (headless Linux build) are designed in docs/DESIGN.md §5
and need the reviewing institution's OWN Unity Pro/Plus seat or Licensing
Server — they are intentionally not enabled in this build.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from .base import BaseRunner, RunContext
from ..models import Capabilities, RawRunOutput, RunResult

_VERSION_RE = re.compile(r"m_EditorVersion:\s*(\S+)")

_STRUCTURAL_NOT_VERIFIED = [
    "the project compiles",
    "a player builds",
    "rendering / graphics",
    "user input / interactivity",
    "VR / AR behaviour",
    "device-specific behaviour",
    "the interactive prototype actually works",
]


class UnityRunner(BaseRunner):
    id = "unity"
    display_name = "Unity project (structural)"
    handles_types = frozenset({"unity"})
    image_key = "unity"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            can_verify=["project detected", "required editor version readable", "project layout sane"],
            cannot_verify=_STRUCTURAL_NOT_VERIFIED,
        )

    # Host-only: no untrusted code runs, so we propose no container.
    def container_spec(self, ctx: RunContext) -> Optional[None]:
        return None

    def interpret(self, raw: Optional[RawRunOutput], ctx: RunContext) -> RunResult:
        proj = ctx.src_dir / ctx.step.target if ctx.step.target not in (".", "") else ctx.src_dir
        proj = proj.resolve()

        version = _read_editor_version(proj)
        has_assets = (proj / "Assets").is_dir()
        has_settings = (proj / "ProjectSettings").is_dir()
        manifest = proj / "Packages" / "manifest.json"
        scenes = list((proj / "Assets").rglob("*.unity")) if has_assets else []
        committed_library = (proj / "Library").is_dir()

        diagnostics = {
            "version_detected": version,
            "has_assets": has_assets,
            "has_project_settings": has_settings,
            "packages_manifest": manifest.is_file(),
            "scene_count": len(scenes),
            "committed_library_dir": committed_library,
            "suggested_editor_image": (
                f"{(ctx.config.pins.get('unity', {}) or {}).get('image_repo', 'unityci/editor')}:{version}"
                if version else None
            ),
        }

        detected = has_assets and has_settings
        claims, not_verified = [], list(_STRUCTURAL_NOT_VERIFIED)
        if detected:
            claims.append("Unity project detected")
        if version:
            claims.append(f"targets Unity {version}")
        if manifest.is_file():
            claims.append("Packages/manifest.json present")
        if committed_library:
            not_verified.append("note: Library/ is committed (build cache bloat; should be gitignored)")

        status = "pass" if (detected and version) else ("partial" if detected else "fail")
        return RunResult(
            runner=self.id, target=ctx.step.target, status=status,
            tier_reached="structural", exit_code=None, duration_s=0.0,
            artifacts=[], expected_met=[], claims=claims,
            not_verified=not_verified, diagnostics=diagnostics,
        )


def _read_editor_version(proj: Path) -> Optional[str]:
    pv = proj / "ProjectSettings" / "ProjectVersion.txt"
    if not pv.is_file():
        return None
    m = _VERSION_RE.search(pv.read_text(encoding="utf-8", errors="replace"))
    return m.group(1) if m else None
