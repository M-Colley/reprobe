"""Golden-report regression (DESIGN §11, Phase 5).

Runs the bundled fixtures through the FULL pipeline — fetch (local), detect,
env planning, badge + verdict logic — with dry-run containers (no Docker
daemon needed, nothing executed) and no LLM, then compares the stable,
machine-independent slice of each report against ``tests/golden/expected.json``.

This is the yearly "did my pins/badges/config edit break the harness?" gate:
it checks the pipeline's *shape*, not real execution (dry-run fabricates
exit 0, so run statuses here document dry-run semantics, not artifact truth).

Deliberately excluded from the compared slice because they vary by machine:
image names/digests, env provenance (base-image fallback), durations, paths,
notes (runner-load errors are appended there).

Regenerate after an intentional behavior change:

    python -m reprobe.golden --update
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Optional

from .config import Config
from .models import Report

GOLDEN_REL = Path("tests") / "golden" / "expected.json"

# Fixture variety: manifest-driven, broken script, notebook+R mix with
# renv.lock, conda env (needs-repo2docker flag), lowercase .r, invalid manifest.
DEFAULT_FIXTURES = [
    "examples/example-python",
    "tests/fixtures/broken-python",
    "tests/fixtures/notebook-r-mix",
    "tests/fixtures/conda-env",
    "tests/fixtures/lowercase-r",
    "tests/fixtures/bad-manifest",
]


def find_repo_root() -> Optional[Path]:
    """The repo checkout this package runs from (editable install or a
    checkout as cwd); None for a plain site-packages install — callers then
    skip the golden check gracefully."""
    for cand in (Path(__file__).resolve().parents[2], Path.cwd()):
        if (cand / "tests" / "fixtures").is_dir() and (cand / "examples").is_dir():
            return cand
    return None


def extract(report: Report) -> dict[str, Any]:
    """The machine-independent slice of a report."""
    det = report.detect or {}
    env = report.environment or {}
    acm = (report.badges.get("acm") or {})
    return {
        "artifact_types": det.get("artifact_types"),
        "inventory": det.get("inventory") or {},
        "run_plan_source": det.get("run_plan_source"),
        "manifest": det.get("manifest"),
        "steps": det.get("steps"),
        "flags": det.get("flags"),
        "strategy": env.get("strategy"),
        "install_commands": env.get("install_commands"),
        "step_statuses": [s.status for s in report.steps],
        "available": acm.get("available"),
        "functional": acm.get("functional"),
        "verdict": (report.verdict or {}).get("overall"),
    }


def run_fixture(root: Path, rel: str, config: Optional[Config] = None) -> dict[str, Any]:
    from .orchestrator import Orchestrator

    with tempfile.TemporaryDirectory(prefix="reprobe-golden-", ignore_cleanup_errors=True) as td:
        orch = Orchestrator(config=config, workroot=td)
        report = orch.run(str(root / rel), use_llm=False, dry_run=True)
        return extract(report)


def compare(root: Path, config: Optional[Config] = None) -> list[tuple[str, bool, str]]:
    """[(fixture, ok, detail)] against expected.json. A missing fixture is a
    failure (the goldens claim it exists), never a silent skip."""
    expected = json.loads((root / GOLDEN_REL).read_text(encoding="utf-8"))
    results: list[tuple[str, bool, str]] = []
    for rel, exp in expected.items():
        if not (root / rel).exists():
            results.append((rel, False, "fixture missing"))
            continue
        act = run_fixture(root, rel, config)
        diffs = [f"{k}: expected {exp.get(k)!r}, got {act.get(k)!r}"
                 for k in sorted(set(exp) | set(act)) if exp.get(k) != act.get(k)]
        results.append((rel, not diffs, "; ".join(diffs) if diffs else "matches"))
    return results


def update(root: Path, config: Optional[Config] = None,
           fixtures: Optional[list[str]] = None) -> Path:
    golden = {rel: run_fixture(root, rel, config) for rel in (fixtures or DEFAULT_FIXTURES)}
    out = root / GOLDEN_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(golden, indent=2) + "\n", encoding="utf-8")
    return out


def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Golden-report regression for reprobe.")
    ap.add_argument("--update", action="store_true",
                    help="regenerate tests/golden/expected.json from the current pipeline")
    args = ap.parse_args()

    root = find_repo_root()
    if root is None:
        print("no repo checkout found (tests/fixtures + examples missing)")
        return 2
    if args.update:
        out = update(root)
        print(f"wrote {out}")
        return 0
    if not (root / GOLDEN_REL).is_file():
        print(f"{root / GOLDEN_REL} missing — run with --update first")
        return 2
    failed = False
    for name, ok, detail in compare(root):
        print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f" — {detail}"))
        failed |= not ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
