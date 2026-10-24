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

import fnmatch
import hashlib
import os
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from ..config import Config
from ..docker_exec import docker_cli_failed
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
    # Conda env the install phase built from the artifact's environment.yml. When
    # set, its bin/ goes first on PATH so `python`/`papermill` are the artifact's
    # own, not the base image's.
    conda_env_prefix: Optional[str] = None


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
def _q(s: object) -> str:
    """POSIX single-quote. The ONLY sanctioned way to put an author-controlled
    value into a runner's ``bash -c`` command — never interpolate into double
    quotes (``$(...)``, backticks and ``$VAR`` stay shell-active there)."""
    return "'" + str(s).replace("'", "'\\''") + "'"


#: Run-phase R environment. R_LIBS_USER holds what the install phase added.
#: HOME and XDG_CACHE_HOME must be writable under the read-only rootfs (as for
#: Python): R writes there for tools::R_user_dir, knitr and renv, and with no
#: usable HOME those fail with errors that read like the artifact's own.
_R_ENV = ("export HOME=/work XDG_CACHE_HOME=/work/.reprobe_cache R_LIBS_USER=/work/.reprobe_Rlib; ")


def env_path_prefix(ctx: "RunContext") -> str:
    """`export PATH=…` putting the artifact's own conda env first, or "".

    One place, so every runner picks up a built environment the same way and a
    step can never silently execute against the base interpreter that the
    artifact's environment.yml exists to replace."""
    prefix = getattr(ctx, "conda_env_prefix", None)
    return f"export PATH={prefix}/bin:$PATH; " if prefix else ""


def own_pythonpath(ctx: "RunContext") -> str:
    """``:/work/<dir>`` for the artifact's own package root (see
    signatures.own_package_root), or "". Only a plain relative directory."""
    d = (ctx.step.args or {}).get("pythonpath")
    if d is None:
        return ""
    d = str(d).strip("/")
    if d and (".." in d.split("/") or not re.fullmatch(r"[\w./-]+", d)):
        return ""
    return ":/work" + (f"/{d}" if d else "")


def env_r_prefix(ctx: "RunContext") -> str:
    """``env_path_prefix`` for R steps. When the artifact's environment provides
    its own R (an environment.yml declaring r-base), that R runs — and the
    harness's R library, built for the base image's R, is left off its path:
    packages compiled for one R version can fail to load in another."""
    prefix = getattr(ctx, "conda_env_prefix", None)
    if not prefix:
        return ""
    return (f"{env_path_prefix(ctx)}if [ -x {_q(prefix + '/bin/Rscript')} ]; "
            "then unset R_LIBS_USER; fi; ")


def snapshot(rundir: Path) -> dict[str, float]:
    """Cheap mtime snapshot of files under rundir, to detect produced files."""
    idx: dict[str, float] = {}
    for root, dirs, files in os.walk(rundir):
        # The harness's own scratch dirs are not artifact output. Walking them
        # is also what makes this expensive: a conda environment built for the
        # artifact is ~50k files, snapshotted twice per step, and every one of
        # them would be reported as a file the run "produced".
        dirs[:] = [d for d in dirs
                   if d not in {".git", "node_modules"} and not d.startswith(".reprobe_")]
        for f in files:
            p = Path(root) / f
            try:
                # lstat: /work is writable by author code, which can plant a link
                # to any host path; following it would stat (and later copy) the
                # host file. A link is recorded as itself and never collected.
                idx[p.relative_to(rundir).as_posix()] = p.lstat().st_mtime  # forward slashes, cross-platform
            except OSError:
                pass
    return idx


def _enforced_timeout(limits: dict) -> int:
    """The wall-clock budget docker_exec actually enforces: the request, clamped to
    ``max_timeout_s``. Reporting the request said "stopped at 99999s" for a step
    that was stopped at the 21600s ceiling."""
    default = int(limits.get("timeout_s", 1800))
    return max(1, min(default, int(limits.get("max_timeout_s") or default)))


def _norm_rel(p: str) -> str:
    """A declared output path in the form snapshot() records: POSIX, relative,
    no ./ or // noise. "./results/out.csv" and "results//out.csv" are the file
    the run wrote as "results/out.csv" — matching only exact strings reported it
    as never produced."""
    p = posixpath.normpath(str(p).replace("\\", "/"))
    return "" if p in (".", "") else p.lstrip("/")


def match_expected(expected: list[str], produced: list[str]) -> list[str]:
    """The declared outputs (as declared) that this run produced: by normalised
    path, by glob (``figures/*.png``), or — for a directory (``results/``) — by any
    file produced under it."""
    return [exp for exp in expected if _matching(exp, produced)]


def _matching(exp: str, produced: list[str]) -> list[str]:
    """The produced paths that satisfy one declared output: the exact file, any file
    under it (a directory), or — only if neither exists — a glob. A literal comes
    first because ``data[1].csv`` is a legal file name, not a character class."""
    n = _norm_rel(exp)
    if not n:
        return []
    exact = [q for q in produced if q == n or q.startswith(n + "/")]
    if exact:
        return exact
    if any(ch in n for ch in "*?["):
        return [q for q in produced if fnmatch.fnmatchcase(q, n)]
    return []


def expand_expected(expected_met: list[str], produced: list[str]) -> list[str]:
    """The concrete files behind the declared outputs a step met — a directory or a
    glob stands for the files this run produced under it. Collection and the
    paper comparison need files, not the pattern the author wrote."""
    out: list[str] = []
    for exp in expected_met:
        # an entry in expected_met was verified as produced; if the produced list does
        # not say which files stand behind it, a literal path stands for itself
        hits = _matching(exp, produced) or ([_norm_rel(exp)] if not any(c in exp for c in "*?") else [])
        for q in hits:
            if q and q not in out:
                out.append(q)
    return out


class BaseRunner:
    id: str = ""
    display_name: str = ""
    handles_types: frozenset[str] = frozenset()
    image_key: str = "python"               # key into pins.yaml:base_images

    def image_key_for(self, step: "RunStep") -> str:
        """The base image this step runs in (a notebook's depends on its kernel)."""
        return self.image_key

    def unsupported(self, step: "RunStep") -> Optional[str]:
        """Why no image can run this step, or None. Such a step is skipped with
        the reason — running it would fail in a way that reads as the artifact's."""
        return None
    host_only: bool = False                 # True -> container_spec returns None; no author code executes

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
        # The executed notebook is the HARNESS's file: it must not satisfy a
        # declared directory or glob (`notebooks/`, `*.ipynb`) on its own.
        theirs = [p for p in produced if not p.endswith(".executed.ipynb")]
        return match_expected(list(ctx.step.expected_outputs), theirs)

    def interpret(self, raw: Optional[RawRunOutput], ctx: RunContext) -> RunResult:
        caps = self.capabilities()
        if raw is None:                       # host-only runners override this
            raise NotImplementedError(f"{self.id} returned no ContainerSpec but did not override interpret()")

        if raw.dry_run:
            # The argv was built and checked; nothing was launched. Saying anything
            # else — even "pass" — puts a fabricated result into a report.
            return RunResult(
                runner=self.id, target=ctx.step.target, status="skipped", executed=False,
                diagnostics={"reason": "dry run — the container was not launched",
                             "argv": raw.argv_redacted},
                not_verified=list(caps.cannot_verify),
            )

        if raw.error:
            # docker_exec sets `error` only for conditions it established itself
            # (no daemon, no image, sandbox violation). Mark them so the LLM
            # diagnoser stays out: the cause is already known exactly, and the
            # log holds no evidence about the artifact for a model to read.
            diagnostics: dict = {"harness_error": raw.error, "infra": True}
            ran = raw.duration_s > 0            # 0.0 = the container never launched
            if ran and raw.log_path:
                # The container did run before the harness lost it, so its output
                # and its half-written outputs are real evidence of how far the
                # step got — the same reason a timeout keeps both.
                diagnostics["log_tail"] = _tail(raw.log_path)
            return RunResult(
                runner=self.id, target=ctx.step.target, status="error",
                exit_code=raw.exit_code, duration_s=raw.duration_s, log_path=raw.log_path,
                artifacts=self._produced(ctx) if ran else [],
                diagnostics=diagnostics,
                not_verified=list(caps.cannot_verify),
            )
        if raw.timed_out:
            # A timeout is evidence, not a void. Keep the same two things a
            # failure keeps: the log tail (which cell was running) and whatever
            # the partial run wrote — papermill/knitr checkpoint their output
            # after every cell, so the half-executed notebook shows exactly how
            # far it got. Reporting neither makes a timeout indistinguishable
            # from a harness bug, and leaves the diagnoser nothing to work with.
            return RunResult(
                runner=self.id, target=ctx.step.target, status="timeout",
                exit_code=raw.exit_code, duration_s=raw.duration_s, log_path=raw.log_path,
                artifacts=self._produced(ctx),
                diagnostics={"timeout_s": _enforced_timeout(ctx.limits),
                             "log_tail": _tail(raw.log_path)},
                not_verified=list(caps.cannot_verify),
            )
        if raw.limit_exceeded or (raw.oom_killed and raw.exit_code != 0):
            # The sandbox's budget ran out, like a timeout: the kernel killed the
            # step at the --memory cap, or the watchdog stopped it for console
            # output / disk use. An analysis that needs 20 GB is not a broken
            # analysis, so this is reported apart from "fail".
            which = raw.limit_exceeded or "memory"
            cap = {"memory": ctx.limits.get("memory"), "output": ctx.limits.get("log_max", "1g"),
                   "disk": ctx.limits.get("work_max", "60g")}.get(which)
            return RunResult(
                runner=self.id, target=ctx.step.target, status="resource-limit",
                exit_code=raw.exit_code, duration_s=raw.duration_s, log_path=raw.log_path,
                artifacts=self._produced(ctx),
                diagnostics={"limit": which, "cap": cap, "log_tail": _tail(raw.log_path)},
                not_verified=list(caps.cannot_verify),
            )
        if (raw.exit_code in (126, 127) and _launcher_failed(raw.log_path))                 or (raw.exit_code == 125 and docker_cli_failed(raw.log_path)):
            # Launcher codes, blamed on the harness only on the launcher's own
            # evidence: 125 when the docker CLI says it failed (an artifact may
            # exit 125 itself, and docker passes that through); 126/127 when bash
            # says OUR interpreter could not start, or the runtime found no shell.
            # A 127 from inside the author's run (a shell-out to a missing tool,
            # `quit(status=127)`) is the artifact's, like any other exit code.
            return RunResult(
                runner=self.id, target=ctx.step.target, status="error",
                exit_code=raw.exit_code, duration_s=raw.duration_s, log_path=raw.log_path,
                diagnostics={
                    "harness_error": f"container command could not start (exit {raw.exit_code}); "
                                     "check base image contents — this usually means the harness image, "
                                     "not the artifact, is broken",
                    "log_tail": _tail(raw.log_path),
                },
                not_verified=list(caps.cannot_verify),
            )

        produced = self._produced(ctx)
        expected_met = self._expected_met(ctx, produced)
        ok = raw.exit_code == 0
        status = "pass" if ok else "fail"
        if ok and ctx.step.expected_outputs and not expected_met \
                and not ctx.step.outputs_inherited:
            # Ran clean but produced none of the outputs IT declared. Outputs
            # merely broadcast from the manifest are excluded: in a multi-step
            # pipeline a prep step is not meant to produce the final artifacts,
            # and marking it "partial" denied the whole pipeline the Functional
            # candidate. Completeness is judged pipeline-wide in report/badges.py.
            status = "partial"

        claims, not_verified = [], list(caps.cannot_verify)
        if ok:
            claims.append(f"{self.display_name} ran to completion (exit 0)")
            claims.extend(c for c in caps.can_verify)
            if expected_met:
                claims.append(f"produced {len(expected_met)} declared output(s)")
        diagnostics = {}
        # Record declared outputs THIS step did not produce. Status stays "pass"
        # for a subset: detection broadcasts the manifest-wide expected_outputs
        # onto every step, so each step of a healthy pipeline legitimately
        # produces only its share — completeness is judged across the pipeline,
        # not per step. But the missing set must be on the record (never
        # over-claim: state what was verified AND what was not).
        missing = sorted(set(ctx.step.expected_outputs) - set(expected_met))
        if missing:
            diagnostics["expected_missing"] = missing
        if not ok:
            diagnostics["log_tail"] = _tail(raw.log_path)
        return RunResult(
            runner=self.id, target=ctx.step.target, status=status,
            exit_code=raw.exit_code, duration_s=raw.duration_s, log_path=raw.log_path,
            artifacts=produced, expected_met=expected_met,
            claims=claims, not_verified=not_verified, diagnostics=diagnostics,
        )


#: The launcher's own words: bash naming the interpreter a runner starts (the
#: `bash -c` command line is line 1), or the runtime finding no executable.
_LAUNCH_FAILED_RE = re.compile(
    r"bash: (?:line \d+: )?(?:\S*/)?(?:python3?|Rscript|R|papermill|jupyter|bash)"
    r": (?:command not found|Permission denied|No such file or directory)"
    r"|executable file not found in \$PATH|OCI runtime (?:create|exec) failed")


def _launcher_failed(log_path: Optional[str]) -> bool:
    return bool(log_path) and bool(_LAUNCH_FAILED_RE.search(_tail(log_path, 80)))


_TAIL_CAP_BYTES = 64 * 1024


def _tail(log_path: Optional[str], n: int = 40) -> str:
    """Last ``n`` lines of a log, reading at most the final ~64 KB. The log is
    the container's uncapped stdout/stderr (author-controlled), so never load the
    whole file into memory just to slice the tail — a multi-GB log would OOM the
    trusted orchestrator."""
    if not log_path:
        return ""
    p = Path(log_path)
    try:
        size = p.stat().st_size
        with p.open("rb") as fh:
            if size > _TAIL_CAP_BYTES:
                fh.seek(size - _TAIL_CAP_BYTES)
            data = fh.read()
    except OSError:
        return ""
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > _TAIL_CAP_BYTES and lines:
        lines = lines[1:]          # drop the first, probably-partial, line
    return "\n".join(lines[-n:])


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
