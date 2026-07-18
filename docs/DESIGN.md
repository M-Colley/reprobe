<!--
  Full recommended design for `reprobe`, synthesized from a research + design
  workflow (repo2docker / ACM badging / sandboxing / GameCI-Unity / local-LLM /
  archive-fetching). README.md has the short version; this file is the reference.
  Note: where the text says "the diagram above", see the trust-boundary diagram
  in the project chat (or regenerate it from §1).
-->

# AutoUI Open-Data Reproducibility Harness — Recommended Design

**Codename:** `reprobe` · **Audience:** AutoUI Open Data / Open Science chair + maintainers · **Status:** Approved recommended design (synthesis of Proposals A "reuse-max" and B "controllable-core")

## 0. Verdict up front

The two proposals agree on ~70% of the design (host orchestrator owning the Docker socket, ephemeral per-run sandboxed containers, pluggable fetcher + runner contracts, tiered license-honest Unity, advisory-only data-in/JSON-out LLM, config-driven annual maintenance). They genuinely diverge on **one axis: who owns the environment build.**

- **Proposal A:** make `repo2docker` the spine; ship a micromamba base only as fallback.
- **Proposal B:** own two pinned micromamba base images as the default; keep `repo2docker` as an opt-in fallback behind a stable interface.

**Recommendation: adopt Proposal B's env-build stance (own pinned bases, repo2docker as opt-in fallback) as the default, but adopt A's reuse-maximalist instinct everywhere the build is *not* the trust boundary** — fetcher, notebook execution (papermill), Unity (GameCI), LLM (Ollama). Rationale below in §3. Everything else is a merge of the strongest concrete elements of both (A's `doctor --refresh-unity`, A's explicit `not_verified[]` field, A's `--allow-net` escape hatch; B's `conda-lock.yml` reproducibility, B's `ContainerSpec` "runner proposes, orchestrator disposes" clamp, B's single `pins.yaml`, B's gVisor toggle).

The product name `reprobe` (from A) is retained; B's internal module structure is largely retained.

---

## 1. Architecture and data flow

A single **host orchestrator** (`reprobe`, Python) is the only process with Docker-socket access. It runs a 6-stage pipeline and **never executes author code in-process**. Untrusted author analysis code runs *only* inside ephemeral, network-isolated, non-root, resource-capped runtime containers. The orchestrator and runtime container communicate solely through a bind-mounted work dir plus the container's exit code and captured logs. (See the trust-boundary diagram above.)

```
submission (URL | DOI | anon link, + optional secret token)
  └─(1) FETCH      network ON, code NOT run   → work/<sub>/src + fetch_manifest.json
    (2) DETECT     deterministic scan; LLM advisory → artifact_plan.json
    (3) PLAN ENV   resolve image/lock         → image ref + resolved-deps digest
    (4) RUN        ephemeral container per step, --network none by default
    (5) ASSESS     exit codes / logs / outputs → run_results.json
    (6) LLM ASSIST advisory only              → llm_notes.json
    (7) REPORT                                → report.json + .md + .html
  └─ BATCH AGGREGATE                          → out/dashboard.html + badges.json
```

### Deployment modes (resolving A's vs B's "controller container")

| Mode | Where CLI runs | When to use |
|---|---|---|
| **`docker-host` (default)** | Native on host, talks to local Docker socket | Chair running batches on a laptop/workstation. Simplest. |
| **`controller-container` (opt-in)** | CLI in a `reprobe-controller` image with `/var/run/docker.sock` mounted | GitHub Actions / self-hosted CI runner. |

Both proposals independently converged here, including the critical detail: **Docker-out-of-Docker (sibling containers via the mounted socket), never Docker-in-Docker.** DooD reuses the host daemon, caches images across runs, and lets the host kernel enforce all sandbox limits. This is the recommended pattern. **Trade-off:** mounting the socket grants the controller root-equivalent power over the host daemon — acceptable because the controller is *our* trusted orchestrator, not author code. Switch to a dedicated VM-per-run only if the institution's threat model forbids socket mounting entirely (then pay the per-run VM boot cost).

---

## 2. Repository layout (final)

```
reprobe/
├── README.md  LICENSE(MIT)  CHANGELOG.md
├── pyproject.toml                # package "reprobe"; console_scripts: reprobe=reprobe.cli:main
├── reprobe.lock                  # uv/pip-tools lock of the HARNESS's own deps
│
├── config/                       # ◄ everything a future chair edits lives here
│   ├── pins.yaml                 # THE ONE FILE: all image tags/digests + model + tool versions
│   ├── runners.yaml              # runner registry: id → plugin + default image + enabled
│   ├── limits.yaml               # cpu/mem/pids/timeout/network defaults + per-runner overrides
│   └── badges.yaml               # declarative ACM badge + FAIR decision rules
│
├── images/                       # WE OWN THESE (default runtimes)
│   ├── base-py/{Dockerfile, env.yaml, conda-lock.yml}   # micromamba py stack, solved lock
│   ├── base-r/{Dockerfile,  env.yaml, conda-lock.yml}   # micromamba R stack, solved lock
│   ├── controller/Dockerfile     # orchestrator + docker-cli, for CI (DooD)
│   └── build-images.sh           # builds & tags all base images from pins.yaml
│
├── src/reprobe/
│   ├── cli.py                    # run | detect | batch | doctor | version (unity-refresh is Phase 3)
│   ├── orchestrator.py           # the 6-stage state machine; owns work dirs
│   ├── config.py  models.py      # pydantic: Submission, RunStep, RunResult, Report, ContainerSpec
│   ├── docker_exec.py            # ◄ SINGLE chokepoint that shells out to `docker run` (§7)
│   │
│   ├── fetch/                    # base.py registry.py doi.py git_host.py
│   │   ├── zenodo.py osf.py figshare.py dryad.py dataverse.py
│   │   ├── software_heritage.py  anonymous_github.py
│   ├── detect/                   # detector.py signatures.py manifest.py
│   ├── envbuild/                 # base.py pinned_base.py repo2docker_builder.py lock.py
│   ├── runners/                  # ◄ THE PLUGIN SURFACE (§4)
│   │   ├── base.py registry.py
│   │   ├── python_script.py r_script.py jupyter.py rmarkdown.py unity.py
│   ├── llm/                      # client.py prompts.py schemas.py guard.py
│   └── report/                   # schema.py badges.py markdown.py html.py dashboard.py
│
├── runner-scripts/               # tiny scripts COPIED INTO runtime containers (no install)
│   ├── run_python.sh run_jupyter.py run_r.sh run_rmarkdown.R unity_compile_check.cs
│
├── schemas/                      # autoui-repro.schema.json  report.schema.json
├── deploy/
│   ├── docker-compose.yml        # ollama service + private network + named volumes
│   └── github-action/action.yml  # composite action wrapping `reprobe run`
├── examples/                     # fixtures, incl. jupyter-xgb-catboost-rf-shap (the target)
└── tests/  unit/ integration/ fixtures/   # good + deliberately-broken artifacts; golden reports
```

The single most important file is **`config/pins.yaml`** (§10).

---

## 3. Container strategy — the one real disagreement, resolved

**Recommended default: own two pinned micromamba base images (`reprobe-base-py`, `reprobe-base-r`); keep `repo2docker` as an opt-in fallback behind the EnvBuilder interface.** (Proposal B's stance wins as default.)

### Why B's stance, not A's

The deciding factor is the chair's explicit, dominant requirement: **reusable year-over-year with minimal maintenance, and runs untrusted code safely.** repo2docker is superb at the *hard* part (dependency resolution from heterogeneous author manifests), and A is right that this is the single highest-maintenance subsystem to hand-roll. But making it the *spine* concedes three things B correctly refuses to concede:

1. **Reproducibility of the base.** repo2docker builds are non-hermetic and its base/opinions drift across releases; A mitigates with `--base-image` digest pinning but still inherits repo2docker's build logic year-over-year. B's solved `conda-lock.yml` makes the default base **byte-reproducible from the lock alone** — a stronger "Artifact Available years later" story, which is the harness's own reason to exist.
2. **The sandbox seam.** repo2docker couples build+run and bakes its own user/entrypoint conventions. Routing every author execution through *our* `docker_exec` chokepoint is cleaner when *we* control the default image's user (uid is known), filesystem layout (`/work`), and the fact that no network/socket is present. B's "the base is immutable; author deps install into a per-run overlay during a network-gated install phase, then run with `--network none`" is the more auditable model.
3. **Upstream-churn isolation.** Both GameCI and repo2docker are volunteer/best-effort. B's design degrades *one runner/strategy* when an upstream breaks, rather than the spine.

### Why we still keep A's reuse instinct

A's core point — *don't maintain dependency resolution forever* — is honored by keeping `repo2docker` as a first-class, pinned, opt-in builder for exactly the repos the bases can't serve (postBuild, apt.txt, Pipfile, Julia, exotic stacks). We are not reinventing dependency resolution; we are choosing it as a strategy, not a spine.

### EnvBuilder decision rule

```
if manifest.environment.image (pinned digest):  use it as-is            [author-image]
elif repo needs only the known scientific stack: reprobe-base-* + overlay [pinned-base]  ← DEFAULT
elif --allow-repo2docker and repo is complex:    repo2docker build        [repo2docker]
else:                                            pinned-base best-effort + warn [pinned-base-besteffort]
```

The report always records `env_provenance` so reviewers know whether the environment was author-specified, harness-default, or repo2docker-built.

**When I'd switch the default to A (repo2docker-first):** if the AutoUI submission corpus turns out to be dominated by repos with bespoke `apt.txt`/`postBuild`/system-library needs that the two bases can't cover, the per-submission overlay-install failure rate will exceed the cost of just letting repo2docker build. Measure this in Year 1 (the report's `env_provenance` distribution tells you directly); flip the default rule ordering if `pinned-base-besteffort` failures dominate.

### Base image sketch (`images/base-py/Dockerfile`)

```dockerfile
FROM mambaorg/micromamba:1.5.10-noble@sha256:<pinned>   # digest in pins.yaml
ARG MAMBA_DOCKERFILE_ACTIVATE=1
COPY --chown=$MAMBA_USER:$MAMBA_USER env.yaml conda-lock.yml /tmp/
RUN micromamba install -y -n base -f /tmp/conda-lock.yml && micromamba clean -ay
USER $MAMBA_USER          # non-root by default (uid 57439)
WORKDIR /work
```

`base-py` carries the AutoUI-typical stack pinned: `python=3.13, jupyter, nbconvert, papermill, numpy, pandas, scikit-learn, xgboost, catboost, lightgbm, shap, matplotlib, seaborn, statsmodels`. `base-r` mirrors via micromamba-managed `r-base=4.4.*, r-tidyverse, r-rmarkdown, r-knitr, r-renv, pandoc` (one solver governs both stacks). Images are tagged `ghcr.io/m-colley/reprobe-base-py:2026.1` (year.rev), digest recorded in every report. Built/cached images and Unity `Library/` live in named volumes for fast re-runs; digests still recorded so caching can't mask a change.

---

## 4. Runner plugin contract (final)

New artifact types are added by dropping a plugin registered under a Python `entry_points` group — **the orchestrator never changes.** The contract merges B's "runner proposes a `ContainerSpec`, the orchestrator disposes and hardens it" with A's explicit `claims[]` / `not_verified[]` honesty fields.

```python
# runners/base.py
@dataclass(frozen=True)
class ContainerSpec:
    image: str                    # MUST resolve to a pins.yaml tag/digest or a built image
    command: list[str]
    workdir: str = "/work"
    mounts: list[Mount] = ()      # orchestrator FORCES src=ro, _out=rw regardless of request
    env: dict[str, str] = ()      # no secrets unless capabilities().requires_secret is set
    network: str = "none"         # "egress" only honored if capabilities().needs_network
    needs_license: bool = False   # Unity T1/T2 → orchestrator verifies creds present
    timeout_s: int = 1800

class RunResult(BaseModel):
    runner: str; target: str
    status: Literal["pass","fail","partial","skipped","error"]
    tier_reached: str | None      # for tiered runners (Unity); else None
    exit_code: int | None; duration_s: float
    log_path: Path; artifacts: list[Path]
    expected_met: list[str]       # which declared expected_outputs were produced
    claims: list[str]             # honest, machine-checkable claims
    not_verified: list[str]       # explicit "this was NOT checked"
    diagnostics: dict             # structured failure info handed to the LLM

class Runner(Protocol):
    id: str; display_name: str; handles_types: frozenset[str]
    def can_handle(self, step: RunStep) -> bool: ...
    def container_spec(self, step, ctx) -> ContainerSpec:
        """PURE. No side effects, no code execution, no network. Proposes a spec."""
    def interpret(self, raw, ctx) -> RunResult:
        """Map exit code + logs + produced files → structured RunResult."""
    def capabilities(self) -> Capabilities:
        """Declares what this runner CAN and CANNOT verify (feeds report honesty)."""
```

**Guarantees enforced by the orchestrator, not the plugin:**
- All execution goes through the injected `docker_exec`; a runner cannot choose its own flags, open network, escalate privileges, or mount the socket. It only *requests* via `ContainerSpec`; `docker_exec` clamps every field to `config/limits.yaml`. A runner requesting `network=egress` without `capabilities().needs_network` is denied.
- `container_spec()` and `can_handle()` never execute author analysis code.
- The **reporter** owns badge mapping; runners never claim badges.
- A lint test greps for `subprocess`/`docker` outside `docker_exec.py` to enforce the chokepoint.

**Discovery / adding a runner (Julia, Node/Playwright, MATLAB) with zero core edits:**
```toml
[project.entry-points."reprobe.runners"]
julia = "reprobe_julia:JuliaRunner"
```
A third party can ship a runner as a separate pip package plus a `runners.yaml` entry.

---

## 5. Unity runner — tiered and license-honest (both proposals agree; merged)

Reports **which tier it reached**, never a bare pass/fail.

| Tier | License? | Action | Claim |
|---|---|---|---|
| **T0 Structural** | none, always on | Detect `Assets/`, `ProjectSettings/`, `Packages/manifest.json`; parse `ProjectVersion.txt` → `m_EditorVersion`; confirm a matching `unityci/editor` tag exists; sanity-check manifest/asmdefs/scenes; flag committed `Library/` bloat. | "Unity project detected; targets X.Y; matching editor image exists." |
| **T1 Compile** | institution's own seat | `unityci/editor:<ver>` running our injected `unity_compile_check.cs` (forces compile, `EditorApplication.Exit(1)` on `compilationHadFailure`); assert exit 0. | "Scripts compile under Unity X.Y." |
| **T2 Build** | institution's own seat | `game-ci/unity-builder@v4` (pinned `v4.8.x`), `targetPlatform=StandaloneLinux64` (opt. WebGL); inspect **BuildReport + engineExitCode**, never process exit alone. | "A Linux player builds." |
| **Tests** | seat | `unity-test-runner@v4` EditMode/PlayMode if present → NUnit XML. | "Tests present and passed." |

**Version→image resolution is first-class:** read `ProjectVersion.txt`, look up an **exact** `unityci/editor:{version}-{module}-{imageVersion}` tag (digest-pinned in `pins.yaml`). **No exact image → fail cleanly** (`no-matching-editor-image`); never silently substitute a wrong version.

**Licensing (hard config boundary):** T1/T2 are **OFF** unless the reviewing institution supplies **its own** `UNITY_EMAIL`/`UNITY_PASSWORD`/`UNITY_SERIAL` (Pro/Plus) or a **Unity Licensing Server** endpoint. Never use submitters' credentials. Pro/Plus licenses are **returned after every run** (concurrency limits); a Licensing Server pool is recommended for batches; the harness errors loudly if return fails. The Unity container is the *only* runtime container allowed a brief credentialed egress window (activation), then dropped. Per-submission hard timeout (default 35 min) kills import/PlayMode hangs; images + `Library/` cached.

**Honest `not_verified` statement (verbatim in report):** `-nographics` + `-batchmode` means no rendering, no input, no interactivity, no VR/AR, no device behavior. A green compile/build does **not** mean the interactive prototype works. Reported claims are limited to: *project detected · version matched · compiles · Linux player builds · tests present/passed*. "Launches and is interactive" is **out of scope** and listed under `not_verified`.

---

## 6. Local-LLM integration — advisory, structurally inert

**Model: Gemma 4 (e4b — effective-4B edge build), served by Ollama**, pinned by digest in `pins.yaml`, CPU-only acceptable (GPU via compose override). Long-lived sidecar `ollama` service on a private Docker network; the host CLI talks to it at `http://127.0.0.1:11434`. **It has no Docker socket, no shell, no file-write, no network to runtime containers.** Fully offline after the initial model pull; `OLLAMA_KEEP_ALIVE` keeps it warm across a batch.

**Three bounded roles only** (`llm/prompts.py`, temperature 0, `format: json`, hard token + 60 s wall-clock caps, validated against `llm/schemas.py`):

1. **`detect_run_order`** — *only when no manifest exists.* In: file tree (paths) + README excerpt + detected manifests. Out: `{steps:[{path,why}], confidence, uncertain[], notes}`. The deterministic detector's ordering is the **default**; the LLM ordering is shown as an alternative for the chair.
2. **`diagnose_failure`** — In: failing step + truncated stderr + env summary. Out: `{likely_cause, suggested_fixes[], confidence, is_advisory:true}`.
3. **`summarize`** — In: the finished `report.json` (facts only). Out: a plain-language paragraph. It summarizes facts the harness already computed; it cannot change a badge.

**The hard "never silently runs code" guarantee is structural, not a prompt instruction:**
- The LLM client returns **data only**. There is no tool/function-calling surface wired to it — it physically cannot execute anything.
- LLM output is **advisory-only** — nothing it returns is ever applied, at any confidence. The llm package enforces `pins.yaml llm.confidence_threshold` by stamping below-threshold advice `meets_threshold: false` ("shown for transparency only, never applied"), and run-order suggestions are validated against the real file tree before being surfaced.
- `llm/guard.py` validates every response against the passive schema and rejects anything with shell/exec strings outside declared fields. Every LLM-derived report statement is labeled `source: "llm-advisory"` with its confidence, visually distinct from harness-verified facts.
- `--no-llm` makes the harness **fully functional and deterministic** with the LLM off (detection falls back to `signatures.py`; reports lose only the narrative summary). Golden tests must pass with the LLM disabled — proving the LLM is non-load-bearing.

---

## 7. Safety / sandboxing (concrete, final)

Every author-code container is launched through `docker_exec.py` with this non-negotiable flag set (defaults in `config/limits.yaml`):

```bash
docker run --rm \
  --network none \                       # default: NO network during execution
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=512m \
  --user 57439:57439 \                   # non-root; reject default-uid-0 images unless overridden
  --cap-drop ALL --security-opt no-new-privileges \
  --security-opt seccomp=default \
  --pids-limit 512 \
  --memory 8g --memory-swap 8g \         # no swap blowout
  --cpus 4 --ulimit nofile=4096 --ulimit nproc=512 \
  -v "$SRC:/work:ro" \                   # source read-only
  -v "$OUT:/work/_out:rw" \              # only outputs dir writable
  --stop-timeout 30 \
  <image> <cmd>
```

Wrapped by a host-side **hard timeout** that `docker kill`s and records `status=timeout`.

**Network policy.** Default `--network none`. Sanctioned, explicit, time-boxed exceptions only: (a) **dependency-install phase** runs in a *separate* container with egress, then the actual run is `--network none` (recorded as a distinct phase); (b) **Unity activation** egress, then dropped; (c) per-submission opt-in `--allow-net <host>...` egress allowlist (A's escape hatch) for artifacts that must download data at runtime — this **downgrades badge confidence** and is flagged loudly. Default behavior when code attempts network under `none`: it fails fast and the report says `needs-network` rather than silently allowing egress.

**Other guarantees:** no Docker socket in any runtime container; no host bind beyond the per-submission work dir; never `--privileged`; secrets passed via env to the *right* container only, never logged; outputs size-capped; logs captured to host files, never streamed into the orchestrator's eval path. **Optional `--runtime=runsc` (gVisor) toggle** in `limits.yaml` (from B) for institutions wanting kernel-level isolation of untrusted code.

---

## 8. Author manifest convention (final)

Define a minimal, **optional** `autoui-repro.yml` (JSON-Schema-validated at `schemas/autoui-repro.schema.json`), and **also read `codecheck.yml`** if present (mapped onto our fields) so existing CODECHECK submissions work unchanged. Authors are never *required* to write anything — deterministic auto-detect is the fallback; a manifest removes all guesswork and the LLM from the loop.

```yaml
version: 1
title: "Feature ranking for takeover readiness (SHAP)"
environment:
  builder: pinned-base            # pinned-base | repo2docker | author-image
  type: python                    # python | r | jupyter | unity | custom
  dependencies: requirements.txt  # or environment.yml / renv.lock
  image: null                     # optional pinned digest to use as-is
data:
  - { path: data/driving.csv, source: "10.5281/zenodo.123456" }   # fetched + checksum-verified
run:
  steps:                          # explicit order beats LLM/auto-detect
    - notebooks/01_prepare.ipynb
    - notebooks/02_rank_xgb_shap.ipynb
    - { tool: unity, project: prototype/, tier: compile }
  network: false                  # default false → sandboxed offline
expected_outputs: [figures/shap_summary.png, results/ranking.csv]
badges_claimed: [available, functional]    # author's claim; harness verifies
```

**Auto-detect fallback (`detect/signatures.py`, deterministic, always runs first):** `*.ipynb`→jupyter (order: manifest > numeric filename prefixes > README mentions > mtime); `requirements.txt`/`environment.yml`/`Pipfile`→python env; `*.R`/`renv.lock`/`DESCRIPTION`→r_script, `*.Rmd`→rmarkdown; `Assets/`+`ProjectVersion.txt`→unity; `Dockerfile`/`postBuild`/`apt.txt`→flag for `--allow-repo2docker`. If order is ambiguous and `--no-llm` is unset, the LLM proposes an ordering shown **alongside** the deterministic one. The detector emits `artifact_plan.json` regardless.

---

## 9. Output

### 9.1 Report schema (`schemas/report.schema.json`, versioned)

```jsonc
{
  "schema_version": "1.0",
  "submission_id": "sub-2026-0042",
  "harness_version": "reprobe 1.0.0",
  "timestamp": "2026-06-27T...Z",
  "source": {
    "input": "https://doi.org/10.5281/zenodo.123456",
    "resolved": { "type": "zenodo", "version_doi": "...", "record_id": "123456" },
    "pin": { "kind": "version_doi|git_sha|swhid", "value": "..." },
    "fetch_layer": "native_api",          // which fallback layer succeeded
    "anonymized": false, "checksum_verified": true,
    "warnings": []                         // lfs-incomplete | embargoed-until-DATE | needs-auth | ...
  },
  "environment": {
    "strategy": "pinned-base",            // pinned-base | repo2docker | author-image | besteffort
    "image": "ghcr.io/m-colley/reprobe-base-py:2026.1@sha256:...",
    "base_image_digest": "sha256:...", "resolved_deps_digest": "...",
    "env_provenance": "author-specified", "repo2docker_version": null
  },
  "detect": { "artifact_types": ["jupyter"], "manifest": "autoui-repro.yml",
              "run_plan_source": "manifest|llm|heuristic", "llm_confidence": null },
  "steps": [ /* RunResult[]: status, tier_reached, exit_code, duration_s,
                produced[], expected_met[], claims[], not_verified[], source */ ],
  "unity": { "tier_reached": "structural", "version_detected": "6000.0.23f1",
             "image_matched": true, "compiles": null, "player_builds": null,
             "not_verified": ["rendering","input","interactivity","VR/AR"] },
  "llm": { "summary": "...", "diagnoses": [], "source": "llm-advisory", "model": "gemma4:e4b" },
  "badges": {
    "acm": { "available": "granted", "functional": "candidate", "results_reproduced": "not-evaluated" },
    "fair": { "findable": true, "accessible": true, "interoperable": "partial", "reusable": "partial" }
  },
  "not_verified": ["interactive Unity behavior", "GPU code paths"],
  "verdict": { "overall": "runs-with-warnings", "human_review_required": true }
}
```

### 9.2 Badge mapping (`config/badges.yaml`, declarative)

- **ACM Artifact Available** (only strictly-required badge): granted **deterministically, no code execution** iff fetch succeeded + the source is archivally pinned (version DOI / SWHID — **not** a bare commit SHA, concept DOI, or moving tag; a commit SHA is reproducibly pinned but not archival, so it yields a *candidate*) + checksum verified where the platform provides one.
- **ACM Artifact Evaluated – Functional**: marked **candidate** (not auto-granted) when declared/primary steps `status=pass` and ≥1 `expected_output` is produced. Value-add; a human confirms.
- **ACM Results Reproduced**: **not auto-granted** (requires comparing produced vs published results); the harness surfaces produced artifacts to aid the human.
- **FAIR**: scored from fetch metadata (persistent ID, open license, standard formats, manifest presence).

Stance, stated plainly in `docs/badges.md`: **the harness grants Available, proposes Functional, assists everything deeper.** Over-claiming is the cardinal sin of an automated repro checker. Rules are data, so a future chair tunes thresholds without code changes.

### 9.3 Human-readable + batch + CI

- Per-submission `report.md` + single-file `report.html`: badge chips, per-step table (claims vs `not_verified`), inline figures, logs, and the LLM summary clearly labeled *advisory*, with a "what was NOT checked" box.
- `reprobe batch submissions.csv` → per-submission reports + static `out/dashboard.html` (sortable: submission, source, badges, verdict) with a `badges.json` export. The dashboard flags emitted today are `warnings`, `anonymized`, and `no-checksum`; richer flags (`needs-auth`, `embargoed-until-DATE`, `lfs-incomplete`, `no-matching-editor-image`, `needs-network`) are planned, not yet emitted.
- **GitHub Actions:** `deploy/github-action/action.yml` installs reprobe on the runner host from the action's own checkout and drives the host Docker daemon (sibling sandboxed containers); authors drop it in their repo for self-checks, the chair runs it as a batch matrix. `.github/workflows/test.yml` runs the deterministic unit suite on Python 3.11/3.13. A `build-base-images.yml` workflow that rebuilds + pushes `reprobe-base-*` to GHCR is planned (Phase 2); until then base images are built manually with `images/build-images.sh`.

```yaml
- uses: m-colley/reprobe/deploy/github-action@main
  with: { submission: "${{ inputs.url }}", tiers: "structural,compile", llm: "true" }
  env:  { GITHUB_TOKEN: "${{ secrets.GITHUB_TOKEN }}" }   # UNITY_* only if the org supplies its own seat
```

---

## 10. Reusability / annual-maintenance playbook

**The yearly job is editing data, not code.** A future chair touches `config/`, never `src/`, in the common case.

**What changes per year (in likelihood order):**
1. **`config/pins.yaml`** — the one file. Bump: `base_images.python` / `base_images.r` (`2027.1`), the `base_images.micromamba_base` digest, `llm.ollama_image`, `llm.model` (`gemma4:e4b`), `tools.repo2docker`, and the `unity` block (`image_repo`, `default_image_version`, `known_tags`).
2. **Rebuild base images** whenever the `base_images.*` tags changed (edit `images/base-*/env.yaml` only if the science stack moved): run `images/build-images.sh` manually (re-solves `conda-lock.yml`, rebuilds, tags `2027.x`). There is no CI image-build workflow yet.
3. **Refresh the `unityci/editor` tag map** — the one list that genuinely ages. `reprobe unity-refresh` (Phase 3, not yet implemented — skip until then) will query Docker Hub and update the digest-pinned tag map for the chair to review and commit.
4. **`config/badges.yaml` / `config/limits.yaml`** — if ACM thresholds or policy (timeouts, caps, default tiers) change.
5. **Secrets** for the year (Unity seat / Licensing Server endpoint; archive tokens) via env — never committed.

**What stays fixed (no annual work):** orchestrator, runner contract, sandbox flags, fetcher plugins, report schema, LLM prompts. New artifact types arrive as **new plugin packages** (entry points), not core edits. Fetcher plugins change only if a *platform* changes its API (rare, isolated to one file).

**Guardrails:** `reprobe doctor` self-checks the whole config on `tests/fixtures/` (a known-good Python/R/Jupyter artifact + a deliberately-broken one), asserts all images are pullable, the lock solves, Ollama is reachable, Unity tags resolve, and golden reports still match — so a chair confirms "it still works" in one command before each review window. Everything is digest-pinned where possible, so an old report reproduces years later. GameCI/repo2docker are treated as **best-effort upstreams** behind our interface: a break degrades one runner/strategy with a clean error, not the harness (the specific labels `no-matching-editor-image` / `repo2docker-build-failed` land with the Phase-2/3 runners).

**Trade-off, stated honestly:** we carry ~2 base-image Dockerfiles + a small orchestrator to maintain. In return there is no annual "the framework changed under us" rebuild — the yearly task collapses to bumping `pins.yaml` and occasionally re-solving a lock.

---

## 11. Phased build plan

**Phase 0 — Safety substrate (1 wk).** `pyproject.toml`, `models.py`, `config.py`, `docker_exec.py` with the full sandbox flag set, report schema, fixtures, `reprobe doctor`. Build `reprobe-base-py` from a committed `conda-lock.yml`. No runners yet. *Exit:* `reprobe run` launches a hello-world container with every safety flag and emits an empty `report.json`.

**Phase 1 — MVP: Python/R/Jupyter (2–3 wks). ← first working value.**
- Fetch: `git_host` (clone + full-SHA pin + LFS skip-smudge) + generic DOI front door + Zenodo.
- Detect: deterministic signatures + `autoui-repro.yml`/`codecheck.yml` parsing.
- EnvBuild: `pinned_base` + network-gated overlay install (and `base-r`).
- Runners: `python_script`, `jupyter` (papermill `--execute`), `r_script`, `rmarkdown`.
- Report: `report.json` + `.md`/`.html`; **grant Available, propose Functional.**
- **Crisp MVP definition / acceptance test:** `reprobe run <github-url>` on the **XGBoost/CatBoost/RF + SHAP notebook repo** fetches + pins it, builds the pinned-base env, executes the notebooks end-to-end offline, detects `figures/shap_summary.png` as a produced expected output, and emits a correct report granting **Artifact Available** and proposing **Functional** — the dominant submission type and the only required badge, covered before any Unity or LLM complexity.

**Phase 2 — Breadth + batch + CI (1–2 wks).** Remaining fetchers (OSF + view_only, figshare, Dryad, Dataverse, Software Heritage, anonymous.4open.science). Batch mode + `dashboard.html`. Reusable GitHub Action + base-image build workflow. `repo2docker` fallback behind `--allow-repo2docker`. FAIR scoring.

**Phase 3 — Unity (2 wks).** T0 structural first (no license — ships and is useful immediately). Version→image resolution + `unity-refresh`. T1 compile + T2 Linux-player build behind the license boundary, with caching, timeouts, the `no-matching-editor-image` clean failure, and the honest `not_verified` wording wired into the report.

**Phase 4 — LLM polish (1 wk).** Ollama sidecar + Gemma 4 (e4b). Wire the three bounded roles + `guard.py` + `--no-llm`; render LLM output as clearly-labeled, confidence-scored advice (advisory-only — never applied). Confirm golden tests pass **with the LLM disabled** (proves the value-add is non-load-bearing).

**Phase 5 — Reuse seal (≤1 wk).** `chair-runbook.md`, `doctor` polish, a pin-only "next year" dry run, tag `v1.0` + base images `2026.1`.

**Build-order rationale:** safety substrate → the most common artifact (Jupyter ML) for the required badge → breadth → the expensive/license-bound Unity path → advisory LLM last, because the LLM must never sit on the critical path or the trust boundary.

---

## 12. Open questions for the chair (human-owner decisions only)

1. **Unity tiers — institutional license commitment.** Will the institution dedicate its *own* Unity Pro/Plus seat(s) or stand up a Unity Licensing Server pool to enable T1/T2? If not, Unity validation is T0-structural only (legally clean, ships free). This is a budget + legal-seat decision, not an engineering one.
2. **How much "Functional" to promise reviewers.** Does AutoUI want the harness to *grant* Functional automatically when checks pass, or only ever mark it **candidate** for a human (the recommended default)? This sets the over-claiming risk posture for the conference.
3. **Runtime-network policy for data-downloading artifacts.** Allow per-submission `--allow-net <host>` egress (with a badge-confidence downgrade), or hard-require offline-runnable artifacts and report `needs-network` as a non-pass? This is a submission-policy call to communicate to authors.
4. **Compute/storage budget for batch validation.** Unity images are multi-GB per version and cold runs can take 30+ min; the full Functional path runs author code per submission. What per-season time/storage budget and per-submission hard timeout does the chair authorize, and is there a self-hosted runner with Docker available?
5. **Default env-build strategy if Year-1 data favors repo2docker.** If `env_provenance` shows pinned-base best-effort failing often, do we flip the default to repo2docker-first (Proposal A)? The chair should decide the criterion (e.g. ">X% best-effort failures → switch").
6. **Anonymized / embargoed submissions.** Policy for `anonymous.4open.science` links that may expire mid-review and Zenodo embargoed/restricted records: does the chair supply reviewer secret tokens/view-only links, and how should the report treat `embargoed-until-DATE` — block, defer, or pass-with-flag?
7. **Secret custody.** Who holds and rotates the Unity serial / Licensing Server endpoint and archive tokens (`ZENODO_TOKEN`, `OSF_TOKEN`, etc.), and where do they live in the CI/secret store?

---

### Summary of opinionated trade-offs
- **Own the base images + run loop; rent the build (repo2docker) only as opt-in fallback** — more code than A, far less upstream-churn risk and tighter sandboxing; revisit if Year-1 corpus data favors repo2docker-first.
- **`--network none` by default**, with a flagged per-submission egress allowlist as the only escape hatch.
- **LLM is data-only, off the trust boundary and off the critical path** — "never silently runs code" is an architectural property, not a promise.
- **Tiered Unity reporting a tier, never pass/fail**, with explicit `not_verified` so "it compiles" can't be read as "the demo works"; T0 ships license-free.
- **Grant only Available automatically; everything deeper is a verified candidate for a human.**
- **The yearly job is editing `pins.yaml`,** not maintaining a framework.