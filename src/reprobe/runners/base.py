"""The runner plugin contract.

A runner PROPOSES a ``ContainerSpec``; the orchestrator (via ``docker_exec``)
DISPOSES — it clamps every field to ``config/limits.yaml`` before any author
code runs. A runner can never choose its own flags, open the network, escalate
privileges, or mount the Docker socket.

``container_spec`` returning ``None`` marks a *host-only* runner that performs
deterministic inspection with NO untrusted code execution (e.g. Unity T0). For
those, ``interpret(None, ctx)`` does the work.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from ..config import Config
from ..models import (
    Capabilities,
    ContainerSpec,
    Mount,
    RawRunOutput,
    RunResult,
    RunStep,
)


@dataclass
class RunContext:
    step: RunStep
    rundir: Path                 # writable throwaway copy, mounted at /work (rw)
    src_dir: Path                # pristine read-only source (host-side inspection only)
    out_dir: Path                # host dir for logs + collected artifacts
    image: Optional[str]
    config: Config
    limits: dict
    pre_index: dict[str, float] = field(default_factory=dict)   # rundir snapshot before run


@runtime_checkable
class Runner(Protocol):
    id: str
    display_name: str
    handles_types: frozenset[str]

    def can_handle(self, step: RunStep) -> bool: ...
    def container_spec(self, ctx: RunContext) -> Optional[ContainerSpec]: ...
    def interpret(self, raw: Optional[RawRunOutput], ctx: RunContext) -> RunResult: ...
    def capabilities(self) -> Capabilities: ...


# --------------------------------------------------------------------------- #
# Shared base class with the common, boring logic.
# --------------------------------------------------------------------------- #
def snapshot(rundir: Path) -> dict[str, float]:
    """Cheap mtime snapshot of files under rundir, to detect produced files."""
    idx: dict[str, float] = {}
    for root, dirs, files in os.walk(rundir):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules"}]
        for f in files:
            p = Path(root) / f
            try:
                idx[p.relative_to(rundir).as_posix()] = p.stat().st_mtime  # forward slashes, cross-platform
            except OSError:
                pass
    return idx


class BaseRunner:
    id: str = ""
    display_name: str = ""
    handles_types: frozenset[str] = frozenset()
    image_key: str = "python"               # key into pins.yaml:base_images

    # -- routing ---------------------------------------------------------- #
    def can_handle(self, step: RunStep) -> bool:
        return step.runner == self.id or (not step.runner and step.kind in self.handles_types)

    def capabilities(self) -> Capabilities:  # overridden by subclasses
        return Capabilities()

    # -- spec ------------------------------------------------------------- #
    def build_command(self, ctx: RunContext) -> list[str]:
        raise NotImplementedError

    def container_spec(self, ctx: RunContext) -> Optional[ContainerSpec]:
        caps = self.capabilities()
        return ContainerSpec(
            image=ctx.image or "",
            command=self.build_command(ctx),
            workdir="/work",
            mounts=[Mount(source=ctx.rundir.as_posix(), target="/work", read_only=False)],
            network="egress" if caps.needs_network else "none",
            needs_license=caps.requires_secret,
            timeout_s=ctx.limits.get("timeout_s"),
        )

    # -- interpretation --------------------------------------------------- #
    def _produced(self, ctx: RunContext) -> list[str]:
        after = snapshot(ctx.rundir)
        produced = [
            p for p, m in after.items()
            if p not in ctx.pre_index or ctx.pre_index[p] != m
        ]
        return sorted(produced)

    def _expected_met(self, ctx: RunContext, produced: list[str]) -> list[str]:
        # An expected output counts ONLY if THIS run created or modified it — a
        # committed-but-unchanged output file does not count (it would otherwise
        # falsely inflate the Functional signal for repos that commit their figures).
        produced_set = set(produced)
        return [exp for exp in ctx.step.expected_outputs if exp in produced_set]

    def interpret(self, raw: Optional[RawRunOutput], ctx: RunContext) -> RunResult:
        caps = self.capabilities()
        if raw is None:                       # host-only runners override this
            raise NotImplementedError(f"{self.id} returned no ContainerSpec but did not override interpret()")

        if raw.error:
            return RunResult(
                runner=self.id, target=ctx.step.target, status="error",
                exit_code=raw.exit_code, duration_s=raw.duration_s, log_path=raw.log_path,
                diagnostics={"harness_error": raw.error},
                not_verified=list(caps.cannot_verify),
            )
        if raw.timed_out:
            return RunResult(
                runner=self.id, target=ctx.step.target, status="timeout",
                exit_code=raw.exit_code, duration_s=raw.duration_s, log_path=raw.log_path,
                diagnostics={"timeout_s": ctx.limits.get("timeout_s")},
                not_verified=list(caps.cannot_verify),
            )

        produced = self._produced(ctx)
        expected_met = self._expected_met(ctx, produced)
        ok = raw.exit_code == 0
        status = "pass" if ok else "fail"
        if ok and ctx.step.expected_outputs and not expected_met:
            status = "partial"            # ran clean but didn't produce what it promised

        claims, not_verified = [], list(caps.cannot_verify)
        if ok:
            claims.append(f"{self.display_name} ran to completion (exit 0)")
            claims.extend(c for c in caps.can_verify)
            if expected_met:
                claims.append(f"produced {len(expected_met)} declared output(s)")
        diagnostics = {}
        if not ok:
            diagnostics["log_tail"] = _tail(raw.log_path)
        return RunResult(
            runner=self.id, target=ctx.step.target, status=status,
            exit_code=raw.exit_code, duration_s=raw.duration_s, log_path=raw.log_path,
            artifacts=produced, expected_met=expected_met,
            claims=claims, not_verified=not_verified, diagnostics=diagnostics,
        )


def _tail(log_path: Optional[str], n: int = 40) -> str:
    if not log_path or not Path(log_path).exists():
        return ""
    try:
        lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return ""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
