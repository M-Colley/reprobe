"""Enforce the architectural invariant: only docker_exec.py may shell out to
`docker run`. Nothing else may launch containers or call subprocess on docker.
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "reprobe"
ALLOWED = {"docker_exec.py"}


def test_only_docker_exec_launches_containers():
    offenders = []
    for py in SRC.rglob("*.py"):
        if py.name in ALLOWED:
            continue
        # strip `#` comments so prose ("docker run itself failed") can't trip
        # the lint — actual launches are code, and code survives the strip
        text = "\n".join(line.split("#", 1)[0]
                         for line in py.read_text(encoding="utf-8").splitlines())
        # crude but effective: a `docker run` string or subprocess+docker in one file
        if re.search(r"docker[\"',\s]+run", text) or ('"docker"' in text and "subprocess" in text):
            offenders.append(str(py.relative_to(SRC)))
    assert not offenders, f"these files bypass the docker_exec chokepoint: {offenders}"
