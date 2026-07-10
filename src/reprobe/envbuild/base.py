"""Environment planning.

Decision rule (see docs/DESIGN.md §3):

    author manifest pins an image        -> use it as-is        [author-image]
    repo needs only the known sci stack  -> reprobe-base-* + overlay   [pinned-base]  (default)
    --allow-repo2docker and repo complex -> repo2docker build   [repo2docker]
    otherwise                            -> pinned-base best-effort + warn

For the MVP the pinned-base path is implemented; repo2docker is a documented
Phase-2 hook. The report always records ``env_provenance`` so a reviewer knows
whether the environment was author-specified or harness-default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import Config
from ..models import DetectResult, EnvPlan


def _needs(kinds: set[str], *want: str) -> bool:
    return any(k in kinds for k in want)


def plan(
    detect_result: DetectResult,
    manifest_meta: dict[str, Any],
    config: Config,
    src_dir: str | Path,
    *,
    allow_repo2docker: bool = False,
) -> EnvPlan:
    src = Path(src_dir)
    env = (manifest_meta or {}).get("environment", {}) or {}
    kinds = set(detect_result.artifact_types)

    # 1) author pinned an image
    if env.get("image"):
        return EnvPlan(strategy="author-image", image=str(env["image"]),
                       env_provenance="author-specified")

    # choose default base image: python stack unless purely R
    py_needed = _needs(kinds, "python", "jupyter")
    r_needed = _needs(kinds, "r", "rmarkdown")
    image_key = "python" if py_needed or not r_needed else "r"
    image = config.base_image(image_key) or config.pins.get("fetch", {}).get("fallback_python_image", "")

    install = _install_commands(env, src, r_needed)

    flags = detect_result.flags
    if "needs-repo2docker" in flags and allow_repo2docker:
        # Phase 2: hand off to repo2docker_builder. For now, record intent.
        return EnvPlan(strategy="repo2docker", image=image, env_provenance="repo2docker-built",
                       install_commands=install, repo2docker_version=config.pins.get("tools", {}).get("repo2docker"),
                       warnings=["repo2docker builder is a Phase-2 hook; using pinned base best-effort"])

    strategy = "pinned-base"
    warnings = []
    if "needs-repo2docker" in flags:
        strategy = "besteffort"
        warnings.append("repo declares system-level setup (Dockerfile/postBuild/apt.txt); "
                        "pinned base may lack some deps. Re-run with --allow-repo2docker for fidelity.")

    return EnvPlan(strategy=strategy, image=image, env_provenance="harness-default",
                   install_commands=install, warnings=warnings)


def _install_commands(env: dict[str, Any], src: Path, r_needed: bool) -> list[str]:
    cmds: list[str] = []
    dep = env.get("dependencies")
    # explicit manifest dependency file
    if dep and (src / dep).exists():
        if str(dep).endswith(".txt"):
            cmds.append(f"pip install --no-input --target=/work/.reprobe_deps -r {dep}")
        elif str(dep).endswith("renv.lock"):
            cmds.append("Rscript -e 'renv::restore(prompt=FALSE)'")
    else:
        # auto-detect common manifests (repo2docker-style conventions)
        if (src / "requirements.txt").is_file():
            cmds.append("pip install --no-input --target=/work/.reprobe_deps -r requirements.txt")
        if (src / "renv.lock").is_file() and r_needed:
            cmds.append("Rscript -e 'if (requireNamespace(\"renv\", quietly=TRUE)) renv::restore(prompt=FALSE)'")
        elif (src / "install.R").is_file():
            cmds.append("Rscript install.R")
    return cmds
