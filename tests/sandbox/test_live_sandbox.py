"""The sandbox envelope, checked from inside real containers.

tests/unit/test_sandbox.py proves build_argv REQUESTS the right flags. It cannot
prove the daemon and kernel honour them. Each test here launches a container
through docker_exec.run_container (the chokepoint the orchestrator uses) and has
a probe script report what author code can actually see and do.

Probes are plain Python files written into the mounted run dir and run as
``python /work/probe.py``. They record outcomes rather than raising, write them
to /work/result.json, and the test asserts one property at a time so a red
check names the property that broke.

Marked ``docker``: deselected from the default suite, run by the CI ``sandbox``
job (``pytest -m docker tests/sandbox``). The skip/fail gate and the image pull
are in conftest.py.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from reprobe import docker_exec
from reprobe.config import load_config
from reprobe.docker_exec import run_container
from reprobe.models import ContainerSpec, Mount, RawRunOutput, RunStep
from reprobe.runners.base import RunContext, snapshot
from reprobe.runners.python_script import PythonScriptRunner

pytestmark = pytest.mark.docker

# Every probe finishes in seconds. A probe still running after this is hung, and
# should fail now rather than at the 1800 s production default.
PROBE_TIMEOUT_S = 60


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
# Shared by every probe: record outcomes, never raise, report as JSON.
_PRELUDE = r'''
import errno, json, os

def attempt(fn):
    """Run fn and record ok/value, or the errno name it failed with. Never raises."""
    try:
        return {"ok": True, "value": fn()}
    except OSError as e:
        code = errno.errorcode.get(e.errno) if isinstance(e.errno, int) else None
        return {"ok": False, "errno": code or str(e.errno), "error": str(e)}
    except Exception as e:
        return {"ok": False, "errno": None, "error": f"{type(e).__name__}: {e}"}

def write(path, mode="w"):
    with open(path, mode) as fh:
        fh.write("written by the probe\n")

def read(path):
    with open(path) as fh:
        return fh.read()

def report(result):
    with open("/work/result.json", "w") as fh:
        json.dump(result, fh, indent=1)
    print(json.dumps(result), flush=True)
'''


def _limits(**overrides) -> dict:
    """The real python-runner envelope from config/limits.yaml, plus overrides."""
    lim = load_config().limits_for("python")
    lim.update(overrides)
    return lim


def _open_dir(path: Path) -> Path:
    """mkdir, then chmod 0777 so the container can write here whatever uid it runs as.

    Only for tests that are NOT about bind-mount ownership. They report through
    /work/result.json and should not go red over the ownership problem, which
    test_orchestrator_rundir_is_writable_and_removable covers by itself. Never
    use this for that test."""
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o777)
    return path


def _probe_spec(image: str, rundir: Path, body: str, *, extra_mounts=(),
                command: list[str] | None = None, timeout_s: int = PROBE_TIMEOUT_S) -> ContainerSpec:
    (rundir / "probe.py").write_text(_PRELUDE + textwrap.dedent(body), encoding="utf-8")
    return ContainerSpec(
        image=image,
        command=command or ["python", "/work/probe.py"],
        workdir="/work",
        mounts=[Mount(source=rundir.as_posix(), target="/work", read_only=False), *extra_mounts],
        timeout_s=timeout_s,
    )


def _log(raw: RawRunOutput, n: int = 4000) -> str:
    try:
        return Path(raw.log_path or "").read_text(encoding="utf-8", errors="replace")[-n:]
    except OSError:
        return "<no log>"


def _flag_value(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None


def _container_name(raw: RawRunOutput) -> str:
    name = _flag_value(raw.argv_redacted, "--name")
    assert name, f"no --name in the docker argv: {raw.argv_redacted}"
    return name


def _containers(name_filter: str) -> set[str]:
    """Names of all containers, running or not, whose name matches the filter."""
    r = subprocess.run(["docker", "ps", "-a", "--filter", f"name={name_filter}", "--format", "{{.Names}}"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"`docker ps -a` failed: {r.stderr}"
    return set(r.stdout.split())


def _assert_no_container(name: str, within_s: float = 10.0) -> None:
    deadline = time.monotonic() + within_s
    while name in _containers(name):
        if time.monotonic() > deadline:
            pytest.fail(f"container {name} still exists {within_s:.0f}s after run_container returned: "
                        "it was orphaned. `docker ps -a` lists it; remove with `docker rm -f`.")
        time.sleep(0.5)


def _run(spec: ContainerSpec, limits: dict, log_dir: Path, *, work_root: Path) -> RawRunOutput:
    raw = run_container(spec, limits, log_dir / "probe.log", work_root=work_root)
    assert raw.error is None, f"harness error before/around the run: {raw.error}\n{_log(raw)}"
    # Every path through run_container must leave no container behind.
    _assert_no_container(_container_name(raw))
    return raw


def _result(rundir: Path, raw: RawRunOutput) -> dict:
    p = rundir / "result.json"
    assert p.is_file(), (f"the probe wrote no result (exit {raw.exit_code}, timed_out={raw.timed_out})"
                         f"\n--- log tail ---\n{_log(raw)}")
    return json.loads(p.read_text(encoding="utf-8"))


def _bytes(v: object) -> int:
    m = re.fullmatch(r"(\d+)([kmg]?)b?", str(v).strip().lower())
    assert m, f"cannot parse a byte size from {v!r}"
    return int(m[1]) * 1024 ** " kmg".index(m[2] or " ")


# --------------------------------------------------------------------------- #
# 1. The envelope: ONE container, many properties, one test each
# --------------------------------------------------------------------------- #
ENVELOPE_PROBE = r'''
import resource, socket, subprocess, threading

r = {"uid": list(os.getresuid()), "gid": list(os.getresgid()), "groups": os.getgroups()}

status = {}
for line in read("/proc/self/status").splitlines():
    key, _, value = line.partition(":")
    status[key.strip()] = value.strip()
r["status"] = status

r["tcp"] = attempt(lambda: socket.create_connection(("1.1.1.1", 53), timeout=5).close())

# getaddrinfo cannot be given a timeout; a resolver that hangs counts as "no answer".
box = {}
t = threading.Thread(daemon=True, target=lambda: box.update(
    dns=attempt(lambda: sorted({a[4][0] for a in socket.getaddrinfo("example.com", 443)}))))
t.start()
t.join(15)
r["dns"] = box.get("dns", {"ok": False, "errno": None, "error": "no answer within 15s"})

r["interfaces"] = {}
for _, name in socket.if_nameindex():
    flags = attempt(lambda: int(read(f"/sys/class/net/{name}/flags"), 16))
    r["interfaces"][name] = {"up": bool(flags["value"] & 1) if flags["ok"] else None}

r["write"] = {p: attempt(lambda: write(p))
              for p in ("/reprobe-probe", "/usr/reprobe-probe", "/var/tmp/reprobe-probe")}

def tmp_script():
    with open("/tmp/probe.sh", "w") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod("/tmp/probe.sh", 0o755)

r["tmp_write"] = attempt(tmp_script)
r["tmp_exec"] = attempt(lambda: subprocess.run(["/tmp/probe.sh"], timeout=10).returncode)
r["mounts"] = [line.split()[:4] for line in read("/proc/self/mounts").splitlines()]
r["docker_sock"] = {p: os.path.lexists(p) for p in ("/var/run/docker.sock", "/run/docker.sock")}
r["rlimit_nofile"] = list(resource.getrlimit(resource.RLIMIT_NOFILE))
r["cgroup"] = ({f: attempt(lambda: read("/sys/fs/cgroup/" + f).strip())
                for f in ("pids.max", "memory.max", "memory.swap.max", "cpu.max")}
               if os.path.exists("/sys/fs/cgroup/cgroup.controllers") else None)
report(r)
'''


@pytest.fixture(scope="module")
def envelope(sandbox_image, tmp_path_factory):
    root = tmp_path_factory.mktemp("envelope")
    rundir = _open_dir(root / "run")
    limits = _limits()
    raw = _run(_probe_spec(sandbox_image, rundir, ENVELOPE_PROBE), limits, root, work_root=rundir)
    assert raw.exit_code == 0, f"envelope probe exited {raw.exit_code}\n{_log(raw)}"
    return SimpleNamespace(r=_result(rundir, raw), raw=raw, limits=limits)


def test_runs_as_the_non_root_user_docker_exec_chose(envelope):
    user = _flag_value(envelope.raw.argv_redacted, "--user")
    assert user, ("docker_exec passed no --user, so the container ran as the image's default user "
                  "(root for python:*)")
    uid_s, _, gid_s = user.partition(":")
    assert uid_s.isdigit(), f"--user {user!r} is not a numeric uid; a name can resolve to anything per image"
    uid = int(uid_s)
    assert envelope.r["uid"] == [uid] * 3, (
        f"real/effective/saved uid inside the container are {envelope.r['uid']}, but docker_exec "
        f"asked for --user {user}")
    if gid_s:
        assert envelope.r["gid"] == [int(gid_s)] * 3, (
            f"real/effective/saved gid inside the container are {envelope.r['gid']}, not {gid_s}")
    assert uid != 0, f"the container runs as root (--user {user})"
    assert 0 not in envelope.r["gid"], f"the container runs with gid 0 (--user {user})"
    assert 0 not in envelope.r["groups"], f"supplementary groups include root: {envelope.r['groups']}"


# CapEff/CapPrm are 0 for ANY non-root uid, so on their own they prove little.
# CapBnd (the bounding set) is what shows --cap-drop ALL took effect: with it
# non-empty, a setuid binary could still regain those capabilities.
@pytest.mark.parametrize("field", ["CapEff", "CapPrm", "CapBnd", "CapAmb", "CapInh"])
def test_no_capabilities(envelope, field):
    value = envelope.r["status"].get(field)
    assert value is not None, f"/proc/self/status has no {field} line"
    assert int(value, 16) == 0, f"{field} = {value}; --cap-drop ALL should leave every capability set empty"


def test_no_new_privileges(envelope):
    assert envelope.r["status"].get("NoNewPrivs") == "1", (
        f"NoNewPrivs = {envelope.r['status'].get('NoNewPrivs')!r}; "
        "`--security-opt no-new-privileges` did not apply, so setuid binaries can still escalate")


def test_seccomp_filter_active(envelope):
    assert envelope.r["status"].get("Seccomp") == "2", (
        f"Seccomp = {envelope.r['status'].get('Seccomp')!r} (2 = filter mode); "
        "the container runs without Docker's seccomp profile")


def test_no_tcp_egress(envelope):
    assert not envelope.r["tcp"]["ok"], "a TCP connect to 1.1.1.1:53 succeeded: --network none is not in effect"


def test_no_dns(envelope):
    assert not envelope.r["dns"]["ok"], (
        f"example.com resolved to {envelope.r['dns'].get('value')}: the container can reach a resolver")


# A kernel with ip_tunnel/sit/gre modules loaded creates these fallback devices
# in EVERY network namespace. They are down and carry no traffic, so they are
# allowed only while down.
_FALLBACK_TUNNELS = frozenset({"tunl0", "sit0", "ip6tnl0", "ip6gre0", "gre0", "gretap0", "erspan0",
                               "ip_vti0", "ip6_vti0", "ip6erspan0"})


def test_only_loopback_interface(envelope):
    ifs = envelope.r["interfaces"]
    assert "lo" in ifs, f"no loopback interface at all: {ifs}"
    others = {n: v for n, v in ifs.items() if n != "lo"}
    unexpected = sorted(set(others) - _FALLBACK_TUNNELS)
    assert not unexpected, f"--network none should leave only lo, but the container has {unexpected}: {ifs}"
    up = sorted(n for n, v in others.items() if v["up"])
    assert not up, f"non-loopback interfaces are UP: {up}"


# A non-root user cannot write to / or /usr even on a WRITABLE rootfs, so for
# those a failed write alone proves nothing. EROFS (rather than EACCES) is what
# shows the mount is read-only. /var/tmp is world-writable (1777) in Debian
# images, so there --read-only is the only thing that can stop the write.
@pytest.mark.parametrize("path", ["/reprobe-probe", "/usr/reprobe-probe", "/var/tmp/reprobe-probe"])
def test_rootfs_is_read_only(envelope, path):
    got = envelope.r["write"][path]
    assert not got["ok"], f"wrote {path}: the root filesystem is writable (--read-only not in effect)"
    assert got["errno"] == "EROFS", (
        f"writing {path} failed with {got['errno']} ({got['error']}), not EROFS: the write was refused "
        "by file permissions, not by a read-only rootfs")


def test_tmp_is_writable_but_not_executable(envelope):
    tmp_mount = [m for m in envelope.r["mounts"] if len(m) > 1 and m[1] == "/tmp"]
    assert envelope.r["tmp_write"]["ok"], (
        f"/tmp is not writable ({envelope.r['tmp_write']['error']}); author code needs scratch space. "
        f"/tmp mount: {tmp_mount}")
    got = envelope.r["tmp_exec"]
    assert not got["ok"], f"executed a script from /tmp (exit {got.get('value')}): /tmp is not noexec. {tmp_mount}"
    assert got["errno"] == "EACCES", (
        f"running /tmp/probe.sh failed with {got['errno']} ({got['error']}), not the EACCES noexec gives. "
        f"/tmp mount: {tmp_mount}")


def test_no_docker_socket(envelope):
    present = [p for p, exists in envelope.r["docker_sock"].items() if exists]
    assert not present, f"the Docker control socket is visible inside the container at {present}"


def test_file_descriptor_cap(envelope):
    nofile = int(envelope.limits["nofile"])
    assert envelope.r["rlimit_nofile"] == [nofile, nofile], (
        f"RLIMIT_NOFILE (soft, hard) is {envelope.r['rlimit_nofile']}, not the configured {nofile}")


@pytest.mark.parametrize("cg_file", ["pids.max", "memory.max", "memory.swap.max", "cpu.max"])
def test_cgroup_limit_matches_config(envelope, cg_file):
    cg = envelope.r["cgroup"]
    if cg is None:
        pytest.skip("cgroup v1 host: no unified /sys/fs/cgroup to read (the memory/pids tests still "
                    "check enforcement)")
    got = cg[cg_file]
    if cg_file == "memory.swap.max" and not got["ok"]:
        pytest.skip("this kernel has no swap accounting (memory.swap.max absent); the memory-cap "
                    "test still checks that the cap is enforced")
    assert got["ok"], f"cannot read /sys/fs/cgroup/{cg_file}: {got['error']}"
    lim = envelope.limits
    if cg_file == "cpu.max":
        quota, period = got["value"].split()
        assert quota != "max", "cpu.max has no quota: --cpus not applied"
        # run_container clamps --cpus to what the daemon has (a 2-vCPU runner cannot
        # be given 4), so the expectation is the clamped value, not the config's
        want = float(lim["cpus"])
        daemon = docker_exec._daemon_cpus()
        if daemon:
            want = min(want, daemon)
        assert int(quota) / int(period) == pytest.approx(want), (
            f"cpu.max = {got['value']!r}, expected {want:g} CPUs")
        return
    expected = {
        "pids.max": str(lim["pids"]),
        "memory.max": str(_bytes(lim["memory"])),
        # --memory-swap == --memory means "no swap on top": swap.max is 0.
        "memory.swap.max": "0",
    }[cg_file]
    assert got["value"] == expected, f"{cg_file} = {got['value']!r}, expected {expected!r}"


# --------------------------------------------------------------------------- #
# 2. The run dir exactly as the orchestrator makes it
# --------------------------------------------------------------------------- #
ROUNDTRIP_PROBE = r'''
import pathlib
w = pathlib.Path("/work")
(w / "outputs").mkdir()
(w / "outputs" / "figure.txt").write_text("made in the container\n")
(w / "committed.txt").write_text("overwritten by the run\n")
(w / "whoami.txt").write_text(f"{os.getuid()}:{os.getgid()}\n")
'''


def test_orchestrator_rundir_is_writable_and_removable(sandbox_image, tmp_path):
    """Author code must be able to write its outputs into /work, and the host must
    be able to read them and wipe the run dir afterwards (fresh_dir rmtree's it
    before every run).

    The run dir is built as Orchestrator.run builds it: the host user creates the
    source tree with the default umask and copies it with shutil.copytree. No
    chmod. On native Linux that dir is host-owned 0755, which a container running
    as the image uid 57439 can read but not write. That is the failure this test
    exists to catch.

    pytest's tmp_path is 0700, but the container only sees the bind-mounted run
    dir itself, so the parent's mode has no effect inside it. The step spec
    comes from the real PythonScriptRunner, so a change to how runners mount
    /work is tested too."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "probe.py").write_text(_PRELUDE + textwrap.dedent(ROUNDTRIP_PROBE), encoding="utf-8")
    (src / "committed.txt").write_text("from the repository\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    rundir = work / "run"
    shutil.copytree(src, rundir)
    if os.name == "posix":
        mode = stat.S_IMODE(rundir.stat().st_mode)
        assert not mode & 0o002, (
            f"run dir is world-writable ({oct(mode)}). The orchestrator never makes it so, and it "
            "would hide the ownership problem this test is for.")

    cfg = load_config()
    runner = PythonScriptRunner()
    ctx = RunContext(step=RunStep(runner=runner.id, target="probe.py", kind="python"),
                     rundir=rundir, src_dir=src, out_dir=tmp_path / "out", image=sandbox_image,
                     config=cfg, limits={**cfg.limits_for(runner.id), "timeout_s": PROBE_TIMEOUT_S},
                     pre_index=snapshot(rundir))
    spec = runner.container_spec(ctx)
    raw = _run(spec, ctx.limits, tmp_path, work_root=rundir)

    assert raw.exit_code == 0, (
        f"author code could not write into /work (exit {raw.exit_code}, --user "
        f"{_flag_value(raw.argv_redacted, '--user')}). On native Linux this is the container uid "
        f"not matching the owner of the host run dir.\n--- log tail ---\n{_log(raw)}")
    assert (rundir / "outputs" / "figure.txt").read_text() == "made in the container\n"
    assert (rundir / "committed.txt").read_text() == "overwritten by the run\n"
    uid = int((rundir / "whoami.txt").read_text().split(":")[0])
    assert uid != 0, "the step ran as root"
    res = runner.interpret(raw, ctx)
    assert res.status == "pass", f"runner reported {res.status}: {res.diagnostics}"
    assert "outputs/figure.txt" in res.artifacts, f"produced files not seen by the host: {res.artifacts}"

    try:
        shutil.rmtree(rundir)
    except OSError as exc:
        pytest.fail(f"the host cannot delete what the container wrote ({exc}). fresh_dir would then "
                    "silently leave the old run dir behind and switch to a run-N sibling.")
    assert not rundir.exists()


# --------------------------------------------------------------------------- #
# 3-5. Resource caps: wall clock, memory, tasks
# --------------------------------------------------------------------------- #
def test_timeout_kills_and_leaves_no_container(sandbox_image, tmp_path):
    rundir = _open_dir(tmp_path / "run")
    spec = ContainerSpec(image=sandbox_image, command=["sleep", "60"], workdir="/work",
                         mounts=[Mount(source=rundir.as_posix(), target="/work", read_only=False)],
                         timeout_s=3)
    raw = run_container(spec, _limits(), tmp_path / "timeout.log", work_root=rundir)
    assert raw.error is None, f"{raw.error}\n{_log(raw)}"
    assert raw.timed_out is True, f"`sleep 60` under a 3 s timeout was not timed out (exit {raw.exit_code})\n{_log(raw)}"
    assert raw.exit_code is None, f"a timed-out run reported exit {raw.exit_code}"
    assert raw.duration_s >= 3, f"stopped after {raw.duration_s}s, before its 3 s budget ran out"
    assert raw.duration_s < 30, f"took {raw.duration_s}s to stop a container with a 3 s budget"
    _assert_no_container(_container_name(raw))


MEMORY_PROBE = r'''
import sys

def cg(name):
    try:
        return read("/sys/fs/cgroup/" + name).strip()
    except OSError as e:
        return f"unreadable ({e})"

print("memory.max", cg("memory.max"), "memory.swap.max", cg("memory.swap.max"), flush=True)
chunks = []
try:
    for i in range(32):
        # Non-zero fill so every page is really committed (zero pages may stay virtual).
        chunks.append(b"\x01" * (16 << 20))
        print(f"allocated {(i + 1) * 16} MiB", flush=True)
except MemoryError:
    print("MEMORY_ERROR_RAISED", flush=True)
    sys.exit(3)
print("ALLOCATED_ALL", flush=True)
'''


def test_memory_cap_is_an_oom_kill(sandbox_image, tmp_path):
    rundir = _open_dir(tmp_path / "run")
    raw = _run(_probe_spec(sandbox_image, rundir, MEMORY_PROBE), _limits(memory="128m"), tmp_path,
               work_root=rundir)
    log = _log(raw)
    assert "ALLOCATED_ALL" not in log, (
        f"allocated 512 MiB under a 128m cap: --memory/--memory-swap are not enforced "
        f"(swap accounting off?)\n{log}")
    assert not raw.timed_out, f"timed out instead of being killed at the memory cap\n{log}"
    assert raw.exit_code == 137, (
        f"expected 137 (SIGKILL from the OOM killer), got {raw.exit_code}. 3 means Python raised "
        f"MemoryError, so the allocation was refused rather than killed.\n{log}")
    assert raw.oom_killed is True, (
        f"exit 137 but oom_killed={raw.oom_killed!r}: docker_exec could not tell an OOM kill from any "
        "other SIGKILL")


THREADS_PROBE = r'''
import threading
gate = threading.Event()
started, errors = [], []
for _ in range(__N__):
    t = threading.Thread(target=gate.wait)
    try:
        t.start()
        started.append(t)
    except (RuntimeError, OSError) as e:
        errors.append(f"{type(e).__name__}: {e}")
gate.set()
for t in started:
    t.join()
try:
    pids_max = read("/sys/fs/cgroup/pids.max").strip()
except OSError:
    pids_max = None
report({"started": len(started), "failed": len(errors), "errors": errors[:3], "pids_max": pids_max})
'''


def test_pids_cap_stops_a_thread_storm(sandbox_image, tmp_path):
    rundir = _open_dir(tmp_path / "run")
    pids = 32
    # nproc off: RLIMIT_NPROC would also stop the storm and hide whether the
    # pids cgroup works. This test is about --pids-limit alone.
    limits = _limits(pids=pids, nproc=None)
    raw = _run(_probe_spec(sandbox_image, rundir, THREADS_PROBE.replace("__N__", "100")), limits, tmp_path,
               work_root=rundir)
    assert raw.exit_code == 0, f"the probe did not exit normally under the pids cap\n{_log(raw)}"
    r = _result(rundir, raw)
    assert r["started"] + r["failed"] == 100
    assert r["started"] >= 1, f"no thread could start at all: {r['errors']}"
    # The main thread is one of the cgroup's tasks, so at most pids - 1 more.
    assert r["started"] < pids, (
        f"{r['started']} of 100 threads started under --pids-limit {pids} (pids.max={r['pids_max']})")


def test_host_users_own_tasks_do_not_starve_the_container(sandbox_image, tmp_path):
    """The container's task budget must not depend on how busy the HOST user is.

    RLIMIT_NPROC (`--ulimit nproc`) is counted per uid across the whole host, not
    per container. Once docker_exec runs the container as the host user (to make
    /work writable), every process and thread that user already has counts against
    the container's nproc. A desktop session has thousands of threads, so every
    step would fail to fork or even exec. This test recreates a busy host user by
    holding nproc + 64 idle threads in this process, then checks the container can
    still start a handful. The per-container cap is --pids-limit, which counts
    only the container's own tasks."""
    rundir = _open_dir(tmp_path / "run")
    limits = _limits()
    n_host = max(int(limits.get("nproc") or 0), 512) + 64
    gate = threading.Event()
    held: list[threading.Thread] = []
    old_stack = threading.stack_size(256 * 1024)        # keep 576 idle threads cheap
    try:
        for _ in range(n_host):
            t = threading.Thread(target=gate.wait, daemon=True)
            t.start()
            held.append(t)
        raw = _run(_probe_spec(sandbox_image, rundir, THREADS_PROBE.replace("__N__", "16")), limits,
                   tmp_path, work_root=rundir)
    finally:
        gate.set()
        for t in held:
            t.join()
        threading.stack_size(old_stack)
    hint = (f"with {n_host} threads held by this host user, the container (--user "
            f"{_flag_value(raw.argv_redacted, '--user')}, --ulimit nproc={limits.get('nproc')}) ")
    assert raw.exit_code == 0, (
        hint + f"could not run (exit {raw.exit_code}). RLIMIT_NPROC counts the host uid's tasks "
        f"host-wide, so a busy host user starves the container.\n{_log(raw)}")
    r = _result(rundir, raw)
    assert r["started"] == 16, hint + f"started only {r['started']} of 16 threads: {r['errors']}"


# --------------------------------------------------------------------------- #
# 6-7. Mount policy
# --------------------------------------------------------------------------- #
READONLY_PROBE = r'''
report({
    "read": attempt(lambda: read("/data/input.txt")),
    "create": attempt(lambda: write("/data/new.txt")),
    "append": attempt(lambda: write("/data/input.txt", "a")),
})
'''


def test_read_only_mount_is_honoured(sandbox_image, tmp_path):
    root = _open_dir(tmp_path / "root")
    rundir = _open_dir(root / "run")
    # World-writable on the host, so the :ro flag is the ONLY thing that can
    # refuse the write. EROFS confirms it was the flag and not file permissions.
    data = _open_dir(root / "data")
    original = "from the host\n"
    (data / "input.txt").write_text(original, encoding="utf-8")
    (data / "input.txt").chmod(0o666)
    spec = _probe_spec(sandbox_image, rundir, READONLY_PROBE,
                       extra_mounts=[Mount(source=data.as_posix(), target="/data", read_only=True)])
    raw = _run(spec, _limits(), tmp_path, work_root=root)
    assert raw.exit_code == 0, _log(raw)
    r = _result(rundir, raw)
    assert r["read"]["ok"] and r["read"]["value"] == original, (
        f"the read-only mount is not there to read: {r['read']}")
    for op in ("create", "append"):
        assert not r[op]["ok"], f"{op} on the :ro mount at /data succeeded"
        assert r[op]["errno"] == "EROFS", (
            f"{op} on /data failed with {r[op]['errno']} ({r[op]['error']}), not EROFS")
    assert not (data / "new.txt").exists()
    assert (data / "input.txt").read_text(encoding="utf-8") == original


VIOLATION_PROBE = r'''
write("/work/LAUNCHED")
'''


@pytest.mark.parametrize("hostile", ["outside-work-root", "docker-socket"])
def test_sandbox_violation_is_refused_before_launch(sandbox_image, tmp_path, hostile):
    root = _open_dir(tmp_path / "root")
    outside = _open_dir(tmp_path / "outside")
    mount = {
        "outside-work-root": Mount(source=outside.as_posix(), target="/outside", read_only=False),
        "docker-socket": Mount(source="/var/run/docker.sock", target="/var/run/docker.sock",
                               read_only=False),
    }[hostile]
    spec = _probe_spec(sandbox_image, root, VIOLATION_PROBE, extra_mounts=[mount])
    before = _containers("reprobe-")
    raw = run_container(spec, _limits(), tmp_path / "violation.log", work_root=root)
    new = _containers("reprobe-") - before
    assert raw.error and raw.error.startswith("sandbox-violation"), (
        f"a {hostile} mount was not refused: error={raw.error!r}, exit={raw.exit_code}\n{_log(raw)}")
    assert raw.exit_code is None and raw.duration_s == 0.0, "the refused spec still reports a run"
    assert raw.argv_redacted == [], "an argv was built for a refused spec"
    assert not (root / "LAUNCHED").exists(), "the probe ran: a container was launched despite the violation"
    assert not new, f"containers appeared during a refused run: {sorted(new)}"
