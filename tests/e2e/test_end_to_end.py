"""The whole pipeline, for real, in the published base images.

Everything else in the suite checks pieces: the argv, the sanitiser's output, the
attribution script under a local bash. What only this checks is that the pieces
work TOGETHER inside ``reprobe-base-*``: the sanitised requirements reach pip,
`python -I` still finds what `--target` installed, an R package comes from the
pinned snapshot with `--vanilla`, the offline run then sees both, and the report
says `runs` because it is true.

Each fixture depends on one package the base image does not ship, so a pass is
only possible if the install phase really installed it."""

from __future__ import annotations

from pathlib import Path

import pytest

from reprobe.orchestrator import Orchestrator

pytestmark = pytest.mark.e2e

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _run(tmp_path, name):
    report = Orchestrator(workroot=tmp_path).run(str(FIXTURES / name), use_llm=False)
    logs = sorted((tmp_path / report.submission_id / "logs").glob("*.log"))
    detail = "\n\n".join(f"== {p.name}\n" + p.read_text(encoding="utf-8", errors="replace")[-4000:]
                         for p in logs)
    return report, detail


def test_a_python_artifact_with_a_declared_requirement_runs(tmp_path):
    report, logs = _run(tmp_path, "py-requirements")
    install = report.environment["install_results"]["python"]
    assert install["ok"], f"install phase failed: {install}\n{logs}"
    # the `-e .` line was dropped and named, not installed
    assert any("were NOT applied" in w and "-e ." in w for w in report.environment["warnings"]), \
        report.environment["warnings"]
    step = report.steps[0]
    assert step.status == "pass", f"{step.status}: {step.diagnostics}\n{logs}"
    assert step.expected_met == ["results/table.csv"]
    assert report.verdict["overall"] == "runs", f"{report.verdict}\n{logs}"
    # the committed output was regenerated and matches (line endings may differ by checkout)
    rows = {r["path"]: r["status"] for r in report.results_comparison["files"]}
    assert rows["results/table.csv"] in ("identical", "equivalent"), rows
    assert report.environment.get("resolved_deps_digest", "").startswith("sha256:")
    assert report.environment.get("base_image_digest", "").startswith("sha256:")


def test_an_r_artifact_with_a_scanned_cran_package_runs(tmp_path):
    report, logs = _run(tmp_path, "r-cran")
    install = report.environment["install_results"]["r"]
    assert install["ok"], f"install phase failed: {install}\n{logs}"
    step = report.steps[0]
    assert step.status == "pass", f"{step.status}: {step.diagnostics}\n{logs}"
    assert report.verdict["overall"] == "runs", f"{report.verdict}\n{logs}"
    rows = {r["path"]: r["status"] for r in report.results_comparison["files"]}
    assert rows["results/n.txt"] in ("identical", "equivalent"), rows


def test_an_artifact_whose_declared_environment_cannot_install_is_its_own_finding(tmp_path):
    """Not the harness's: the author's requirements.txt names a package that does
    not exist. The verdict must say the artifact failed, and why."""
    art = tmp_path / "art"
    art.mkdir()
    (art / "run.py").write_text("import reprobe_e2e_no_such_package\n", encoding="utf-8")
    (art / "requirements.txt").write_text("reprobe-e2e-no-such-package==1.0\n", encoding="utf-8")
    report = Orchestrator(workroot=tmp_path / "wk").run(str(art), use_llm=False)
    install = report.environment["install_results"]["python"]
    assert install["ok"] is False and [f["origin"] for f in install["failures"]] == ["author-declared"], install
    assert report.verdict["overall"] == "runs-with-failures", report.verdict
    assert "declared environment does not install" in report.verdict.get("note", "")


def test_an_renv_projects_restored_library_is_usable_offline(tmp_path):
    """renv installs into its global cache and symlinks the project library to
    it. The cache lived in the install container's HOME, so every restored
    package was a dangling link in the run phase — and the step's "there is no
    package called" was read as the artifact's fault."""
    report, logs = _run(tmp_path, "r-renv")
    install = report.environment["install_results"]["r"]
    assert install["ok"], f"install phase failed: {install}\n{logs}"
    step = report.steps[0]
    assert step.status == "pass", f"{step.status}: {step.diagnostics}\n{logs}"
    assert step.expected_met == ["results/version.txt"]
    produced = tmp_path / report.submission_id / "run" / "results" / "version.txt"
    assert produced.read_text(encoding="utf-8").strip() == "1.0.0"
