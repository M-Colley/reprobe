# Chair runbook — running reprobe each year

This is the whole job. In the common year you edit **one file** and run **one
check**. Everything else is fixed.

## Once per year (≈ 30 min)

1. **Bump `config/pins.yaml`.** This is the only file you should normally touch:
   - `year` / `revision` → e.g. `2027` / `1` (base-image tags become `2027.1`).
   - `base_images.python` / `.r` → the new tags (built in step 2).
   - `base_images.micromamba_base` → optionally a newer pinned digest.
   - `llm.model` → keep `gemma4:e4b` unless you want to move.
   - `unity.default_image_version` and the editor tag map (step 3).
2. **Rebuild base images** *only if the science stack changed.* Edit
   `images/base-py/env.yaml` (and `base-r`), then:
   ```bash
   bash images/build-images.sh        # re-solves conda-lock, rebuilds, tags 2027.x
   ```
   (CI does this automatically when `images/**` or `pins.yaml` changes.)
3. **Refresh the Unity editor tag map** (only the part that genuinely ages):
   ```bash
   reprobe unity-refresh              # Phase 3; updates digest-pinned tag map
   ```
4. **Confirm it still works**:
   ```bash
   reprobe doctor --smoke             # config + Docker + base images + Ollama + a sandbox smoke test
   python -m pytest -q                # the deterministic test suite (detection, badges, sandbox flags)
   ```
   (Golden-report regression against bundled good/broken fixtures is the Phase-5
   addition to `doctor` — see docs/DESIGN.md §11.)

## During the review window

- **One submission:** `reprobe run <url>` → read `work/<id>/out/report.html`.
- **A batch:** put one URL per line (or a `url` column) in `submissions.csv`:
  ```bash
  reprobe batch submissions.csv      # -> out/dashboard.html + out/badges.json
  ```
  Sort the dashboard by verdict; triage anything flagged `no-checksum`,
  `anonymized`, `needs-network`, or `no-matching-editor-image`.
- **Anonymized (double-blind) submissions:** `anonymous.4open.science` links work
  directly but carry no archival pin — the report says so. Remind authors that
  the **Available** badge needs a durable Zenodo/OSF deposit before publication.

## Secrets (never commit)

Provide as environment variables when needed:
- `UNITY_EMAIL` / `UNITY_PASSWORD` / `UNITY_SERIAL` **or** a Unity Licensing
  Server endpoint — only if you enable Unity T1/T2 (your institution's own seat).
- `ZENODO_TOKEN`, `OSF_TOKEN` — only for restricted/embargoed records.

## What you never touch

`src/` (orchestrator, runner contract, sandbox flags, fetchers, report schema),
the safety envelope, and the LLM prompts. New artifact types arrive as **new
runner plugin packages** (entry points), not core edits.

## If an upstream breaks

GameCI and repo2docker are best-effort upstreams behind our interface. A break
degrades one runner/strategy with a clean error (`no-matching-editor-image`,
`repo2docker-build-failed`) — never the whole harness. Everything pinned is
recorded in each report, so an old report reproduces years later.
