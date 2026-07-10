"""The sandbox envelope is the security surface — assert it without a daemon."""

from reprobe.config import load_config
from reprobe.docker_exec import build_argv
from reprobe.models import ContainerSpec, Mount


def _argv(network="none", allow_egress=False):
    cfg = load_config()
    spec = ContainerSpec(image="img", command=["echo", "hi"],
                         mounts=[Mount(source="/h/run", target="/work", read_only=False)],
                         network=network)
    return build_argv(spec, cfg.limits_for("python"), container_name="t", allow_egress=allow_egress)


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


def test_no_privileged_or_socket():
    a = " ".join(_argv())
    assert "--privileged" not in a
    assert "docker.sock" not in a
