"""Add packages to a base image's conda-lock.yml without moving anything locked.

    python images/lock-add.py images/base-r

Every package already in the lock is pinned to its exact version AND build;
whatever env.yaml declares that the lock lacks is solved on top. The result is
the old environment plus the new packages (and only the dependencies they bring),
so reports made on the previous revision stay comparable with the next one.

A plain `conda-lock lock -f env.yaml` re-solves EVERYTHING when env.yaml gains a
package (its `--update` only moves packages that are already locked), which
would move the whole stack to whatever is newest on the day. Use that, on
purpose, when the stack is meant to move; use this when it is not.

Needs conda-lock on PATH. Refuses to write a lock in which any previously locked
package changed version or build.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml


def _build(pkg: dict) -> str:
    """The build string, from the package filename `<name>-<version>-<build>.conda`."""
    fname = pkg["url"].rsplit("/", 1)[-1]
    stem = re.sub(r"\.(conda|tar\.bz2)$", "", fname)
    prefix = f"{pkg['name']}-{pkg['version']}-"
    if not stem.startswith(prefix):
        sys.exit(f"cannot read the build string of {pkg['name']} from {fname}")
    return stem[len(prefix):]


def _declared_name(dep: str) -> str:
    return re.split(r"[=<>!~ ]", dep, maxsplit=1)[0].strip().lower()


def main(image_dir: str) -> None:
    d = Path(image_dir)
    env = yaml.safe_load((d / "env.yaml").read_text(encoding="utf-8"))
    lock = yaml.safe_load((d / "conda-lock.yml").read_text(encoding="utf-8"))
    old = {p["name"]: p for p in lock["package"] if p["manager"] == "conda"}
    if any(p["manager"] != "conda" for p in lock["package"]):
        sys.exit("the lock has pip packages; this script handles conda-only environments")

    declared = [x for x in env["dependencies"] if isinstance(x, str)]
    new = [x for x in declared if _declared_name(x) not in old]
    if not new:
        sys.exit("env.yaml declares nothing the lock lacks; nothing to add")
    pins = [f"{n}=={p['version']}={_build(p)}" for n, p in sorted(old.items())]

    with tempfile.TemporaryDirectory() as tmp:
        spec = Path(tmp) / "env.yaml"                  # same basename as the real source
        spec.write_text(yaml.safe_dump({"name": env.get("name", "base"),
                                        "channels": env["channels"],
                                        "dependencies": pins + new}), encoding="utf-8")
        out = Path(tmp) / "conda-lock.yml"
        subprocess.run(["conda-lock", "lock", "-f", str(spec), "-p", "linux-64",
                        "--lockfile", str(out)], check=True)
        result = yaml.safe_load(out.read_text(encoding="utf-8"))
        got = {p["name"]: p for p in result["package"]}
        moved = [f"{n}: {p['version']}={_build(p)} -> "
                 + (f"{got[n]['version']}={_build(got[n])}" if n in got else "REMOVED")
                 for n, p in old.items()
                 if n not in got or (got[n]["version"], _build(got[n])) != (p["version"], _build(p))]
        if moved:
            sys.exit("refusing: previously locked packages changed:\n  " + "\n  ".join(moved))
        added = sorted(set(got) - set(old))
        shutil.copyfile(out, d / "conda-lock.yml")
    print(f"added {len(added)} package(s) for {', '.join(new)}; nothing previously locked moved:")
    for n in added:
        print(f"  + {n} {got[n]['version']}={_build(got[n])}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
