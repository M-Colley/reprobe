# Changelog

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
