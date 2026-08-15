# Benchmark artifacts

Real artifacts, kept as regression cases. Fixtures prove a code path works;
these prove the harness still says true things about deposits it does not
control — which is the part that breaks silently when a platform changes its API
or an author restructures a repo.

Run the whole set:

```bash
reprobe batch benchmarks/artifacts.csv
```

Fetch + detect only (minutes rather than hours — no code executes, no
environment is built):

```bash
reprobe batch benchmarks/artifacts.csv --no-run --no-llm
```

`artifacts.csv` uses the `data` column for the composite cases; any other column
(here `notes`) is ignored by the reader.

**Do not add `--reuse-downloads` to a benchmark run you intend to trust.** The
cache is shared across submissions, so the point of running nine real deposits —
that each one resolves and installs from scratch — is exactly what it removes.
It is fine while iterating on one case.

## What each case is here to catch

| artifact | shape | what it exercises |
| --- | --- | --- |
| `M-Colley/shed-some-fear-data` | R, one entry point | the ordinary case: `run_all.R`, detected heuristically |
| `M-Colley/bvi-auditory-hav` | R **and** Python | a mixed-language artifact — two runners, two install phases, two base images |
| `M-Colley/roads-chi25-data` | R, three scripts | a multi-step pipeline; the broadcast-`expected_outputs` bug lived here |
| `M-Colley/ehmi-optimization-chi25-data` | R, one script | single-step R |
| `M-Colley/ehmi-for-all-chi26-data` | R, two scripts | ordering — a logs script and a main script |
| `jorgpg5/PDRA_XAI_OS` | 5 Jupyter notebooks | the long-running case: one notebook has hit the 90-min jupyter cap and reported `timeout`, which must stay distinguishable from a crash |
| `ciao-group/PerceivedRisk` | code in git + data on OSF | the composite artifact, via `--data`. Also the case that motivated installing undeclared imports (`shap`, `ray[tune]`) |
| `OSF 4wj86` | deposit, **no code** | must land on `not-run` with Available candidate — "nothing to execute" is not a failure |
| `ammarjamal/ARena` + Zenodo | code 404s, data embargoed | must report *why* both halves are unreachable, including the embargo lift date. Re-check after **2026-09-21**, when the Zenodo embargo lifts |

## Observed, 2026-08-15 (full execution pass)

Every artifact pins to a `git_sha` (or none) and lands Available **candidate** —
a commit is not an archival identifier, which is the right answer for a
GitHub-only deposit. Nothing here earns a badge automatically; that is the point.

| artifact | verdict | Functional | why |
| --- | --- | --- | --- |
| shed-some-fear-data | **runs** | **candidate** | clean. The only artifact needing no human review, and the reference for what the others could be |
| bvi-auditory-hav | runs-with-failures | not-met | R passes; the Python step needs positional CLI arguments the deposit documents nowhere |
| roads-chi25-data | runs-with-failures | not-met | 3 scripts, all `Error: RStudio not running` |
| ehmi-optimization-chi25-data | runs-with-failures | not-met | same, 1 script |
| ehmi-for-all-chi26-data | runs-with-failures | not-met | same, 2 scripts — and three more defects behind it (see below) |
| PerceivedRisk | runs-with-failures | not-met | OSF deposit merged (18 files, checksums verified); the deposited `Demographics.csv` headers do not match the columns the code selects |
| OSF 4wj86 | not-run | not-evaluated | no code at all — 2 datasets, 5 PDFs, 12 videos. Correctly not a failure |
| ARena | fetch-failed | — | repo not publicly reachable; Zenodo deposit embargoed until 2026-09-21 |
| PDRA_XAI_OS | infra-error | not-evaluated | **not an artifact result** — see below |

### What the failures are actually made of

`ehmi-for-all-chi26-data` was driven all the way down by patching a local copy,
one blocker at a time. Each fix revealed the next, which is why a single run only
ever names the first:

1. `setwd(dirname(getActiveDocumentContext()$path))` — needs a live RStudio IDE
2. `colleyRstats_setup()` activates `conflicted`, whose `library()` shim breaks
   `easystats`'s `.onAttach` (`object 'quietly' not found`)
3. `gather()` called ~285 lines before `library(tidyr)`
4. `dplyr::replace_values()` called with the old vector-pair signature

Only (1) is visible without fixing (1). Two of the repos here —
`bvi-auditory-hav` and `shed-some-fear-data` — already carry a guarded opener
that handles both `Rscript` and RStudio, so the fix for (1) is a repo-local
precedent rather than an invention.

### PDRA_XAI_OS is blocked on the host, not on itself

`PDRA_CatBoost_OS.ipynb` reproducibly kills the Docker VM: exit **125** with the
engine gone afterwards (HTTP 500 on the pipe), twice, at 60 and 20 minutes. That
is a container claiming its 8 GiB cap inside a 12 GiB VM, taking the VM with it
rather than being OOM-killed cleanly. Raise Docker Desktop's memory (24 GiB has
worked on this host) and re-run. The remaining four notebooks are correctly
recorded as `skipped` — never attempted — rather than as four more failures.

## Not included

- `M-Colley/hitl-mobo-rating-scales` — returned **404** on 2026-08-13. Private
  or renamed; add it once it resolves.
