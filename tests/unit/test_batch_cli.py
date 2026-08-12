"""batch CLI: --resume reuses completed reports, badges.csv is emitted.
Daemon-free: --no-run never launches containers."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from reprobe.cli import app

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "example-python"

runner = CliRunner()


def _batch(tmp_path: Path, *extra: str):
    csv_file = tmp_path / "subs.csv"
    csv_file.write_text(f"url\n{EXAMPLE}\n", encoding="utf-8")
    return runner.invoke(app, [
        "batch", str(csv_file),
        "--workroot", str(tmp_path / "work"), "--out", str(tmp_path / "out"),
        "--no-run", "--no-llm", *extra,
    ])


def _report_json(tmp_path: Path) -> Path:
    hits = list((tmp_path / "work").glob("*/out/report.json"))
    assert len(hits) == 1
    return hits[0]


def test_batch_emits_badges_csv_and_dashboard(tmp_path):
    res = _batch(tmp_path)
    assert res.exit_code == 0, res.output
    assert (tmp_path / "out" / "dashboard.html").is_file()
    assert (tmp_path / "out" / "badges.json").is_file()
    csv_text = (tmp_path / "out" / "badges.csv").read_text(encoding="utf-8")
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("submission_id,input,verdict")
    assert len(lines) == 2 and "not-run" in lines[1]


def test_batch_resume_skips_completed_reports(tmp_path):
    assert _batch(tmp_path).exit_code == 0
    rep_path = _report_json(tmp_path)

    # plant a marker; a resumed run must keep it, a fresh run must overwrite it
    data = json.loads(rep_path.read_text(encoding="utf-8"))
    data["harness_version"] = "reprobe MARKER"
    rep_path.write_text(json.dumps(data), encoding="utf-8")

    res = _batch(tmp_path, "--resume")
    assert res.exit_code == 0, res.output
    assert "resumed from existing report" in res.output
    assert "MARKER" in rep_path.read_text(encoding="utf-8")

    res = _batch(tmp_path)                      # no --resume -> re-run
    assert res.exit_code == 0, res.output
    assert "MARKER" not in rep_path.read_text(encoding="utf-8")


def test_batch_resume_retries_fetch_failed(tmp_path):
    assert _batch(tmp_path).exit_code == 0
    rep_path = _report_json(tmp_path)
    data = json.loads(rep_path.read_text(encoding="utf-8"))
    data["verdict"]["overall"] = "fetch-failed"
    data["harness_version"] = "reprobe MARKER"
    rep_path.write_text(json.dumps(data), encoding="utf-8")

    res = _batch(tmp_path, "--resume")
    assert res.exit_code == 0, res.output
    assert "resumed" not in res.output          # retried, not reused
    assert "MARKER" not in rep_path.read_text(encoding="utf-8")


# --- _read_refs robustness + CSV formula-injection guard ---------------------

from reprobe.cli import _badges_csv, _csv_safe, _read_refs


def test_read_refs_strips_bom(tmp_path):
    # Excel/PowerShell write a UTF-8 BOM; header + rows must still parse.
    f = tmp_path / "subs.csv"
    f.write_bytes(b"\xef\xbb\xbfurl\nhttps://github.com/a/b\n")
    assert _read_refs(str(f)) == [{"url": "https://github.com/a/b", "data": []}]


def test_read_refs_empty_file_no_crash(tmp_path):
    f = tmp_path / "empty.csv"
    f.write_text("", encoding="utf-8")
    assert _read_refs(str(f)) == []          # must not IndexError / abort the season


def test_read_refs_plain_list_skips_comments(tmp_path):
    f = tmp_path / "list.csv"
    f.write_text("# a comment\nhttps://github.com/a/b\n\n", encoding="utf-8")
    assert _read_refs(str(f)) == [{"url": "https://github.com/a/b", "data": []}]


def test_csv_safe_neutralizes_formulas():
    for danger in ("=cmd()", "+1", "-2", "@x", "\tx", "\rx"):
        assert _csv_safe(danger).startswith("'")
    assert _csv_safe("https://github.com/a/b") == "https://github.com/a/b"
    assert _csv_safe(None) == "" and _csv_safe(True) == "True"


def test_badges_csv_quotes_formula_input():
    rep = {"submission_id": "s1", "source": {"input": "=HYPERLINK(evil)"},
           "verdict": {"overall": "runs"}, "badges": {"acm": {}, "fair": {}}, "detect": {}}
    out = _badges_csv([rep])
    # the malicious input cell must be prefixed so a spreadsheet treats it as text
    assert "'=HYPERLINK(evil)" in out
    assert ",=HYPERLINK(evil)" not in out


def test_doctor_fails_loudly_on_a_config_dir_that_does_not_exist(tmp_path):
    """This row hardcoded "ok" and printed the path without looking at it, so a
    non-editable install — whose config resolves to <prefix>/Lib/config — was
    vouched for by the one check that could have caught it."""
    missing = tmp_path / "no-such-config"
    res = runner.invoke(app, ["doctor", "--config-dir", str(missing)])
    out = " ".join(res.stdout.split())
    assert "does not exist" in out
    assert res.exit_code != 0, "doctor exited 0 with an unusable config dir"


def test_doctor_accepts_a_real_config_dir():
    cfg = Path(__file__).resolve().parents[2] / "config"
    res = runner.invoke(app, ["doctor", "--config-dir", str(cfg)])
    out = " ".join(res.stdout.split())
    assert "does not exist" not in out
    assert "year=2026" in out or "pins.yaml ok" in out.replace("│", "")


def test_non_editable_install_detection_matches_this_checkout():
    """An editable install (or a plain checkout) leaves the package in the repo;
    only a copied install lands under site-packages."""
    from reprobe.config import installed_non_editably
    assert installed_non_editably() is False, \
        "the test suite is running against a copied install, not this checkout"


def test_read_refs_carries_a_data_column(tmp_path):
    """`--data` exists for artifacts whose README links the deposit in prose and
    declares nothing a machine can read. Batch is where a chair reviews a season,
    so without this column that whole artifact shape was unreviewable at scale."""
    f = tmp_path / "subs.csv"
    f.write_text("url,data\nhttps://github.com/a/b,https://osf.io/cwd6h\n", encoding="utf-8")
    assert _read_refs(str(f)) == [
        {"url": "https://github.com/a/b", "data": ["https://osf.io/cwd6h"]}]


def test_read_refs_splits_several_deposits_in_one_cell(tmp_path):
    # ',' is the column separator and '::' the subdir marker, so ';' and
    # whitespace are what is left — and a URL can contain neither.
    f = tmp_path / "subs.csv"
    f.write_text("url,data\nhttps://x/y,https://osf.io/a ; https://zenodo.org/records/1::data\n",
                 encoding="utf-8")
    assert _read_refs(str(f))[0]["data"] == ["https://osf.io/a",
                                             "https://zenodo.org/records/1::data"]


def test_read_refs_data_column_is_case_and_space_insensitive(tmp_path):
    f = tmp_path / "subs.csv"
    f.write_text(" URL , Data \nhttps://x/y,https://osf.io/a\n", encoding="utf-8")
    row = _read_refs(str(f))[0]
    assert row["url"] == "https://x/y" and row["data"] == ["https://osf.io/a"]


def test_read_refs_tolerates_extra_columns(tmp_path):
    f = tmp_path / "subs.csv"
    f.write_text("url,notes,data\nhttps://x/y,paper 12,https://osf.io/a\n", encoding="utf-8")
    assert _read_refs(str(f))[0]["data"] == ["https://osf.io/a"]


def test_batch_passes_each_rows_deposits_to_the_run(tmp_path, monkeypatch):
    """A silently ignored column is worse than an unsupported one: the season
    would complete, and every composite artifact in it would be failed for a
    missing input the chair had already supplied."""
    import reprobe.cli as cli_mod
    from reprobe.models import Report

    seen = []

    class FakeOrch:
        def run(self, ref, **kw):
            seen.append((ref, kw.get("data_sources")))
            return Report(submission_id="sid", harness_version="t", timestamp="",
                          verdict={"overall": "not-run", "human_review_required": True})

    monkeypatch.setattr(cli_mod, "_orch", lambda *a, **k: FakeOrch())
    f = tmp_path / "subs.csv"
    f.write_text("url,data\nhttps://github.com/a/b,https://osf.io/cwd6h\nhttps://github.com/c/d,\n",
                 encoding="utf-8")
    cli_mod.batch(str(f), workroot=str(tmp_path / "w"), out=str(tmp_path / "o"),
                  config_dir=None, no_run=True, no_llm=True, no_functional=False,
                  no_install=False, allow_lfs=False, timeout=None, reuse_downloads=False,
                  resume=False)
    assert seen == [("https://github.com/a/b", ["https://osf.io/cwd6h"]),
                    ("https://github.com/c/d", [])]
