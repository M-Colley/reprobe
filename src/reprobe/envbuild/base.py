"""Environment planning.

Decision rule (see docs/DESIGN.md §3):

    author manifest pins an image        -> use it as-is        [author-image]
    repo needs only the known sci stack  -> reprobe-base-* + overlay   [pinned-base]  (default)
    --allow-repo2docker and repo complex -> repo2docker build   [repo2docker]
    otherwise                            -> pinned-base best-effort + warn

The pinned-base path is the only one implemented. ``--allow-repo2docker`` is
accepted and says plainly that no repo2docker build happens; a plan never claims
a builder that did not run. The report always records ``env_provenance`` so a
reviewer knows whether the environment was author-specified or harness-default.

THE INSTALL PHASE HAS NETWORK EGRESS, so what it is asked to run is decided here:

* the artifact's own code is never executed — no ``install.R`` (its package lists
  are read statically instead), no ``-e .``, no ``git+``/URL requirements;
* the artifact's dependency files are reduced to plain package requirements
  (``sanitize.py``), and the sanitised text is what is embedded in the command;
* every harness Python runs ``-I`` (cwd and PYTHONPATH off ``sys.path``, so a
  committed ``pip.py`` or ``sitecustomize.py`` is never imported), every Rscript
  runs ``--vanilla`` (no author ``.Rprofile``/``.Renviron``);
* each command carries an ORIGIN — ``author-declared`` (their file),
  ``harness-detected`` (an import or ``library()`` reprobe inferred) or
  ``harness-record`` (a version listing) — so a failed install is attributed to
  whoever asked for it.

What still runs with network is third-party package build code (an sdist's
``setup.py``, a conda post-link script) from the public indexes the plan allows.
"""

from __future__ import annotations

import re
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from ..config import Config
from ..docker_exec import docker_available, image_present
from ..models import DetectResult, EnvPlan
from .sanitize import (
    DEFAULT_CONDA_CHANNELS,
    MAX_EMBED_BYTES,
    classify_renv_lock,
    embed_file_command,
    filter_requirement_lines,
    filter_requirements,
    inside_tree,
    sanitize_conda_env,
)

# Origins of an install command (see the module docstring).
AUTHOR = "author-declared"
DETECTED = "harness-detected"
RECORD = "harness-record"

#: Where the sanitised copies of the artifact's dependency files are written in
#: the install container. Never read from the artifact's own tree.
_REQ_DIR = "/work/.reprobe_req"
#: micromamba/pip find user config under $HOME, and HOME is /work — the artifact's
#: own directory. A separate, empty HOME keeps an author's .condarc/.mambarc from
#: choosing channels for the build.
INSTALL_HOME = "/var/tmp/reprobe-home"


#: What renv's generated activate.R pins: `version <- "1.2.3"`. Only a plain
#: version number is accepted — it is put into R source in the install phase.
_ACTIVATE_VERSION_RE = re.compile(r'^\s*version\s*<-\s*"(\d+(?:\.\d+){1,3}(?:-\d+)?)"', re.M)


def _renv_activation(src: Path) -> Optional[str]:
    """None if the project does not activate renv; else the renv version its
    activate.R asks for ("" when it names none we accept).

    Activation means the project's .Rprofile sources renv/activate.R, so every
    step's R session runs on renv's project library — and activate.R loads renv
    ITSELF from that library, downloading it when absent, which the offline run
    cannot do."""
    prof, act = src / ".Rprofile", src / "renv" / "activate.R"
    if not inside_tree(src, prof) or not prof.is_file():
        return None
    try:
        if "renv/activate.R" not in prof.read_text(encoding="utf-8", errors="replace")[:20000]:
            return None
        text = act.read_text(encoding="utf-8", errors="replace")[:4000] \
            if inside_tree(src, act) and act.is_file() else ""
    except OSError:
        return None
    m = _ACTIVATE_VERSION_RE.search(text)
    return m.group(1) if m else ""


def _activates_renv(src: Path) -> bool:
    return _renv_activation(src) is not None


def _needs(kinds: set[str], *want: str) -> bool:
    return any(k in kinds for k in want)


# Same invocation for manifest-declared and auto-detected lockfiles: guarded so
# an image without renv fails loudly (non-zero -> install noted failed/skipped)
# instead of a cryptic traceback or a silent "ok".
def _renv_restore_command(cran_repo: str, excluded: list[str],
                          activation: Optional[str] = None) -> str:
    """Restore an renv.lock from the pinned CRAN snapshot, minus every package the
    lockfile sources from somewhere else (GitHub, a URL, a local path).

    ``--vanilla``: the project's ``.Rprofile`` (renv's activate.R, plus whatever
    else the author put there) is not sourced during the network phase. Without
    activation renv restores into the FIRST library on the path — the harness's
    R_LIBS_USER, which the run phase also puts on the path. That is right for a
    lockfile alone, and wrong for a project that activates renv (``activation``
    not None, see ``_renv_activation``): its activate.R puts only the project
    library and the system library on the path, so every restored package would
    be invisible ("there is no package called ...", read as the artifact's
    fault). Such a project is restored into the library activate.R uses, and
    renv itself — at the version activate.R asks for — is installed there too:
    activate.R loads renv from that library and, finding it absent, tries to
    download it, which the offline run cannot. Verified against renv 1.3.1."""
    repo = cran_repo or "https://cloud.r-project.org"
    excl = "c(" + ", ".join(f'"{n}"' for n in excluded) + ")" if excluded else "NULL"
    repos = f'c(CRAN=\"{repo}\")'
    # Restored from a clean project holding only a copy of the lockfile: the
    # artifact's own renv/ folder (settings.json can add library paths, a
    # profile file switches the lockfile) must not steer a step that has network.
    # The target library is named explicitly either way.
    lib = ("renv::paths$library(project=getwd())" if activation is not None
           else 'Sys.getenv(\"R_LIBS_USER\")')
    body = ("{ p <- file.path(tempdir(), \"reprobe-renv\"); dir.create(p, showWarnings=FALSE); "
            "stopifnot(file.copy(\"renv.lock\", file.path(p, \"renv.lock\"), overwrite=TRUE)); "
            f"lib <- {lib}; dir.create(lib, recursive=TRUE, showWarnings=FALSE); "
            f"renv::restore(project=p, library=lib, prompt=FALSE, repos={repos}, exclude={excl})")
    if activation is not None:
        want = f'\"{activation}\"' if activation else 'as.character(packageVersion(\"renv\"))'
        body += (f"; v <- {want}; have <- tryCatch(as.character(packageVersion(\"renv\", lib.loc=lib)), "
                 "error=function(e) \"\"); "
                 f"if (!identical(have, v)) renv::install(paste0(\"renv@\", v), project=p, library=lib, "
                 f"repos={repos}, prompt=FALSE)")
    body += " }"
    return ("Rscript --vanilla -e 'if (requireNamespace(\"renv\", quietly=TRUE)) "
            f"{body} "
            "else { message(\"renv not preinstalled in image; renv.lock NOT restored\"); quit(status=3) }'")


_R_VERSIONS = "Rscript --vanilla -e 'print(installed.packages()[, c(\"Package\", \"Version\")])'"


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
        # Posit Package Manager decides source-vs-binary on the User-Agent as
        # well as the `__linux__/<codename>` path, and R's default UA names no
        # distribution — so the pinned snapshot served SOURCE tarballs and every
        # R artifact rebuilt its whole tree. roads-chi25-data (2026-08-13) spent
        # the full 2-hour install budget compiling brms/rstan/lme4 and was
        # reported infra-error. This is the UA from Posit's own setup snippet; it
        # is inert against a plain CRAN mirror, which ignores it.
        'options(HTTPUserAgent = sprintf("R/%s R (%s)", getRversion(), '
        'paste(getRversion(), R.version$platform, R.version$arch, R.version$os))); '
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
    return "Rscript --vanilla -e '" + r_code + "'"


def _py(conda_prefix: Optional[str]) -> str:
    """The interpreter the harness itself runs in the install phase: ``-I`` keeps
    the artifact's directory (cwd) and PYTHONPATH off ``sys.path``, so a committed
    ``pip.py``, ``subprocess.py`` or ``sitecustomize.py`` is never imported by
    the one process in the pipeline that has network."""
    return f"{conda_prefix}/bin/python -I" if conda_prefix else "python -I"


def _py_detected_install_command(dists: list[str], conda_prefix: Optional[str]) -> str:
    """Install statically-detected imports the environment does not already ship.

    The Python twin of ``_cran_install_command``, and it follows the same rules:
    the presence check happens INSIDE the container at install time, so nothing
    already provided by the base image (or by the artifact's own built env) is
    touched; and a name PyPI does not have is *reported*, never faked and never
    fatal — a mis-detected local module must not fail the phase.

    These names are GUESSES made from import statements, and an unregistered name
    is one anyone can register. So nothing a package ships may RUN while the network
    is open:

    * **wheels only** (``--only-binary=:all:``): no sdist ``setup.py``/build backend;
    * **download first, install once**: ``pip download`` fetches each wheel (and its
      dependency closure) without installing anything, then ONE ``pip install
      --no-index`` installs them all. A wheel's ``.pth`` file is executed at the NEXT
      interpreter start in the environment, so installing package by package (each
      its own pip process) let an earlier squatted package run code in the next pip
      call. A single install leaves no later process in this phase;
    * pip runs from an empty directory, never the artifact's.

    A package that ships no wheel is reported as not installed.

    Distribution names are pre-validated to ``[A-Za-z0-9][A-Za-z0-9._-]*`` and
    module names to an identifier, so embedding them in the double-quoted Python
    literals inside this single-quoted ``-c`` argument cannot inject shell or
    Python code. (The code therefore uses no single quotes.)"""
    from ..detect.signatures import py_module_for_dist

    # An extra gets an empty module name, which the generated code treats as
    # "always offer to pip". A presence check cannot answer for an extra:
    # find_spec("ray.tune") succeeds on the file layout alone, while importing
    # it still raises because ray[tune]'s dependencies are absent — which is the
    # exact failure this mapping exists to fix. pip is idempotent, so offering
    # an already-satisfied extra costs a no-op.
    pairs = [("" if "[" in d else py_module_for_dist(d), d) for d in dists]
    spec = ", ".join(f'("{m}", "{d}")' for m, d in pairs)
    target = ', "--target=/work/.reprobe_deps"' if not conda_prefix else ""
    code = (
        # -I drops PYTHONPATH, so earlier installs into /work/.reprobe_deps are
        # added back explicitly (after the stdlib, never before it). The working
        # directory is moved off the artifact's tree before any pip process starts.
        'import sys, os; sys.path.append("/work/.reprobe_deps"); '
        'os.makedirs("/var/tmp/reprobe-pip", exist_ok=True); os.chdir("/var/tmp/reprobe-pip"); '
        "import importlib.util as u, importlib.metadata as md, subprocess; "
        f"pairs = [{spec}]; "
        'norm = lambda s: s.lower().replace("_", "-"); '
        # Two independent presence checks: the module may be importable without
        # dist metadata (conda packages sometimes are), and the dist may be
        # installed under a name whose import differs from what we guessed.
        'have = {norm(getattr(x, "name", "") or "") for x in md.distributions()}; '
        "need = sorted({d for m, d in pairs "
        "if not m or (u.find_spec(m) is None and norm(d) not in have)}); "
        'print("reprobe: imports the environment does not provide:", need or "none", flush=True); '
        'W = "/var/tmp/reprobe-wheels"; '
        'run = lambda *a: subprocess.call([sys.executable, "-I", "-m", "pip", *a]); '
        'got = [d for d in need if run("download", "--no-input", "--only-binary=:all:", "-d", W, d) == 0]; '
        f'bad = bool(got) and run("install", "--no-input", "--no-index", "--only-binary=:all:", '
        f'"--find-links", W{target}, *got) != 0; '
        'fail = [d for d in need if d not in got] + (got if bad else []); '
        'print("reprobe: pip could NOT install (reported, not faked):", fail or "none")'
    )
    return f"{_py(conda_prefix)} -c '{code}'"


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
    nb_langs = {s.args.get("language", "python") for s in detect_result.steps if s.kind == "jupyter"}
    py_needed = "python" in kinds or "python" in nb_langs
    r_needed = _needs(kinds, "r", "rmarkdown") or "r" in nb_langs
    image_key = "python" if py_needed or not r_needed else "r"
    image = config.base_image(image_key) or config.pins.get("fetch", {}).get("fallback_python_image", "")

    ip = _install_plan(
        env, src, r_needed,
        detected_r_packages=detect_result.r_packages,
        cran_repo=config.cran_repo,
        executes_code=bool(detect_result.steps),
        notebooks=_needs(kinds, "jupyter"),
        py_needed=py_needed,
        detected_py_packages=detect_result.py_packages,
        conda_channels=tuple((config.pins.get("install") or {}).get("conda_channels")
                             or DEFAULT_CONDA_CHANNELS),
        step_dirs=_step_dirs(detect_result))
    install, dep_warnings, conda_prefix = ip.commands, ip.warnings, ip.conda_prefix

    flags = detect_result.flags
    strategy = "pinned-base"
    warnings = []
    if ("needs-repo2docker" in flags or builder == "repo2docker") and allow_repo2docker:
        # Not built — and not claimed. Recording "repo2docker-built" with a
        # repo2docker version here told the report's reader an environment came
        # from a builder that never ran.
        warnings.append("--allow-repo2docker was given, but this build of reprobe has no "
                        "repo2docker builder: nothing was built by repo2docker, and the pinned "
                        "base image was used best-effort")
        strategy = "besteffort"
    elif builder == "repo2docker":
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
                        "pinned base may lack some deps, and reprobe has no repo2docker builder to "
                        "supply them")

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
                   install_commands=install, install_origins=ip.origins,
                   conda_env_prefix=conda_prefix, warnings=warnings + dep_warnings)


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


def _conda_env_commands(src: Path, env_file: str, notebooks: bool,
                        channels: tuple[str, ...]) -> tuple[Optional[str], list[str]]:
    """Build the artifact's declared conda environment with micromamba.

    The pinned base IS a micromamba image, so the environment an artifact
    declared can be built in the same sanctioned egress phase that installs pip
    and CRAN deps. Before this, `environment.yml` was detected, warned about, and
    ignored — and the artifact was then failed for the very import it had
    declared (`ModuleNotFoundError: torch` against an environment.yml listing
    ultralytics).

    The file is SANITISED first (see sanitize.py): channels outside the allow-list,
    URL/path/archive specs and VCS or custom-index lines in its ``pip:`` section are
    dropped and reported, and micromamba reads the sanitised copy this command
    writes — not the author's file. Returns ``(command_or_None, warnings)``."""
    if not inside_tree(src, src / env_file):
        return None, [f"'{env_file}' is outside the artifact (or a symbolic link); the declared "
                      "conda environment was NOT built"]
    try:
        spec = yaml.safe_load((src / env_file).read_text(encoding="utf-8", errors="replace"))
    except (OSError, yaml.YAMLError) as e:
        return None, [f"'{env_file}' could not be read as YAML ({type(e).__name__}); the declared "
                      "conda environment was NOT built"]
    clean, dropped = sanitize_conda_env(spec, channels)
    warnings = _dropped_warnings(env_file, dropped)
    content = yaml.safe_dump(clean, sort_keys=False)
    if len(content.encode("utf-8")) > MAX_EMBED_BYTES:
        return None, warnings + [f"'{env_file}' is too large to embed in the install command; "
                                 "the declared conda environment was NOT built"]
    dest = f"{_REQ_DIR}/environment.yml"
    mm = f"HOME={INSTALL_HOME} micromamba"
    parts = [
        embed_file_command(content, dest),
        f"mkdir -p {INSTALL_HOME} && cd {INSTALL_HOME}",
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
        f"{mm} create -y -q -p {CONDA_ENV_PREFIX} -f {dest}",
    ]
    if notebooks:
        # papermill/ipykernel live in the BASE image, not in the artifact's env.
        # Without them here the notebook runner would find the base image's
        # papermill on PATH and execute the notebook against the wrong
        # interpreter — the one environment.yml exists to replace.
        parts.append(f"{mm} install -y -q -p {CONDA_ENV_PREFIX} -c conda-forge "
                     "papermill ipykernel nbconvert")
    return " && ".join(parts), warnings


def _dropped_warnings(name: str, dropped: list[tuple[str, str]]) -> list[str]:
    """One warning naming what was NOT applied from a dependency file, and why."""
    if not dropped:
        return []
    shown = "; ".join(f"`{line[:70]}` ({why})" for line, why in dropped[:8])
    more = f"; … and {len(dropped) - 8} more" if len(dropped) > 8 else ""
    return [f"{len(dropped)} entr{'y' if len(dropped) == 1 else 'ies'} in '{name}' were NOT applied "
            f"in the install phase, which has network access and so only takes plain package "
            f"requirements from public indexes: {shown}{more}. The environment was built "
            "without them."]


def lint_requirements(path: Path) -> list[str]:
    """Findings readable off requirements.txt WITHOUT installing anything.

    pip resolves a requirements file all-or-nothing: one unsatisfiable line and
    NOTHING installs, so the step then dies on a `ModuleNotFoundError` for some
    unrelated package that was never even attempted. That error names the wrong
    package, and the real cause is a line the author can see.

    Standard-library names are the case worth catching before the install phase
    burns: `itertools` has never been on PyPI and never can be, and listing it is
    a common and invisible mistake — invisible because an author with a working
    environment never installs from the file they shipped."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    findings: list[str] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0].strip().replace("_", "-").lower()
        if name.replace("-", "_") in stdlib:
            findings.append(
                f"requirements.txt line {n} asks pip for `{name}`, which is part of the Python "
                "standard library and has never been on PyPI. pip resolves the file "
                "all-or-nothing, so this one line stops EVERY package in it from installing — "
                "the run then fails on whichever import happens to come first, which names the "
                "wrong package. Delete the line; `import " + name.replace("-", "_") +
                "` works without it.")
    return findings


def _requirements_command(src: Path, req_file: str,
                          conda_prefix: Optional[str]) -> tuple[Optional[str], list[str]]:
    """`pip install -r <sanitised copy>`, into a built conda env when there is one.

    The author's requirements file is filtered (sanitize.filter_requirements): an
    editable install, a ``git+``/URL requirement or a custom index would run the
    author's code, or fetch from the author's server, in a phase with network. What
    remains is embedded in the command and written to ``/work/.reprobe_req/``, so
    pip never reads the author's file. The path is a harness constant; the file
    NAME is never interpolated into the shell. Returns ``(command_or_None, warnings)``."""
    path = src / req_file
    if not inside_tree(src, path):
        return None, [f"'{req_file}' is outside the artifact (or a symbolic link); it was NOT read"]
    kept, dropped = filter_requirements(path, src)
    warnings = _dropped_warnings(req_file, dropped)
    if not kept:
        return None, warnings + ([f"nothing installable remained in '{req_file}'"] if dropped else [])
    content = "\n".join(kept) + "\n"
    if len(content.encode("utf-8")) > MAX_EMBED_BYTES:
        return None, warnings + [f"'{req_file}' is too large to embed in the install command; it was "
                                 "NOT installed"]
    return _pip_install_embedded(content, "requirements.txt", conda_prefix), warnings


def _pip_install_embedded(content: str, name: str, conda_prefix: Optional[str]) -> str:
    """`pip install -r` of requirement lines embedded in the command (never read
    from the artifact's tree), into the conda env when there is one."""
    dest = f"{_REQ_DIR}/{name}"
    target = "" if conda_prefix else " --target=/work/.reprobe_deps"
    # cd first: pip reads a requirement ending in an archive suffix as a file in ITS
    # working directory, which must not be the artifact's tree
    return (f"{embed_file_command(content, dest)} && cd {_REQ_DIR} && "
            f"{_py(conda_prefix)} -m pip install --no-input{target} -r {dest}")


def _pyproject_command(src: Path, rel: str,
                       conda_prefix: Optional[str]) -> tuple[Optional[str], list[str]]:
    """The PEP 621 ``[project].dependencies`` of a pyproject.toml, through the same
    filter as a requirements file. The project itself is never built or installed:
    its build backend is the artifact's code, and this phase has network."""
    path = src / rel
    if not inside_tree(src, path):
        return None, [f"'{rel}' is outside the artifact (or a symbolic link); it was NOT read"]
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (tomllib.TOMLDecodeError, OSError, RecursionError):
        return None, [f"'{rel}' could not be parsed as TOML; its dependencies were NOT installed"]
    proj = data.get("project") if isinstance(data.get("project"), dict) else {}
    warnings: list[str] = []
    tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    if isinstance(tool.get("poetry"), dict) and tool["poetry"].get("dependencies"):
        warnings.append(f"'{rel}' declares [tool.poetry.dependencies], whose constraint syntax is "
                        "Poetry's own; they were NOT installed (only PEP 621 [project].dependencies "
                        "are read)")
    if "dependencies" in (proj.get("dynamic") or []):
        warnings.append(f"'{rel}' computes its dependencies in the build backend (dynamic), which "
                        "is never run in the install phase; they were NOT installed")
    deps = proj.get("dependencies")
    if not isinstance(deps, list) or not deps:
        return None, warnings
    kept, dropped = filter_requirement_lines([str(d) for d in deps])
    warnings += _dropped_warnings(rel, dropped)
    if not kept:
        return None, warnings
    content = "\n".join(kept) + "\n"
    if len(content.encode("utf-8")) > MAX_EMBED_BYTES:
        return None, warnings + [f"'{rel}' dependencies are too large to embed; NOT installed"]
    return _pip_install_embedded(content, "pyproject-requirements.txt", conda_prefix), warnings


def _conda_disclosures(path: Path, notebooks: bool) -> list[str]:
    """What building the env does NOT reproduce. Never let a built environment
    read as a faithful one."""
    out = [
        f"conda file '{path.name}' was built with micromamba into {CONDA_ENV_PREFIX}; Python "
        "steps run with its interpreter, and R steps with its R when it declares one (r-base) — "
        "else with the base image's R. Not reproduced: conda `activate` hooks and any "
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


def describe_install_command(cmd: str) -> str:
    """What an install command DOES, in words an author recognises. The command
    itself embeds a sanitised copy of their file as base64, which is useless in a
    report — and showing a truncated blob read as a harness bug."""
    if "-r /work/.reprobe_req/requirements.txt" in cmd:
        return "pip install -r requirements.txt (the sanitised copy)"
    if "micromamba create" in cmd:
        return "micromamba create from environment.yml (the sanitised copy)"
    if "renv::restore" in cmd:
        return "renv::restore of renv.lock"
    if "install.packages(ok" in cmd:
        pkgs = re.search(r"pkgs <- c\(([^)]*)\)", cmd)
        names = pkgs.group(1).replace('"', "") if pkgs else ""
        return f"install of CRAN packages ({names[:60]}{'…' if len(names) > 60 else ''})"
    if "reprobe: imports the environment" in cmd:
        return "install of the imports found in the code"
    if "pip freeze" in cmd or "micromamba list" in cmd or "installed.packages()[, c(" in cmd:
        return "listing of installed versions"
    return cmd[:60]


@dataclass
class _InstallPlan:
    """The install commands, who asked for each, and what was not applied."""

    commands: list[str] = field(default_factory=list)
    origins: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    conda_prefix: Optional[str] = None

    def add(self, cmd: Optional[str], origin: str) -> None:
        if cmd:
            self.commands.append(cmd)
            self.origins.append(origin)


def _install_commands(env: dict[str, Any], src: Path, r_needed: bool,
                      detected_r_packages: list[str] | tuple[str, ...] = (),
                      cran_repo: str = "",
                      executes_code: bool = False,
                      notebooks: bool = False,
                      py_needed: bool = False,
                      detected_py_packages: list[str] | tuple[str, ...] = (),
                      ) -> tuple[list[str], list[str], Optional[str]]:
    """``(commands, warnings, conda_prefix)`` — see :func:`_install_plan` for the
    per-command origins."""
    ip = _install_plan(env, src, r_needed, detected_r_packages, cran_repo, executes_code,
                       notebooks, py_needed, detected_py_packages)
    return ip.commands, ip.warnings, ip.conda_prefix


def _step_dirs(detect_result: DetectResult) -> tuple[str, ...]:
    """The directories the planned steps live in, nearest first, each followed by
    its parents (never the root itself): where a dependency file kept next to the
    code is looked for when the root has none."""
    out: list[str] = []
    for st in detect_result.steps:
        parts = Path(str(st.target).replace("\\", "/")).parts[:-1]
        for i in range(len(parts), 0, -1):
            d = "/".join(parts[:i])
            if d not in out and ".." not in parts:
                out.append(d)
    return tuple(out)


def _install_plan(env: dict[str, Any], src: Path, r_needed: bool,
                  detected_r_packages: list[str] | tuple[str, ...] = (),
                  cran_repo: str = "",
                  executes_code: bool = False,
                  notebooks: bool = False,
                  py_needed: bool = False,
                  detected_py_packages: list[str] | tuple[str, ...] = (),
                  conda_channels: tuple[str, ...] = DEFAULT_CONDA_CHANNELS,
                  step_dirs: tuple[str, ...] = ()) -> _InstallPlan:
    """Any declared or detected dependency file the pinned-base builder does NOT
    install must surface as a warning — the report says what was not installed
    (never over-claim)."""
    ip = _InstallPlan()
    uses_pip_target = False
    handled = False          # a supported, existing manifest dependency file was applied
    dep = str(env.get("dependencies") or "")
    if dep and not inside_tree(src, src / dep):
        # a manifest path of `../x` or an absolute path names a file on the chair's
        # machine, and its first lines would have been echoed into the report
        ip.warnings.append("manifest declares a dependencies path outside the artifact; it was "
                           "ignored and the artifact's own files were auto-detected instead")
        dep = ""

    def requirements(rel: str, conda_prefix: Optional[str]) -> None:
        nonlocal uses_pip_target
        cmd, w = _requirements_command(src, rel, conda_prefix)
        ip.warnings.extend(w)
        ip.add(cmd, AUTHOR)
        uses_pip_target = uses_pip_target or (cmd is not None and not conda_prefix)

    def conda(rel: str) -> None:
        cmd, w = _conda_env_commands(src, rel, notebooks, conda_channels)
        ip.warnings.extend(w)
        if cmd:
            ip.add(cmd, AUTHOR)
            ip.conda_prefix = CONDA_ENV_PREFIX
            ip.warnings.extend(_conda_disclosures(src / rel, notebooks))

    def renv(rel: str) -> None:
        lock = src / rel
        if not inside_tree(src, lock):
            ip.warnings.append(f"'{rel}' is outside the artifact (or a symbolic link); R "
                               "dependencies were NOT restored from it")
            return
        try:
            text = lock.read_text(encoding="utf-8", errors="replace")
        except OSError:
            ip.warnings.append(f"'{rel}' could not be read; R dependencies were NOT restored from it")
            return
        excluded, notes = classify_renv_lock(text)
        ip.warnings.extend(_dropped_warnings(rel, notes))
        if excluded is None:                    # refused: nothing is restored from it
            return
        ip.add(_renv_restore_command(cran_repo, excluded, _renv_activation(src)), AUTHOR)

    # explicit manifest dependency file
    if dep and not (src / dep).exists():
        ip.warnings.append(f"manifest declares dependencies '{dep}' but the file does not exist; "
                           "auto-detecting instead")
    elif dep.endswith(".txt"):
        handled = True
        requirements(dep, None)
    elif dep.endswith("renv.lock"):
        handled = True
        renv(dep)
    elif dep.endswith((".yml", ".yaml")):
        handled = True
        conda(dep)
        if (src / "requirements.txt").is_file():
            # repo2docker applies both, and so do the authors of repos that ship
            # both. Install it INTO the env — a --target install would land on
            # PYTHONPATH, which precedes site-packages and would shadow the very
            # environment the manifest declared.
            requirements("requirements.txt", ip.conda_prefix)
    elif dep:
        ip.warnings.append(f"declared dependency file '{dep}' is not a supported type "
                           "(requirements.txt | environment.yml | renv.lock); it was NOT installed")
    # Read the file before spending an install phase on it: one unsatisfiable
    # line means nothing installs, and the resulting error names an innocent
    # package. Cheap, offline, and it fires whether or not the file is declared.
    def find(*names: str) -> Optional[str]:
        """A dependency file at the root, else the nearest one beside the code
        (`code/requirements.txt`): ignoring it installed nothing the author
        declared, and then accused the artifact of declaring nothing."""
        for d in ("", *step_dirs):
            for n in names:
                rel = f"{d}/{n}" if d else n
                if (src / rel).is_file() and inside_tree(src, src / rel):
                    return rel
        return None

    req_file = find("requirements.txt")
    if req_file:
        ip.warnings.extend(lint_requirements(src / req_file))
    if not ip.commands and not handled:
        # auto-detect common manifests (repo2docker-style conventions). Conda
        # first: when there is an env to build, pip has to install INTO it.
        conda_file = next((c for c in ("environment.yml", "environment.yaml",
                                       "binder/environment.yml") if (src / c).is_file()), None) \
            or find("environment.yml", "environment.yaml")
        if conda_file:
            conda(conda_file)
        if req_file:
            requirements(req_file, ip.conda_prefix)
        elif (pyproject := find("pyproject.toml")):
            cmd, w = _pyproject_command(src, pyproject, ip.conda_prefix)
            ip.warnings.extend(w)
            if cmd:
                ip.add(cmd, AUTHOR)
                uses_pip_target = uses_pip_target or not ip.conda_prefix
        if (src / "renv.lock").is_file():
            if r_needed:
                renv("renv.lock")
            else:
                ip.warnings.append("renv.lock present but no R steps detected; R dependencies were "
                                   "NOT restored")
        if (src / "install.R").is_file():
            if r_needed:
                # NEVER executed: it is the author's code, and this is the phase with
                # network. Its install.packages() lists were already read statically
                # (they are in the detected R packages below), so the CRAN packages
                # it names are still installed — from the pinned snapshot. Said even
                # when an renv.lock is restored too: it is not run either way.
                ip.warnings.append(
                    "install.R was NOT executed: it is the artifact's own code and the install phase "
                    "has network access. The packages its install.packages() calls name were read "
                    "statically and are installed from the pinned CRAN snapshot instead; anything "
                    "else it does (system setup, GitHub installs, data downloads) was not done")
            else:
                ip.warnings.append("install.R present but no R steps detected; it was NOT run")
    # CRAN packages the base image lacks: author-declared (manifest r_packages)
    # + statically detected library()/require()/pkg:: usages. The install itself
    # picks only the CRAN-available, not-already-present subset (see
    # _cran_install_command) and runs in the sanctioned egress phase; the author
    # analysis still runs offline.
    cran_pkgs = _cran_packages(env, list(detected_r_packages))
    if cran_pkgs:
        if r_needed:
            declared = _cran_packages(env, [])
            detected = [n for n in cran_pkgs if n not in declared]
            if declared:
                ip.add(_cran_install_command(declared, cran_repo), AUTHOR)
            if detected:
                ip.add(_cran_install_command(detected, cran_repo), DETECTED)
            if not cran_repo:
                ip.warnings.append("no r.cran_snapshot pinned in pins.yaml; detected R packages install "
                                   "from a live CRAN mirror (versions not reproducible across time)")
        else:
            ip.warnings.append(f"R packages {cran_pkgs} were declared/detected but no R steps were "
                               "found; they were NOT installed")

    # Python imports the code reaches for but no manifest declares. Same policy
    # as the CRAN block above: install only what the environment lacks, report
    # what PyPI refuses. Without this, an artifact that imports shap without
    # declaring it either died on ModuleNotFoundError or — worse — passed by
    # silently borrowing the package from the harness base image.
    py_pkgs = [str(d) for d in (detected_py_packages or [])]
    if py_pkgs:
        if py_needed:
            ip.add(_py_detected_install_command(py_pkgs, ip.conda_prefix), DETECTED)
            uses_pip_target = uses_pip_target or not ip.conda_prefix
            # State what the install phase was ASKED to do, not what it achieved.
            # This warning is composed while the plan is built, before any
            # container has run — claiming "were installed" here asserted an
            # outcome that a killed install phase then contradicted, in the very
            # report that had to explain the failure.
            ip.warnings.append(
                f"{len(py_pkgs)} Python import(s) were detected statically; the install phase was "
                "asked to add only those the environment does not already provide, as wheels only, "
                "downloaded first and installed in one step (nothing a package ships runs while the "
                "network is open): "
                + ", ".join(py_pkgs[:12]) + ("…" if len(py_pkgs) > 12 else "")
                + ". Anything in that list the artifact's own manifest does not declare is a "
                  "reproducibility defect of the artifact, not of the harness — the install log "
                  "records which were actually missing and whether pip got them.")
        else:
            ip.warnings.append(f"Python packages {py_pkgs} were detected but no Python steps "
                               "were found; they were NOT installed")

    # The silent case, and the most over-claimable one: the artifact declares NO
    # dependencies anywhere. Every library its code imports then comes from
    # whatever the harness base image happens to ship, so a clean run proves the
    # code works *in reprobe's environment* — not that the artifact describes the
    # environment it needs. Without this the report shows an empty warnings list
    # and a green step, which reads as "self-contained and reproducible".
    from ..detect.signatures import DEP_MANIFESTS
    if executes_code and not any((src / d / m).is_file() for d in ("", *step_dirs) for m in DEP_MANIFESTS):
        ip.warnings.append(
            "the artifact declares NO dependency manifest (no requirements.txt, environment.yml, "
            "renv.lock, pyproject.toml, DESCRIPTION, ...). Every library its code uses came from the "
            "harness base image, whose contents are reprobe's choice and change between years — so a "
            "passing step here does NOT show the artifact specifies its own environment, and the same "
            "code may fail on a different base. This is a reproducibility defect of the artifact.")

    # record resolved versions in the install log so two runs are comparable
    if uses_pip_target:
        ip.add(f"{_py(None)} -m pip freeze --path=/work/.reprobe_deps", RECORD)
    if any(c.startswith("Rscript") for c in ip.commands):
        ip.add(_R_VERSIONS, RECORD)
    if ip.conda_prefix:
        ip.add(f"mkdir -p {INSTALL_HOME} && cd {INSTALL_HOME} && "
               f"HOME={INSTALL_HOME} micromamba list -p {CONDA_ENV_PREFIX}", RECORD)   # versions into the log
    return ip
