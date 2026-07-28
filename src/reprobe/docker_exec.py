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

import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

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


def _validate_mounts(spec: ContainerSpec, work_root: Optional[str | Path]) -> None:
    root = Path(work_root).resolve() if work_root else None
    for m in spec.mounts:
        # Check the raw string BEFORE resolve(): resolving a Windows device
        # path (\\.\pipe\...) touches the actual pipe and raises OSError.
        _sock_check(str(m.source).replace("\\", "/").lower(), m.source)
        try:
            src = Path(m.source).resolve()
        except OSError as exc:
            raise SandboxViolation(f"mount source could not be resolved: {m.source} ({exc})") from exc
        _sock_check(src.as_posix().lower(), m.source)
        if root is not None and not src.is_relative_to(root):
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
               work_root: str | Path | None = None) -> list[str]:
    """Pure function: ContainerSpec + limits -> the exact `docker run` argv.

    Kept pure and importable so tests can assert the sandbox flags without a
    daemon. This is the security surface — read it carefully.

    Raises ``SandboxViolation`` for a flag-shaped/malformed image reference, a
    docker-socket mount, or (when ``work_root`` is given) a bind-mount source
    outside the per-run work root. Callers should always pass ``work_root``;
    ``None`` only skips the containment check, never the socket/image checks."""
    _validate_image(spec.image)
    _validate_mounts(spec, work_root)
    argv: list[str] = ["docker", "run", "--rm", "--name", container_name]

    # --- network ---------------------------------------------------------
    want_egress = spec.network == "egress" and allow_egress
    if want_egress:
        # default bridge network; the install/activation phases use this briefly.
        pass
    else:
        argv += ["--network", "none"]

    # --- identity & privilege -------------------------------------------
    user = limits.get("user", "57439:57439")
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
        argv += ["--cpus", str(limits["cpus"])]
    if limits.get("nofile"):
        argv += ["--ulimit", f"nofile={limits['nofile']}"]
    if limits.get("nproc"):
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
    secret_vals = set(spec.env.values())
    out = []
    for a in argv:
        red = a
        for sv in secret_vals:
            if sv and sv in red:
                red = red.replace(sv, "***")
        out.append(red)
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


def run_container(
    spec: ContainerSpec,
    limits: dict,
    log_path: str | Path,
    *,
    allow_egress: bool = False,
    dry_run: bool = False,
    work_root: str | Path | None = None,
) -> RawRunOutput:
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if not spec.image:
        return RawRunOutput(exit_code=None, duration_s=0.0, error="no-image-resolved")

    name = f"reprobe-{uuid.uuid4().hex[:12]}"
    try:
        argv = build_argv(spec, limits, container_name=name, allow_egress=allow_egress,
                          work_root=work_root)
    except SandboxViolation as exc:
        msg = f"sandbox-violation: {exc}"
        log_path.write_text(msg + "\n", encoding="utf-8")
        return RawRunOutput(exit_code=None, duration_s=0.0, image=spec.image,
                            log_path=str(log_path), error=msg)
    redacted = _redact(argv, spec)

    if dry_run:
        return RawRunOutput(exit_code=0, duration_s=0.0, image=spec.image, argv_redacted=redacted)

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

    # Runner proposes, policy disposes — same rule as every other field. The spec's
    # request is clamped to the configured ceiling so neither a runner plugin nor a
    # CLI override can widen the wall-clock envelope beyond limits.yaml.
    default_timeout = int(limits.get("timeout_s", 1800))
    ceiling = int(limits.get("max_timeout_s") or default_timeout)
    timeout_s = max(1, min(int(spec.timeout_s or default_timeout), ceiling))
    start = time.monotonic()
    timed_out = False
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        log.write("$ " + " ".join(redacted) + "\n\n")
        log.flush()
        completed = False
        try:
            proc = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, timeout=timeout_s)
            exit_code = proc.returncode
            completed = True
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = None
            log.write(f"\n[reprobe] hard timeout after {timeout_s}s — container killed\n")
        finally:
            if not completed:
                # --rm only fires when the container exits. On every non-normal
                # path (timeout, KeyboardInterrupt, OSError, ...) force-stop by
                # name so no exception can orphan a live author-code container.
                subprocess.run(["docker", "kill", name], capture_output=True)
                subprocess.run(["docker", "rm", "-f", name], capture_output=True)

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
    )
