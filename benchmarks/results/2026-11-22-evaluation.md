# reprobe on 36 ACM-badged artifacts: before and after the fixes

Evaluation set: `benchmarks/acm-badged.csv` (selection rule and listing commits in
`benchmarks/acm-badged-selection.md`). All runs: GitHub Actions
(`.github/workflows/benchmark.yml`), `--no-llm`, per-step timeout 2700 s, 8 GiB memory
cap, no GPU. Raw per-artifact verdicts are in the `comparison.csv` of each run folder.

| run | commit | ran cleanly | not run | ran with failures | steps passed |
|---|---|---|---|---|---|
| baseline | 44555a0 | 4 | 16 | 16 | 12/75 |
| first fixes | 18b4796 | 4 | 16 | 16 | 15/52 |
| detection fixes | d4fdc68 | 4 | 16 | 16 | 15/52 |
| as-of install, CPU torch | 9929e71 | 4 | 16 | 16 | 13/52 |
| as-of install fixed | 7274965 | 5 | 16 | 15 | 16/52 |

The detection fixes (up to d4fdc68) removed wrong steps, wrong failures and silent
omissions (75 to 52 steps: 23 fewer steps that should never have run; 3 more passes)
but made no further artifact pass. The as-of-date install and CPU PyTorch (9929e71) first
lost two steps; with its five defects fixed (7274965) one more artifact runs (SV2TTS,
row 16) and one drift row (STRIP, row 4) gets past drift to the artifact's own defect.
Each artifact that still fails now fails for its own reason (classified below), not
because of reprobe.

Agreement between "ran cleanly" and the badges is low (7274965): 2/18 Reproduced, 0/5 Reusable,
3/12 Functional. This is expected and is not evidence against the badges. Evaluators
follow READMEs: they run shell scripts, pass arguments, download data, and use GPUs
and clusters. reprobe without an LLM runs only what it can identify as an entry point.
A badge records that a human could make the artifact work. It does not mean the
artifact runs unattended.

## reprobe defects found and fixed (all with regression tests)

| artifact (row) | defect | commit |
|---|---|---|
| ICC, Ribbon (23, 26) | Zenodo deposit `Owner/repo-tag.zip` one folder down was not extracted | 18b4796 |
| TuxBot (36) | a release as both `.zip` and `.tar.gz` was extracted neither | 18b4796 |
| CacheSched (18) | `__MACOSX/._*.ipynb` resource forks ran as 26 notebook steps | 18b4796 |
| CacheSched (18) | notebooks under `docs/` of a bundled framework ran as the analysis | 18b4796 |
| dcp, SV2TTS (33, 16) | a script failed to import its own package from an ancestor folder | 18b4796 |
| 14 artifacts | nothing ran, yet the note said "other scripts were found to run the analysis" | 18b4796 |
| JStap (3) | 26 package modules reported as "1 document" | 390009f |
| STRIP (4) | `import keras` installed Keras 3 without its TensorFlow backend | 390009f |
| CacheSched (18) | one conflict (`guidance` vs. `vllm`) left all 60 scanned imports uninstalled | ea2fe94 |
| scalingattacks (15) | `cv2` mapped to the GUI OpenCV wheel, which needs libxcb | ea2fe94 |
| scalingattacks (15) | TensorFlow 1.13 pins need Python 3.7, which was not a candidate | ea2fe94 |
| sandbox CI | OOM kill read as plain SIGKILL when moby reset `OOMKilled` | d4fdc68 |
| sosp-paper211-ae, phoenix (28, 34) | scripts two folders down went unmentioned when nothing ran | 13c4546 |
| rethinking-serverless (22) | Python 3.13 for pins with wheels up to 3.10 only (found before this set) | 58980d3 |
| RE-Tool (6) | R plot with `family="Arial"` and no device failed under Rscript | 44555a0 |
| SV2TTS, CacheSched (16, 18) | guessed `torch` imports pulled ~3 GB of CUDA wheels and filled the disk | aca5c4d |
| STRIP, cluster-resource-forecast, POP (4, 20, 30) | open dependencies installed as of today, not of the artifact's date | 9929e71 |
| STRIP (4) | artifact dated by a 2024 README edit; code from 2019 | 7274965 |
| STRIP (4) | as-of releases pinned separately (`==`) conflict: TensorFlow 2.18 vs numpy 2.1.3 | 7274965 |
| ICS concealment (8) | as-of ipython 8.14 chose Python 3.9 over the pinned scikit-learn 0.23.2 (no 3.9 wheel) | 7274965 |
| ICS concealment (8) | detected imports ignored the author's exact pins (tensorflow 2.9.1 came back as 2.20) | 7274965 |
| POP (30) | conda-forge solved pip < 26, so `--uploaded-prior-to` never applied (numpy 2 for 2021) | 7274965 |
| SV2TTS, POP (16, 30) | an as-of release without a wheel (lws 1.2.7, future 0.18.2) was left out | 7274965 |
| 8 LLM proposals | LLM run order accepted README.md, LICENSE, .json, Dockerfile; no README for one-folder deposits | 67a7f2b |
| LLM run order | `.qmd` accepted as runnable; there is no Quarto runner | d00fc3e |
| 10 LLM proposals | a discarded proposal left no reason in the report | 2a2b0f3 |
| MAIAN (1) | proposal named `maian.py` for `tool/maian.py` and was discarded | a7175f2 |
| MAIAN (1) | assisted run with no detected steps got an environment planned for no code: no as-of install (web3 8.0 for a 2018 artifact) | e7bf3e0 |

Found and not yet fixed:

| artifact (row) | defect |
|---|---|
| MAIAN (1) | the as-of install chose Python 3.13 for a 2018 artifact: with no exact pins, the Python tie-break ignores the date. web3 3.16.5's 2018 dependencies have no 3.13 wheels, its current release conflicts with `rlp<=0.6.0`, and web3 was left out |
| scalingattacks (15) | each detected import is downloaded on its own: `opencv-python-headless<=4.8.0.76` resolves to 4.8 without seeing the pinned `numpy==1.16.3`, and the joint install then conflicts, so cv2 is missing |

## Every artifact in the last run (7274965), by cause

H = outside what reprobe can do without instructions (entry point only in README or
shell, arguments, build step, cluster or GPU). A = artifact defect (file not in the
deposit, Colab-only, Python 2). D = drift the artifact's date does not fix: the code
needs an API removed before that date. W = a dependency publishes no wheel and the
install phase builds no source while the network is open.

| row | artifact | badge | verdict | cause | class |
|---|---|---|---|---|---|
| 6 | RE-Tool-Usability-Artifacts | functional | runs 1/1 | — | — |
| 10 | fault-tolerant-secure-agg | functional | runs 6/6 | — | — |
| 16 | SV2TTS | functional | runs 1/1 | — (filled the disk with CUDA torch before aca5c4d) | — |
| 17 | randcompile | reproduced | runs 1/1 | — | — |
| 29 | hipress | reproduced | runs 1/1 | — | — |
| 21 | TraceSplitter | available | not run | Available only; nothing to execute required | — |
| 1 | MAIAN | functional | not run | CLI tool, needs a contract as argument | H |
| 3 | JStap | reusable | not run | package modules only; run as `python -m` with arguments | H |
| 7 | ACSAC22-Artifacts | functional | not run | 4 experiment scripts, none entry-named | H |
| 11 | popkorn-artifact | reusable | not run | 12 evaluation scripts, none entry-named | H |
| 12 | smartdns-study | functional | not run | analysis scripts not entry-named | H |
| 23 | ICC | functional | not run | 51 documents; Python only below two folders | H |
| 24 | LoRAFusion | reproduced | not run | GPU code; one script (`tools/tune_kernels.py`), not an entry point | H |
| 25 | PASS | reproduced | not run | 6 YCSB post-processing scripts, none entry-named | H |
| 26 | Ribbon | reproduced | not run | 2 scripts (`bayesian_optimization.py`, `util.py`), none entry-named | H |
| 27 | SOSP21_artifact_eval | reproduced | not run | experiment drivers (`run_experiment.py`), none entry-named | H |
| 28 | sosp-paper211-ae | reproduced | not run | 2 scripts in `scripts/multi/`; README-driven | H |
| 32 | caribou | functional | not run | AWS deployment | H |
| 34 | phoenix | reproduced | not run | build via `scripts/*.sh`; one helper script | H |
| 35 | I3DP | reproduced | not run | replay scripts not entry-named | H |
| 36 | TuxBot | reproduced | not run | datasets and documents; Python only below two folders | H |
| 14 | OAuthpaper-code | reproduced | fails 0/1 | `IndexError` on `sys.argv`: needs arguments | H |
| 15 | scalingattacks | functional | fails 0/1 | Cython extension needs `build_ext`; also a bogus `usenix` requirement | H |
| 18 | CacheSched | reproduced | fails 0/3 | two CLIs need arguments (the disk no longer fills) | H |
| 31 | acto | reproduced | fails 0/1 | needs a Kubernetes cluster | H |
| 33 | dcp | reproduced | fails 0/1 | `dcp_cpp` is a C++ extension to be built first | H |
| 2 | shredder | functional | fails 0/4 | Python 2 code (no Python 2 offered) | A |
| 5 | MAppGraph | reusable | fails 0/4 | `google.colab` imports; author requirements fail | A |
| 8 | ICS concealment | reusable | fails 4/6 | `../Data/BATADAL/train_dataset.csv` not in the repository | A |
| 9 | ENIDrift | functional | fails 0/2 | `data/labels.npy` not in the repository | A |
| 19 | agent-design-study | reproduced | fails 0/1 | `experiment_config.yaml` not in the repository | A |
| 22 | rethinking-serverless | functional | fails 1/8 | output folder `analysis_data/` not in the deposit | A |
| 4 | STRIP | reusable | fails 0/1 | `google.colab` import (Colab-only notebook); as of 2019-09 on Python 3.7 every import installed | A |
| 30 | POP | reproduced | fails 1/2 | `font_manager._rebuild`, removed in matplotlib 3.4 (2021-03); last code commit 2021-12 | D |
| 13 | dpra | reproduced | fails 0/6 | `seaborn` style, removed in matplotlib 3.8.0 (2023-09-15), 16 days before the last commit | D |
| 20 | cluster-resource-forecast | reproduced | fails 0/1 | apache-beam needs avro-python3, which has never shipped a wheel | W |

Totals: 5 run, 1 needs no run, 20 H, 7 A, 2 D, 1 W.

## Decisions implemented

1. **Open dependencies as of the artifact's date** (9929e71, fixed in 7274965). An
   artifact at least 365 days old (`install.as_of_min_age_days`) gets a fresh
   environment. Each open name is bounded by the newest release PyPI had on the date of
   the last commit that changed more than documentation (or the Zenodo record's
   creation). Exact pins are kept and choose the Python; dependencies of dependencies
   resolve as of that date with pip ≥ 26 (Python 3.9+). Effect: STRIP (4) moved from
   drift to its own defect. POP (30) and dpra (13) turned out not to be fixable by the
   date: their code needs APIs removed before it. cluster-resource-forecast (20) is
   blocked by the wheels-only install policy. No row that ran before regressed (6, 10,
   17, 29 unchanged; 8 back to 4/6).
2. **CPU PyTorch for guessed imports** (aca5c4d): torch, torchvision, torchaudio only,
   `--no-deps`, from download.pytorch.org/whl/cpu. SV2TTS (16) runs; CacheSched (18) no
   longer fills the disk and fails on missing arguments instead.
3. **LLM run-order proposals** (plan-only, `llm: plan-only` in benchmark.yml; Ollama
   gemma4:e4b, temperature 0; proposals are advisory and never executed, so no verdict
   changes). Results for the 20 H rows: `2026-11-22-7def8e8-llm/` and
   `2026-11-22-67a7f2b-llm/`.

   | | 7def8e8 | 67a7f2b (runnable files only, nested README) |
   |---|---|---|
   | proposal with only runnable files | 10 | 11 |
   | proposal naming README/LICENSE/config/data files | 8 | 0 (filtered) |
   | no proposal | 2 | 9 |

   Judged against the READMEs where checked: MAIAN (1) named `tool/maian.py` first in
   7def8e8 and nothing in 67a7f2b (a regression from the prompt change); JStap (3) and
   MAIAN need command-line arguments the schema cannot express; dcp (33) and Ribbon (26)
   moved from library modules to experiment drivers. With no argument field, none of
   the 20 proposals would run as proposed except possibly the notebook for
   scalingattacks (15), which also needs a Cython build. Executing proposals needed an
   `args` field and a separate result that never counts toward a badge: item 4.
4. **Assisted run** (`--assisted`, d00fc3e; fixed in 2a2b0f3, a7175f2, e7bf3e0;
   `llm: assisted` in benchmark.yml). The proposal carries each file's command-line
   arguments (at most 20 of 200 characters, guard-screened, shell-quoted). After the
   verdict, badges and summary are decided, the proposed steps run in the same sandbox;
   when nothing ran for the verdict, the environment is planned from the proposed steps.
   The result is stored under `llm.assisted_run` with `counted_in_verdict: false`.
   Results for the 20 H rows at e7bf3e0: `2026-11-22-e7bf3e0-assisted/` (first run with
   the defects: `2026-11-22-d00fc3e-assisted/`; discard reasons, plan-only:
   `2026-11-22-2a2b0f3-llm/`). Verdicts and steps are identical to 7274965 on all 20 rows.

   **No artifact ran under assisted execution.** 11 of 20 got a proposal of 26 steps; 25
   failed and one exited 0 without output (JStap, run with the placeholder argument
   `FOLDER_NAME`). Arguments were given for 2 of 26 steps: MAIAN's are the README's
   (`-s ParityWalletLibrary.sol WalletLibrary -c 0`), JStap's a placeholder.

   | row | artifact | proposed (e7bf3e0) | what stopped it |
   |---|---|---|---|
   | 1 | MAIAN | `tool/maian.py -s ParityWalletLibrary.sol WalletLibrary -c 0` | web3 not installed (reprobe defect, open: Python 3.13 for 2018) |
   | 3 | JStap | `pdg_generation/pdgs_generation.py FOLDER_NAME` | placeholder argument; exit 0, no output |
   | 7 | ACSAC22-Artifacts | 3 experiment scripts, no arguments | input folders `sample/`, `Initial/` not in the deposit |
   | 11 | popkorn-artifact | `angr_analysis/angr_full_blown.py` | angr did not install (pip ResolutionImpossible on Python 3.12) |
   | 12 | smartdns-study | 4 scripts, no arguments | `data/*.csv` not in the deposit (3); `NameError: ArgumentParser` (1) |
   | 14 | OAuthpaper-code | `Analyze_Pathconfusion.py`, no arguments | needs `sys.argv[1]` |
   | 15 | scalingattacks | `scaleatt/Tutorial-Detection.ipynb` | cv2 not installed (reprobe defect, open) |
   | 18 | CacheSched | 4 CLIs, no arguments | required arguments not given (4) |
   | 31 | acto | `reproduce_bugs.py` | ran; `ZeroDivisionError`: no bug reproduced without a Kubernetes cluster |
   | 33 | dcp | 8 scripts, no arguments | arguments (3), data not in deposit (3), C++ extension (1), Redis server (1) |
   | 35 | I3DP | `I3DP-1.0.0/run_benchmarks.py`, no arguments | needs `OPENROUTER_API_KEY` or `--llm-replay PATH` |
   | 24, 26, 34, 36 | LoRAFusion, Ribbon, phoenix, TuxBot | shell scripts (discarded) | no shell runner |
   | 23, 32 | ICC, caribou | README / QUICK_START.md (discarded) | not code |
   | 25, 28 | PASS, sosp-paper211-ae | nothing named | — |
   | 27 | SOSP21_artifact_eval | no JSON response within 900 s | — |

   By cause, over the 11 proposals: missing arguments 5 (14, 18, 33, 35, and the
   placeholder in 3), inputs not in the deposit 3 (7, 12, 33), dependencies not
   installed 3 (1, 11, 15; two of them reprobe defects), infrastructure 1 (31). The
   model reads arguments from the README excerpt (2000 characters) in 1 of 11 cases.
   It names shell scripts as the entry point for 4 to 5 of the 9 without a proposal,
   which agrees with the H classification above (README- and shell-driven).

   **The proposals are not reproducible from run to run.** Same prompt, same Ollama
   image digest, same model tag (`gemma4:e4b`), temperature 0, CPU runners: between
   2a2b0f3 and e7bf3e0 the response changed for 4 of the 11 rows without a proposal
   (23, 25, 26, 27), and between d00fc3e and e7bf3e0 the step lists of 18 (3 → 4) and
   33 (9 → 8) changed. An assisted result is evidence about one run of the model, not
   a property of the artifact. The model is pinned by tag only, not by digest as
   DESIGN.md §6 states, so its weights are not fixed either.
