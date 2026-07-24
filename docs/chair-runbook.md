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
2. **Rebuild base images.** Required whenever the `base_images.*` tags in
   `pins.yaml` changed (a bumped tag exists only once you build it — otherwise
   every run fails with `image-not-present`). Edit `images/base-py/env.yaml`
   (and `base-r`) only if the science stack must also move, then:
   ```bash
   bash images/build-images.sh        # re-solves conda-lock, rebuilds, tags 2027.x
   ```
   You must run this manually — no CI rebuilds images yet. `pip install
   conda-lock` first for reproducible bases.
   On a machine that only *consumes* already-published images (a fresh laptop,
   a helper's PC), skip the build: `reprobe pull` fetches the pinned base
   images and the smoke image in one command. That machine still needs a
   working Docker (on Windows: Docker Desktop with the **WSL 2 backend**, or
   **admin/IT permission** to enable Hyper-V virtualization — see the Install
   prerequisites in [README.md](../README.md#install)); `reprobe doctor` tells
   you if it is missing.
3. **Refresh the Unity editor tag map** — **skip until Phase 3 ships**; the
   `reprobe unity-refresh` command does not exist yet and `unity.known_tags`
   stays empty:
   ```bash
   # reprobe unity-refresh            # Phase 3 — not yet implemented
   ```
4. **Confirm it still works**:
   ```bash
   reprobe doctor --smoke --golden    # config + Docker + base images + Ollama + sandbox smoke + golden regression
   python -m pytest -q                # the deterministic test suite (detection, badges, sandbox flags)
   ```
   `--golden` replays the bundled good/broken fixtures through the dry-run
   pipeline and diffs the result against `tests/golden/expected.json` — the
   one check that catches a pins/badges edit silently changing pipeline
   behavior. After an *intentional* change, regenerate the goldens with
   `python -m reprobe.golden --update` and commit the diff.

## During the review window

- **One submission:** `reprobe run <url>` → read `work/<id>/out/report.html`.
- **Missing R packages** are installed automatically: reprobe detects the CRAN
  packages a repo needs and installs the CRAN-available ones (pinned to
  `r.cran_snapshot` in `pins.yaml`) in the sandboxed install phase. A package
  that isn't on CRAN (e.g. an author's private package) is reported, not faked —
  the author must vendor it or deposit it.
- **Datasets in git-LFS** are not pulled by default. If the report warns
  `git-lfs present; … skip-smudge`, re-run that submission with `--allow-lfs`
  (hardened: refuses a repo whose `.lfsconfig` declares a custom transfer agent
  or an internal `lfs.url`, and caps the total payload).
- **A batch:** put one URL per line (or a `url` column) in `submissions.csv`:
  ```bash
  reprobe batch submissions.csv      # -> out/dashboard.html + out/badges.json + out/badges.csv
  ```
  Sort the dashboard by verdict; triage anything flagged `no-checksum`,
  `anonymized`, or `warnings`. `badges.csv` is the flat per-submission export
  for reconciling against the PCS/EasyChair spreadsheet.
- **Interrupted batch?** Re-run with `--resume`: finished reports are reused
  as-is; only `fetch-failed` / `infra-error` submissions are retried.
- **Author reply:** every `report.md` ends its badge section with a
  copy-pasteable **Feedback for authors** block — paste it into your decision
  email instead of composing from scratch.
- **Anonymized (double-blind) submissions:** `anonymous.4open.science` links work
  directly but carry no archival pin — the report says so. Remind authors that
  the **Available** badge needs a durable Zenodo/OSF deposit before publication.

## Secrets (never commit)

Provide as environment variables when needed:
- `UNITY_EMAIL` / `UNITY_PASSWORD` / `UNITY_SERIAL` **or** a Unity Licensing
  Server endpoint — only if you enable Unity T1/T2 (your institution's own seat).
- `ZENODO_TOKEN`, `OSF_TOKEN` — only for restricted/embargoed records.

## Running in CI (controller mode)

The optional `reprobe-controller` image (built by `bash images/build-images.sh
controller`) mounts the host Docker socket at runtime — that is
**root-equivalent on the host**. Run it only on a disposable CI runner or a
dedicated VM, never on a personal machine. Author code still only ever runs in
the sibling sandboxed containers; the socket belongs to the trusted
orchestrator alone.

## What you never touch

`src/` (orchestrator, runner contract, sandbox flags, fetchers, report schema),
the safety envelope, and the LLM prompts. New artifact types arrive as **new
runner plugin packages** (entry points), not core edits.

## If a URL reports "no fetcher matched"

Institutional hosts the built-in fetchers don't recognize are a config fix, not
a code fix: add the hostname to `pins.yaml` under `fetch.extra_git_hosts`
(institutional GitLab/Gitea, e.g. `gitlab.lrz.de`) or `fetch.dataverse_hosts`
(Dataverse installs whose hostname lacks "dataverse", e.g.
`darus.uni-stuttgart.de`), then re-run.

## If an upstream breaks

GameCI and repo2docker are best-effort upstreams behind our interface. A break
degrades one runner/strategy with a clean error — never the whole harness.
(Specific error labels such as `no-matching-editor-image` and
`repo2docker-build-failed` arrive with the Phase-2/3 runners.) Everything
pinned is recorded in each report, so an old report reproduces years later.
