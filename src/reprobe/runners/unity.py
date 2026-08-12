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

import os
import re
from pathlib import Path
from typing import Optional

from ..models import Capabilities, RawRunOutput, RunResult
from .base import BaseRunner, RunContext

_VERSION_RE = re.compile(r"m_EditorVersion:\s*(\S+)")

# The structural check runs host-side over an untrusted tree: never follow
# symlinks, and stop after this many files (no container = no timeout).
_MAX_SCAN_FILES = 200_000

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
    host_only = True

    def capabilities(self) -> Capabilities:
        return Capabilities(
            can_verify=["project detected", "required editor version readable", "project layout sane"],
            cannot_verify=_STRUCTURAL_NOT_VERIFIED,
        )

    # Host-only: no untrusted code runs, so we propose no container.
    def container_spec(self, ctx: RunContext) -> Optional[None]:
        return None

    def interpret(self, raw: Optional[RawRunOutput], ctx: RunContext) -> RunResult:
        src_root = ctx.src_dir.resolve()
        proj = src_root if ctx.step.target in (".", "") else (ctx.src_dir / ctx.step.target).resolve()
        # The target comes verbatim from the author manifest: absolute paths
        # replace the join base and ../ walks out — refuse anything that
        # resolves outside the fetched source tree (trust boundary: untrusted
        # bytes must never steer host-side filesystem access).
        if not proj.is_relative_to(src_root):
            return RunResult(
                runner=self.id, target=ctx.step.target, status="error",
                exit_code=None, duration_s=0.0,
                diagnostics={"harness_error": "unity target escapes the source directory; refusing host-side inspection"},
                not_verified=list(_STRUCTURAL_NOT_VERIFIED),
            )

        version = _read_editor_version(proj)
        has_assets = (proj / "Assets").is_dir()
        has_settings = (proj / "ProjectSettings").is_dir()
        manifest = proj / "Packages" / "manifest.json"
        scene_count, truncated = _count_scenes(proj / "Assets") if has_assets else (0, False)
        committed_library = (proj / "Library").is_dir()

        diagnostics = {
            "version_detected": version,
            "has_assets": has_assets,
            "has_project_settings": has_settings,
            "packages_manifest": manifest.is_file(),
            "scene_count": scene_count,
            "committed_library_dir": committed_library,
            "suggested_editor_image": (
                f"{(ctx.config.pins.get('unity', {}) or {}).get('image_repo', 'unityci/editor')}:{version}"
                if version else None
            ),
        }
        if truncated:
            diagnostics["scan_truncated"] = f"stopped after {_MAX_SCAN_FILES} files; scene_count is a lower bound"

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


def _count_scenes(assets: Path) -> tuple[int, bool]:
    scenes = seen = 0
    for root, dirs, files in os.walk(assets, followlinks=False):
        for f in files:
            seen += 1
            if seen > _MAX_SCAN_FILES:
                return scenes, True
            if f.endswith(".unity"):
                scenes += 1
    return scenes, False


def _read_editor_version(proj: Path) -> Optional[str]:
    pv = proj / "ProjectSettings" / "ProjectVersion.txt"
    if not pv.is_file() or not pv.resolve().is_relative_to(proj):   # a symlinked version file could point anywhere
        return None
    m = _VERSION_RE.search(pv.read_text(encoding="utf-8", errors="replace"))
    return m.group(1) if m else None
