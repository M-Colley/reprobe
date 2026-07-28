# Changelog

## 0.3.0 — the artifact is not always one repository

### Composite artifacts: code in git, data on OSF

The common shape of a real submission is two deposits: the code in a git repo
and the data in a repository (OSF, Zenodo, Dryad, ...) that the README links to
in prose. reprobe could fetch either half and neither combination — point it at
the code and every step dies at the first `read_csv`; point it at the data and
there is nothing to run. Both outcomes were reported as if they said something
about the artifact.

- **`--data URL[::SUBDIR]` (repeatable) merges a second deposit into the
  artifact tree.** Sources go through the SAME fetcher registry as a submission,
  so OSF / Zenodo / Dryad / figshare / Dataverse / git / local paths all work.
  `::subdir` places the deposit where the code expects it; the default merges at
  the tree root. Merging happens BEFORE detection, so the data is part of the
  inventory, the run plan, and the tree that reaches the container — data that
  appeared only at run time would be missing from every statement the report
  makes about what the artifact contains.
- **Manifest `data_sources:`** declares the same thing permanently, so the
  artifact carries its own data provenance instead of relying on the chair
  knowing. (`data:` is unchanged — it stays the per-file, checksummed form.)
- **A data source can never overwrite the code.** Collisions are skipped and
  named in the report; silently replacing a script would mean the code reviewed
  is not the code submitted.
- **A data source never strengthens Available.** It is author-controlled bytes
  fetched at review time, recorded with its own (usually `none`) pin. The badge
  still follows the primary source alone.
- **Bare http(s) data URLs are supported** — READMEs paste OSF `?zip=` bundle
  links, not API references. They reuse the existing SSRF guard, byte caps and
  zip-bomb guards, and the archive is unpacked and removed. This is deliberately
  data-only: a bare URL as a *submission* still fails with the supported-sources
  list, because a submission needs a pin and a URL has none.
- **Prose-only data links are now a stated finding.** When the documentation
  points at a data repository and nothing declares it machine-readably, the
  report says so and prints the `--data` command that fixes it, instead of
  letting the run fail on missing inputs and read like broken code.
- **The OSF fetcher stopped claiming links it could not resolve.** `"osf.io"` is
  a substring of `files.de-1.osf.io`, OSF's own bundle host, so pasting the URL
  from a README produced `could not parse OSF guid` — a usable download turned
  into an unfetchable source. It now claims only refs carrying a real guid.
- **`.reprobe.yaml` is read as a manifest.** The report told authors to "declare
  `steps:` in .reprobe.yaml" while the loader only looked for `autoui-repro.yml`,
  so following the advice produced a file nothing would ever load.

### `reprobe doctor` no longer vouches for a path it did not look at

The config-dir row printed a hardcoded "ok". A non-editable install resolves its
config to `<prefix>/Lib/config`, which exists for nobody — and the one check that
could have caught it confirmed it instead, leaving `pins.yaml FAIL year=None` and
two `base image FAIL None` rows to be explained some other way. The row now stats
the directory, fails loudly, and names the cause when the package is installed
outside a checkout.

### stop blaming the artifact for the harness's gaps

Findings from a real submission (`PDRA_XAI_OS`, 2026-07): of five notebooks, two
were failed for a package the *base image* lacked, one was killed by a budget
tuned for scripts rather than model fitting, and the one that passed was reported
without mentioning that everything it imported came from reprobe rather than from
the artifact. Every item below comes from that run.

- **The base image can now read the formats the detector advertises.** `base-py`
  gains `pyarrow`, `openpyxl`, `xlrd`, `pytables`, `netcdf4` and `pyreadstat`.
  `detect/signatures.py` inventories `.parquet`/`.xlsx`/`.h5` as *dataset* files,
  but pandas in the image could open none of them — artifacts died with
  `ImportError: Unable to find a usable engine` and were scored as broken code.
  A unit test now fails if an extension is added on one side and not the other.
  **Base images republished as `2026.3`; run `bash images/build-images.sh`.**
- **`build-images.sh` refuses to build on a stale lock.** The Dockerfile prefers
  `conda-lock.yml` over `env.yaml`, so a leftover lock silently outvoted a
  freshly added dependency — you got an image missing the package you just added,
  tagged as though it had it.
- **A timeout now reports evidence.** It keeps the log tail and the partially
  executed notebook (papermill checkpoints after every cell), where before it
  recorded neither and was indistinguishable from a harness bug.
- **Notebooks are executed with `papermill --log-output`.** papermill's default is
  silent, so a notebook that hung for the entire budget emitted one line and the
  log could not say which cell stalled. Cell boundaries are now logged.
- **The LLM diagnoser is never asked to explain an empty log.** Given nothing it
  narrated the absence, quoted the harness's own untrusted-data fences back into
  the report, and invented fixes. Steps with no output get a clearly-labelled
  deterministic note instead.
- **Notebook budgets raised to 90 min** (`jupyter`, `rmarkdown`), and
  `reprobe run --timeout N` overrides per run — clamped to the new
  `limits.yaml:max_timeout_s` ceiling, which also closes a gap where a runner's
  `ContainerSpec` timeout request was applied unclamped. A non-default budget is
  recorded in the report.
- **Inferred run order is stated, and downstream steps run last.** Plain
  alphabetical ordering ran `analyse_combined_*.ipynb` *before* the models it
  aggregates, so it would have read the committed outputs of steps that had not
  re-run — a pass on stale data. Numeric prefixes and README order still win; the
  name heuristic only breaks ties, and the report says when order was inferred.
- **"Declares no dependencies at all" is now a stated finding.** Previously that
  case produced an empty warnings list and a green step, which reads as
  self-contained. It is the artifact's most consequential reproducibility defect
  and is now named as one.
- **FAIR `reusable` counts a `LICENSE` file and a dependency manifest.** It scored
  licensing purely from fetcher metadata, which `git clone` never populates — so
  every git-sourced artifact was marked unlicensed even with a LICENSE at the
  root. Rebuilding the environment is now also rewarded (`reward_dependency_manifest`).

### a dead daemon is not a broken artifact

A second run of the same submission lost the Docker engine 30 minutes into the
first notebook. The harness then produced five failure rows, four LLM diagnoses,
and a recommendation to check whether a base-image tag existed — for a tag that
was present locally and published on ghcr the whole time. One host outage, five
statements that read as findings about the artifact.

- **Exit 125 is no longer read as "the base image is broken".** `docker run`
  returns 125 both when a container fails to start *and* when the daemon dies
  under a running one; the harness assumed the first and told the operator to
  check the image, for a step that had been fitting models for 1798s. It now
  separates the two by the CLI's own disconnect wording (`error waiting for
  container: unexpected EOF`) and by re-probing the daemon, and reports
  `docker-daemon-lost` with the elapsed time.
- **A missing image is only claimed when the daemon can answer.** `docker image
  inspect` fails identically for "no such image" and "no daemon", so every step
  after the crash was reported as `image-not-present` with a `docker pull`
  instruction that would have changed nothing. That path now checks the daemon
  first and reports `docker-unavailable`.
- **The pipeline stops when the daemon goes.** Remaining steps are recorded as
  `skipped — not attempted`, instead of four identical infra failures that read
  like independent findings.
- **Infrastructure failures never reach the LLM diagnoser.** Handed a log full of
  the artifact's own healthy output, the model explained the *artifact* — it
  advised verifying a published image tag and speculated about the notebook
  hanging. The harness already knows these causes exactly, so it states them and
  asks nothing.
- **A harness error keeps the evidence of a container that did run.** The
  daemon-loss case now carries the log tail and the checkpointed notebook, the
  same way a timeout does; before, a 30-minute partial run was indistinguishable
  from one that never started.

### results vs the paper (advisory)

Until now a report could say "the code ran and produced its declared outputs"
but never touched the question a reviewer actually cares about: *do the numbers
still match the paper?* That stays a human judgement — but the harness can now
put the comparison in front of the human instead of making them do it cold.

- **New advisory check: produced results vs the paper's claims.** reprobe locates
  the paper two ways — a **PDF committed in the repo** (best: full text, no
  network) or a **DOI** taken from the manifest, `CITATION.cff`, the README or
  the archival pin. A DOI is resolved through OpenAlex to an open-access full
  text when one is genuinely downloadable, and to the abstract otherwise. A local
  LLM then compares claim by claim and the report renders a table of
  paper value vs reproduced value with a verdict of
  `match` / `mismatch` / `unclear` / `not-reported`.
- **It never grants a badge.** `results_reproduced` stays `not-evaluated`
  whatever the model says — the LLM is told it cannot change decisions, and that
  stays true. A `mismatch` only adds a line to "what was NOT checked" for the
  human. Every rendering is labelled advisory.
- **Coverage is stated, never implied.** The report says whether it compared
  against full text or *ABSTRACT ONLY*, because paywalled venues advertise an
  open-access PDF that then refuses automated requests (ACM returns 403), and an
  abstract states "significantly higher", not `F(1,16)=11.12`. Authors who want
  this check to be worth anything should commit the paper PDF or set
  `paper.pdf` in the manifest.
- **Bounded on every untrusted edge.** Only fixed metadata APIs are queried, with
  the DOI validated against the registered shape first; an advertised OA link
  must pass the SSRF guard, download under the byte cap, and actually begin with
  `%PDF` before it is parsed; PDF reading is capped by bytes, pages and
  characters and never raises. Paper text and the run's own output are both
  fenced as untrusted before reaching the model.
- Manifest gains `paper: {doi, pdf}`; PDF text extraction is the optional extra
  `pip install 'reprobe[paper]'` — without it the report says the PDF was **not
  read**, never that it matched.

### dependency & data provisioning

Real submissions failed for two mundane reasons: a needed R package wasn't in the
base image, and LFS-tracked datasets were never pulled. Both are now handled while
keeping the author analysis offline — the network stays in the sanctioned phases.

- **CRAN R packages installed automatically.** Detection statically discovers the
  R packages a repo needs (`library()`/`require()`/`requireNamespace()`/`pkg::`
  in `.R`/`.Rmd`/R-kernel notebooks, plus `DESCRIPTION` Imports/Depends, plus
  `setup.R`-style `install.packages(c(...))` lists — the only place on-demand
  dependencies like `FSA`/`Hmisc`/`rstatix` are ever named), and
  authors may also list them under `environment.r_packages` in the manifest. The
  **CRAN-available, not-already-present** subset is installed into `R_LIBS_USER`
  during the existing egress **install phase**; the author run still executes with
  `--network none`. Packages not on CRAN (e.g. a private package) are reported,
  never faked. Versions are pinned to a dated CRAN snapshot
  (`r.cran_snapshot` in `pins.yaml`, a Posit P3M date) for year-over-year
  reproducibility. Discovered names are validated to `[A-Za-z][A-Za-z0-9.]*` and
  the whole `install.packages()` call rides inside one single-quoted `-e`
  argument, so a hostile `library()` name cannot inject shell or R code.
  Two fixes make this actually build compiled packages: the dependency-install
  `/tmp` is now mounted **`exec`** (Docker's `--tmpfs` is `noexec` by default, so
  every source package's `./configure` previously failed with "exists but is not
  executable"), and the base-r image gains a **compiler toolchain**
  (`c-/cxx-/fortran-compiler`) so `Rcpp*`/`gmp`/`later`-style packages compile —
  rebuild it with `bash images/build-images.sh`. The CRAN step now also **exits
  non-zero when a CRAN-available package fails to build**, so the install phase is
  reported failed instead of a silent `ok`.
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

- **Base images republished as `2026.2`, and the lock is real now.** The base-r
  contents changed twice in this cycle while the tag stayed `2026.1`, so one tag
  named two different images — which breaks the promise that a stored report
  re-runs years later, since the digest it recorded no longer exists. `revision`
  is now `2` and the tags are `2026.2`; `2026.1` is left untouched for reports
  that already reference it. The runbook's rebuild rule now triggers on an
  `env.yaml` edit (not just a tag change) and requires bumping `revision` in the
  same edit. Related: `images/base-*/conda-lock.yml` are now actually **solved
  and committed** — four places claimed "the committed conda-lock.yml is the
  reproducibility pin" while no lock existed and both Dockerfiles silently fell
  back to the unpinned `env.yaml`.
- **Multi-step pipelines can earn the Functional candidate.** Detection
  broadcasts the manifest's `expected_outputs` onto every step, and a clean step
  producing none of them was marked `partial` — so a prep step that was never
  meant to produce the final artifacts failed `all_pass`, denied the Functional
  candidate and downgraded the verdict. `RunStep` now records whether its outputs
  were broadcast, and only step-declared outputs can make a step `partial`; a
  one-step plan still owns the manifest's outputs. Shipped with the guard that
  makes it safe: a pipeline where every step passes but **no** declared output is
  produced stays `runs-with-warnings` instead of becoming a green `runs`.
- **Phase disclosures are actually visible.** `environment.notes` — where the
  dependency-install, dataset-download and runtime-egress disclosures are
  written — was rendered by neither `report.md` nor `report.html`. The
  `--allow-net` statement ("badge confidence downgraded … this grants full egress
  for the run phase") was therefore invisible in the human-readable reports it
  exists to warn. Both renderers now emit it, and the egress disclosure is a
  **warning** rather than a note.
- **Declared install lists are scoped to the install call.** Harvesting every
  `c(...)` in a file that calls `install.packages` swept up unrelated character
  vectors: factor levels like `c("car","boot")` are real CRAN names and were
  installed for nothing, while `c("Male","Female")` became bogus "not on CRAN"
  noise. Only names reachable from an `install.packages()` argument are taken now
  — following the assignment chain (`install.packages(missing)` →
  `missing <- pkgs[…]` → `pkgs <- c(…)`).
- **The R version listing survives a failed install.** The phase runs under
  `set -e`, so the CRAN step's non-zero exit aborted the shell before the trailing
  `installed.packages()` listing could run — losing the record of what *is*
  installed in exactly the run a chair needs to diagnose. The CRAN script now
  prints it before quitting.
- **A submission can be re-run.** Every fetch now starts from a pristine
  `work/<sid>/src/`. Previously a second run of the same submission failed
  outright — `git clone` refused the existing directory ("destination path
  already exists and is not an empty directory"), so the work dir had to be
  deleted by hand, and `batch --resume` hit the same wall on exactly the
  `fetch-failed` submissions it exists to retry. The local fetcher had the
  quieter half: `copytree(dirs_exist_ok=True)` merged the old tree into the new
  one, so files deleted upstream lingered as ghosts. `src/` now uses the same
  freshness rule `run/` already had (Windows file-lock fallback to a uniquely
  named sibling instead of crashing mid-batch); `reprobe detect` too.

### security & robustness hardening

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
