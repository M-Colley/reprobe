# Changelog

## Unreleased — dependency & data provisioning

Real submissions failed for two mundane reasons: a needed R package wasn't in the
base image, and LFS-tracked datasets were never pulled. Both are now handled while
keeping the author analysis offline — the network stays in the sanctioned phases.

- **CRAN R packages installed automatically.** Detection statically discovers the
  R packages a repo needs (`library()`/`require()`/`requireNamespace()`/`pkg::`
  in `.R`/`.Rmd`/R-kernel notebooks, plus `DESCRIPTION` Imports/Depends), and
  authors may also list them under `environment.r_packages` in the manifest. The
  **CRAN-available, not-already-present** subset is installed into `R_LIBS_USER`
  during the existing egress **install phase**; the author run still executes with
  `--network none`. Packages not on CRAN (e.g. a private package) are reported,
  never faked. Versions are pinned to a dated CRAN snapshot
  (`r.cran_snapshot` in `pins.yaml`, a Posit P3M date) for year-over-year
  reproducibility. Discovered names are validated to `[A-Za-z][A-Za-z0-9.]*` and
  the whole `install.packages()` call rides inside one single-quoted `-e`
  argument, so a hostile `library()` name cannot inject shell or R code.
- **Author-declared datasets downloaded.** The manifest `data[]` array
  (`{path, source, checksum}`) is now consumed: each http(s) `source` is fetched
  into the run tree before the offline analysis, reusing `download()`'s byte cap,
  path containment, and checksum honesty, behind a hardened **SSRF guard** — the
  host must resolve to a public IP (loopback / link-local / `169.254.169.254` /
  RFC1918 and IPv4-mapped-IPv6 wrappers all refused), redirects are followed
  manually and re-checked per hop, and the validated IP is **pinned** for the
  connect so a name can't DNS-rebind to an internal address between check and use.
  Author data does not strengthen the Available badge.
- **Opt-in, hardened git-LFS (`--allow-lfs`).** Off by default (skip-smudge
  stays). When enabled, LFS data is pulled on the host through `run_git` (so
  `GIT_ALLOW_PROTOCOL` fences transports; `GIT_CONFIG_NOSYSTEM=1` now also set).
  The committed `.lfsconfig` is **neutralized** (renamed) before the pull rather
  than key-denylisted — git-lfs honors many endpoint/exec keys from it
  (`lfs.url`, `remote.*.lfsurl`, `lfs.pushurl`, custom transfer agents, and on old
  git-lfs `credential.helper`/`core.*`), so removing it and letting git-lfs derive
  the endpoint from the already-validated origin URL closes all of them at once.
  The origin host is re-checked public, and the aggregate declared LFS payload is
  byte-capped (git-lfs bypasses `download()`'s per-file cap). LFS objects are
  content-addressed, so pulled data is reproducibly pinned by the commit.
  (Residual: git-lfs still trusts the origin server's batch-API object hrefs —
  run `--allow-lfs` only on repos whose git host you trust.)

## Unreleased — security & robustness hardening

Findings from a full code review; the trust boundary is the fetch/detect stages
that handle untrusted submitter input on the host, *before* any container exists.

- **Git-clone host RCE closed (critical).** A crafted repo ref could select
  git's `ext::` remote-helper transport and run a command on the trusted
  orchestrator host. `run_git` now sets `GIT_ALLOW_PROTOCOL=http:https:git:ssh`
  (blocking `ext::`/`fd::`/`file::`), the git fetcher rejects refs that look like
  an option (`-…`) or carry a `::` transport marker or a non-network scheme, and
  the `clone` argv gets an explicit `--` end-of-options guard. Hostile-input
  tests added.
- **Host-DoS bounds on fetch.** `download()` enforces a byte cap (and rejects an
  oversized `Content-Length`); archive extraction rejects decompression bombs
  (declared-uncompressed-size and member-count caps) for both zip and tar; the
  anonymous-GitHub fetcher streams to disk instead of buffering the whole
  response in RAM.
- **Dataverse SSRF narrowed.** Dispatch now matches the actual host, not the
  substring `dataverse` appearing anywhere in the ref (which let an internal
  address route through the download path).
- **Untrusted-tree scan hardened.** Detection walks the fetched repo with
  `followlinks=False` (no symlink loops/escapes) and a file-count cap, so a
  hostile deposit can't hang the harness.
- **Manifest schema now ships in the wheel.** The JSON Schema moved under
  `reprobe/schemas/` and loads via `importlib.resources`; a non-editable install
  previously skipped schema validation of untrusted manifests silently.
- **Smaller hardening:** advisory-LLM `summarize()` now fences the report like
  the other roles; `badges.csv` neutralizes spreadsheet formula injection;
  `_read_refs` strips a UTF-8 BOM and no longer crashes on an empty CSV; the
  manifest `dependencies` filename is shell-quoted before the install phase; the
  Markdown report neutralizes raw HTML in untrusted free-text; log tails are read
  from a bounded window instead of loading the whole (uncapped) log into memory;
  digest pinning records every base image used in a mixed python+R run.

## 0.2.0 — chair quality-of-life wave

- **`reprobe pull`:** one-command new-machine bootstrap — pulls the pinned
  base images + smoke image from pins.yaml (controller stays build-only). The
  GitHub Action now uses it, and sets `REPROBE_CONFIG_DIR` to its own checkout
  (a non-editable pip install carries no `config/`, so doctor/run on a stock
  runner previously could not resolve pins.yaml).
- **Non-code artifact classification:** detection now inventories video /
  audio / dataset / document / 3D-model files (the AutoUI submission-form
  categories). A media-only deposit reads as what it is instead of
  "artifact types: (none)"; `artifact_types`, reports, the detect CLI, and a
  new dashboard **Types** column carry it. Advisory only — never affects badge
  decisions, never schedules a step; Unity project trees are excluded (engine
  assets are not research artifacts).
- **`reprobe batch --resume`:** reuses finished per-submission reports and
  retries only `fetch-failed` / `infra-error` ones — an interrupted season no
  longer restarts from zero.
- **`badges.csv`:** flat per-submission export (badges, FAIR, verdict, types,
  triage flags) next to `badges.json`, for spreadsheet reconciliation.
- **Feedback for authors:** every `report.md` now includes a copy-pasteable
  block after the badge section, restating only machine-checked facts.
- **Golden-report regression:** `reprobe doctor --golden` replays the bundled
  fixtures through the dry-run pipeline and diffs a machine-independent slice
  against `tests/golden/expected.json` (regenerate intentionally via
  `python -m reprobe.golden --update`); enforced in CI by the test suite. This
  ships the DESIGN §11 Phase-5 item.
- **Doctor digest advisories:** non-failing warnings when
  `base_images.micromamba_base` / `llm.ollama_image` are tag-only (pins.yaml
  asks for `@sha256:` digests after first pull).
- **Dashboard `no-checksum` triage flag** now actually emitted (the runbook
  documented it; the code never set it); flag logic shared with `badges.csv`.

## 0.1.1 — fix wave (post-review hardening)

- **Version stamped:** `__version__`/pyproject bumped to 0.1.1 (reports embed
  `harness_version`; it previously still said 0.1.0).
- **`doctor --smoke` exit code is now honest:** a failed smoke test fails the
  command (it previously exited 0). The pins-declared smoke image is pulled
  automatically when absent, so the smoke works on a fresh machine; author-code
  images are still never auto-pulled.
- **GitHub Action pulls the pinned base images** (read from the action's own
  `pins.yaml`) before `reprobe doctor` — on a stock runner doctor previously
  always failed.
- **image-not-present guidance:** doctor and the sandbox error now suggest
  `docker pull <published image>` first; building locally is the alternative.
- **DESIGN.md drift fixes:** §9.2 wrongly listed a bare commit SHA as an
  archival pin (code/config/README correctly treat it as candidate-only); the
  Action usage example still pointed at the old `autoui` namespace.

- **Python 3.13 is the default runtime** (was 3.11): `base-py` env, controller
  image, `fetch.fallback_python_image`, and the GitHub Action.
- **Harness now requires Python ≥ 3.11** (`requires-python`, was `>=3.10`): drops
  the oldest interpreter for the chair's orchestrator and moves the floor toward
  the safe `tarfile.extractall(filter="data")` backport (the manual member
  validator still covers 3.11.0–3.11.3).
- **GitHub Action:** installs reprobe from the action's own checkout
  (`$GITHUB_ACTION_PATH`), never the caller's workspace (which may hold an
  untrusted submission); `reprobe doctor` failures now fail the job instead of
  being swallowed by `|| true`.
- **docker-compose:** Ollama image pinned (new `llm.ollama_image` key in
  pins.yaml) instead of `:latest`; model pull parameterized via
  `REPROBE_LLM_MODEL` (default kept in sync with `llm.model`); model-pull now
  waits on an Ollama healthcheck instead of racing container start.
- **images/build-images.sh:** builds the controller image (repo-root context);
  no longer tells the chair to record local `.Id` digests in pins.yaml — the
  committed `conda-lock.yml` is the reproducibility pin for local builds.
- **CI:** new `.github/workflows/test.yml` runs the daemon-free unit suite on
  Python 3.11/3.13.
- **Docs truth pass:** removed the false "CI rebuilds images" claim (manual
  `images/build-images.sh` until the Phase-2 workflow ships); marked
  `reprobe unity-refresh` as Phase 3 / not implemented; trimmed dashboard
  triage flags to what the code emits (`warnings`, `anonymized`,
  `no-checksum`); README model name corrected to Gemma 4 (e4b); DESIGN §10 pin
  keys now match pins.yaml; runbook warns that controller mode's Docker-socket
  mount is host-root-equivalent.
- **Config honesty:** `limits.yaml` `allow_net_hosts` now states plainly that
  it is declared but not yet enforced.

## 0.1.0 — MVP (Phase 0 + Phase 1)

Initial scaffold of the AutoUI open-data reproducibility harness.

- **Safety substrate:** single `docker_exec` chokepoint; full sandbox envelope
  (`--network none`, non-root, `--cap-drop ALL`, `--read-only`, tmpfs, pids/mem/cpu
  caps, host-side hard timeout). Chokepoint enforced by a lint test.
- **Pipeline:** fetch → detect → plan-env → run → assess → report orchestrator.
- **Fetchers:** git hosts, Zenodo, figshare, Dryad, OSF (+ view-only tokens),
  Dataverse, Software Heritage (SWHID/vault), anonymous.4open.science, local
  paths, and resolvable DOIs (doi.org follow + re-dispatch).
- **Detection:** deterministic signatures + `autoui-repro.yml` / `codecheck.yml`
  manifest; LLM proposes alternative ordering only when ambiguous (advisory).
- **Runners:** python, jupyter (papermill/nbconvert), r, rmarkdown, unity (T0
  structural). Pluggable via entry points.
- **Local LLM:** Ollama + Gemma 4 (e4b), advisory roles only, data-in/JSON-out,
  `--no-llm` fully functional.
- **Reporting:** JSON + Markdown + single-file HTML; ACM/FAIR badge mapping
  (grant Available only, propose Functional); batch dashboard.
- **Reusability:** all knobs in `config/*.yaml`; `pins.yaml` is the yearly edit.
- Test suite: sandbox flags, detection, badges, guard, chokepoint, rendering.

### Not yet (see docs/DESIGN.md §11)
Phase 2: repo2docker fallback, FAIR refinements. Real base-image build (heavy
DS/R stacks). Unity T1/T2.
Phase 3: Unity T1 compile / T2 build (needs a Unity seat).
Phase 4/5: LLM polish, `doctor` golden-fixture regression, base-image publishing.
