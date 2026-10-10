"""report.json is a published interface (badges.json, dashboards, other tools
read it), and its schema had drifted from what the code writes: every
fetch-failed report violated it, and a fallback-generic environment was not in
its vocabulary. Validate the real thing, for each kind of report, here."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import reprobe.orchestrator as orch_mod
from reprobe.models import RawRunOutput
from reprobe.orchestrator import Orchestrator

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "example-python"
BROKEN = ROOT / "tests" / "fixtures" / "broken-python"
SCHEMA = json.loads(resources.files("reprobe").joinpath("schemas/report.schema.json")
                    .read_text(encoding="utf-8"))


def _assert_valid(tmp_path, report):
    data = json.loads((tmp_path / report.submission_id / "report" / "report.json")
                      .read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(SCHEMA).iter_errors(data), key=lambda e: list(e.path))
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors]


def _simulated(monkeypatch, exit_code):
    def fake(spec, limits, log_path, **kw):
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text("output\n", encoding="utf-8")
        return RawRunOutput(exit_code=exit_code, duration_s=0.1, image=spec.image, log_path=str(log_path))
    monkeypatch.setattr(orch_mod, "run_container", fake)
    monkeypatch.setattr(orch_mod, "reap_orphans", lambda: [])
    monkeypatch.setattr(orch_mod, "_image_digest", lambda image: None)


@pytest.mark.parametrize("kind", ["dry-run", "no-run", "fetch-failed", "full-pass", "full-fail"])
def test_every_kind_of_report_matches_the_published_schema(tmp_path, monkeypatch, kind):
    o = Orchestrator(workroot=tmp_path)
    if kind == "dry-run":
        report = o.run(str(EXAMPLE), use_llm=False, dry_run=True)
    elif kind == "no-run":
        report = o.run(str(EXAMPLE), use_llm=False, do_run=False)
    elif kind == "fetch-failed":
        report = o.run(str(tmp_path / "no-such-artifact"), use_llm=False)
    else:
        _simulated(monkeypatch, 0 if kind == "full-pass" else 1)
        report = o.run(str(EXAMPLE if kind == "full-pass" else BROKEN), use_llm=False)
    _assert_valid(tmp_path, report)
