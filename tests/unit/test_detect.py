from pathlib import Path

from reprobe.config import Config
from reprobe.detect import detect, signatures
from reprobe.envbuild import plan as plan_env
from reprobe.models import DetectResult

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "example-python"
FIXTURES = REPO / "tests" / "fixtures"


def _cfg() -> Config:
    return Config(config_dir=Path("."), pins={"base_images": {"python": "py-img", "r": "r-img"}})


def test_manifest_drives_detection():
    res, meta = detect(EXAMPLE, use_llm=False)
    assert res.run_plan_source == "manifest"
    assert "python" in res.artifact_types
    assert [s.target for s in res.steps] == ["01_analyze.py"]
    assert "results/summary.csv" in res.steps[0].expected_outputs
    assert "functional" in meta["badges_claimed"]


def test_heuristic_orders_notebooks_numerically(tmp_path):
    (tmp_path / "02_second.ipynb").write_text("{}")
    (tmp_path / "01_first.ipynb").write_text("{}")
    (tmp_path / "README.md").write_text("run notebooks")
    res = signatures.scan(tmp_path)
    assert res.artifact_types == ["jupyter"]
    assert [s.target for s in res.steps] == ["01_first.ipynb", "02_second.ipynb"]


def test_unity_structural_detection(tmp_path):
    (tmp_path / "Assets").mkdir()
    ps = tmp_path / "ProjectSettings"
    ps.mkdir()
    (ps / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.0.23f1\n")
    res = signatures.scan(tmp_path)
    assert "unity" in res.artifact_types


def test_repo2docker_flag_on_dockerfile(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM python:3.11\n")
    (tmp_path / "run.py").write_text("print(1)\n")
    res = signatures.scan(tmp_path)
    assert "needs-repo2docker" in res.flags


# --------------------------------------------------------------------------- #
# signatures: lowercase .r, entry-point tightening, notebook suppression
# --------------------------------------------------------------------------- #
def test_lowercase_r_scripts_detected():
    res = signatures.scan(FIXTURES / "lowercase-r")
    assert "r" in res.artifact_types
    assert [s.target for s in res.steps] == ["analysis.r"]


def test_entry_regex_word_boundary_and_depth(tmp_path):
    (tmp_path / "figures_config.py").write_text("X = 1\n")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "run_analysis.py").write_text("print(1)\n")
    nested = tmp_path / "src" / "utils"
    nested.mkdir(parents=True)
    (nested / "run_helpers.py").write_text("def f(): pass\n")
    models = tmp_path / "models"
    models.mkdir()
    (models / "train_utils.py").write_text("def f(): pass\n")
    res = signatures.scan(tmp_path)
    assert [s.target for s in res.steps] == ["scripts/run_analysis.py"]


def test_notebook_r_mix_keeps_r_visible():
    res = signatures.scan(FIXTURES / "notebook-r-mix")
    # the notebook drives the run, but R stays in artifact_types so renv restores
    assert [s.kind for s in res.steps] == ["jupyter"]
    assert "r" in res.artifact_types
    assert any("not scheduled" in n for n in res.notes)
    assert signatures.is_ambiguous(res)


def test_notebook_mix_still_schedules_entry_named_scripts(tmp_path):
    (tmp_path / "01_explore.ipynb").write_text("{}")
    (tmp_path / "reproduce.py").write_text("print(1)\n")
    res = signatures.scan(tmp_path)
    assert {(s.kind, s.target) for s in res.steps} == {("jupyter", "01_explore.ipynb"),
                                                       ("python", "reproduce.py")}


def test_conda_environment_yml_flags_repo2docker():
    res = signatures.scan(FIXTURES / "conda-env")
    assert "needs-repo2docker" in res.flags


# --------------------------------------------------------------------------- #
# manifest: validation, fallback, kind clamping
# --------------------------------------------------------------------------- #
def test_unknown_tool_clamped_to_custom():
    res, _meta = detect(FIXTURES / "bad-manifest", use_llm=False)
    assert res.run_plan_source == "manifest"
    assert res.steps[0].runner == "godot"
    assert res.steps[0].kind == "custom"


def test_malformed_yaml_manifest_falls_back_to_heuristic(tmp_path):
    (tmp_path / "autoui-repro.yml").write_text("version: [1\n")   # unclosed flow sequence
    (tmp_path / "run.py").write_text("print(1)\n")
    res, _meta = detect(tmp_path, use_llm=False)
    assert res.run_plan_source == "heuristic"
    assert any("manifest present but invalid" in n for n in res.notes)
    assert [s.target for s in res.steps] == ["run.py"]


def test_non_mapping_manifest_falls_back(tmp_path):
    (tmp_path / "autoui-repro.yml").write_text("- just\n- a list\n")
    (tmp_path / "run.py").write_text("print(1)\n")
    res, _meta = detect(tmp_path, use_llm=False)
    assert res.run_plan_source == "heuristic"
    assert any("manifest present but invalid" in n for n in res.notes)


def test_wrong_version_manifest_falls_back(tmp_path):
    (tmp_path / "autoui-repro.yml").write_text("version: 2\nrun:\n  steps: [a.py]\n")
    (tmp_path / "a.py").write_text("print(1)\n")
    res, _meta = detect(tmp_path, use_llm=False)
    assert res.run_plan_source == "heuristic"
    assert any("manifest present but invalid" in n for n in res.notes)


def test_manifest_repo_keeps_heuristic_flags(tmp_path):
    (tmp_path / "autoui-repro.yml").write_text("version: 1\nrun:\n  steps: [analysis.py]\n")
    (tmp_path / "analysis.py").write_text("print(1)\n")
    (tmp_path / "Dockerfile").write_text("FROM python:3.11\n")
    res, _meta = detect(tmp_path, use_llm=False)
    assert res.run_plan_source == "manifest"
    assert "needs-repo2docker" in res.flags


# --------------------------------------------------------------------------- #
# envbuild: declared-but-not-installed warnings, builder, renv gating
# --------------------------------------------------------------------------- #
def test_conda_env_warns_not_installed():
    det = signatures.scan(FIXTURES / "conda-env")
    p = plan_env(det, {"environment": {}}, _cfg(), FIXTURES / "conda-env")
    assert not any("environment.yml" in c for c in p.install_commands)
    assert any("NOT installed" in w for w in p.warnings)


def test_declared_conda_warns_and_still_autodetects(tmp_path):
    (tmp_path / "environment.yml").write_text("dependencies: [pandas]\n")
    (tmp_path / "requirements.txt").write_text("pandas\n")
    det = DetectResult(artifact_types=["jupyter"])
    p = plan_env(det, {"environment": {"dependencies": "environment.yml"}}, _cfg(), tmp_path)
    assert any("requirements.txt" in c for c in p.install_commands)
    assert any("NOT installed" in w for w in p.warnings)


def test_declared_missing_dependency_file_warns(tmp_path):
    det = DetectResult(artifact_types=["python"])
    p = plan_env(det, {"environment": {"dependencies": "requirments.txt"}}, _cfg(), tmp_path)
    assert any("does not exist" in w for w in p.warnings)


def test_builder_repo2docker_request_warns_without_flag(tmp_path):
    det = DetectResult(artifact_types=["python"])
    p = plan_env(det, {"environment": {"builder": "repo2docker"}}, _cfg(), tmp_path)
    assert p.strategy == "besteffort"
    assert any("--allow-repo2docker" in w for w in p.warnings)


def test_notebook_r_mix_still_restores_renv():
    det = signatures.scan(FIXTURES / "notebook-r-mix")
    p = plan_env(det, {}, _cfg(), FIXTURES / "notebook-r-mix")
    assert any("renv::restore" in c for c in p.install_commands)


def test_renv_lock_skipped_without_r_steps_warns(tmp_path):
    (tmp_path / "renv.lock").write_text("{}\n")
    det = DetectResult(artifact_types=["jupyter"])
    p = plan_env(det, {}, _cfg(), tmp_path)
    assert not any("renv" in c for c in p.install_commands)
    assert any("NOT restored" in w for w in p.warnings)


def test_install_r_gated_on_r_steps(tmp_path):
    (tmp_path / "install.R").write_text("install.packages('lme4')\n")
    det = DetectResult(artifact_types=["jupyter"])
    p = plan_env(det, {}, _cfg(), tmp_path)
    assert not any(c.startswith("Rscript") for c in p.install_commands)
    assert any("NOT run" in w for w in p.warnings)


def test_resolved_versions_recorded_in_install_log(tmp_path):
    (tmp_path / "requirements.txt").write_text("pandas\n")
    det = DetectResult(artifact_types=["python"])
    p = plan_env(det, {}, _cfg(), tmp_path)
    assert any(c.startswith("pip freeze") for c in p.install_commands)


# --------------------------------------------------------------------------- #
# signatures: non-code artifact classification (video/audio/dataset/...)
# --------------------------------------------------------------------------- #
def test_noncode_only_deposit_classified(tmp_path):
    (tmp_path / "condition_a.mp4").write_bytes(b"\x00")
    (tmp_path / "interview.wav").write_bytes(b"\x00")
    (tmp_path / "responses.csv").write_text("a,b\n1,2\n")
    (tmp_path / "protocol.pdf").write_bytes(b"%PDF")
    (tmp_path / "mount.stl").write_bytes(b"\x00")
    res = signatures.scan(tmp_path)
    assert res.steps == []
    assert res.artifact_types == ["3d-model", "audio", "dataset", "document", "video"]
    assert res.inventory == {"video": 1, "audio": 1, "dataset": 1,
                             "document": 1, "3d-model": 1}
    assert any("non-code artifacts" in n for n in res.notes)


def test_repo_noise_is_not_classified(tmp_path):
    (tmp_path / "run.py").write_text("print(1)\n")
    (tmp_path / "README.md").write_text("docs, not a document artifact")
    (tmp_path / "config.json").write_text("{}")
    res = signatures.scan(tmp_path)
    assert res.inventory == {}
    assert res.artifact_types == ["python"]


def test_unity_assets_excluded_from_inventory(tmp_path):
    assets = tmp_path / "Assets"
    assets.mkdir()
    (assets / "clip.wav").write_bytes(b"\x00")
    ps = tmp_path / "ProjectSettings"
    ps.mkdir()
    (ps / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.0.23f1\n")
    res = signatures.scan(tmp_path)
    assert "unity" in res.artifact_types
    assert res.inventory == {}


def test_manifest_detection_merges_scan_inventory():
    res, _meta = detect(EXAMPLE, use_llm=False)
    assert res.run_plan_source == "manifest"
    assert "dataset" in res.artifact_types
    assert res.inventory.get("dataset") == 1


def test_iter_files_ignores_symlinks(tmp_path):
    # A symlinked file in an untrusted deposit must not be scanned/executed.
    (tmp_path / "real.py").write_text("print(1)\n")
    try:
        (tmp_path / "link.py").symlink_to(tmp_path / "real.py")
    except (OSError, NotImplementedError):
        import pytest
        pytest.skip("symlinks not creatable on this platform/user")
    names = {p.name for p in signatures._iter_files(tmp_path)}
    assert "real.py" in names and "link.py" not in names


def test_scan_does_not_follow_symlinked_dirs(tmp_path):
    # A symlink loop (dir -> parent) must not hang the walk (followlinks=False).
    (tmp_path / "a.py").write_text("print(1)\n")
    try:
        (tmp_path / "loop").symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        import pytest
        pytest.skip("symlinks not creatable on this platform/user")
    res = signatures.scan(tmp_path)   # must terminate
    assert "python" in res.artifact_types


def test_autoui_schema_is_packaged():
    # Regression for the "schema not shipped -> validation silently skipped" bug.
    from reprobe.detect.manifest import _load_schema
    schema = _load_schema()
    assert isinstance(schema, dict) and schema.get("properties")


def test_invalid_manifest_is_caught_by_schema(tmp_path):
    # With the schema packaged, jsonschema validation must actually run.
    import pytest
    pytest.importorskip("jsonschema")
    from reprobe.detect.manifest import _validate_autoui
    # version must be integer 1 per schema; a string should fail validation
    err = _validate_autoui({"version": "not-an-int", "run": {"steps": []}})
    assert err and "schema violation" in err


def test_install_command_quotes_untrusted_dep_filename(tmp_path):
    # A manifest 'dependencies' filename is untrusted and later runs via bash -c;
    # it must be shell-quoted so it can't inject a command into the install phase.
    from reprobe.envbuild.base import _install_commands
    weird = "a b;c.txt"                       # valid filename, shell-hostile if raw
    (tmp_path / weird).write_text("numpy\n")
    cmds, _ = _install_commands({"dependencies": weird}, tmp_path, r_needed=False)
    pip = next(c for c in cmds if c.startswith("pip install"))
    assert "'a b;c.txt'" in pip                # shlex-quoted
    assert "-r a b;c.txt" not in pip           # never the raw injectable form


# --------------------------------------------------------------------------- #
# R package discovery (static) + CRAN install-command generation
# --------------------------------------------------------------------------- #
def test_r_packages_detected_from_calls(tmp_path):
    (tmp_path / "analysis.R").write_text(
        "library(dplyr)\nrequire(ggplot2)\nrequireNamespace('data.table')\n"
        "y <- tidyr::pivot_longer(x)\n")
    res = signatures.scan(tmp_path)
    assert set(res.r_packages) >= {"dplyr", "ggplot2", "data.table", "tidyr"}


def test_r_packages_exclude_base_and_python(tmp_path):
    (tmp_path / "s.R").write_text("library(stats)\nlibrary(MASS)\nlibrary(lme4)\n")
    (tmp_path / "app.py").write_text("import antigravity\n")   # python import must not leak in
    res = signatures.scan(tmp_path)
    assert res.r_packages == ["lme4"]         # stats (base) + MASS (recommended) dropped


def test_r_packages_from_description(tmp_path):
    (tmp_path / "DESCRIPTION").write_text(
        "Package: foo\nImports:\n    dplyr,\n    lme4 (>= 1.1)\nDepends: R (>= 4.0), Matrix\n")
    res = signatures.scan(tmp_path)
    assert "dplyr" in res.r_packages and "lme4" in res.r_packages
    assert "R" not in res.r_packages and "Matrix" not in res.r_packages   # R + recommended dropped


def test_r_packages_r_kernel_notebook_only(tmp_path):
    import json
    r_nb = {"metadata": {"kernelspec": {"language": "R", "name": "ir"}},
            "cells": [{"cell_type": "code", "source": ["library(brms)\n"]}]}
    py_nb = {"metadata": {"kernelspec": {"language": "python", "name": "python3"}},
             "cells": [{"cell_type": "code", "source": ["library(evil)\n"]}]}
    (tmp_path / "r.ipynb").write_text(json.dumps(r_nb))
    (tmp_path / "py.ipynb").write_text(json.dumps(py_nb))
    res = signatures.scan(tmp_path)
    assert "brms" in res.r_packages
    assert "evil" not in res.r_packages       # a python-kernel notebook is never R-scanned


def test_cran_command_generated_for_detected_packages(tmp_path):
    (tmp_path / "analysis.R").write_text("library(brms)\n")
    det = signatures.scan(tmp_path)
    p = plan_env(det, {}, _cfg(), tmp_path)
    cran = next(c for c in p.install_commands if "install.packages" in c)
    assert cran.startswith("Rscript -e '") and 'c("brms")' in cran


def test_cran_command_honors_declared_packages(tmp_path):
    det = DetectResult(artifact_types=["r"])                     # r step present, none detected
    p = plan_env(det, {"environment": {"r_packages": ["lme4", "brms"]}}, _cfg(), tmp_path)
    cran = next(c for c in p.install_commands if "install.packages" in c)
    assert 'c("brms", "lme4")' in cran                          # sorted + deduped


def test_cran_command_gated_on_r_steps(tmp_path):
    det = DetectResult(artifact_types=["python"], r_packages=["brms"])
    p = plan_env(det, {}, _cfg(), tmp_path)
    assert not any("install.packages" in c for c in p.install_commands)
    assert any("no R steps" in w for w in p.warnings)


def test_cran_command_uses_pinned_snapshot(tmp_path):
    det = DetectResult(artifact_types=["r"], r_packages=["brms"])
    cfg = Config(config_dir=Path("."),
                 pins={"base_images": {"r": "r-img"}, "r": {"cran_snapshot": "https://snap/2026"}})
    p = plan_env(det, {}, cfg, tmp_path)
    assert any('repo <- "https://snap/2026"' in c for c in p.install_commands)


def test_cran_command_unpinned_warns_nonreproducible(tmp_path):
    det = DetectResult(artifact_types=["r"], r_packages=["brms"])
    p = plan_env(det, {}, _cfg(), tmp_path)                     # _cfg has no r.cran_snapshot
    assert any("not reproducible" in w for w in p.warnings)


def test_cran_command_is_single_quote_injection_safe():
    from reprobe.envbuild.base import _cran_install_command
    cmd = _cran_install_command(["dplyr"], "https://packagemanager.posit.co/cran/2026-07-01")
    # the whole R program rides inside ONE '...' -e argument; exactly two quotes,
    # so no discovered name/URL can break out of it into the shell.
    assert cmd.startswith("Rscript -e '") and cmd.endswith("'")
    assert cmd.count("'") == 2
