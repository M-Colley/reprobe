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

## Observed, 2026-08-13 (fetch + detect pass)

All five `M-Colley` repos fetch clean, pin to a `git_sha`, and land Available
**candidate** — a commit is not an archival identifier, which is the correct
answer for a GitHub-only deposit.

| artifact | verdict | detected |
| --- | --- | --- |
| shed-some-fear-data | not-run | 1 R step |
| bvi-auditory-hav | not-run | 1 R + 1 Python step |
| roads-chi25-data | not-run | 3 R steps |
| ehmi-optimization-chi25-data | not-run | 1 R step |
| ehmi-for-all-chi26-data | not-run | 2 R steps |
| PDRA_XAI_OS | not-run | 5 notebooks |
| PerceivedRisk | not-run | 1 Python step, +1 deposit merged |
| OSF 4wj86 | not-run | no code: 2 dataset, 5 document, 12 video |
| ARena | fetch-failed | repo not publicly accessible; deposit embargoed |

(`not-run` is what `--no-run` produces — it means "no code was executed", not
"this artifact does not run". A full pass replaces these.)

## Not included

- `M-Colley/hitl-mobo-rating-scales` — returned **404** on 2026-08-13. Private
  or renamed; add it once it resolves.
