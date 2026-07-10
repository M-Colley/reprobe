# Changelog

## 0.1.1 — fix wave (post-review hardening)

- **Python 3.13 is the default runtime** (was 3.11): `base-py` env, controller
  image, `fetch.fallback_python_image`, and the GitHub Action.
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
  Python 3.10/3.13.
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
