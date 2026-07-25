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

> [!IMPORTANT]
> **Docker must be running — this is a hard prerequisite.** `reprobe`
> executes every analysis inside Docker, so a working, *running* Docker Engine
> is mandatory (`reprobe doctor` verifies it; only the code-free commands
> `reprobe detect` and `reprobe run --no-run` work without it).
>
> **Start Docker yourself first.** Docker Desktop does not always launch
> automatically after login or a reboot — start it manually (open Docker
> Desktop) and wait for the daemon to be ready (`docker info` succeeds, or
> `reprobe doctor` shows `docker … ok`) before any `reprobe pull`/`run`/`batch`.
>
> - **Windows** — install Docker Desktop, which needs **either** the **WSL 2
>   backend** (recommended: enable *"Use the WSL 2 based engine"* in Docker
>   Desktop → Settings) **or**, if WSL 2 is unavailable to you, the Hyper-V
>   backend. Both rely on hardware virtualization, so on a **managed or
>   locked-down machine you will need administrator / IT permission** to
>   install Docker Desktop and turn virtualization on. Without WSL 2 **or** that
>   permission, Docker will not start and no runs are possible.
> - **Linux** — the daemon must be running and your user must be allowed to use
>   it: either be in the `docker` group, or use rootless Docker.
> - **macOS** — Docker Desktop (or an equivalent daemon) must be running.

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

# Pull git-lfs data during fetch (opt-in, hardened; default keeps skip-smudge):
reprobe run <url> --allow-lfs
```

Missing R packages are handled automatically: reprobe detects the CRAN packages a
repo needs (`library()`/`require()`/`pkg::` and `DESCRIPTION`) and installs the
CRAN-available ones — pinned to a dated snapshot (`r.cran_snapshot` in
`config/pins.yaml`) — during the sandboxed **install phase**, so the author
analysis still runs with `--network none`. Packages not on CRAN are reported, not
faked.

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
[src/reprobe/schemas/autoui-repro.schema.json](src/reprobe/schemas/autoui-repro.schema.json) and
[examples/example-python](examples/example-python). Two knobs worth knowing:

- `environment.r_packages: [pkg1, pkg2]` — pin exactly the CRAN packages to
  install (reprobe also auto-detects them, so this is only for overriding).
- `data: [{path, source, checksum}]` — data files reprobe should download (from
  an http(s) `source`) into the run tree before your analysis runs.
- `paper: {doi, pdf}` — the paper this artifact reproduces. reprobe compares your
  produced numbers against its claims and shows a reviewer the differences
  (advisory only — it never grants a badge). **Committing the PDF is worth far
  more than the DOI:** paywalled venues refuse automated downloads, leaving only
  the abstract, which states "significantly higher" rather than `F(1,16)=11.12`.
  Without a manifest reprobe still looks for a PDF in the repo and a DOI in
  `CITATION.cff` / the README.

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
