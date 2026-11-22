# reprobe on 36 ACM-badged artifacts: before and after the fixes

Evaluation set: `benchmarks/acm-badged.csv` (selection rule and listing commits in
`benchmarks/acm-badged-selection.md`). All runs: GitHub Actions
(`.github/workflows/benchmark.yml`), `--no-llm`, per-step timeout 2700 s, 8 GiB memory
cap, no GPU. Raw per-artifact verdicts are in the `comparison.csv` of each run folder.

| run | commit | ran cleanly | not run | ran with failures | steps passed |
|---|---|---|---|---|---|
| baseline | 44555a0 | 4 | 16 | 16 | 12/75 |
| first fixes | 18b4796 | 4 | 16 | 16 | 15/52 |
| final | d4fdc68 | 4 | 16 | 16 | 15/52 |

**The headline number did not move.** The fixes removed wrong steps, wrong failures,
and silent omissions (75 to 52 steps: 23 fewer steps that should never have run; 3 more
passes). They did not make another artifact pass. Each artifact that still fails now
fails for its own reason (classified below), not because of reprobe.

Agreement between "ran cleanly" and the badges is low: 2/18 Reproduced, 0/5 Reusable,
2/12 Functional. This is expected and is not evidence against the badges. Evaluators
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
| sosp-paper211-ae, phoenix (28, 34) | scripts two folders down went unmentioned when nothing ran | after d4fdc68 |
| rethinking-serverless (22) | Python 3.13 for pins with wheels up to 3.10 only (found before this set) | 58980d3 |
| RE-Tool (6) | R plot with `family="Arial"` and no device failed under Rscript | 44555a0 |

## Every artifact in the final run, by cause

H = outside what reprobe can do without instructions (entry point only in README or
shell, arguments, build step, cluster or GPU). A = artifact defect (file not in the
deposit, Colab-only, Python 2). D = drift: an unpinned dependency resolved today has
removed an API. L = resource limit of the runner.

| row | artifact | badge | verdict | cause | class |
|---|---|---|---|---|---|
| 6 | RE-Tool-Usability-Artifacts | functional | runs 1/1 | — | — |
| 10 | fault-tolerant-secure-agg | functional | runs 6/6 | — | — |
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
| 18 | CacheSched | reproduced | fails 0/3 | two CLIs need arguments; scanned install filled the disk (CUDA torch) | H, L |
| 31 | acto | reproduced | fails 0/1 | needs a Kubernetes cluster | H |
| 33 | dcp | reproduced | fails 0/1 | `dcp_cpp` is a C++ extension to be built first | H |
| 2 | shredder | functional | fails 0/4 | Python 2 code (no Python 2 offered) | A |
| 5 | MAppGraph | reusable | fails 0/4 | `google.colab` imports; author requirements fail | A |
| 8 | ICS concealment | reusable | fails 4/6 | `../Data/BATADAL/train_dataset.csv` not in the repository | A |
| 9 | ENIDrift | functional | fails 0/2 | `data/labels.npy` not in the repository | A |
| 19 | agent-design-study | reproduced | fails 0/1 | `experiment_config.yaml` not in the repository | A |
| 22 | rethinking-serverless | functional | fails 1/8 | output folder `analysis_data/` not in the deposit | A |
| 4 | STRIP | reusable | fails 0/1 | `keras.utils.np_utils` removed in today's Keras | D |
| 20 | cluster-resource-forecast | reproduced | fails 0/1 | `DataFrame.first` removed in pandas 3 | D |
| 30 | POP | reproduced | fails 1/2 | `matplotlib.font_manager._rebuild` removed; author requirements fail | D |
| 13 | dpra | reproduced | fails 0/6 | kernel killed at the 8 GiB cap (3 notebooks) | L |
| 16 | SV2TTS | functional | fails 0/1 | scanned install filled the disk (CUDA torch wheels) | L |

Totals: 4 run, 1 needs no run, 20 H, 6 A, 3 D, 2 L (row 18 counted under H).

## Open decisions (not implemented)

1. **Drift (rows 4, 20, 30; 13 also uses a removed seaborn style).** Unpinned
   dependencies could be resolved as of the artifact's date (`pip
   --uploaded-prior-to`, already used for pinned deposits). That means installing them
   into a fresh environment instead of reusing base-py's packages: slower, but it
   matches what the authors had.
2. **CUDA PyTorch on a machine without a GPU (rows 16, 18).** The CPU wheels from
   download.pytorch.org are about 10x smaller. A second index is a dependency-confusion
   risk (the 2022 `torchtriton` incident), so it would be restricted to torch,
   torchvision and torchaudio with `--no-deps`.
3. **README-driven entry points (20 H rows: 15 not run, 5 failing).** These runs used `--no-llm`. Rerunning
   the set with the LLM-assisted planner measures how many H rows it recovers.
