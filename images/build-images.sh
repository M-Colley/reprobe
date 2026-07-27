#!/usr/bin/env bash
# Build (and tag) the reprobe base images from config/pins.yaml.
# Re-solves conda-lock.yml so the base is reproducible from the lock alone.
#
#   bash images/build-images.sh            # build py + r + controller
#   bash images/build-images.sh py         # build only python base
#   bash images/build-images.sh controller # build only the CI controller image
set -euo pipefail
cd "$(dirname "$0")/.."

read_pin() { python - "$1" <<'PY'
import sys, yaml
print((yaml.safe_load(open("config/pins.yaml"))) .get("base_images", {}).get(sys.argv[1], ""))
PY
}
read_top() { python - "$1" <<'PY'
import sys, yaml
print(yaml.safe_load(open("config/pins.yaml")).get(sys.argv[1], ""))
PY
}

MAMBA_BASE="$(read_pin micromamba_base)"
PY_TAG="$(read_pin python)"
R_TAG="$(read_pin r)"
CTRL_TAG="$(read_pin controller)"
WHICH="${1:-all}"

solve_lock() {  # $1 = images/base-xx dir
  local dir="$1"
  if command -v conda-lock >/dev/null 2>&1; then
    echo ">> solving lock for $dir"
    conda-lock lock -f "$dir/env.yaml" -p linux-64 --lockfile "$dir/conda-lock.yml" || \
      echo "!! conda-lock failed"
  else
    echo "!! conda-lock not installed. (pip install conda-lock for reproducible bases)"
  fi
  # The Dockerfile PREFERS conda-lock.yml over env.yaml, so a lock left behind
  # from an earlier solve silently wins over a freshly edited env.yaml — you get
  # an image missing the package you just added, tagged as if it had it. Refuse
  # to build on a lock that does not cover every declared dependency.
  if [ -f "$dir/conda-lock.yml" ]; then
    python - "$dir" <<'PY' || exit 1
import sys, pathlib, re, yaml
d = pathlib.Path(sys.argv[1])
env = yaml.safe_load((d / "env.yaml").read_text(encoding="utf-8")) or {}
# Top-level conda deps only: skip the nested {"pip": [...]} mapping, and strip
# any version constraint ("python=3.13" -> "python").
declared = {re.split(r"[=<>!~ ]", str(x), maxsplit=1)[0].strip().lower()
            for x in env.get("dependencies", []) if isinstance(x, str)}
lock = yaml.safe_load((d / "conda-lock.yml").read_text(encoding="utf-8")) or {}
locked = {str(p.get("name", "")).lower() for p in (lock.get("package") or [])}
missing = sorted(declared - locked)
if missing:
    sys.exit(f"!! {d}/conda-lock.yml is STALE — it does not contain {missing}.\n"
             f"!! The Dockerfile PREFERS the lock, so it would build without them and\n"
             f"!! tag the image as if they were there.\n"
             f"!! Install conda-lock and re-run, or delete {d}/conda-lock.yml to build from env.yaml.")
print(f">> lock for {d} covers all {len(declared)} declared dependencies")
PY
  fi
}

build() {  # $1 = dir, $2 = tag
  local dir="$1" tag="$2"
  solve_lock "$dir"
  echo ">> building $tag from $dir (base: $MAMBA_BASE)"
  docker build --build-arg "MICROMAMBA_BASE=$MAMBA_BASE" -t "$tag" "$dir"
}

if [ "$WHICH" = "all" ] || [ "$WHICH" = "py" ]; then build images/base-py "$PY_TAG"; fi
if [ "$WHICH" = "all" ] || [ "$WHICH" = "r" ];  then build images/base-r  "$R_TAG"; fi
if [ "$WHICH" = "all" ] || [ "$WHICH" = "controller" ]; then
  # The controller COPYs pyproject/src/config, so it needs the repo-root context.
  echo ">> building $CTRL_TAG from repo root"
  docker build -f images/controller/Dockerfile -t "$CTRL_TAG" .
fi

echo "done. locally built images have no pullable registry digest — the committed"
echo "conda-lock.yml next to each Dockerfile is the reproducibility pin. (Only add"
echo "@sha256 digests to pins.yaml for images actually pushed/pulled, e.g. after"
echo "'docker pull'; see the micromamba_base comment.)"
