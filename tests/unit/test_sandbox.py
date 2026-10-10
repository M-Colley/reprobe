"""The sandbox envelope is the security surface — assert it without a daemon.

Benign specs must get the full envelope; hostile specs (mount escapes, docker
socket, flag-shaped images) must be rejected outright. No test here may touch
a Docker daemon: build_argv is pure, and run_container tests mock the docker CLI
(tests/sandbox/ runs the same envelope against a real daemon in CI).
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


def test_install_phase_tmpfs_is_executable():
    # The dependency-install phase relaxes /tmp to EXECUTABLE so source packages
    # can run ./configure and compile. Docker's --tmpfs is noexec by default, so
    # `exec` must be explicit — merely dropping noexec is not enough.
    cfg = load_config()
    relaxed = {**cfg.limits_for("python"), "tmpfs_noexec": False}
    argv = build_argv(
        ContainerSpec(image="img", command=["x"],
                      mounts=[Mount(source="/h/run", target="/work", read_only=False)]),
        relaxed, container_name="t", allow_egress=True, work_root="/h/run")
    joined = " ".join(argv)
    assert "--tmpfs /tmp:rw,exec,nosuid" in joined
    assert "noexec" not in joined
    # the hardened RUN default stays noexec
    assert "--tmpfs /tmp:rw,noexec,nosuid" in " ".join(_argv())


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
        "ghcr.io/m-colley/reprobe-base-py:2026.1",
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
# run_container — docker CLI mocked (Popen for `docker run`, run for the rest).
# --------------------------------------------------------------------------- #
class _FakeProc:
    """Stands in for the `docker run` CLI process. ``wait_effect(timeout)``
    returns an exit code or raises (TimeoutExpired = still running)."""

    def __init__(self, argv, wait_effect):
        self.argv = argv
        self._wait = wait_effect

    def wait(self, timeout=None):
        return self._wait(timeout)

    def kill(self):
        pass


def _patch_docker(monkeypatch, calls, wait_effect, *, stdout="", inspect_out="false",
                  daemon_up=True):
    def fake_popen(argv, stdout=None, stderr=None, **kw):
        calls.append(list(argv))
        if stdout is not None and stdout_text:
            stdout.write(stdout_text)
            stdout.flush()
        return _FakeProc(argv, wait_effect)

    stdout_text = stdout

    def fake_run(argv, **kw):
        calls.append(list(argv))
        if argv[:2] == ["docker", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=inspect_out + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(docker_exec.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(docker_exec.subprocess, "run", fake_run)
    monkeypatch.setattr(docker_exec, "image_present", lambda image: True)
    monkeypatch.setattr(docker_exec, "docker_available", lambda: daemon_up)
    monkeypatch.setattr(docker_exec, "host_user", lambda limits: None)


def _fast_clock(monkeypatch, step=1.0):
    """A monotonic clock that advances ``step`` seconds per read, so a watchdog
    loop reaches its deadline without the test sleeping through it."""
    t = {"now": 0.0}

    def tick():
        t["now"] += step
        return t["now"]
    monkeypatch.setattr(docker_exec.time, "monotonic", tick)


def _still_running(timeout):
    raise subprocess.TimeoutExpired("docker run", timeout)


def _spec():
    return ContainerSpec(image="img", command=["echo", "hi"])


def _limits(**over):
    return {**load_config().limits_for("python"), **over}


def test_argv_has_a_reaper_label_and_no_rm():
    # No --rm: the exited container must survive long enough to be asked whether
    # it was OOM-killed. run_container removes it itself on every path instead.
    argv = _argv()
    assert "--rm" not in argv
    assert f"{docker_exec.LABEL}=1" in argv


def test_timeout_kills_and_removes_container(monkeypatch, tmp_path):
    calls = []
    _patch_docker(monkeypatch, calls, _still_running)
    _fast_clock(monkeypatch)
    raw = run_container(_spec(), _limits(timeout_s=5), tmp_path / "run.log")
    assert raw.timed_out and raw.exit_code is None
    assert any(c[:2] == ["docker", "kill"] for c in calls)
    assert any(c[:3] == ["docker", "rm", "-f"] for c in calls)
    assert "hard timeout after 5s" in (tmp_path / "run.log").read_text(encoding="utf-8")


def test_requested_timeout_is_clamped_to_the_configured_ceiling():
    """Runner proposes, policy disposes — the same rule every other field obeys.
    Applies to a runner's ContainerSpec and to `reprobe run --timeout` alike."""
    limits = {"timeout_s": 60, "max_timeout_s": 900}
    assert docker_exec.effective_timeout(ContainerSpec(image="img", command=["e"], timeout_s=99_999),
                                         limits) == 900
    assert docker_exec.effective_timeout(ContainerSpec(image="img", command=["e"], timeout_s=300),
                                         limits) == 300


def test_timeout_ceiling_defaults_to_the_plain_limit_when_unset():
    spec = ContainerSpec(image="img", command=["e"], timeout_s=99_999)
    assert docker_exec.effective_timeout(spec, {"timeout_s": 120}) == 120


def test_any_exception_kills_and_removes_container(monkeypatch, tmp_path):
    calls = []

    def boom(timeout):
        raise KeyboardInterrupt
    _patch_docker(monkeypatch, calls, boom)
    with pytest.raises(KeyboardInterrupt):
        run_container(_spec(), _limits(), tmp_path / "run.log")
    assert any(c[:2] == ["docker", "kill"] for c in calls)
    assert any(c[:3] == ["docker", "rm", "-f"] for c in calls)


def test_normal_exit_inspects_then_removes_without_kill(monkeypatch, tmp_path):
    calls = []
    _patch_docker(monkeypatch, calls, lambda timeout: 0)
    raw = run_container(_spec(), _limits(), tmp_path / "run.log")
    assert raw.exit_code == 0 and raw.oom_killed is False
    assert not any(c[:2] == ["docker", "kill"] for c in calls)
    inspect = next(i for i, c in enumerate(calls) if c[:2] == ["docker", "inspect"])
    remove = next(i for i, c in enumerate(calls) if c[:3] == ["docker", "rm", "-f"])
    assert inspect < remove


def test_oom_kill_is_read_from_the_exited_container(monkeypatch, tmp_path):
    # 137 alone is "SIGKILL from somewhere"; docker's own OOMKilled flag says the
    # memory cap did it, which makes it a budget finding and not a crash.
    _patch_docker(monkeypatch, [], lambda timeout: 137, inspect_out="true")
    raw = run_container(_spec(), _limits(), tmp_path / "run.log")
    assert raw.exit_code == 137 and raw.oom_killed is True


def test_runaway_console_output_is_stopped(monkeypatch, tmp_path):
    calls = []
    _patch_docker(monkeypatch, calls, _still_running, stdout="x" * 5000)
    raw = run_container(_spec(), _limits(log_max="1k"), tmp_path / "run.log")
    assert raw.limit_exceeded == "output" and not raw.timed_out
    assert any(c[:2] == ["docker", "kill"] for c in calls)


def test_runaway_disk_use_under_work_is_stopped(monkeypatch, tmp_path):
    work = tmp_path / "run"
    work.mkdir()
    (work / "big.bin").write_bytes(b"0" * 4096)
    monkeypatch.setattr(docker_exec, "_DISK_EVERY_S", 0.0)
    _patch_docker(monkeypatch, [], _still_running)
    spec = ContainerSpec(image="img", command=["e"],
                         mounts=[Mount(source=str(work), target="/work", read_only=False)])
    raw = run_container(spec, _limits(work_max="2k"), tmp_path / "run.log", work_root=work)
    assert raw.limit_exceeded == "disk"


def test_run_container_surfaces_sandbox_violation_as_error(monkeypatch, tmp_path):
    calls = []
    _patch_docker(monkeypatch, calls, lambda timeout: 0)
    spec = ContainerSpec(image="img", command=["echo"],
                         mounts=[Mount(source="/var/run/docker.sock",
                                       target="/var/run/docker.sock", read_only=False)])
    raw = run_container(spec, _limits(), tmp_path / "run.log")
    assert raw.error and raw.error.startswith("sandbox-violation:")
    assert raw.exit_code is None
    assert not any(c[:2] == ["docker", "run"] for c in calls)  # never launched


def test_dry_run_is_validated_too(tmp_path):
    # dry_run returns the argv before any daemon check — validation must
    # already have happened by then.
    spec = ContainerSpec(image="--privileged", command=["echo"])
    raw = run_container(spec, _limits(), tmp_path / "run.log", dry_run=True)
    assert raw.error and "sandbox-violation" in raw.error


def test_dry_run_reports_no_exit_code(tmp_path):
    # A dry run's exit code used to be 0, which every consumer read as "ran clean".
    raw = run_container(_spec(), _limits(), tmp_path / "run.log", dry_run=True)
    assert raw.dry_run and raw.exit_code is None and raw.argv_redacted


def test_extra_roots_admit_only_the_named_cache(tmp_path):
    run, cache, other = tmp_path / "run", tmp_path / "cache", tmp_path / "other"
    spec = ContainerSpec(image="img", command=["e"],
                         mounts=[Mount(source=str(run), target="/work", read_only=False),
                                 Mount(source=str(cache), target="/reprobe-cache", read_only=False)])
    build_argv(spec, _limits(), container_name="t", allow_egress=True, work_root=run,
               extra_roots=[cache])
    with pytest.raises(SandboxViolation, match="escapes the per-run work root"):
        build_argv(spec.model_copy(update={"mounts": [Mount(source=str(other), target="/x")]}),
                   _limits(), container_name="t", allow_egress=True, work_root=run,
                   extra_roots=[cache])


# --- host_user: a Linux bind mount must be writable by the container user ----

def _posix_host(monkeypatch, uid, gid=1000, rootless=False):
    monkeypatch.setattr(docker_exec.os, "getuid", lambda: uid, raising=False)
    monkeypatch.setattr(docker_exec.os, "getgid", lambda: gid, raising=False)
    monkeypatch.setattr(docker_exec, "_rootless_daemon", lambda: rootless)


def test_host_user_maps_a_non_root_linux_user(monkeypatch):
    _posix_host(monkeypatch, 1001, 118)
    assert docker_exec.host_user({"map_host_user": True}) == "1001:118"


def test_host_user_never_maps_to_root(monkeypatch):
    _posix_host(monkeypatch, 0, 0)
    assert docker_exec.host_user({"map_host_user": True}) is None


def test_host_user_respects_opt_out_and_rootless(monkeypatch):
    _posix_host(monkeypatch, 1001)
    assert docker_exec.host_user({"map_host_user": False}) is None
    _posix_host(monkeypatch, 1001, rootless=True)
    assert docker_exec.host_user({"map_host_user": True}) is None


def test_mapped_user_replaces_the_configured_one():
    spec = ContainerSpec(image="img", command=["e"])
    argv = build_argv(spec, _limits(), container_name="t", allow_egress=False, user="1001:118")
    assert argv[argv.index("--user") + 1] == "1001:118"


# --- reap_orphans: leftovers of a hard-killed orchestrator ------------------

def test_reaper_removes_only_what_has_outlived_its_own_deadline(monkeypatch):
    """Whatever state a container is in, it is removed only once ITS deadline has
    passed: a concurrent reprobe's live container, or one that just exited and is
    about to be inspected for an OOM kill, must never be touched."""
    import time as _time
    now = int(_time.time())
    rows = "\n".join([
        f"reprobe-exited-overdue\texited\t{now - 5}",
        f"reprobe-exited-fresh\texited\t{now + 999}",      # a concurrent run, just finished
        f"reprobe-created-fresh\tcreated\t{now + 999}",
        f"reprobe-overdue\trunning\t{now - 10}",
        f"reprobe-live\trunning\t{now + 999}",
        "reprobe-no-label\texited\t",
        "someone-else\texited\t" + str(now - 99),            # not ours, whatever its labels
    ])
    removed = []

    def fake_run(argv, **kw):
        if argv[:2] == ["docker", "ps"]:
            return SimpleNamespace(returncode=0, stdout=rows, stderr="")
        if argv[:3] == ["docker", "rm", "-f"]:
            removed.append(argv[3])
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(docker_exec.subprocess, "run", fake_run)
    assert docker_exec.reap_orphans() == ["reprobe-exited-overdue", "reprobe-overdue"]
    assert removed == ["reprobe-exited-overdue", "reprobe-overdue"]


def test_host_user_never_maps_to_roots_group_either(monkeypatch):
    _posix_host(monkeypatch, 1001, 0)
    assert docker_exec.host_user({"map_host_user": True}) is None


def test_a_tree_of_millions_of_empty_files_cannot_hold_the_watchdog(tmp_path, monkeypatch):
    monkeypatch.setattr(docker_exec, "_MAX_TREE_ENTRIES", 50)
    for i in range(80):
        (tmp_path / f"f{i}").write_bytes(b"")
    assert docker_exec._tree_exceeds(tmp_path, 10**12) is True


def test_mapped_host_user_drops_the_host_wide_nproc_limit():
    # RLIMIT_NPROC counts the uid's processes across the whole host; mapped to
    # the chair's uid it would count their desktop session against the step.
    spec = ContainerSpec(image="img", command=["e"])
    mapped = build_argv(spec, _limits(), container_name="t", allow_egress=False, user="1001:118")
    assert not any(a.startswith("nproc=") for a in mapped)
    assert "--pids-limit" in mapped                     # the per-container cap stays
    assert "nproc=512" in build_argv(spec, _limits(), container_name="t", allow_egress=False)


def test_cpus_are_clamped_to_what_the_daemon_has():
    spec = ContainerSpec(image="img", command=["e"])
    argv = build_argv(spec, _limits(), container_name="t", allow_egress=False, max_cpus=2)
    assert argv[argv.index("--cpus") + 1] == "2"


def test_redaction_touches_env_values_only():
    spec = ContainerSpec(image="img", command=["e"], env={"MODE": "none", "TOKEN": "s3cret"})
    argv = build_argv(spec, _limits(), container_name="t", allow_egress=False)
    red = docker_exec._redact(argv, spec)
    assert red[red.index("--network") + 1] == "none"          # untouched
    assert "TOKEN=***" in red and "MODE=***" in red
    assert not any("s3cret" in a for a in red)
