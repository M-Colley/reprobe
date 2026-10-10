"""Core data contracts shared across the whole harness.

Everything that ends up in a report is a pydantic model so it serializes
cleanly and validates at the boundary. ``ContainerSpec`` is the request a
runner makes; ``docker_exec`` clamps it to ``config/limits.yaml`` before any
author code runs (runner proposes, orchestrator disposes).
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Artifact kinds a runner can claim. Extend by shipping a runner plugin, not by
# editing this list — it is advisory, used for routing and reporting only.
ArtifactKind = Literal["python", "jupyter", "r", "rmarkdown", "unity", "custom"]

# "timeout" and "resource-limit" are the sandbox's budget running out (wall
# clock; memory / output / disk), not the artifact failing — reported apart from
# "fail" so an analysis that needs more than a review laptop is not called broken.
RunStatus = Literal["pass", "fail", "partial", "skipped", "error", "timeout", "resource-limit"]


# --------------------------------------------------------------------------- #
# Execution request / result
# --------------------------------------------------------------------------- #
class Mount(BaseModel):
    source: str          # host path
    target: str          # container path
    read_only: bool = True


class ContainerSpec(BaseModel):
    """A runner's *request* to run something. Immutable; the orchestrator
    re-derives mounts/network/user from policy and never trusts these blindly."""

    model_config = ConfigDict(frozen=True)

    image: str
    command: list[str]
    workdir: str = "/work"
    mounts: list[Mount] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    network: Literal["none", "egress"] = "none"
    needs_license: bool = False
    timeout_s: Optional[int] = None     # None -> use limits default for this runner


class RawRunOutput(BaseModel):
    """What docker_exec hands back after running a container. Pure facts."""

    exit_code: Optional[int]
    duration_s: float
    timed_out: bool = False
    log_path: Optional[str] = None
    image: Optional[str] = None
    argv_redacted: list[str] = Field(default_factory=list)
    error: Optional[str] = None         # harness-side error (e.g. image missing)
    # The kernel OOM-killed a process at the --memory cap (docker inspect
    # State.OOMKilled). None = could not be determined (daemon gone, dry run).
    oom_killed: Optional[bool] = None
    # The host-side watchdog stopped the container for exceeding a budget that is
    # not wall-clock: "output" (log bytes) or "disk" (bytes written under /work).
    limit_exceeded: Optional[str] = None
    dry_run: bool = False


class Capabilities(BaseModel):
    """What a runner CAN and CANNOT verify. Feeds report honesty so a green
    check is never silently over-read."""

    needs_network: bool = False
    requires_secret: bool = False
    can_verify: list[str] = Field(default_factory=list)
    cannot_verify: list[str] = Field(default_factory=list)


class RunStep(BaseModel):
    """One unit of work routed to a runner."""

    runner: str = ""                    # runner id; "" means "let detection decide"
    target: str                         # path relative to src (or project dir)
    kind: ArtifactKind = "custom"
    args: dict[str, Any] = Field(default_factory=dict)        # runner-specific options
    argv: list[str] = Field(default_factory=list)            # command-line args passed to the script
    expected_outputs: list[str] = Field(default_factory=list)
    # True when expected_outputs were BROADCAST from the manifest onto this step
    # rather than declared by the step itself. In a multi-step pipeline each step
    # legitimately produces only its share, so a step must not be marked
    # "partial" for failing to produce a peer's output — completeness is judged
    # pipeline-wide (see report/badges.py).
    outputs_inherited: bool = False
    description: Optional[str] = None
    primary: bool = True                # primary steps gate the Functional candidate


class RunResult(BaseModel):
    runner: str
    target: str
    status: RunStatus
    executed: bool = True               # False for host-only tiers (no author code ran)
    tier_reached: Optional[str] = None
    exit_code: Optional[int] = None
    duration_s: float = 0.0
    log_path: Optional[str] = None
    artifacts: list[str] = Field(default_factory=list)
    expected_met: list[str] = Field(default_factory=list)
    claims: list[str] = Field(default_factory=list)
    not_verified: list[str] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Fetch / detect / env
# --------------------------------------------------------------------------- #
class Pin(BaseModel):
    kind: Literal["version_doi", "git_sha", "swhid", "git_tag", "none"] = "none"
    value: str = ""


class FetchResult(BaseModel):
    input: str
    resolved_type: str                  # git | zenodo | osf | local | anonymous_github | ...
    src_dir: str
    pin: Pin = Field(default_factory=Pin)
    fetch_layer: str = "native"         # which fallback layer succeeded
    anonymized: bool = False
    checksum_verified: bool = False
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DetectResult(BaseModel):
    artifact_types: list[str] = Field(default_factory=list)
    inventory: dict[str, int] = Field(default_factory=dict)   # non-code files by category
    steps: list[RunStep] = Field(default_factory=list)
    manifest_path: Optional[str] = None
    # Root-level LICENSE/COPYING file, and the first recognized dependency
    # manifest, if any. Both feed the FAIR "reusable" score, which previously
    # relied on fetcher metadata that most fetchers never populate.
    license_file: Optional[str] = None
    dep_manifest: Optional[str] = None
    run_plan_source: Literal["manifest", "llm", "heuristic"] = "heuristic"
    llm_confidence: Optional[float] = None
    flags: list[str] = Field(default_factory=list)   # e.g. "needs-repo2docker"
    notes: list[str] = Field(default_factory=list)
    # R package names statically discovered in the source (library()/require()/
    # pkg::/DESCRIPTION). Advisory input to the env planner, which installs the
    # CRAN-available subset in the sanctioned egress phase — never authority.
    r_packages: list[str] = Field(default_factory=list)
    py_packages: list[str] = Field(default_factory=list)


class EnvPlan(BaseModel):
    strategy: Literal["pinned-base", "repo2docker", "author-image", "besteffort"] = "pinned-base"
    image: str
    env_provenance: Literal["author-specified", "harness-default", "repo2docker-built",
                            "fallback-generic"] = "harness-default"
    install_commands: list[str] = Field(default_factory=list)   # run in a gated-egress phase
    # Parallel to install_commands: who asked for each command. A failed install
    # is the ARTIFACT's finding when its own declared file does not install, and
    # the harness's when something reprobe chose to add does not.
    #   "author-declared"  — requirements.txt / environment.yml / renv.lock / install.R list
    #   "harness-detected" — packages reprobe inferred from imports / library() calls
    #   "harness-record"   — pip freeze / installed.packages() / micromamba list
    install_origins: list[str] = Field(default_factory=list)
    # Prefix of a conda env the install phase built from the artifact's own
    # environment.yml. When set, author code runs with THAT interpreter, not the
    # base image's.
    conda_env_prefix: Optional[str] = None
    base_image_digest: Optional[str] = None
    resolved_deps_digest: Optional[str] = None
    repo2docker_version: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
class Report(BaseModel):
    schema_version: str = "1.0"
    submission_id: str
    harness_version: str
    timestamp: str
    # "full" | "dry-run" (containers planned and checked, none launched) |
    # "no-run" (--no-run: fetch + detect only). Recorded so no consumer, and no
    # `batch --resume`, can mistake a plan for a result.
    run_mode: str = "full"
    source: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)   # pins + config hashes for later re-runs
    environment: dict[str, Any] = Field(default_factory=dict)
    detect: dict[str, Any] = Field(default_factory=dict)
    steps: list[RunResult] = Field(default_factory=list)
    unity: Optional[dict[str, Any]] = None
    llm: dict[str, Any] = Field(default_factory=dict)
    badges: dict[str, Any] = Field(default_factory=dict)
    not_verified: list[str] = Field(default_factory=list)
    # Concrete changes the author can make, gathered from every step's diagnosis
    # into one ordered list. The verdict says WHETHER an artifact ran; this says
    # WHAT TO DO about it, which is the part an author acts on — and scattered
    # one-per-step through a long report, it was being missed.
    fix_list: list[dict[str, Any]] = Field(default_factory=list)
    # Committed outputs the run regenerated, compared file by file against the
    # authors' copies (see compare.py). Evidence for the human reviewer only —
    # it never sets results_reproduced.
    results_comparison: Optional[dict[str, Any]] = None
    verdict: dict[str, Any] = Field(default_factory=dict)
