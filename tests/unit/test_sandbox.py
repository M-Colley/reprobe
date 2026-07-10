"""The sandbox envelope is the security surface — assert it without a daemon.

Benign specs must get the full envelope; hostile specs (mount escapes, docker
socket, flag-shaped images) must be rejected outright. No test here may touch
a Docker daemon: build_argv is pure, and run_container tests mock subprocess.
"""

import subprocess
from types import SimpleNamespace

import pytest

from reprobe import docker_exec
from reprobe.config import load_config
from reprobe.docker_exec import SandboxViolation, build_argv, run_container
from reprobe.models import ContainerSpec, Mount


def _argv(network="none", allow_egress=False, **spec_kw):
    cfg = load_config()
    spec_kw.setdefault("image", "img")
    spec_kw.setdefault("command", ["echo", "hi"])
    spec_kw.setdefault("mounts", [Mount(source="/h/run", target="/work", read_only=False)])
    spec = ContainerSpec(network=network, **spec_kw)
    return build_argv(spec, cfg.limits_for("python"), container_name="t",
                      allow_egress=allow_egress, work_root="/h/run")


def test_default_sandbox_flags_present():
    a = " ".join(_argv())
    for required in [
        "--network none", "--user 57439:57439", "--cap-drop ALL",
        "--security-opt no-new-privileges", "--read-only",
        "--tmpfs /tmp:rw,noexec,nosuid", "--pids-limit 512",
        "--memory 8g", "--memory-swap 8g", "--cpus 4",
        "--ulimit nofile=4096", "--ulimit nproc=512",
    ]:
        assert required in a, f"missing sandbox flag: {required}"


def test_network_none_unless_egress_allowed():
    # runner asks for egress but policy does not allow -> still locked down
    assert "--network none" in " ".join(_argv(network="egress", allow_egress=False))
    # egress requested AND allowed -> no --network none (default bridge)
    assert "--network none" not in " ".join(_argv(network="egress", allow_egress=True))


# --------------------------------------------------------------------------- #
# Hostile specs — every one of these must be rejected, not sandboxed-and-run.
# --------------------------------------------------------------------------- #
def test_mount_escape_rejected():
    with pytest.raises(SandboxViolation, match="escapes the per-run work root"):
        _argv(mounts=[Mount(source="/h/other", target="/work", read_only=False)])
    with pytest.raises(SandboxViolation, match="escapes the per-run work root"):
        _argv(mounts=[Mount(source="/h/run/../../etc", target="/work", read_only=True)])


def test_docker_socket_mount_rejected():
    for sock in ["/var/run/docker.sock", "//./pipe/docker_engine",
                 "\\\\.\\pipe\\docker_engine", "/h/run/docker.sock"]:
        with pytest.raises(SandboxViolation, match="docker control socket"):
            _argv(mounts=[Mount(source=sock, target="/var/run/docker.sock", read_only=False)])


def test_flag_shaped_or_malformed_image_rejected():
    for image in ["--privileged", "-v", "--security-opt=seccomp=unconfined",
                  "", " img", "img --privileged", "img\n--privileged", "IMG"]:
        with pytest.raises(SandboxViolation, match="image reference rejected"):
            _argv(image=image)


def test_valid_image_refs_accepted():
    for image in [
        "hello-world",
        "python:3.11-slim",
        "mambaorg/micromamba:1.5.10-noble",
        "ghcr.io/autoui/reprobe-base-py:2026.1",
        "unityci/editor:ubuntu-2022.3.10f1-linux-il2cpp-3",
        "localhost:5000/img:tag",
        "python:3.11-slim@sha256:" + "a" * 64,
    ]:
        argv = _argv(image=image)
        assert argv.index(image) < argv.index("echo")  # image is the first positional


def test_flag_injection_filenames_stay_contained():
    # A work-root file/dir named like a flag must never become a standalone
    # docker option token: mount sources ride inside a `-v` value, command
    # tokens come after the image positional.
    argv = _argv(mounts=[Mount(source="/h/run/--privileged", target="/work", read_only=False)],
                 command=["python", "--privileged.py"])
    assert "--privileged" not in argv
    assert argv.index("img") < argv.index("--privileged.py")


# --------------------------------------------------------------------------- #
# run_container cleanup — subprocess mocked, no daemon.
# --------------------------------------------------------------------------- #
def _patch_docker(monkeypatch, calls, run_effect):
    def fake_run(argv, **kw):
        calls.append(list(argv))
        if argv[:2] == ["docker", "run"]:
            return run_effect(argv)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(docker_exec.subprocess, "run", fake_run)
    monkeypatch.setattr(docker_exec, "image_present", lambda image: True)


def _spec():
    return ContainerSpec(image="img", command=["echo", "hi"])


def test_timeout_kills_and_removes_container(monkeypatch, tmp_path):
    calls = []
    def boom(argv):
        raise subprocess.TimeoutExpired(argv, 1)
    _patch_docker(monkeypatch, calls, boom)
    raw = run_container(_spec(), load_config().limits_for("python"), tmp_path / "run.log")
    assert raw.timed_out and raw.exit_code is None
    assert any(c[:2] == ["docker", "kill"] for c in calls)
    assert any(c[:3] == ["docker", "rm", "-f"] for c in calls)


def test_any_exception_kills_and_removes_container(monkeypatch, tmp_path):
    calls = []
    def boom(argv):
        raise KeyboardInterrupt
    _patch_docker(monkeypatch, calls, boom)
    with pytest.raises(KeyboardInterrupt):
        run_container(_spec(), load_config().limits_for("python"), tmp_path / "run.log")
    assert any(c[:2] == ["docker", "kill"] for c in calls)
    assert any(c[:3] == ["docker", "rm", "-f"] for c in calls)


def test_normal_exit_relies_on_rm_flag_only(monkeypatch, tmp_path):
    calls = []
    _patch_docker(monkeypatch, calls, lambda argv: SimpleNamespace(returncode=0))
    raw = run_container(_spec(), load_config().limits_for("python"), tmp_path / "run.log")
    assert raw.exit_code == 0
    assert not any(c[:2] == ["docker", "kill"] for c in calls)


def test_run_container_surfaces_sandbox_violation_as_error(monkeypatch, tmp_path):
    calls = []
    _patch_docker(monkeypatch, calls, lambda argv: SimpleNamespace(returncode=0))
    spec = ContainerSpec(image="img", command=["echo"],
                         mounts=[Mount(source="/var/run/docker.sock",
                                       target="/var/run/docker.sock", read_only=False)])
    raw = run_container(spec, load_config().limits_for("python"), tmp_path / "run.log")
    assert raw.error and raw.error.startswith("sandbox-violation:")
    assert raw.exit_code is None
    assert not any(c[:2] == ["docker", "run"] for c in calls)  # never launched


def test_dry_run_is_validated_too(tmp_path):
    # dry_run returns the argv before any daemon check — validation must
    # already have happened by then.
    spec = ContainerSpec(image="--privileged", command=["echo"])
    raw = run_container(spec, load_config().limits_for("python"), tmp_path / "run.log",
                        dry_run=True)
    assert raw.error and "sandbox-violation" in raw.error
