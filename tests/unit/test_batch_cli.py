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
    assert _read_refs(str(f)) == ["https://github.com/a/b"]


def test_read_refs_empty_file_no_crash(tmp_path):
    f = tmp_path / "empty.csv"
    f.write_text("", encoding="utf-8")
    assert _read_refs(str(f)) == []          # must not IndexError / abort the season


def test_read_refs_plain_list_skips_comments(tmp_path):
    f = tmp_path / "list.csv"
    f.write_text("# a comment\nhttps://github.com/a/b\n\n", encoding="utf-8")
    assert _read_refs(str(f)) == ["https://github.com/a/b"]


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
