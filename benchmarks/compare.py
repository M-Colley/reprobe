"""Benchmark helper for .github/workflows/benchmark.yml (standard library only).

    compare.py rows    CSV SELECTION      -> `rows=[1,2,...]` for the job matrix
    compare.py one     CSV ROW            -> a one-row CSV for `reprobe batch`
    compare.py compare CSV REPORTS --out D -> per-artifact table + cross-tab (markdown
                                             on stdout, comparison.csv in D)

Rows are 1-based data rows (comments and blank lines skipped). REPORTS holds
one `report-<row>/` folder per job (actions/download-artifact), each with
`<submission>/report/report.json`.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

#: Highest badge first: an artifact is tabulated under the strongest badge it holds.
BADGE_ORDER = ("reproduced", "replicated", "reusable", "functional", "available")
#: Verdicts in which every executed step passed (with or without warnings).
RAN_CLEANLY = {"runs", "runs-with-warnings"}


def read_rows(path: str) -> tuple[list[str], list[dict[str, str]]]:
    text = Path(path).read_text(encoding="utf-8-sig")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    return list(reader.fieldnames or []), list(reader)


def strongest(badges: str) -> str:
    have = {b.strip().lower() for b in (badges or "").replace(",", ";").split(";") if b.strip()}
    return next((b for b in BADGE_ORDER if b in have), "none")


def cmd_rows(path: str, selection: str) -> None:
    _, rows = read_rows(path)
    if selection.strip().lower() == "all":
        picked = list(range(1, len(rows) + 1))
    else:
        picked = sorted({int(x) for x in selection.replace(" ", "").split(",") if x})
        bad = [i for i in picked if not 1 <= i <= len(rows)]
        if bad:
            sys.exit(f"rows out of range 1..{len(rows)}: {bad}")
    print("rows=" + json.dumps(picked))


def cmd_one(path: str, row: int) -> None:
    _, rows = read_rows(path)
    r = rows[row - 1]
    w = csv.writer(sys.stdout, lineterminator="\n")
    w.writerow(["url", "data"])
    w.writerow([r["url"], r.get("data", "")])


def _report(reports: Path, row: int) -> dict | None:
    found = sorted((reports / f"report-{row}").glob("*/report/report.json"))
    if not found:
        return None
    try:
        return json.loads(found[0].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cmd_compare(path: str, reports: Path, out: Path) -> None:
    _, rows = read_rows(path)
    table = []
    for i, r in enumerate(rows, start=1):
        rep = _report(reports, i)
        acm = ((rep or {}).get("badges") or {}).get("acm") or {}
        steps = (rep or {}).get("steps") or []
        table.append({
            "row": i, "url": r["url"], "venue": r.get("venue", ""), "year": r.get("year", ""),
            "badges": r.get("badges", ""), "strongest": strongest(r.get("badges", "")),
            "language": r.get("language", ""),
            "verdict": ((rep or {}).get("verdict") or {}).get("overall", "no-report"),
            "available": acm.get("available", ""), "functional": acm.get("functional", ""),
            "steps": len(steps),
            "steps_passed": sum(1 for s in steps if s.get("status") == "pass"),
            "step_statuses": ";".join(sorted({str(s.get("status")) for s in steps})),
        })
    out.mkdir(parents=True, exist_ok=True)
    with (out / "comparison.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(table[0]) if table else ["row"])
        w.writeheader()
        w.writerows(table)

    verdicts = sorted({t["verdict"] for t in table})
    levels = [b for b in (*BADGE_ORDER, "none") if any(t["strongest"] == b for t in table)]
    cross = Counter((t["strongest"], t["verdict"]) for t in table)
    print(f"## reprobe verdicts vs. awarded badges ({len(table)} artifacts)\n")
    print("| strongest badge | " + " | ".join(verdicts) + " | ran cleanly |")
    print("|---|" + "---|" * (len(verdicts) + 1))
    for b in levels:
        n = sum(cross[(b, v)] for v in verdicts)
        clean = sum(cross[(b, v)] for v in verdicts if v in RAN_CLEANLY)
        print(f"| {b} | " + " | ".join(str(cross[(b, v)]) for v in verdicts) + f" | {clean}/{n} |")
    print("\n## Per artifact\n")
    print("| row | badges | verdict | steps passed | url |")
    print("|---|---|---|---|---|")
    for t in table:
        print(f"| {t['row']} | {t['badges']} | {t['verdict']} | {t['steps_passed']}/{t['steps']} | {t['url']} |")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("rows")
    p.add_argument("csv")
    p.add_argument("selection")
    p = sub.add_parser("one")
    p.add_argument("csv")
    p.add_argument("row", type=int)
    p = sub.add_parser("compare")
    p.add_argument("csv")
    p.add_argument("reports", type=Path)
    p.add_argument("--out", type=Path, default=Path("comparison"))
    a = ap.parse_args(argv)
    if a.cmd == "rows":
        cmd_rows(a.csv, a.selection)
    elif a.cmd == "one":
        cmd_one(a.csv, a.row)
    else:
        cmd_compare(a.csv, a.reports, a.out)


if __name__ == "__main__":
    main()
