# reprobe — AutoUI open-data reproducibility harness

**Give a URL, get a runnable check.** `reprobe` fetches a research artifact (a
git repo or an archival DOI), detects what it is, builds a sandboxed
environment, runs the data analyses (Python / R / Jupyter / R&nbsp;Markdown),
and writes a report that maps to ACM artifact badges and FAIR principles. Unity
prototypes are supported at a structural tier. A small **local** LLM (Gemma&nbsp;4
(e4b) via Ollama, fully offline) optionally assists — but only ever advises.

Built for the AutoUI Open Data chair to **reuse year over year**: the annual job
is editing `config/pins.yaml`, not the code.

> Full architecture and rationale: [docs/DESIGN.md](docs/DESIGN.md).
> Running it as chair each year: [docs/chair-runbook.md](docs/chair-runbook.md).

## The one idea: a trust boundary

The orchestrator is the only trusted process. **Untrusted author code runs only
inside ephemeral containers** with `--network none`, a non-root user, all
capabilities dropped, a read-only root filesystem, and cpu/memory/pid/time caps.
The LLM returns data only — it has no shell, no socket, and cannot execute
anything. `reprobe --no-llm` is fully functional and deterministic.

## Install

```bash
pip install -e .            # Python 3.11+; needs Docker available for runs
reprobe pull               # fetch the pinned base images (published, no login needed)
reprobe doctor --smoke     # self-check: config, Docker, base images, Ollama, sandbox
```

The pinned Python/R analysis environments are published images —
`ghcr.io/m-colley/reprobe-base-py:2026.1` and `…-base-r:2026.1`, built by
[`images/build-images.sh`](images/build-images.sh). Authors can test their code
against the exact same environment reviewers use (see
[docs/chair-runbook.md](docs/chair-runbook.md)).

## Use

```bash
# Single artifact (fetch + detect + sandboxed run + report)
reprobe run https://github.com/jorgpg5/PDRA_XAI_OS

# A Zenodo deposit by DOI
reprobe run https://doi.org/10.5281/zenodo.123456

# Detection only — no code executed (shows the run plan reprobe would use)
reprobe detect ./examples/example-python

# A whole review season -> sortable dashboard + badges.json + badges.csv
reprobe batch submissions.csv      # add --resume to continue an interrupted season

# Available badge only, never execute code:
reprobe run <url> --no-run
```

Outputs land in `work/<submission>/out/`: `report.json` (machine-readable),
`report.md`, and a single-file `report.html`.

## What the badges mean here

| Badge | reprobe behaviour |
|---|---|
| **ACM Artifact Available** | *Granted automatically* — but only on an **archival** persistent ID (Zenodo version DOI, Software Heritage SWHID). A bare GitHub commit is reproducibly pinned yet not archival, so it becomes a *candidate* with a "deposit this in Zenodo" note. No code is executed for this badge. |
| **ACM Artifacts Evaluated — Functional** | *Proposed as a candidate* for a human when declared steps pass and produce a declared output. Never auto-granted. Opt-in. |
| **ACM Results Reproduced** | Not auto-granted; reprobe surfaces produced artifacts to help the human reviewer. |
| **FAIR** | Scored from fetch metadata (persistent ID, license, formats, manifest). |

The cardinal rule: **the harness grants only *Available* automatically and
proposes everything deeper.** Over-claiming is the failure mode it avoids — every
step reports both what it *verified* and what it explicitly did **not**.

## Authors: make it effortless (optional)

Drop an `autoui-repro.yml` in your repo to remove all guesswork (existing
CODECHECK `codecheck.yml` is also read). See
[schemas/autoui-repro.schema.json](schemas/autoui-repro.schema.json) and
[examples/example-python](examples/example-python).

## Status

MVP + breadth: fetch (git / Zenodo / figshare / Dryad / OSF / Dataverse /
Software Heritage / anonymous.4open.science / local / resolvable DOIs),
detection (runnable code **and** non-code artifacts: video / audio / dataset /
document / 3D — the AutoUI submission-form categories), sandboxed
Python/R/Jupyter/Rmd execution with CLI args + a split-by-language
dependency-install phase, Unity T0 structural, badges, reports with a
copy-pasteable author-feedback block, batch dashboard (+ `--resume`,
`badges.csv`), a golden-report regression (`reprobe doctor --golden`), and the
advisory LLM. Base images build via
[`images/build-images.sh`](images/build-images.sh) and publish to
`ghcr.io/m-colley/reprobe-base-{py,r}:2026.1`. Remaining: repo2docker fallback
and Unity compile/build tiers — scoped in [docs/DESIGN.md §11](docs/DESIGN.md).
