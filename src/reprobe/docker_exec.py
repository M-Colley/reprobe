"""The SINGLE chokepoint that runs containers.

Nothing else in the harness may shell out to ``docker run`` (a lint test in
tests/ enforces this). Every author-code container is launched here with the
full non-negotiable sandbox envelope from ``config/limits.yaml``. A runner only
*requests* a ``ContainerSpec``; this module clamps it.

Hard guarantees applied here regardless of what a runner asks for:
  * network is ``none`` unless the runner declared egress AND policy allows it
  * non-root user, all caps dropped, no-new-privileges, read-only rootfs
  * cpu / memory / pids / file-descriptor / timeout caps
  * /tmp is a noexec,nosuid tmpfs; no Docker socket; no host bind beyond the
    per-run work dir
  * a host-side hard timeout that ``docker kill``s the container
  * a spec that violates the envelope (flag-shaped image, docker-socket or
    out-of-work-root mount) raises ``SandboxViolation`` in ``build_argv``;
    ``run_container`` surfaces it as a harness error, never as a launch
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Iterable, Optional

from .models import ContainerSpec, RawRunOutput


class SandboxViolation(RuntimeError):
    """A ContainerSpec asked for something the sandbox envelope forbids."""


# Strict image reference: name[:tag][@sha256:digest], lowercase repo path,
# optional registry[:port]/ prefix. Anything else — leading '-', whitespace,
# shell metacharacters — is rejected so an untrusted manifest image string can
# never smuggle a docker flag into the argv (docker parses options up to the
# first positional; the image is that positional).
_IMAGE_RE = re.compile(
    r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?::[0-9]+)?"   # first component (or registry[:port])
    r"(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*"          # further path components
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?"              # :tag
    r"(?:@sha256:[0-9a-f]{64})?$"                           # @sha256:digest
)


def _validate_image(image: str) -> None:
    if not _IMAGE_RE.fullmatch(image or ""):
        raise SandboxViolation(
            f"image reference rejected (must match name[:tag][@sha256:digest]): {image!r}"
        )


def _sock_check(s: str, source: str) -> None:
    if s.endswith("/docker.sock") or s == "docker.sock" \
            or s.startswith("//./pipe/") or "docker_engine" in s:
        raise SandboxViolation(f"mount source touches the docker control socket: {source}")


def _validate_mounts(spec: ContainerSpec, work_root: Optional[str | Path],
                     extra_roots: Iterable[str | Path] = ()) -> None:
    root = Path(work_root).resolve() if work_root else None
    # A mount may also sit under a root the ORCHESTRATOR names (the opt-in
    # --reuse-downloads cache) — never one a runner or a manifest supplies.
    allowed = [root, *(Path(r).resolve() for r in extra_roots)] if root is not None else []
    for m in spec.mounts:
        # Check the raw string BEFORE resolve(): resolving a Windows device
        # path (\\.\pipe\...) touches the actual pipe and raises OSError.
        _sock_check(str(m.source).replace("\\", "/").lower(), m.source)
        try:
            src = Path(m.source).resolve()
        except OSError as exc:
            raise SandboxViolation(f"mount source could not be resolved: {m.source} ({exc})") from exc
        _sock_check(src.as_posix().lower(), m.source)
        if root is not None and not any(src.is_relative_to(a) for a in allowed):
            raise SandboxViolation(f"mount source escapes the per-run work root ({root}): {m.source}")


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def image_present(image: str) -> bool:
    if not image:
        return False
    try:
        r = subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def pull_image(image: str, timeout: int = 600) -> bool:
    """Pull a TRUSTED image (declared in the chair's own pins.yaml). Author
    code images are never auto-pulled — run_container requires them present."""
    if not image:
        return False
    try:
        _validate_image(image)
    except SandboxViolation:
        return False
    try:
        r = subprocess.run(["docker", "pull", image], capture_output=True, timeout=timeout)
        return r.returncode == 0
    except Exception:
        return False


def image_digest(image: str) -> Optional[str]:
    """Resolve the immutable digest of the image actually on disk, so a report
    pins exact bytes even though pins.yaml carries a mutable tag. Prefers the
    registry RepoDigest (portable, pullable); falls back to the local content
    Id when the image was built locally and never pushed. Returns None if the
    image is absent or docker is unreachable (recorded as such, never faked)."""
    if not image:
        return None
    try:
        r = subprocess.run(
            ["docker", "image", "inspect", image,
             "--format", "{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}"],
            capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return None
        out = r.stdout.strip()
        # RepoDigests look like "repo@sha256:..."; keep just the digest part.
        return out.split("@", 1)[1] if "@" in out else out or None
    except Exception:
        return None


def _docker_path(p: str) -> str:
    # Bind-mount sources MUST be absolute — Docker treats a relative path as a
    # named volume. Resolve to an absolute, forward-slash path (Docker Desktop
    # accepts "C:/Users/..." on Windows).
    return Path(p).resolve().as_posix()


def build_argv(spec: ContainerSpec, limits: dict, *, container_name: str, allow_egress: bool,
               work_root: str | Path | None = None, extra_roots: Iterable[str | Path] = (),
               user: Optional[str] = None, deadline: Optional[int] = None,
               max_cpus: Optional[float] = None) -> list[str]:
    """Pure function: ContainerSpec + limits -> the exact `docker run` argv.

    Kept pure and importable so tests can assert the sandbox flags without a
    daemon. This is the security surface — read it carefully.

    Raises ``SandboxViolation`` for a flag-shaped/malformed image reference, a
    docker-socket mount, or (when ``work_root`` is given) a bind-mount source
    outside the per-run work root. Callers should always pass ``work_root``;
    ``None`` only skips the containment check, never the socket/image checks.

    ``user`` overrides ``limits["user"]`` (run_container maps it to the host
    user on Linux, see host_user). No ``--rm``: run_container inspects the exited
    container for an OOM kill and then removes it itself, on every path. The
    label lets ``reap_orphans`` find a container whose orchestrator died."""
    _validate_image(spec.image)
    _validate_mounts(spec, work_root, extra_roots)
    argv: list[str] = ["docker", "run", "--name", container_name, "--label", f"{LABEL}=1"]
    if deadline is not None:
        argv += ["--label", f"{LABEL}.deadline={deadline}"]

    # --- network ---------------------------------------------------------
    want_egress = spec.network == "egress" and allow_egress
    if want_egress:
        # default bridge network; the install/activation phases use this briefly.
        pass
    else:
        argv += ["--network", "none"]

    # --- identity & privilege -------------------------------------------
    mapped_user = user
    user = user or limits.get("user", "57439:57439")
    if user:
        argv += ["--user", str(user)]
    if limits.get("drop_all_caps", True):
        argv += ["--cap-drop", "ALL"]
    if limits.get("no_new_privileges", True):
        argv += ["--security-opt", "no-new-privileges"]
    seccomp = limits.get("seccomp", "default")
    if seccomp and seccomp != "default":
        argv += ["--security-opt", f"seccomp={seccomp}"]

    # --- filesystem ------------------------------------------------------
    if limits.get("read_only_rootfs", True):
        argv += ["--read-only"]
    tmpfs_size = limits.get("tmpfs_size", "512m")
    # /tmp is noexec for hardened RUN containers; the dependency-INSTALL phase
    # relaxes this (tmpfs_noexec=False) because compiling packages executes in
    # /tmp. `exec` must be set EXPLICITLY — Docker's --tmpfs defaults to noexec,
    # so merely omitting noexec leaves /tmp non-executable and every R/C source
    # package fails its `configure` step with "exists but is not executable".
    tmpfs_opts = "rw,noexec,nosuid" if limits.get("tmpfs_noexec", True) else "rw,exec,nosuid"
    argv += ["--tmpfs", f"/tmp:{tmpfs_opts},size={tmpfs_size}"]

    # --- resources -------------------------------------------------------
    if limits.get("pids"):
        argv += ["--pids-limit", str(limits["pids"])]
    mem = limits.get("memory")
    if mem:
        argv += ["--memory", str(mem), "--memory-swap", str(mem)]   # == memory -> no swap
    if limits.get("cpus"):
        # docker refuses --cpus above what the daemon has (exit 125, "Range of CPUs
        # is from 0.01 to 2.00") — a 2-vCPU runner or a small Docker Desktop VM
        # then fails every step for a reason that has nothing to do with the code.
        cpus = float(limits["cpus"])
        if max_cpus:
            cpus = min(cpus, float(max_cpus))
        argv += ["--cpus", f"{cpus:g}"]
    if limits.get("nofile"):
        argv += ["--ulimit", f"nofile={limits['nofile']}"]
    if limits.get("nproc") and not mapped_user:
        # RLIMIT_NPROC counts every process of that uid on the HOST, not in this
        # container. Under the host-user mapping that includes the chair's whole
        # desktop session, so author code could not start a thread. --pids-limit
        # is the per-container cap either way; nproc only adds anything for the
        # image's own uid.
        argv += ["--ulimit", f"nproc={limits['nproc']}"]
    if limits.get("runtime"):
        argv += ["--runtime", str(limits["runtime"])]               # e.g. runsc (gVisor)

    # --- workdir / mounts / env -----------------------------------------
    argv += ["-w", spec.workdir]
    for m in spec.mounts:
        suffix = ":ro" if m.read_only else ""
        argv += ["-v", f"{_docker_path(m.source)}:{m.target}{suffix}"]
    for k, v in spec.env.items():
        argv += ["-e", f"{k}={v}"]

    argv += [spec.image, *spec.command]
    return argv


def _redact(argv: list[str], spec: ContainerSpec) -> list[str]:
    """The argv as logged: every ``-e KEY=value`` shows its key only. Redacting by
    searching for the values anywhere in the argv also rewrote unrelated tokens
    that happened to equal one (an env value "none" turned ``--network none``
    into ``--network ***`` in the log)."""
    out = list(argv)
    for i in range(1, len(out)):
        if out[i - 1] == "-e" and "=" in out[i]:
            out[i] = out[i].split("=", 1)[0] + "=***"
    return out


# --------------------------------------------------------------------------- #
# Daemon loss. `docker run` exits 125 for "the CLI/daemon failed, not the
# container's command" — one code covering two very different events: the
# container never started (bad flag, unusable image), or the daemon went away
# WHILE the container ran. Only the CLI's own wording separates them.
# --------------------------------------------------------------------------- #
_DAEMON_LOST_MARKERS = (
    "error waiting for container",       # "…: unexpected EOF" — engine vanished mid-run
    "cannot connect to the docker daemon",
    "error during connect",
    "is the docker daemon running",
)

# Error prefixes meaning "the daemon, not the artifact". Callers use these to
# tell an infrastructure failure from a failure of the code under test.
DAEMON_DOWN_ERRORS = ("docker-daemon-lost", "docker-unavailable")

_RESTART_HINT = (
    "Restart Docker (Desktop: quit it fully and relaunch; Linux: "
    "`sudo systemctl restart docker`), confirm with `reprobe doctor`, then re-run."
)


def _log_tail_text(path: Path, n_bytes: int = 4096) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - n_bytes))
            return fh.read().decode("utf-8", "replace").lower()
    except OSError:
        return ""


def _daemon_lost(log_path: Path) -> bool:
    """True when the docker CLI reported losing the daemon rather than failing to
    start the container — the two cases exit 125 alike."""
    return any(m in _log_tail_text(log_path) for m in _DAEMON_LOST_MARKERS)


#: Label on every container this module starts; reap_orphans() keys on it.
LABEL = "reprobe.managed"

#: How often the watchdog wakes to check the clock, the log and the disk.
_POLL_S = 1.0
#: The /work size walk is the expensive check, so it runs less often.
_DISK_EVERY_S = 30.0


def effective_timeout(spec: ContainerSpec, limits: dict) -> int:
    """Runner proposes, policy disposes — same rule as every other field. The
    spec's request is clamped to the configured ceiling so neither a runner
    plugin nor a CLI override can widen the wall-clock envelope beyond limits.yaml."""
    default_timeout = int(limits.get("timeout_s", 1800))
    ceiling = int(limits.get("max_timeout_s") or default_timeout)
    return max(1, min(int(spec.timeout_s or default_timeout), ceiling))


@functools.lru_cache(maxsize=1)
def _rootless_daemon() -> bool:
    try:
        r = subprocess.run(["docker", "info", "--format", "{{json .SecurityOptions}}"],
                           capture_output=True, text=True, timeout=20)
        return "rootless" in (getattr(r, "stdout", "") or "")
    except Exception:
        return False


def host_user(limits: dict) -> Optional[str]:
    """The uid:gid to run as so the bind-mounted run dir is writable, or None to
    keep ``limits["user"]``.

    A bind mount keeps host ownership. On native Linux the run dir belongs to the
    chair (mode 0755), so the image's own non-root uid 57439 can read it but not
    write a single output — and every step then fails on PermissionError, which
    reads in the report as the artifact's fault. Running as the host user keeps
    the container non-root AND lets the host delete what it wrote. Docker Desktop
    on Windows has no getuid and remaps ownership on its file share, so the
    configured uid stays. Never maps to root: a host uid of 0 keeps the
    configured non-root user. A rootless daemon maps container uids into a subuid
    range, where the host uid would NOT be the host user — keep the configured
    user there too (see the chair runbook)."""
    if not limits.get("map_host_user", True) or not hasattr(os, "getuid"):
        return None
    uid, gid = os.getuid(), os.getgid()
    if uid == 0 or gid == 0 or _rootless_daemon():      # never root, nor root's group
        return None
    return f"{uid}:{gid}"


def _parse_bytes(v: object) -> int:
    """'8g' / '512m' / '100k' / 1234 -> bytes; 0 for unset or unparseable."""
    if not v:
        return 0
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?)b?\s*", str(v).lower())
    if not m:
        return 0
    return int(float(m.group(1)) * 1024 ** "_kmgt".index(m.group(2) or "_"))


_MAX_TREE_ENTRIES = 3_000_000


def _tree_exceeds(root: Path, cap: int) -> bool:
    """True once the files under root pass ``cap`` bytes — or an absurd number of
    entries (millions of empty files exhaust inodes and would hold this loop for as
    long as the walk takes). Never follows links (the tree is author-controlled) and
    stops counting as soon as the answer is yes."""
    total = 0
    entries = 0
    stack = [root]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    entries += 1
                    if entries > _MAX_TREE_ENTRIES:
                        return True
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
                    if total > cap:
                        return True
        except OSError:
            continue
    return False


def _oom_killed(name: str) -> Optional[bool]:
    try:
        r = subprocess.run(["docker", "inspect", "--format", "{{.State.OOMKilled}}", name],
                           capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return None
        out = (getattr(r, "stdout", "") or "").strip().lower()
        return True if out == "true" else False if out == "false" else None
    except Exception:
        return None


def _docker_quiet(args: list[str], timeout: int = 60) -> None:
    """A cleanup call that can neither raise nor hang: a wedged daemon must not
    turn into a wedged orchestrator in the middle of a season."""
    try:
        subprocess.run(["docker", *args], capture_output=True, timeout=timeout)
    except Exception:
        pass


def _stop_and_remove(name: str) -> None:
    _docker_quiet(["kill", name])
    _docker_quiet(["rm", "-f", name])


@functools.lru_cache(maxsize=1)
def _daemon_cpus() -> Optional[float]:
    try:
        r = subprocess.run(["docker", "info", "--format", "{{.NCPU}}"],
                           capture_output=True, text=True, timeout=20)
        n = float((getattr(r, "stdout", "") or "").strip())
        return n if n > 0 else None
    except Exception:
        return None


def reap_orphans() -> list[str]:
    """Remove containers a previous, hard-killed orchestrator left behind.

    The ``finally`` in run_container covers every exception, Ctrl-C and SIGTERM
    (the CLI turns it into SystemExit) — but not SIGKILL or a lost host. Those
    leave an exited container (there is no --rm) or, worse, a running one with
    nobody enforcing its timeout. A container is removed only once its OWN deadline
    label has passed — whatever state it is in — so a concurrent reprobe's live
    container, or one that has just exited and is about to be inspected for an OOM
    kill, is never touched. Returns the names removed."""
    fmt = "{{.Names}}\t{{.State}}\t{{.Label \"" + LABEL + ".deadline\"}}"
    try:
        r = subprocess.run(["docker", "ps", "-a", "--filter", f"label={LABEL}=1", "--format", fmt],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return []
        rows = (getattr(r, "stdout", "") or "").splitlines()
    except Exception:
        return []
    now = int(time.time())
    removed = []
    for row in rows:
        name, _state, deadline = (row.split("\t") + ["", ""])[:3]
        if not name.startswith("reprobe-"):
            continue
        if deadline.isdigit() and int(deadline) < now:
            _stop_and_remove(name)
            removed.append(name)
    return removed


def run_container(
    spec: ContainerSpec,
    limits: dict,
    log_path: str | Path,
    *,
    allow_egress: bool = False,
    dry_run: bool = False,
    work_root: str | Path | None = None,
    extra_roots: Iterable[str | Path] = (),
) -> RawRunOutput:
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if not spec.image:
        return RawRunOutput(exit_code=None, duration_s=0.0, error="no-image-resolved")

    name = f"reprobe-{uuid.uuid4().hex[:12]}"
    timeout_s = effective_timeout(spec, limits)
    try:
        argv = build_argv(spec, limits, container_name=name, allow_egress=allow_egress,
                          work_root=work_root, extra_roots=extra_roots,
                          user=None if dry_run else host_user(limits),
                          deadline=int(time.time()) + timeout_s + 300,
                          max_cpus=None if dry_run else _daemon_cpus())
    except SandboxViolation as exc:
        msg = f"sandbox-violation: {exc}"
        log_path.write_text(msg + "\n", encoding="utf-8")
        return RawRunOutput(exit_code=None, duration_s=0.0, image=spec.image,
                            log_path=str(log_path), error=msg)
    redacted = _redact(argv, spec)

    if dry_run:
        # Nothing ran, so there is no exit code to report — a 0 here once read as
        # "ran to completion" all the way into the badge.
        return RawRunOutput(exit_code=None, duration_s=0.0, image=spec.image,
                            argv_redacted=redacted, dry_run=True)

    if not image_present(spec.image):
        # `docker image inspect` fails the same way for "no such image" and for
        # "no daemon to ask", so the absence of an image is only a fact once the
        # daemon has answered. Reporting the wrong one of these sends a chair to
        # `docker pull` a tag that was present all along.
        if not docker_available():
            msg = (
                "docker-unavailable: the Docker daemon is not reachable, so no image can be "
                f"checked or run. This says nothing about {spec.image} or about the artifact. "
                + _RESTART_HINT
            )
        else:
            msg = (
                f"image-not-present: {spec.image}. `docker pull {spec.image}` "
                f"(base images are published) or build with `bash images/build-images.sh`, then retry."
            )
        log_path.write_text(msg + "\n", encoding="utf-8")
        return RawRunOutput(exit_code=None, duration_s=0.0, image=spec.image,
                            argv_redacted=redacted, log_path=str(log_path), error=msg)

    # Budgets that are not wall-clock. Container stdout is author-controlled and
    # lands on the host disk, and /work is a host bind mount with no quota — six
    # hours of either is enough to fill the disk a whole review season runs on.
    log_cap = _parse_bytes(limits.get("log_max", "1g"))
    disk_cap = _parse_bytes(limits.get("work_max", "60g"))
    disk_root = Path(work_root) if work_root else None

    start = time.monotonic()
    timed_out = False
    limit_exceeded: Optional[str] = None
    exit_code: Optional[int] = None
    oom: Optional[bool] = None
    proc: Optional[subprocess.Popen] = None
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        log.write("$ " + " ".join(redacted) + "\n\n")
        log.flush()
        completed = False
        try:
            proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
            next_disk = start + _DISK_EVERY_S
            while True:
                try:
                    exit_code = proc.wait(timeout=_POLL_S)
                    completed = True
                    break
                except subprocess.TimeoutExpired:
                    pass
                now = time.monotonic()
                if now - start >= timeout_s:
                    timed_out = True
                    log.write(f"\n[reprobe] hard timeout after {timeout_s}s — container killed\n")
                    break
                if log_cap and log_path.stat().st_size > log_cap:
                    limit_exceeded = "output"
                    log.write(f"\n[reprobe] console output passed {limits.get('log_max', '1g')} — "
                              "container killed\n")
                    break
                if disk_cap and disk_root is not None and now >= next_disk:
                    next_disk = now + _DISK_EVERY_S
                    if _tree_exceeds(disk_root, disk_cap):
                        limit_exceeded = "disk"
                        log.write(f"\n[reprobe] files under /work passed {limits.get('work_max', '60g')} "
                                  "— container killed\n")
                        break
        finally:
            if completed:
                # No --rm, so the exited container is still there to ask whether
                # the kernel OOM-killed it; then it goes, on this path too.
                oom = _oom_killed(name)
                _docker_quiet(["rm", "-f", name])
            else:
                # Every non-normal path (timeout, a budget, KeyboardInterrupt,
                # SystemExit from SIGTERM, OSError, ...) force-stops by name so
                # no exception can orphan a live author-code container.
                _stop_and_remove(name)
                if proc is not None:
                    try:
                        proc.wait(timeout=30)
                    except Exception:
                        proc.kill()

    duration_s = round(time.monotonic() - start, 2)

    # A 125 whose log ends in a CLI disconnect — or that leaves no reachable
    # daemon behind — is the engine dying under a running container, not a
    # container that failed to start. Only this module can tell the difference,
    # and getting it wrong blames the base image for a host outage.
    error: Optional[str] = None
    if exit_code == 125 and (_daemon_lost(log_path) or not docker_available()):
        error = (
            f"docker-daemon-lost: the Docker daemon stopped responding {round(duration_s)}s into "
            "this step (`docker run` exited 125 after the container had been running). The run was "
            "lost to a host failure — nothing here is a statement about the artifact. " + _RESTART_HINT
        )

    return RawRunOutput(
        exit_code=exit_code,
        duration_s=duration_s,
        timed_out=timed_out,
        log_path=str(log_path),
        image=spec.image,
        argv_redacted=redacted,
        error=error,
        oom_killed=oom,
        limit_exceeded=limit_exceeded,
    )


def docker_cli_failed(log_path: Optional[str]) -> bool:
    """True when the docker CLI itself reported a failure (``docker: Error …``).

    Exit 125 is docker's own "could not run the container" code — but it is also
    an ordinary exit status for the author's program, and docker passes the
    container's status straight through. Only the CLI's own message tells the
    two apart; without it, an artifact calling ``sys.exit(125)`` was reported as
    a broken base image."""
    if not log_path:
        return False
    text = _log_tail_text(Path(log_path), 16384)
    return bool(re.search(r"^docker: |error response from daemon", text, re.M))
