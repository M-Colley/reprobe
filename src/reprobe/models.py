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

RunStatus = Literal["pass", "fail", "partial", "skipped", "error", "timeout"]


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


class EnvPlan(BaseModel):
    strategy: Literal["pinned-base", "repo2docker", "author-image", "besteffort"] = "pinned-base"
    image: str
    env_provenance: Literal["author-specified", "harness-default", "repo2docker-built",
                            "fallback-generic"] = "harness-default"
    install_commands: list[str] = Field(default_factory=list)   # run in a gated-egress phase
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
    source: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)   # pins + config hashes for later re-runs
    environment: dict[str, Any] = Field(default_factory=dict)
    detect: dict[str, Any] = Field(default_factory=dict)
    steps: list[RunResult] = Field(default_factory=list)
    unity: Optional[dict[str, Any]] = None
    llm: dict[str, Any] = Field(default_factory=dict)
    badges: dict[str, Any] = Field(default_factory=dict)
    not_verified: list[str] = Field(default_factory=list)
    verdict: dict[str, Any] = Field(default_factory=dict)
