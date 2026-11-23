## reprobe verdicts vs. awarded badges (20 artifacts)

| strongest badge | not-run | runs-with-failures | ran cleanly |
|---|---|---|---|
| reproduced | 8 | 4 | 0/12 |
| reusable | 2 | 0 | 0/2 |
| functional | 5 | 1 | 0/6 |

## Per artifact

| row | badges | verdict | steps passed | url |
|---|---|---|---|---|
| 1 | functional | not-run | 0/0 | https://github.com/MAIAN-tool/MAIAN |
| 3 | reusable | not-run | 0/0 | https://github.com/Aurore54F/JStap |
| 7 | functional | not-run | 0/0 | https://github.com/moesabry/ACSAC22-Artifacts |
| 11 | reusable | not-run | 0/0 | https://github.com/ucsb-seclab/popkorn-artifact/ |
| 12 | functional | not-run | 0/0 | https://github.com/GUSecLab/smartdns-study |
| 14 | reusable;reproduced | runs-with-failures | 0/1 | https://github.com/innotommy/OAuthpaper-code |
| 15 | functional | runs-with-failures | 0/1 | https://github.com/EQuiw/2023-detection-scalingattacks.git |
| 18 | available;functional;reproduced | runs-with-failures | 0/3 | https://doi.org/10.5281/zenodo.23205571 |
| 23 | available;functional | not-run | 0/0 | https://doi.org/10.5281/zenodo.13712326 |
| 24 | available;functional;reproduced | not-run | 0/0 | https://github.com/CentML/LoRAFusion |
| 25 | available;functional;reproduced | not-run | 0/0 | https://github.com/ddxxdd-code/PASS/tree/main |
| 26 | available;functional;reproduced | not-run | 0/0 | https://doi.org/10.5281/zenodo.5262865 |
| 27 | available;functional;reproduced | not-run | 0/0 | https://github.com/fsuri/SOSP21_artifact_eval |
| 28 | available;functional;reproduced | not-run | 0/0 | https://github.com/VeriGu/sosp-paper211-ae |
| 31 | available;functional;reproduced | runs-with-failures | 0/1 | https://github.com/xlab-uiuc/acto/tree/sosp-ae |
| 32 | available;functional | not-run | 0/0 | https://github.com/ubc-cirrus-lab/caribou |
| 33 | available;functional;reproduced | runs-with-failures | 0/1 | https://doi.org/10.5281/zenodo.16921000 |
| 34 | available;functional;reproduced | not-run | 0/0 | https://github.com/OrderLab/phoenix |
| 35 | available;functional;reproduced | not-run | 0/0 | https://doi.org/10.5281/zenodo.22025609 |
| 36 | available;functional;reproduced | not-run | 0/0 | https://doi.org/10.5281/zenodo.21285693 |

## LLM run-order proposals (advisory, never applied)

| row | heuristic steps | proposal |
|---|---|---|
| 1 | 0 | LLM proposed an alternative run order (advisory; never counted in the verdict): tool/maian.py -s ParityWalletLibrary.sol WalletLibrary -c 0 |
| 3 | 0 | LLM proposed an alternative run order (advisory; never counted in the verdict): pdg_generation/pdgs_generation.py FOLDER_NAME |
| 7 | 0 | LLM proposed an alternative run order (advisory; never counted in the verdict): 'Experiment Code/CreateSample.py', 'Experiment Code/RunConfExp.py', 'Experiment Code/RunIntExp.py' |
| 11 | 0 | LLM proposed an alternative run order (advisory; never counted in the verdict): angr_analysis/angr_full_blown.py |
| 12 | 0 | LLM proposed an alternative run order (advisory; never counted in the verdict): scripts/scrub_records.py, scripts/remove_disqualified.py, scripts/dns_knowledgeSpearman.py, scripts/dns_understanding_alluvium.py |
| 14 | 1 | LLM proposed an alternative run order (advisory; never counted in the verdict): Analyze_Pathconfusion.py |
| 15 | 1 | LLM proposed an alternative run order (advisory; never counted in the verdict): scaleatt/Tutorial-Detection.ipynb |
| 18 | 3 | LLM proposed an alternative run order (advisory; never counted in the verdict): CacheSched-artifact/benchmark/run.py, CacheSched-artifact/benchmark/extract.py, CacheSched-artifact/benchmark/analyze_slo.py, CacheSched-artifact/benchmark/skewness.py |
| 23 | 0 | LLM run-order proposal discarded: no proposed step is a runnable file in the tree (it named: README) |
| 24 | 0 | LLM run-order proposal discarded: no proposed step is a runnable file in the tree (it named: benchmarks_paper/scripts/setup/setup_env.sh, benchmarks_paper/scripts/run_all.sh) |
| 25 | 0 | LLM run-order proposal discarded: no proposed step is a runnable file in the tree (it named none) |
| 26 | 0 | LLM run-order proposal discarded: no proposed step is a runnable file in the tree (it named: all_scheme.sh) |
| 27 | 0 | LLM run-order proposal discarded: no JSON response (timeout, error or unparseable output) |
| 28 | 0 | LLM run-order proposal discarded: no proposed step is a runnable file in the tree (it named none) |
| 31 | 1 | LLM proposed an alternative run order (advisory; never counted in the verdict): reproduce_bugs.py |
| 32 | 0 | LLM run-order proposal discarded: no proposed step is a runnable file in the tree (it named: QUICK_START.md) |
| 33 | 1 | LLM proposed an alternative run order (advisory; never counted in the verdict): dcp-main/benchmark/microbenchmark/run_experiments.py, dcp-main/benchmark/mlm/pretrain_gpt.py, dcp-main/benchmark/mlm/run_experiments.py, dcp-main/benchmark/plotting/plot_microbenchmark_results.py, dcp-main/benchmark/plotting/plot_e2e_results.py, dcp-main/benchmark/preprocessing/draw_seqlen_distribution.py, dcp-main/benchmark/preprocessing/LongAlign/get_longalign_batches.py, dcp-main/benchmark/preprocessing/LongDataCollection/get_ldc_batches.py |
| 34 | 0 | LLM run-order proposal discarded: no proposed step is a runnable file in the tree (it named: scripts/env_setup.sh, scripts/build_kernel.sh, scripts/build_runtime.sh, experiments/redis/runvanilla.sh, experiments/redis/runphx.sh) |
| 35 | 0 | LLM proposed an alternative run order (advisory; never counted in the verdict): I3DP-1.0.0/run_benchmarks.py |
| 36 | 0 | LLM run-order proposal discarded: no proposed step is a runnable file in the tree (it named: functional_example/run.sh, functional_example/run.sh) |
