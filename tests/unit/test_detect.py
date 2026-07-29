from pathlib import Path

import pytest

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


def test_downstream_notebook_runs_after_the_steps_it_aggregates(tmp_path):
    """Alphabetical order put `analyse_combined_*` first, so the aggregator read
    the *committed* outputs of models that had not re-run yet — a stale pass.
    Regression for PDRA_XAI_OS (2026-07)."""
    for name in ("analyse_combined_feature_ranking.ipynb", "PDRA_CatBoost.ipynb",
                 "PDRA_RF.ipynb", "PDRA_XGBoost.ipynb"):
        (tmp_path / name).write_text("{}")
    (tmp_path / "README.md").write_text("no file names here")
    res = signatures.scan(tmp_path)
    assert [s.target for s in res.steps][-1] == "analyse_combined_feature_ranking.ipynb"
    assert any("run order is inferred" in n for n in res.notes)


def test_numeric_prefix_still_outranks_the_stage_guess(tmp_path):
    """The name heuristic must only break ties the author left unordered."""
    (tmp_path / "01_analysis.ipynb").write_text("{}")
    (tmp_path / "02_cleanup.ipynb").write_text("{}")
    (tmp_path / "README.md").write_text("")
    res = signatures.scan(tmp_path)
    assert [s.target for s in res.steps] == ["01_analysis.ipynb", "02_cleanup.ipynb"]
    # order was declared numerically, so no "inferred order" warning
    assert not any("run order is inferred" in n for n in res.notes)


def test_readme_order_outranks_the_stage_guess(tmp_path):
    (tmp_path / "summary.ipynb").write_text("{}")
    (tmp_path / "model.ipynb").write_text("{}")
    (tmp_path / "README.md").write_text("First run summary.ipynb, then model.ipynb.")
    res = signatures.scan(tmp_path)
    assert [s.target for s in res.steps] == ["summary.ipynb", "model.ipynb"]


def test_root_license_and_dependency_manifest_are_detected(tmp_path):
    (tmp_path / "nb.ipynb").write_text("{}")
    (tmp_path / "LICENSE").write_text("CC-BY-4.0")
    (tmp_path / "requirements.txt").write_text("pandas\n")
    res = signatures.scan(tmp_path)
    assert res.license_file == "LICENSE"
    assert res.dep_manifest == "requirements.txt"


def test_missing_license_and_dependency_manifest_are_none(tmp_path):
    (tmp_path / "nb.ipynb").write_text("{}")
    res = signatures.scan(tmp_path)
    assert res.license_file is None and res.dep_manifest is None


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
def test_conda_env_is_built_not_ignored():
    """environment.yml used to be detected, warned about, and skipped — and the
    artifact was then failed on the very import it had declared. The base image
    IS micromamba, so the declared environment gets built in the install phase."""
    det = signatures.scan(FIXTURES / "conda-env")
    p = plan_env(det, {"environment": {}}, _cfg(), FIXTURES / "conda-env")
    assert any("micromamba create" in c and "environment.yml" in c for c in p.install_commands)
    assert p.conda_env_prefix == "/work/.reprobe_env"
    # built is not the same as faithful — say what it does not reproduce
    assert any("activate" in w for w in p.warnings)


def test_conda_env_and_requirements_txt_do_not_shadow_each_other(tmp_path):
    """A --target pip install lands on PYTHONPATH, which precedes site-packages —
    it would shadow the very environment the manifest declared. Both go into the
    env instead."""
    (tmp_path / "environment.yml").write_text("dependencies: [pandas]\n")
    (tmp_path / "requirements.txt").write_text("pandas\n")
    det = DetectResult(artifact_types=["jupyter"])
    p = plan_env(det, {"environment": {"dependencies": "environment.yml"}}, _cfg(), tmp_path)

    pip = [c for c in p.install_commands if "pip install" in c]
    assert pip and all(c.startswith("/work/.reprobe_env/bin/pip") for c in pip), pip
    assert not any("--target=/work/.reprobe_deps" in c for c in p.install_commands)


def test_notebook_env_gets_papermill_or_it_runs_the_wrong_python(tmp_path):
    """papermill lives in the BASE image. Without it inside the built env, the
    notebook runner finds the base papermill on PATH and executes against the
    interpreter environment.yml exists to replace."""
    (tmp_path / "environment.yml").write_text("dependencies: [pandas]\n")
    nb = plan_env(DetectResult(artifact_types=["jupyter"]), {}, _cfg(), tmp_path)
    py = plan_env(DetectResult(artifact_types=["python"]), {}, _cfg(), tmp_path)
    assert any("papermill" in c for c in nb.install_commands)
    assert not any("papermill" in c for c in py.install_commands)


def test_anaconda_defaults_channel_is_flagged(tmp_path):
    (tmp_path / "environment.yml").write_text("channels: [defaults]\ndependencies: [pandas]\n")
    p = plan_env(DetectResult(artifact_types=["python"]), {}, _cfg(), tmp_path)
    assert any("terms of service" in w for w in p.warnings)


def test_declared_missing_dependency_file_warns(tmp_path):
    det = DetectResult(artifact_types=["python"])
    p = plan_env(det, {"environment": {"dependencies": "requirments.txt"}}, _cfg(), tmp_path)
    assert any("does not exist" in w for w in p.warnings)


def test_no_dependency_manifest_at_all_is_warned(tmp_path):
    """The silent over-claim: with nothing declared, every import comes from the
    harness base image, so a green step says nothing about the artifact."""
    (tmp_path / "nb.ipynb").write_text("{}")
    det = signatures.scan(tmp_path)
    p = plan_env(det, {}, _cfg(), tmp_path)
    assert any("declares NO dependency manifest" in w for w in p.warnings)


def test_declared_dependencies_suppress_the_no_manifest_warning(tmp_path):
    (tmp_path / "nb.ipynb").write_text("{}")
    (tmp_path / "requirements.txt").write_text("pandas\n")
    det = signatures.scan(tmp_path)
    p = plan_env(det, {}, _cfg(), tmp_path)
    assert not any("declares NO dependency manifest" in w for w in p.warnings)


def test_no_manifest_warning_needs_runnable_code(tmp_path):
    """A data-only deposit has no code, so it cannot be blamed for not pinning
    an environment it never uses."""
    (tmp_path / "data.csv").write_text("a,b\n1,2\n")
    det = signatures.scan(tmp_path)
    p = plan_env(det, {}, _cfg(), tmp_path)
    assert not any("declares NO dependency manifest" in w for w in p.warnings)


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
    cmds, _, _ = _install_commands({"dependencies": weird}, tmp_path, r_needed=False)
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


def test_r_packages_detected_from_setup_install_list(tmp_path):
    # setup.R-style: the full dep set is declared in install.packages(c(...)),
    # NOT via library() calls — reprobe must pick these up (they include on-demand
    # deps like FSA/Hmisc that no library() call reveals).
    (tmp_path / "setup.R").write_text(
        'pkgs <- c(\n'
        '  "colleyRstats",  # checkAssumptionsForAnova(), reportART()\n'
        '  "FSA",           # dunn test helper\n'
        '  "Hmisc"\n'
        ')\n'
        'missing <- pkgs[!pkgs %in% rownames(installed.packages())]\n'
        'install.packages(missing, repos = "https://cloud.r-project.org")\n')
    res = signatures.scan(tmp_path)
    assert {"colleyRstats", "FSA", "Hmisc"} <= set(res.r_packages)
    assert not any("http" in p or "cloud" in p for p in res.r_packages)   # URL not a package


def test_c_vector_not_harvested_without_install_packages(tmp_path):
    # a plain c(...) of string literals in code that doesn't install packages
    # must NOT be mistaken for a package list (no false positives from data).
    (tmp_path / "analysis.R").write_text('cols <- c("Days", "Time"); print(cols)\n')
    res = signatures.scan(tmp_path)
    assert "Days" not in res.r_packages and "Time" not in res.r_packages


def test_r_ipynb_recursion_bomb_does_not_crash_scan(tmp_path):
    # A deeply-nested .ipynb JSON (a RecursionError bomb, ~100 KB — far under the
    # read cap) must never crash detection before any container runs.
    depth = 60000
    (tmp_path / "bomb.ipynb").write_text("[" * depth + "]" * depth)
    res = signatures.scan(tmp_path)                # must not raise
    assert res.r_packages == []


def test_cran_command_is_single_quote_injection_safe(tmp_path):
    # Drive HOSTILE names through the real path (plan_env -> _cran_packages ->
    # _cran_install_command): the name validation must drop anything that could
    # break out of the single-quoted `-e` argument, leaving exactly two quotes.
    hostile = ["a'; touch /pwned; #", "foo bar", "back`tick`", "semi;colon",
               "dollar$(id)", "new\nline", "dplyr"]
    det = DetectResult(artifact_types=["r"])
    p = plan_env(det, {"environment": {"r_packages": hostile}}, _cfg(), tmp_path)
    cran = next(c for c in p.install_commands if "install.packages" in c)
    assert 'c("dplyr")' in cran, cran            # only the legitimate name survives
    assert cran.startswith("Rscript -e '") and cran.endswith("'")
    assert cran.count("'") == 2                  # no break-out of the -e argument
    for bad in ("touch /pwned", "`", "$(", "\n"):
        assert bad not in cran


def test_declared_install_list_ignores_unrelated_vectors(tmp_path):
    # A setup.R usually holds more than the package list. Only names reachable
    # from the install.packages() call may be harvested — factor levels like
    # c("car","boot") are real CRAN names and would otherwise be installed for
    # nothing, and the rest become bogus "not on CRAN" noise in the report.
    (tmp_path / "setup.R").write_text(
        'pkgs <- c("colleyRstats", "FSA")\n'
        'levels <- c("car", "boot", "Days", "Time")\n'
        'conds <- c("Male", "Female")\n'
        'missing <- pkgs[!pkgs %in% rownames(installed.packages())]\n'
        'install.packages(missing, repos = "https://cloud.r-project.org")\n')
    res = signatures.scan(tmp_path)
    assert set(res.r_packages) == {"colleyRstats", "FSA"}


@pytest.mark.parametrize("src,expected", [
    ('install.packages(c("dplyr","lme4"))', {"dplyr", "lme4"}),
    ('install.packages("brms")', {"brms"}),
    ('x <- c("Days","Time")', set()),                       # no install call at all
])
def test_declared_install_list_forms(tmp_path, src, expected):
    from reprobe.detect.signatures import _declared_install_packages
    assert _declared_install_packages(src) == expected


def test_conda_package_cache_is_never_on_the_work_bind_mount(tmp_path):
    """/work is a host bind mount, and on Windows/macOS that filesystem folds
    case — while micromamba's package cache is integrity-checked file-for-file.
    ncurses (a CPython dependency) ships share/terminfo/N and .../n, so a cache
    on /work dies with "Cannot find a valid extracted directory cache". Observed
    on a real run; the cache belongs on the container's own filesystem."""
    (tmp_path / "environment.yml").write_text("dependencies: [pandas]\n", encoding="utf-8")
    p = plan_env(DetectResult(artifact_types=["python"]), {}, _cfg(), tmp_path)
    create = next(c for c in p.install_commands if "micromamba create" in c)
    root_prefix = create.split("MAMBA_ROOT_PREFIX=")[1].split()[0]
    assert not root_prefix.startswith("/work"), root_prefix
    assert not root_prefix.startswith("/tmp"), f"{root_prefix} is a 4g tmpfs; torch overflows it"


def test_case_folding_host_is_disclosed(tmp_path, monkeypatch):
    """On a case-folding host the built env is close to, but not byte-identical
    with, the one that was solved. Say so rather than let it pass as faithful."""
    import reprobe.envbuild.base as eb
    (tmp_path / "environment.yml").write_text("dependencies: [pandas]\n", encoding="utf-8")

    monkeypatch.setattr(eb, "_case_insensitive", lambda path: True)
    assert any("folds case" in w for w in
               plan_env(DetectResult(artifact_types=["python"]), {}, _cfg(), tmp_path).warnings)

    monkeypatch.setattr(eb, "_case_insensitive", lambda path: False)
    assert not any("folds case" in w for w in
                   plan_env(DetectResult(artifact_types=["python"]), {}, _cfg(), tmp_path).warnings)
