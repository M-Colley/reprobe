"""Tiny, dependency-free analysis used as a reprobe fixture. Reads a CSV and
writes a summary + a 'figure', mirroring the shape of a real analysis repo."""

import csv
import os
import statistics

os.makedirs("results", exist_ok=True)
os.makedirs("figures", exist_ok=True)

with open("data/measurements.csv", newline="", encoding="utf-8") as fh:
    rows = list(csv.DictReader(fh))
values = [float(r["reaction_time_s"]) for r in rows]

with open("results/summary.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["metric", "value"])
    w.writerow(["n", len(values)])
    w.writerow(["mean", round(statistics.mean(values), 4)])
    w.writerow(["stdev", round(statistics.pstdev(values), 4)])

with open("figures/summary.txt", "w", encoding="utf-8") as fh:
    fh.write(f"n={len(values)} mean={statistics.mean(values):.3f}s\n")

print(f"OK: analyzed {len(values)} takeover events -> results/summary.csv, figures/summary.txt")
