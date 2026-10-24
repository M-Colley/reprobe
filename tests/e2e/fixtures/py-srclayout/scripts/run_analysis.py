# A src-layout project: the script imports the project's own package (which
# `pip install -e .` would make importable) and a dependency declared only in
# pyproject.toml. Neither is in the base image or on the path by default.
import os

import cowsay
from studypkg import mean

os.makedirs("results", exist_ok=True)
with open("results/mean.txt", "w") as f:
    f.write(f"{mean([1, 2, 3])}\n")
cowsay.cow("done")
