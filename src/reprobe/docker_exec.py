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
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

from .models import ContainerSpec, RawRunOutput


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


def _docker_path(p: str) -> str:
    # Bind-mount sources MUST be absolute — Docker treats a relative path as a
    # named volume. Resolve to an absolute, forward-slash path (Docker Desktop
    # accepts "C:/Users/..." on Windows).
    return Path(p).resolve().as_posix()


def build_argv(spec: ContainerSpec, limits: dict, *, container_name: str, allow_egress: bool) -> list[str]:
    """Pure function: ContainerSpec + limits -> the exact `docker run` argv.

    Kept pure and importable so tests can assert the sandbox flags without a
    daemon. This is the security surface — read it carefully."""
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
    # relaxes this (tmpfs_noexec=False) because compiling packages executes in /tmp.
    tmpfs_opts = "rw,noexec,nosuid" if limits.get("tmpfs_noexec", True) else "rw,nosuid"
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


def run_container(
    spec: ContainerSpec,
    limits: dict,
    log_path: str | Path,
    *,
    allow_egress: bool = False,
    dry_run: bool = False,
) -> RawRunOutput:
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if not spec.image:
        return RawRunOutput(exit_code=None, duration_s=0.0, error="no-image-resolved")

    name = f"reprobe-{uuid.uuid4().hex[:12]}"
    argv = build_argv(spec, limits, container_name=name, allow_egress=allow_egress)
    redacted = _redact(argv, spec)

    if dry_run:
        return RawRunOutput(exit_code=0, duration_s=0.0, image=spec.image, argv_redacted=redacted)

    if not image_present(spec.image):
        msg = (
            f"image-not-present: {spec.image}. Build base images with "
            f"`bash images/build-images.sh` (or `docker pull` the fallback), then retry."
        )
        log_path.write_text(msg + "\n", encoding="utf-8")
        return RawRunOutput(exit_code=None, duration_s=0.0, image=spec.image,
                            argv_redacted=redacted, log_path=str(log_path), error=msg)

    timeout_s = int(spec.timeout_s or limits.get("timeout_s", 1800))
    start = time.monotonic()
    timed_out = False
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        log.write("$ " + " ".join(redacted) + "\n\n")
        log.flush()
        try:
            proc = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, timeout=timeout_s)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = None
            subprocess.run(["docker", "kill", name], capture_output=True)
            log.write(f"\n[reprobe] hard timeout after {timeout_s}s — container killed\n")

    return RawRunOutput(
        exit_code=exit_code,
        duration_s=round(time.monotonic() - start, 2),
        timed_out=timed_out,
        log_path=str(log_path),
        image=spec.image,
        argv_redacted=redacted,
    )
