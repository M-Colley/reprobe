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
- **Results Reproduced** — never auto-granted (needs produced-vs-published
  comparison); reprobe surfaces produced artifacts to aid the human.

## FAIR

Scored from fetch metadata: a persistent identifier (Findable), open/documented
access (Accessible), standard formats (Interoperable), and a manifest + open
licence (Reusable). Reported as `true` / `partial` / `no`, never as a grade.

## Why a manifest helps authors

An optional `autoui-repro.yml` removes guesswork (and the LLM) from detection:
the author declares the run order, environment, and expected outputs, and the
harness verifies them. Existing CODECHECK `codecheck.yml` files are also read.
