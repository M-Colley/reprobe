"""The committed goldens must match the live pipeline — this is the same check
`reprobe doctor --golden` runs, enforced in CI so expected.json never drifts."""

from __future__ import annotations

from pathlib import Path

from reprobe import golden

ROOT = Path(__file__).resolve().parents[2]


def test_find_repo_root():
    assert golden.find_repo_root() == ROOT


def test_goldens_match_live_pipeline():
    results = golden.compare(ROOT)
    assert results, "expected.json is empty"
    drift = [(name, detail) for name, ok, detail in results if not ok]
    assert not drift, (
        "golden drift — if the change is intentional, regenerate with "
        f"`python -m reprobe.golden --update`: {drift}"
    )


def test_goldens_cover_default_fixtures():
    import json

    expected = json.loads((ROOT / golden.GOLDEN_REL).read_text(encoding="utf-8"))
    assert set(expected) == set(golden.DEFAULT_FIXTURES)
