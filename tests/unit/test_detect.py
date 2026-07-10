from pathlib import Path

from reprobe.detect import detect, signatures

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "example-python"


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
