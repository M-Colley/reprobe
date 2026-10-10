# Badges & honesty policy

reprobe maps to the **ACM Artifact Review and Badging v1.1** scheme. AutoUI's
formal deliverable is **Artifact Available**; everything deeper is a value-add.

## The stance

> **Grant only *Available* automatically. Propose *Functional* as a candidate
> for a human. Assist everything deeper. Never over-claim.**

An automated reproducibility checker that over-claims is worse than none: a green
"it runs" must never be read as "the results are correct." Every step in a
report carries two lists — `claims` (what was machine-verified) and
`not_verified` (what was explicitly **not** checked).

## Decision rules (data, not code — see `config/badges.yaml`)

- **Artifact Available** — granted with **no code execution** iff the artifact is
  fetched and pinned by an **archival** persistent identifier (a Zenodo *version*
  DOI or a Software Heritage SWHID) and checksums verify where the platform
  provides them. A bare GitHub commit SHA is reproducibly pinned but **not
  archival** → *candidate* + "deposit in Zenodo / archive via Software Heritage".
- **Artifacts Evaluated — Functional** — *candidate* (not auto-granted) when the
  primary declared steps pass and at least one declared `expected_output` is
  produced. Opt-in courtesy; a human confirms.
- **Results Reproduced** — never auto-granted. reprobe puts two kinds of
  evidence in front of the human:
  - **regenerated vs committed** (deterministic): every file the authors
    committed that the run rewrote is compared with their copy — identical,
    *equivalent* within `rtol`/`atol` (integers must match exactly), *differs*
    (with the largest difference and where), or *not compared* (binary, too
    large, unreadable — never a guess). A difference can be nondeterminism, so
    it is a finding to review, not a verdict.
  - **results vs the paper** (LLM-advisory): a local model lines up the
    paper's numbers with the run's output. A value the model quotes as produced
    must actually occur in the output, or its match/mismatch is downgraded to
    *unclear*; below `llm.confidence_threshold` the comparison is shown but adds
    no finding.

## Whose failure is it?

The report never folds the harness's failures into the artifact's:

- A step stopped by the sandbox's **budget** — the wall-clock timeout, the
  `--memory` cap (docker's own OOM flag), or the console-output / disk caps in
  `limits.yaml` — is `timeout` / `resource-limit`, and the verdict is
  `resource-limit`, not `runs-with-failures`.
- A dependency install **reprobe chose** (packages inferred from imports) that
  fails is `infra-error`; the artifact's **own declared** requirements.txt /
  environment.yml / renv.lock failing to install is a finding about the
  artifact. Network trouble during either is never pinned on the author.
- Exit 125 is docker's "could not start" code only when the docker CLI says
  so; an artifact can exit 125 itself.

## FAIR

Scored from fetch metadata: a persistent identifier (Findable), open/documented
access (Accessible), standard formats (Interoperable), and a manifest + open
licence (Reusable). Reported as `true` / `partial` / `no`, never as a grade.

## Why a manifest helps authors

An optional `autoui-repro.yml` removes guesswork (and the LLM) from detection:
the author declares the run order, environment, and expected outputs, and the
harness verifies them. Existing CODECHECK `codecheck.yml` files are also read.
