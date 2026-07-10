#!/usr/bin/env bash
# Build (and tag) the reprobe base images from config/pins.yaml.
# Re-solves conda-lock.yml so the base is reproducible from the lock alone.
#
#   bash images/build-images.sh            # build py + r
#   bash images/build-images.sh py         # build only python base
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
WHICH="${1:-all}"

solve_lock() {  # $1 = images/base-xx dir
  local dir="$1"
  if command -v conda-lock >/dev/null 2>&1; then
    echo ">> solving lock for $dir"
    conda-lock lock -f "$dir/env.yaml" -p linux-64 --lockfile "$dir/conda-lock.yml" || \
      echo "!! conda-lock failed; image will install from env.yaml (less reproducible)"
  else
    echo "!! conda-lock not installed; image will install from env.yaml. (pip install conda-lock for reproducible bases)"
  fi
}

build() {  # $1 = dir, $2 = tag
  local dir="$1" tag="$2"
  solve_lock "$dir"
  echo ">> building $tag from $dir (base: $MAMBA_BASE)"
  docker build --build-arg "MICROMAMBA_BASE=$MAMBA_BASE" -t "$tag" "$dir"
}

[ "$WHICH" = "all" ] || [ "$WHICH" = "py" ] && build images/base-py "$PY_TAG"
[ "$WHICH" = "all" ] || [ "$WHICH" = "r" ]  && build images/base-r  "$R_TAG"

echo "done. record the new digests in config/pins.yaml:"
for t in "$PY_TAG" "$R_TAG"; do
  docker image inspect "$t" --format '  {{index .RepoTags 0}}  ->  {{.Id}}' 2>/dev/null || true
done
