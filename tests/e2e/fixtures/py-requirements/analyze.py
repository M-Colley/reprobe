"""A tiny artifact whose one dependency is NOT in the base image, so the run only
passes if the install phase really built it."""
import csv
import os

import cowsay

os.makedirs("results", exist_ok=True)
rows = [["group", "n"], ["a", 1], ["b", 2]]
with open("results/table.csv", "w", newline="") as fh:
    csv.writer(fh).writerows(rows)
print(cowsay.get_output_string("cow", "done"))
