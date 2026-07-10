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
