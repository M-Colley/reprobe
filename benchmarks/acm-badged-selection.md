# ACM-badged artifact evaluation set for reprobe

Companion to `acm-badged.csv` (36 artifacts).

## Listings used

Accessed 2026-11-22 by shallow `git clone` of the GitHub sources of both sites (the YAML front matter of each `results.md` is the data behind the rendered results page).

| listing | source repository | commit | rendered page pattern |
|---|---|---|---|
| sysartifacts | https://github.com/sysartifacts/sysartifacts.github.io | 673135d5f59cd74ba7c6b7a8f94e1ab630b25d71 (2026-11-11) | https://sysartifacts.github.io/{conf}/results.html |
| secartifacts | https://github.com/secartifacts/secartifacts.github.io | 8283e3f372884cf0f2eab84f1a7c7119d1e54fca (2026-11-12) | https://secartifacts.github.io/{conf}/results.html |

All `_conferences/*/results.md` files of both repositories were parsed (57 result sections, 2793 rows). Only sections that declare ACM badge images were in scope:

- sysartifacts (`acm_available_1.1.png`, `acm_functional_1.1.png`, `acm_reproduced_1.1.png`): EuroSys 2021-2026, SOSP 2019/2021/2023/2024/2025/2026, SC 2021, USENIX ATC 2026, CAIS 2026. Not in scope: sections with USENIX badge images (ATC 2022-2025, FAST, OSDI, ...) and sections without badge data.
- secartifacts (`acmartifactbadge_*.png`): ACSAC 2017-2023. Not in scope: ACSAC 2024-2025 (IEEE Xplore code badges), NDSS/USENIX Security/PETS/CHES/WOOT/S&P/SysTEX (not ACM badges).

Rows in scope: sysartifacts 559, secartifacts 181, total 740.

## Inclusion rule (verbatim)

An artifact is included in the population if it is a row of a results section listed above (sysartifacts: EuroSys 2021-2026, SOSP 2019-2026, SC 2021, ATC 2026, CAIS 2026; secartifacts: ACSAC 2017-2023), it holds at least one ACM badge in that listing, and, in this order: (1) the row has an artifact link (`artifact_url`, else `github_url`, else `repository_url`; bare `github.com/...` and bare DOIs are prefixed with https://, the literal MISSING counts as no link); (2) the link host is GitHub, GitLab.com, or a Zenodo record or Zenodo DOI (10.5281); (3) the link resolves (GitHub/GitLab/Zenodo API return 200 for the repository or record); (4) the primary language can be determined, from the GitHub/GitLab language statistics for repositories, or, for Zenodo records, from the uncompressed size of source files by extension inside the record's .zip files (read remotely via HTTP range requests) or from plain record files; records whose only files are tar/tgz/tar.gz/tar.xz/7z/rar archives, or that contain no source files, fail this step; (5) Python (incl. Jupyter Notebook) plus R (incl. R Markdown/Quarto) account for at least 50 % of the bytes of source code, where bytes of Shell, Makefile, Dockerfile, CMake, TeX, HTML, CSS, Batchfile, PowerShell, Roff, Nix, Starlark and M4 are not counted; (6) no docker-compose/compose YAML file in the repository (or archive) root; (7) none of the root-level README*, requirements*.txt, environment.y(a)ml, pyproject.toml, setup.py matches the case-insensitive regex `cuda|nvidia|cudnn|\bgpus?\b` (proxy for "needs a GPU"; it also excludes artifacts that merely mention GPUs).

Rows failing a criterion are excluded and recorded with the first failing criterion. All rows that pass are eligible.

## Subsetting rule

The eligible rows are sorted by paper title (case-folded, ties by conference id) and every 3rd row is taken, starting with the first (indices 0, 3, 6, ...). 106 eligible rows give 36 included rows. No manual choice was made. The sample was not stratified; the badge mix is whatever the systematic sample produced.

## Counts

Rows seen in scope: 740. Eligible: 106. Included: 36 (sysartifacts 19, secartifacts 17). Eligible but not sampled: 70.

Per listing rows in scope:
- sec: 181 rows
- sys: 559 rows

Outcome per row (first failing criterion; `eligible` = passes all):

| outcome | rows |
|---|---|
| 1 no artifact link in listing | 14 |
| 2 host not supported by reprobe | 65 |
| 3 link does not resolve | 15 |
| 4 language not determinable (tar/other archive, or no source files) | 102 |
| 5 not predominantly Python/R | 354 |
| 6 docker-compose at repo root | 4 |
| 7 GPU keyword in README/dependency file | 80 |
| eligible | 106 |

Included rows by badge set (as listed): available;functional;reproduced 15, functional 9, reusable 5, available;functional 3, reusable;reproduced 3, available 1.

## Method notes and limitations

- Tools: GitHub REST API (languages, contents), GitLab REST API (languages), Zenodo REST API, `remotezip` (https://pypi.org/project/remotezip/) for zip listings. Scripts are in the session scratchpad and are not part of this deliverable; the rule above is complete enough to re-implement.
- All 36 included `url` values were requested again (final check): the GitHub/GitLab pages returned HTTP 200; the 7 Zenodo DOIs returned 200 with curl (a Python client without browser User-Agent got 403 from doi.org/Zenodo) and their Zenodo API records returned 200.
- Language in the CSV is the top language by the byte rule above; the notes column gives the Python/R share.
- GitHub links are not pinned to a commit; the repository HEAD may differ from the evaluated state. Links given as `tree/<branch>` are included as repository links (the CSV keeps the listing's URL).
- Criteria 4 and 7 introduce selection effects: Zenodo records with only tar/other archives or without source files (102 rows: 85 unreadable archives, 17 without source files; many in ATC 2026) are systematically excluded, and the GPU keyword rule removes ML artifacts regardless of whether a GPU is actually required. GitLab repositories get no GPU/compose check (1 included repository: hipress).
- Criterion 2 excludes 65 rows: Bitbucket, Google Drive/Docs, project web pages, anonymous.4open.science, other GitLab instances, and Figshare/OSF/other-DOI links (reprobe supports Figshare and OSF, but language assessment was not implemented for them; their count is in the excluded table).

## Badge data quality

- secartifacts ACSAC lists no "Available" badge (functional, reusable, reproduced only), and `reusable` rows do not also list `functional` although ACM's Reusable implies Functional. Badges are copied as listed.
- Badge criteria are committee-specific and changed between years; the listings do not record which ACM badge version applied except through the image name.
- A listing records only badges awarded; artifacts submitted but rejected are not listed, so no negative class exists in this set.
- Reusable-only rows and the Reproduced badge in ACSAC 2023 (`reusable;reproduced`) are rare; per-level comparisons have small n.
- Some rows have badges in free-text variants (`Available, Functional`), normalised to lowercase, and ordered available;functional;reusable;reproduced.


## Eligible but not sampled (70 rows)

| conf | title | badges | link |
|---|---|---|---|
| acsac2017 | A Secure Mobile Authentication Alternative To Biometrics | functional | https://github.com/casprlab/ai.lock |
| eurosys2025 | Achilles: Efficient TEE-Assisted BFT Consensus via Rollback Resilient Recovery | available;functional;reproduced | https://doi.org/10.5281/zenodo.14830621 |
| acsac2018 | Analyzing Cache Side Channels Using Deep Neural Networks | functional | https://github.com/tianweiz07/DeepLearningSideChannel |
| acsac2020 | App-Agnostic Post-Execution Semantic Analysis of Android In-Memory Forensics Artifacts | functional | https://github.com/apphackuno/OAGen |
| acsac2021 | argXtract: Deriving IoT Security Configurations via Automated Static Analysis of Stripped ARM Cortex-M Binaries | reusable | https://github.com/projectbtle/argXtract |
| acsac2023 | Artemis: Defanging Software Supply Chain Attacks in Multi-repository Update Systems | reusable;reproduced | https://github.com/mnm678/artemis-artifacts |
| sosp2024 | Autobahn: Seamless high speed BFT | available;functional;reproduced | https://github.com/neilgiri/autobahn-artifact |
| acsac2020 | AVclass2: Massive Malware Tag Extraction from AV Labels | reusable | https://github.com/malicialab/avclass |
| acsac2023 | Binary Sight-Seeing: Accelerating Reverse Engineering via Point-of-Interest-Beacons | functional | https://github.com/UHH-ISS/binary-sight-seeing |
| sosp2025 | cache_ext: Customizing the Page Cache with eBPF | available;functional;reproduced | https://github.com/cache-ext/cache_ext |
| sc2021 | CAKE: Matrix Multiplication Using Constant-Bandwidth Blocks | available;functional;reproduced | https://doi.org/10.5281/zenodo.5148715 |
| eurosys2026 | Canopy: Property-Driven Learning for Congestion Control | available;functional | https://github.com/ldos-project/Canopy |
| sc2021 | Clairvoyant Prefetching for Distributed Machine Learning I/O | available | https://doi.org/10.5281/zenodo.5166929 |
| acsac2020 | Constrained Concealment Attacks against Reconstruction-based Anomaly Detectors in Industrial Control Systems | functional | https://github.com/scy-phy/ICS-Evasion-Attacks |
| sosp2024 | Cookie Monster: Efficient On-Device Budgeting for Differentially-Private Ad-Measurement Systems | available;functional;reproduced | https://github.com/columbia/cookiemonster |
| acsac2022 | Curiosity-Driven and Victim-Aware Adversarial Policies | functional | https://github.com/soarsmu/Curiosity_in_Adversarial_Policy |
| eurosys2026 | Demystifying Serverless Costs on Public Platforms: Bridging Billing, Architecture, and OS Scheduling | available;functional;reproduced | https://github.com/ubc-cirrus-lab/demystifying-serverless-costs |
| acsac2019 | Detecting organized eCommerce fraud using scalable categorical clustering | functional | https://github.com/SSGAalto/recagglo |
| acsac2020 | Double Patterns: A Usable Solution to Increase the Security of Android Unlock Patterns | functional | https://github.com/gwusec/2020-ACSAC-Double-Pattern-Artifacts |
| eurosys2026 | ECCB: Boosting Block Propagation of Blockchain with Erasure-Coded Compact Block | available;functional | https://github.com/cbysal/eccbae |
| sc2021 | Exploiting User Activeness for Data Retention in HPC Systems | available;functional | https://doi.org/10.5281/zenodo.5168853 |
| eurosys2022 | FaaSnap: FaaS Made Fast Using Snapshot-based VMs | available | https://doi.org/10.5281/zenodo.6360833 |
| eurosys2025 | Flex: Fast, Accurate DNN Inference on Low-Cost Edges Using Heterogeneous Accelerator Execution | available | https://github.com/Tanmoy058/Elastically-Scheduling-Multiple-DNN-Inference-Jobs-on-an-Edge-Device-for-High-Efficiency-and-Accurac/blob/main/README.md |
| cais2026 | FORGE: Self-Evolving Agent Memory With No Weight Updates via Population Broadcast | available;functional;reproduced | https://github.com/isbogdanov/forge-protocol |
| sosp2021 | FragPicker: A New Defragmentation Tool for Modern Storage Devices | available;functional;reproduced | https://github.com/jonggyup/FragPicker |
| acsac2019 | Function Boundary Detection in Stripped Binaries | functional | https://github.com/CenterforSecureAndDependableSystems/FunctionBoundary |
| sosp2023 | Grove: a Separation-Logic Library for Verifying Distributed Systems | available;functional;reproduced | https://github.com/mit-pdos/grove-artifact |
| eurosys2025 | HyperAlloc: Efficient VM Memory De/Inflation via Hypervisor-Shared Page-Frame Allocators | available;functional;reproduced | https://github.com/luhsra/hyperalloc-bench/pkgs/container/hyperalloc_ae |
| sosp2019 | I4: Incremental Inference of Inductive Invariants for Verification of Distributed Protocols | available;functional;reproduced | https://github.com/GLaDOS-Michigan/I4 |
| eurosys2026 | In-Production Characterization of an Open Source Serverless Platform and New Scaling Strategies | available;functional;reproduced | https://github.com/ubc-cirrus-lab/femux |
| eurosys2026 | iRoute: Local Routing Table-based Workflow Management in Serverless Computing | available;functional | https://github.com/tanksys/iRoute |
| sosp2026 | It's the Kernel's Fault! Custom Page Fault Handling With bpf_fault | available;functional;reproduced | https://zenodo.org/records/22150443 |
| acsac2021 | LaserShark: Establishing Fast, Bidirectional Communication into Air-Gapped Systems | reusable | https://github.com/intellisec/lasershark |
| eurosys2026 | LCMP: Distributed Long-Haul Cost-Aware Multi-Path Routing for Inter-Datacenter RDMA Networks | available;functional;reproduced | https://github.com/dyyuCS/LCMP |
| sosp2019 | Lineage Stash: Fault Tolerance Off the Critical Path | available;functional;reproduced | https://github.com/stephanie-wang/ray/tree/lineage-stash |
| sosp2024 | LoongServe: Efficiently Serving Long-Context Large Language Models with Elastic Sequence Parallelism | available;functional;reproduced | https://github.com/LoongServe/LoongServe |
| acsac2022 | Make Data Reliable: An Explanation-powered Cleaning on Malware Dataset Against Backdoor Poisoning Attacks | functional | https://github.com/wxt406611016/MDR |
| acsac2022 | Making Memory Account Accountable: Analyzing and Detecting Memory Missing-account bugs for Container Platforms | reusable | https://github.com/ZJU-SEC/MANTA |
| acsac2020 | Measuring the Effectiveness of Privacy Policies for Voice Assistant Applications | functional | https://github.com/voice-assistant-research/voice-assistant |
| acsac2022 | More is Better (Mostly): On the Backdoor Attacks in Federated Graph Neural Networks | functional | https://github.com/xujing1994/bkd_fedgnn |
| eurosys2021 | Odyssey: The Impact of Modern Hardware on Strongly-Consistent Replication Protocols | available | https://github.com/vasigavr1/Odyssey |
| eurosys2023 | OLPart: Online Learning based Resource Partitioning for Colocating Multiple Latency-Critical Jobs on Commodity Computers | available | https://doi.org/10.5281/zenodo.7726928 |
| acsac2020 | On the Feasibility of Automating Stock Market Manipulation | functional | https://github.com/carter-yagemann/Bot2Stock |
| acsac2019 | Opening Pandora's Box through ATFuzzer: Dynamic Analysis of AT Interface for Android Smartphones | functional | https://github.com/Imtiazkarimik23/ATFuzzer |
| cais2026 | optimize_anything: Unified Text Optimization can Outperform Specialized Systems | available;functional;reproduced | https://github.com/lukeleeai/optimize_anything_artifact |
| eurosys2021 | PaSh: Light-touch Data-Parallel Shell Processing | available;functional;reproduced | https://github.com/andromeda/pash |
| acsac2023 | PhishReplicant: A Language Model-based Approach to Detect Generated Squatting Domain Names | functional | https://github.com/tkoide398/PhishReplicant |
| acsac2020 | Policy-based Chameleon Hash for Blockchain Rewriting with Black-box Accountability | functional | https://github.com/SMC-SMU/Accountable-Blockchain-Rewriting |
| acsac2022 | Practical Binary Code Similarity Detection with BERT-based Transferable Similarity Learning | functional | https://github.com/asw0316/binshot |
| eurosys2024 | Pronghorn: Effective Checkpoint Orchestration for Serverless Hot-Starts | available;functional | http://doi.org/10.5281/zenodo.10089094 |
| sosp2023 | Pushing Performance Isolation Boundaries into Application with pBox | available;functional | https://github.com/OrderLab/pBox |
| eurosys2023 | R2C: AOCR-Resilient Diversity with Reactive and Reflective Camouflage | available;functional | https://doi.org/10.5281/zenodo.7728972 |
| eurosys2025 | Revealing the Unstable Foundations of eBPF-Based Kernel Extensions | available;functional;reproduced | https://github.com/ShawnZhong/DepSurf |
| acsac2019 | Revisiting Utility Metrics for Location Privacy-Preserving Mechanisms | functional | https://github.com/vrt1shjwlkr/Ride-Hailing-Service-Emulator |
| cais2026 | Robust Agent Compensation (RAC): Teaching AI Agents to Compensate | available;functional | https://zenodo.org/records/19753969 |
| eurosys2026 | Serverless Replication of Dynamic Objects across Clouds and Regions | available;functional;reproduced | https://github.com/pkusys/LambdaReplicaCLI |
| eurosys2021 | Site-to-Site Internet Traffic Control | available;functional;reproduced | https://github.com/bundler-project/evaluation |
| acsac2022 | Snappy: Efficient Fuzzing with Adaptive and Mutable Snapshots | functional | https://github.com/vusec/fuzzbench-snappy |
| acsac2022 | StateDiver: Testing Deep Packet Inspection Systems with State-Discrepancy Guidance | reusable | https://github.com/StateDiver/StateDiver |
| acsac2022 | Stepping out of the MUD: Contextual threat information for IoT devices with manufacturer-provided behavior profiles | reusable | https://github.com/lucamrgs/MUDscope |
| sosp2024 | SWARM: Replicating Shared Disaggregated-Memory Data in No Time | available;functional;reproduced | https://github.com/LPD-EPFL/swarm-artifacts |
| sc2021 | Systematically Inferring I/O Performance Variability by Examining Repetitive Job Behavior | available;functional;reproduced | https://doi.org/10.5281/zenodo.5236852 |
| sosp2026 | TensorDex: A Compact, Tensor-Centric Storage System for Modern AI Models | available;functional;reproduced | https://doi.org/10.5281/zenodo.22133305 |
| acsac2021 | They See Me Rollin': Inherent Vulnerability of the Rolling Shutter in CMOS Image Sensors | reusable | https://github.com/ssloxford/they-see-me-rollin |
| sosp2025 | TrainVerify: Equivalence-Based Verification for Distributed LLM Training | available;functional;reproduced | https://github.com/verify-llm/TrainVerify |
| acsac2023 | Triereme: Speeding up hybrid fuzzing through efficient query scheduling | reusable;reproduced | https://github.com/vusec/fuzzbench-triereme |
| atc2026 | Understanding and Detecting Performance Regressions in Modern Open-Source Server Applications | available;functional | https://doi.org/10.5281/zenodo.23265238 |
| sosp2024 | Unearthing Semantic Checks for Cloud Infrastructure-as-Code Programs | available | https://github.com/824728350/Zodiac/tree/master |
| cais2026 | ViBench: A Benchmark on Vibe Coding | available;functional | https://zenodo.org/records/19881536 |
| acsac2018 | Wi Not Calling: Practical Privacy and Availability Attacks in Wi-Fi Calling | functional | https://github.com/sefcom/Wi-Fi-Calling-scource-code |

## Excluded rows (reason = first failing criterion)

| conf | title | reason | link |
|---|---|---|---|
| acsac2019 | A Game of "Cut and Mouse": Bypassing Antivirus by Simulating User Inputs | no artifact link in listing |  |
| atc2026 | DAG-KV: DAG-Guided KV Propagation for Structure-Aware Knowledge Reuse | no artifact link in listing |  |
| atc2026 | RS-Infer: A Fast and Secure LLM Inference System for Mobile Devices with Recallable Resource Isolation | no artifact link in listing |  |
| cais2026 | CAMI: Cost-Aware Agent-Guided Multi-Indexing for Semantic Retrieval | no artifact link in listing |  |
| eurosys2022 | DAMYSUS: Streamlined BFT Consensus Leveraging Trusted Components | no artifact link in listing |  |
| eurosys2022 | KASLR in the age of MicroVMs | no artifact link in listing |  |
| eurosys2023 | Accelerating Graph Mining Systems with Subgraph Reshaping | no artifact link in listing |  |
| eurosys2025 | ParallelEVM: Operation-Level Concurrent Transaction Execution for EVM-Compatible Blockchains | no artifact link in listing |  |
| sc2021 | Chimera: Efficiently Training Large-Scale Neural Networks with Bidirectional Pipelines | no artifact link in listing |  |
| sosp2019 | AutoMine: Harmonizing High-Level Abstraction and High Performance for Graph Mining | no artifact link in listing |  |
| sosp2021 | CLoF: A Compositional Lock Framework for Multi-level NUMA Systems | no artifact link in listing |  |
| sosp2023 | Flexible Advancement in Asynchronous BFT Consensus | no artifact link in listing |  |
| sosp2025 | Managing Scalable Direct Storage Accesses for GPUs with GoFS | no artifact link in listing |  |
| sosp2025 | Proto: A Guided Journey through Modern OS Construction | no artifact link in listing |  |
| acsac2017 | Decanter: Detection Of Anomalous Outbound Http Traffic By Passive Application Fingerprinting | host not supported by reprobe | http://scs.ewi.utwente.nl/downloads/show,Data%20Exfiltration%20Malware%20(DEM)/ https://github.com/rbortolameotti/decanter |
| acsac2017 | Grid Shock: Coordinated Load-change Attacks On Power Grids | host not supported by reprobe | http://seclab.tuwien.ac.at/people/atrox/gridshock-dabrowski.zip |
| acsac2017 | Measuring Popularity Of Cryptographic Libraries In Internet-wide Scans | host not supported by reprobe | https://crocs.fi.muni.cz/public/papers/acsac2017 https://github.com/crocs-muni/classifyRSAkey |
| acsac2017 | Picky Attackers: Quantifying The Role Of System Properties On Intruder Behavior | host not supported by reprobe | https://drive.google.com/uc?id=1q5jZin2KozCnkX-PDVleEU7dqybbGX4S&export=download |
| acsac2017 | Quasar: Quantitative Attack Space Analysis And Reasoning | host not supported by reprobe | https://drive.google.com/open?id=1aLmAwEXR1g3hAJeQwThn6JDpOI6Ff_u_ |
| acsac2017 | Secure And Efficient Software-based Attestation For Industrial Control Devices With Arm Processors | host not supported by reprobe | http://www.illinois.adsc.com.sg/attestation/Attestation-ADSC-Release-2017.zip |
| acsac2017 | Vulcan: Efficient Component Authentication And Software Isolation For Automotive Control Networks | host not supported by reprobe | https://distrinet.cs.kuleuven.be/software/vulcan/ https://github.com/sancus-pma/vulcan/ |
| acsac2018 | A Measurement Study of Authentication Rate-Limiting Mechanisms of Modern Websites | host not supported by reprobe | https://docs.google.com/spreadsheets/d/1K2Y5PH1jIhhIbf_8FDb2YvqZTHFLNHjzlY75QiOYs90/edit?usp=sharing |
| acsac2018 | An Extensive Evaluation of the Internet's Open Proxies | host not supported by reprobe | https://openproxies-acsac18.github.io/ |
| acsac2018 | An Historical Analysis of the SEAndroid Policy Evolution | host not supported by reprobe | http://imbumjin.github.io/gitmining_sepolicy.tar.gz |
| acsac2018 | Noise Matters: Using Sensor and Process Noise Fingerprint to Detect Stealthy Cyber Attacks and Authenticate sensors in CPS | host not supported by reprobe | https://itrust.sutd.edu.sg/research/dataset/dataset_characteristics/#swat |
| acsac2018 | SENSS Against Volumetric DDoS Attacks | host not supported by reprobe | https://steel.isi.edu/projects/SENSS/ACSAC2018/ |
| acsac2018 | SmarTor: Smarter Tor with Smart Contracts | host not supported by reprobe | https://greubelhome.ddns.net/index.php/s/NY2BxWnd88LP8XN |
| acsac2018 | TIFF: Using Input Type Inference To Improve Fuzzing | host not supported by reprobe | https://www.vusec.net/projects/#testing |
| acsac2019 | Co-Evaluation of Pattern Matching Algorithms on IoT Devices with Embedded GPUs | host not supported by reprobe | https://bitbucket.org/mpastyl/acsac_pattern_matching_benchmark_opencl/src/master/ |
| acsac2019 | Will You Trust This TLS Certificate? Perceptions of People Working in IT | host not supported by reprobe | https://crocs.fi.muni.cz/public/papers/acsac2019 |
| acsac2019 | WooKey: Designing a Trusted and Efficient USB Device | host not supported by reprobe | https://wookey-project.github.io/ |
| acsac2020 | Dragonblood is Still Leaking: Practical Cache-based Side-Channel in the Wild | host not supported by reprobe | https://gitlab.inria.fr/ddealmei/poc-iwd-acsac2020 |
| acsac2020 | Februus: Input Purification Defense Against Trojan Attacks on Deep Neural Network Systems | host not supported by reprobe | https://februustrojandefense.github.io/ |
| acsac2020 | Practical Fine-Grained Binary Code Randomization+ | host not supported by reprobe | http://seclab.cs.sunysb.edu/soumyakant/acsac-artifact.tar.gz |
| acsac2020 | Quantifying measurement quality and load distribution in Tor | host not supported by reprobe | https://go.uniwue.de/20 |
| acsac2020 | SAIBERSOC: Synthetic Attack Injection to Benchmark and Evaluate the Performance of Security Operation Centers | host not supported by reprobe | https://gitlab.tue.nl/saibersoc/acsac2020-artifacts/ |
| acsac2020 | Security Study of Service Worker Cross-Site Scripting. | host not supported by reprobe | https://u.tamu.edu/sw-scanner |
| acsac2020 | Understanding User Perceptions of Security and Privacy for Group Chat: A Survey of Users in the US and UK | host not supported by reprobe | https://bitbucket.org/user-lab/oesch2020understanding/src/master/ |
| acsac2021 | CommanderGabble: A Universal Attack Against ASR Systems Leveraging Fast Speech | host not supported by reprobe | https://commandergabble.info/ |
| acsac2021 | Eluding ML-based Adblockers With Actionable Adversarial Examples | host not supported by reprobe | https://drive.google.com/file/d/1NXgE7zZmZdT1dPtbAkhNuxuETBU0p1xL/view?usp=sharing |
| acsac2021 | What's in a Cyber Threat Intelligence sharing platform?: A mixed-methods user experience investigation of MISP | host not supported by reprobe | https://www.dropbox.com/sh/yzbsw0hwok84wdt/AAANthZlzQuse8Tegjf4yQ0Ea?dl=0 |
| acsac2022 | BayesImposter: Bayesian Estimation Based.bss Imposter Attack on Industrial Control Systems | host not supported by reprobe | https://sites.google.com/view/bayesmem/home |
| acsac2022 | DitDetector: Bimodal Learning based on Deceptive Image and Text for Macro Malware Detection | host not supported by reprobe | https://gitee.com/yjasper/dit-detector |
| acsac2022 | Drone Authentication via Acoustic Fingerprint | host not supported by reprobe | https://gitfront.io/r/user-4171440/bJkdSdUFd9b6/authentication-ACSAC/ |
| acsac2022 | FAuST: Striking a Bargain between Forensic Auditing's Security and Throughput | host not supported by reprobe | https://bitbucket.org/sts-lab/faust |
| acsac2022 | One Fuzz Doesn't Fit All: Optimizing Directed Fuzzing via Target-tailored Program State Restriction | host not supported by reprobe | https://drive.google.com/drive/folders/1ZnHr28Quk7OiGv8c-o63aecr0KS-GOUh?usp=sharing |
| acsac2022 | SpacePhish: The Evasion-space of Adversarial Attacks against Phishing Website Detectors using Machine Learning | host not supported by reprobe | https://spacephish.github.io/ |
| acsac2022 | Spacelord: Private and Secure Smart Space Sharing | host not supported by reprobe | https://project-spacelord.github.io/acsac22-ae/ |
| acsac2022 | Transformer-Based Language Models for Software Vulnerability Detection | host not supported by reprobe | https://bitbucket.csiro.au/users/jan087/repos/acsac-2022-submission/browse |
| acsac2023 | ANDetect: A Third-party Ad Network Libraries Detection Framework for Android Applications | host not supported by reprobe | https://sites.google.com/view/andetect |
| acsac2023 | Can Large Language Models Provide Security & Privacy Advice? Measuring the Ability of LLMs to Refute Misconceptions | host not supported by reprobe | https://osf.io/xq37z/?view_only=00c450b4baef41979eae73f9c971e095 |
| acsac2023 | DOPE: DOmain Protection Enforcement with PKS | host not supported by reprobe | https://extgit.iaik.tugraz.at/sesys/dope |
| acsac2023 | Detection of Anomalies in Electric Vehicle Charging Sessions | host not supported by reprobe | https://code.fbi.h-da.de/seacop/ev-charging-ids-data-sets/ |
| acsac2023 | Domain and Website Attribution beyond WHOIS | host not supported by reprobe | https://hub.docker.com/repository/docker/dianecode/whosedomain/general |
| acsac2023 | FLARE: Fingerprinting Deep Reinforcement Learning Agents using Universal Adversarial Masks | host not supported by reprobe | https://anonymous.4open.science/r/FLARE-04F8/README.md |
| acsac2023 | From Attachments to SEO: Click Here to Learn More about Clickbait PDFs! | host not supported by reprobe | https://www.kaggle.com/datasets/emerald101/from-attachments-to-seo |
| acsac2023 | Global Analysis with Aggregation-based Beaconing Detection across Large Campus Networks | host not supported by reprobe | https://anonymous.4open.science/r/bcndetection-55E0 |
| acsac2023 | Lightweight Privacy-Preserving Proximity Discovery for Remotely-Controlled Drones | host not supported by reprobe | https://anonymous.4open.science/r/lppd-CC94/ |
| acsac2023 | PSP-Mal: Evading Malware Detection via Prioritized Experience-based Reinforcement Learning with Shapley Prior | host not supported by reprobe | https://anonymous.4open.science/r/PSP-Mal-1B74 |
| eurosys2023 | Hi-Speed DNN Training with Espresso: Unleashing the Full Potential of Gradient Compression with Near-Optimal Usage Strategies | host not supported by reprobe | https://doi.org/10.6084/m9.figshare.22233487.v1 |
| eurosys2023 | Viper: A Fast Snapshot Isolation Checker | host not supported by reprobe | https://doi.org/10.6084/m9.figshare.22223914.v1 |
| eurosys2024 | CDMPP: A Device-Model Agnostic Framework for Latency Prediction of Tensor Programs | host not supported by reprobe | http://doi.org/10.6084/m9.figshare.24156084 |
| eurosys2024 | Improving GPU Energy Efficiency through an Application-transparent Frequency Scaling Policy with Performance Assurance | host not supported by reprobe | http://doi.org/10.6084/m9.figshare.24523315.v1 |
| eurosys2024 | Model Selection for Latency-Critical Inference Serving | host not supported by reprobe | http://doi.org/10.6084/m9.figshare.22721161.v2 |
| sc2021 | DistGNN: scalable distributed training for large-scale graph neural networks | host not supported by reprobe | https://doi.org/10.1145/3476483 |
| sc2021 | Dr. Top-k: delegate-centric Top-k on GPUs | host not supported by reprobe | https://doi.org/10.1145/3476484 |
| sc2021 | Hybrid, scalable, trace-driven performance modeling of GPGPUs | host not supported by reprobe | https://doi.org/10.1145/3476485 |
| sc2021 | Minimizing privilege for building HPC containers | host not supported by reprobe | https://doi.org/10.6084/m9.figshare.14396099 |
| sc2021 | PEPPA-X: finding program test inputs to bound silent data corruption vulnerability in HPC applications | host not supported by reprobe | https://doi.org/10.1145/3476481 |
| sc2021 | Representation of Women in High-Performance Computing Conferences | host not supported by reprobe | https://doi.org/10.1145/3476480 |
| sc2021 | Tensor processing primitives: a programming abstraction for efficiency and portability in deep learning workloads | host not supported by reprobe | https://doi.org/10.1145/3476482 |
| sosp2019 | Scaling symbolic evaluation for automated verification of systems code with Serval | host not supported by reprobe | https://unsat.cs.washington.edu/projects/serval/ |
| sosp2021 | Exploiting Nil-Externality for Fast Replicated Storage | host not supported by reprobe | https://bitbucket.org/aganesan4/skyros/ |
| sosp2021 | HeMem: Scalable Tiered Memory Management for Big Data Applications and Real NVM | host not supported by reprobe | https://bitbucket.org/ajaustin/hemem/src/sosp-submission/ |
| sosp2023 | Blueprint: A Toolchain for Highly-Reconfigurable Microservice Applications | host not supported by reprobe | https://gitlab.mpi-sws.org/cld/blueprint/blueprint-compiler |
| sosp2023 | Mira: A Program-Behavior-Guided Far Memory System | host not supported by reprobe | https://bitbucket.org/mira-sosp23/mira-ae |
| sosp2025 | FlexGuard: Fast Mutual Exclusion Independent of Subscription | host not supported by reprobe | https://gitlab.inria.fr/flexguard/flexguard |
| sosp2025 | Oasis: Pooling PCIe Devices Over CXL to Boost Utilization | host not supported by reprobe | https://bitbucket.org/yuhong_zhong/oasis |
| sosp2026 | Cardamom: A Multi-GPU Framework For TB-Scale Encrypted Inference | host not supported by reprobe | https://doi.org/10.1184/R1/33340515 |
| acsac2017 | Automated Analysis Of Secure Internet Of Things Protocols | link does not resolve (GitHub API 404) | https://github.com/jun-kim/Automated-security-verification-of-IoT-protocols |
| acsac2018 | Improving Accuracy of Android Malware Detection with Lightweight Contextual Awareness | link does not resolve (GitHub API 404) | https://github.com/jallen89/pikadroid.git |
| acsac2020 | Verify&Revive: Secure Detection and Recovery of Compromised Low-end Embedded Devices | link does not resolve (GitHub API 404) | https://github.com/m3mmar/VerifyRevive |
| acsac2021 | Obfuscation Revealed: Leveraging Electromagnetic Signals for Obfuscated Malware Classification | link does not resolve (GitHub API 404) | https://github.com/ahma-hub https://zenodo.org/record/5414107 |
| acsac2022 | Alphuzz: Monte Carlo Search on Seed-Mutation Tree for Coverage-Guided Fuzzing | link does not resolve (GitHub API 404) | https://github.com/artifact11/Alphuzz_overview.git |
| acsac2022 | CryptoGo: Automatic Detection of Go Cryptographic API Misuses | link does not resolve (GitHub API 404) | https://github.com/CryptoGoIIE/cryptogo |
| eurosys2021 | M3: End-to-End Memory Management in Elastic System Software Stacks | link does not resolve (no repo path) | https://github.com/dsrg-uoft |
| eurosys2021 | OFC: An Opportunistic Caching System for FaaS Platforms | link does not resolve (GitLab API 404) | https://gitlab.com/lenapster/faascache/-/tree/master/artifactEvaluation |
| eurosys2023 | Aggregate VM: Why Reduce or Evict VM's Resources When You Can Borrow Them From Other Nodes? | link does not resolve (Zenodo API 410) | https://doi.org/10.5281/zenodo.7725803 |
| eurosys2025 | CAPSys: Contention-aware task placement for data stream processing | link does not resolve (Zenodo API 410) | https://zenodo.org/doi/10.5281/zenodo.13717642 |
| eurosys2025 | Garbage Collection Does Not Only Collect Garbage: Piggybacking-Style Defragmentation for Deduplicated Backup Storage | link does not resolve (GitHub API 404) | https://github.com/Borelset/GCCDF |
| sosp2021 | dSpace: Composable Abstractions for Smart Spaces | link does not resolve (GitHub API 404) | https://github.com/digi-project/sosp21-artifact |
| sosp2023 | Achieving Microsecond-Scale Tail Latency Efficiently with Approximate Optimal Scheduling | link does not resolve (GitHub API 404) | https://github.com/m8/concord |
| sosp2023 | Antipode: Enforcing Cross-Service Causal Consistency in Distributed Applications | link does not resolve (no repo path) | https://github.com/Antipode-SOSP23 |
| sosp2024 | TrEnv: Transparently Share Serverless Execution Environments Across Different Functions and Nodes | link does not resolve (no repo path) | https://github.com/switch-container |
| acsac2018 | Side-Channel Analysis of SM2: A Late-Stage Featurization Case Study | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.1436828 |
| acsac2019 | Privacy Preserving Substring Search Protocol with Polylogarithmic Communication Cost | language not determinable (tar/other archive, or no source files) | https://dx.doi.org/10.5281/zenodo.3384814 |
| acsac2023 | On the Feasibility of Cross-Language Detection of Malicious Packages in npm and PyPI | language not determinable (tar/other archive, or no source files) | https://zenodo.org/record/7945511 |
| atc2026 | BPF-Ext: Safely Extending the eBPF Compilation Pipeline with Native Operations | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.22907396 |
| atc2026 | Boomslang: A Modular Framework for Database Transaction Isolation Checking | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.23203856 |
| atc2026 | CacheFS: Peer-to-Peer Cooperative Caching for LLM Weight Distribution at Cloud Scale | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.23167350 |
| atc2026 | DeltaBox: Millisecond-Scale Sandbox Checkpoint and Rollback for Stateful AI Agents | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.23202153 |
| atc2026 | Detecting API Misuses Across Context Boundaries in Linux Kernel Drivers | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.23205620 |
| atc2026 | Dynamic Analysis of Unsafe Rust: Behaviors and Benchmarks | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.18420987 |
| atc2026 | EBFuzz: Breaking Hidden Structural Assumptions in Compilers via Extended Assembly Injection | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.23242185 |
| atc2026 | Efficient Training on Multiple Consumer GPUs with RoundPipe | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.23220089 |
| atc2026 | EntangledEdge: A Distributed System for Orbital Quantum Sensing and Learning | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.22909579 |
| atc2026 | EvoFuzz: IR-Aware Fuzzing for JIT Compilers through the Lens of Node Evolution | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.23212119 |
| atc2026 | FDP-Rocks: An Effective Use Case of FDP in RocksDB | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.22885948 |
| atc2026 | GPipeANN: Taming Throughput-Latency Tradeoff in GPU-SSD ANNS | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.23074357 |
| atc2026 | HeapLENS: Heap Layout Evaluation &#38; Navigation Suite | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.23206285 |
| atc2026 | IBTpoline: Flexible, Robust, and Intel IBT-compliant System Call Hooking | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.23201689 |
| atc2026 | MBCheck: Practical Static Analysis Framework for Detecting Memory Ordering Violations under WMM | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.22909905 |
| atc2026 | Model-as-a-Service in the Wild: Characterizing Production LLM Inference Clusters at Scale | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.23217994 |
| atc2026 | Mosaic: Efficient CPU–GPU Coordinated Block-Sparse Attention for Long-Context Decoding | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.23201742 |
| atc2026 | OpenNetLab: An Open Platform for AI-Based Bandwidth Estimation in Real-Time Communications | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.23253574 |
| atc2026 | Sharing is caring: Attestable and Trusted Cloud Pipelines out of Distrustful Components | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.23208984 |
| atc2026 | Spectra: Bandwidth-Faithful Data Placement for Tiered Memory | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.23203301 |
| atc2026 | SplineFS: Adaptive Learned Directory Indexing for Memory-Constrained Filesystems | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.23203474 |
| atc2026 | TensorBridge: Bridging Microscaled FP4 Weights to FP8 Tensor Cores for LLM Serving | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.23045885 |
| atc2026 | The GPU Changes Everything: Rethinking GPU I/O Stack at Massive Scale | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.23270957 |
| atc2026 | UniSketch: A Unified Sketch for Efficient General Flow Spread Measurement | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.23120734 |
| atc2026 | Zswap Boost: Accelerating Transparent Memory Compression in the Cloud | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.23221535 |
| atc2026 | fRAC: Remote Accelerator Calls on FPGAs | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.23200153 |
| eurosys2022 | BetrFS: A Compleat File System for Commodity SSDs | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.6345303 |
| eurosys2022 | Building an Efficient Key-Value Store in a Flexible Address Space | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.6345713 |
| eurosys2022 | Improving Scalability of Database Systems by Reshaping User Parallel I/O | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.6336004 |
| eurosys2022 | OS Scheduling with Nest: Keeping Tasks Close Together on Warm Cores | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.6344960 |
| eurosys2022 | PKRU-Safe: Automatically Locking Down the Heap Between Safe and Unsafe Languages | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.6336301 |
| eurosys2022 | VMSH: Hypervisor-agnostic Guest Overlays for VMs | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.6337102 |
| eurosys2022 | Verified Programs Can Party: Optimizing Kernel Extensions via Post-Verification In-Kernel Merging | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.6336103 |
| eurosys2023 | DRAMHiT: A Hash Table Architected for the Speed of DRAM | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.7719328 |
| eurosys2023 | DyTIS: A Dynamic Dataset Targeted Index Structure Simultaneously Efficient for Search, Insert, and Scan | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.7721895 |
| eurosys2023 | Effective Performance Issue Diagnosis with Value-Assisted Cost Profiling | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.7734627 |
| eurosys2023 | Efficient and Safe I/O Operations for Intermittent Systems | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.7735158 |
| eurosys2023 | Fail through the Cracks: Cross-System Interaction Failures in Modern Cloud Systems | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.7726172 |
| eurosys2023 | Groundhog: Efficient Request Isolation in FaaS | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.7703209 |
| eurosys2023 | Integrating Non-Volatile Main Memory in a Deterministic Database | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.7706922 |
| eurosys2023 | Unikernel Linux (UKL) | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.7737645 |
| eurosys2024 | Adaptable Runtime Monitoring for Intermittent Systems | language not determinable (tar/other archive, or no source files) | http://doi.org/10.5281/zenodo.10792132 |
| eurosys2024 | CSAL: the Next-Gen Local Disks for the Cloud | language not determinable (tar/other archive, or no source files) | http://doi.org/10.5281/zenodo.10086260 |
| eurosys2024 | Dordis: Efficient Federated Learning with Dropout-Resilient Differential Privacy | language not determinable (tar/other archive, or no source files) | http://doi.org/10.5281/zenodo.10023703 |
| eurosys2024 | Draconis: Network-Accelerated Scheduling for Micro-Scale Workloads | language not determinable (tar/other archive, or no source files) | http://doi.org/10.5281/zenodo.10688914 |
| eurosys2024 | DynaPipe: Optimizing Multi-task Training through Dynamic Pipelines | language not determinable (tar/other archive, or no source files) | http://doi.org/10.5281/zenodo.8413925 |
| eurosys2024 | Efficient Auditing of Event-driven Web Applications | language not determinable (tar/other archive, or no source files) | http://doi.org/10.5281/zenodo.10790870 |
| eurosys2024 | Enoki: High Velocity Linux Kernel Scheduler Development | language not determinable (tar/other archive, or no source files) | http://doi.org/10.5281/zenodo.10076583 |
| eurosys2024 | FLOAT: Federated Learning Optimizations with Automated Tuning | language not determinable (tar/other archive, or no source files) | http://doi.org/10.5281/zenodo.10791491 |
| eurosys2024 | HD-IOV: SW-HW Co-designed I/O Virtualization with Scalability and Flexibility for Hyper-Density Cloud | language not determinable (tar/other archive, or no source files) | http://doi.org/10.5281/zenodo.8361554 |
| eurosys2024 | On the Limitations of Carbon-Aware Temporal and Spatial Workload Shifting in the Cloud | language not determinable (tar/other archive, or no source files) | http://doi.org/10.5281/zenodo.10790855 |
| eurosys2024 | Puddles: Application-Independent Recovery and Location-Independent Data for Persistent Memory | language not determinable (tar/other archive, or no source files) | http://doi.org/10.5281/zenodo.8400339 |
| eurosys2024 | Serialization/Deserialization-free State Transfer in Serverless Workflows with RDMA-based Remote Memory Map | language not determinable (tar/other archive, or no source files) | http://doi.org/10.5281/zenodo.10078917 |
| eurosys2024 | SplitFT: Fault Tolerance for Disaggregated Datacenters via Remote Memory Logging | language not determinable (tar/other archive, or no source files) | http://doi.org/10.5281/zenodo.10076707 |
| eurosys2024 | TraceUpscaler: Upscaling Traces to Evaluate Systems at High Load | language not determinable (tar/other archive, or no source files) | http://doi.org/10.5281/zenodo.10042017 |
| eurosys2024 | Unison: A Parallel-Efficient and User-Transparent Network Simulation Kernel | language not determinable (tar/other archive, or no source files) | http://doi.org/10.5281/zenodo.10077300 |
| eurosys2025 | Efeu: generating efficient, verified, hybrid hardware/software drivers for I2C devices | language not determinable (tar/other archive, or no source files) | https://zenodo.org/doi/10.5281/zenodo.13734520 |
| eurosys2025 | Empowering WebAssembly with Thin Kernel Interfaces | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.14829424 |
| eurosys2025 | JABAS: Joint Adaptive Batching and Automatic Scaling for DNN Training on Heterogeneous GPUs | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.13827000 |
| eurosys2025 | Ladon: High-Performance Multi-BFT Consensus via Dynamic Global Ordering | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.13714937 |
| eurosys2025 | MEPipe: Democratizing LLM Training with Memory-Efficient Slice-Level Pipeline Scheduling on Cost-Effective Accelerators | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.14829560 |
| eurosys2025 | Pegasus: Transparent and Unified Kernel-Bypass Networking for Fast Local and Remote Communication | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.13714712 |
| eurosys2025 | RAKIS: Secure Fast I/O Primitives Across Trust Boundaries on Intel SGX | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.13800029 |
| eurosys2025 | SeBS-Flow: Benchmarking Serverless Cloud Function Workflows | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.14809924 |
| eurosys2025 | TUNA: Tuning Unstable and Noisy Cloud Applications | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.14872390 |
| eurosys2026 | A Case for Elastic Quantum Error Correction Decoders | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.18555903 |
| eurosys2026 | On-device Semantic Selection Made Low Latency and Memory Efficient with Monolithic Forwarding | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.18809731 |
| eurosys2026 | Queqiao: A Dynamic Binary Translation Framework for Efficient x86-64 Emulation on RISC-V | language not determinable (tar/other archive, or no source files) | https://zenodo.org/records/18611831 |
| eurosys2026 | Suika: Efficient and High-quality Re-scheduling of 3D-parallelized LLM Training Jobs in Shared Clusters | language not determinable (tar/other archive, or no source files) | https://zenodo.org/records/18617527 |
| eurosys2026 | TZ-LLM: Protecting On-Device Large Language Models with Arm TrustZone | language not determinable (tar/other archive, or no source files) | https://zenodo.org/records/17054270 |
| eurosys2026 | Untangling GPU Power Consumption: Job-Level Inference in Cloud Shared Settings | language not determinable (tar/other archive, or no source files) | https://zenodo.org/records/16981547 |
| sc2021 | Discovering and Balancing Fundamental Cycles in Large Signed Graphs | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.5148797 |
| sc2021 | Efficient Tensor Core-based GPU Kernels for Structured Sparsity under Reduced Precision | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.5136675 |
| sc2021 | HatRPC: Hint-Accelerated Thrift RPC over RDMA | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.5203281 |
| sc2021 | High Performance Uncertainty Quantification with Parallelized Multilevel Markov Chain Monte Carlo | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.5158988 |
| sc2021 | Index Launches: Scalable, Flexible Representation of Parallel Task Groups | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.5164404 |
| sc2021 | MAPA: Multi-Accelerator Pattern Allocation Policy for Multi-Tenant GPU Servers | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.5152741 |
| sc2021 | Non-Recurring Engineering (NRE) Best Practices: A Case Study with the NERSC/NVIDIA OpenMP Contract | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.5168093 |
| sc2021 | Online Evolutionary Batch Size Orchestration for Scheduling Deep Learning Workloads in GPU Clusters | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.5513082 |
| sc2021 | PAGANI: A Parallel Adaptive GPU Algorithm for Numerical Integration full strip note | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.5204819 |
| sc2021 | Paths to OpenMP in the Kernel | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.5164666 |
| sc2021 | SEEC: Stochastic Escape Express Channel | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.5171429 |
| sc2021 | The Hidden cost of the Edge: A Performance Comparison ofEdge and Cloud Latencies | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.5163851 |
| sosp2023 | Validating JIT Compilers via Compilation Space Exploration | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.8188346 |
| sosp2025 | Demeter: A Scalable and Elastic Tiered Memory Solution for Virtualized Cloud via Guest Delegation | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.16912877 |
| sosp2026 | Ahead-of-time Analysis of Shell Program Effects | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.22103126 |
| sosp2026 | Axiom: Achieving Determinism in LLM Inference | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.22058709 |
| sosp2026 | Beyond Utilization: Energy-Conscious GPU Sharing for Inference Serving | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.22114792 |
| sosp2026 | Big Bird: Resilient Privacy Budgeting Across Untrusted Web Domains | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.21983297 |
| sosp2026 | Bug-finding and proofs for platform configurations and hardware based on reference manuals | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.21976745 |
| sosp2026 | DDB: Source-Level Interactive Debugging for Distributed Applications | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.21971347 |
| sosp2026 | Democratizing MoE LLM Decoding via Barrier-Free Expert Parallelism | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.22100356 |
| sosp2026 | Efficient GPU Multitasking with Morphable Kernels | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.22104933 |
| sosp2026 | Flux: Unleashing Linux as a High-Performance Application Kernel | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.22124324 |
| sosp2026 | Lion: Modular Verification of Async Runtime Liveness | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.22061599 |
| sosp2026 | M2K: Making the Model-Kernel Interface Explicit for Reliable CUDA Kernel Verification | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.22106921 |
| sosp2026 | SANDHI: Fine-Grained Merging for Memory Efficient Multi-Model Serving | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.22108093 |
| sosp2026 | Sintr: Safe Interactive Transactions in the Presence of Byzantine Clients | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.22050053 |
| sosp2026 | Skill VM: Write Once, Run Everywhere Efficiently | language not determinable (tar/other archive, or no source files) | https://doi.org/10.5281/zenodo.22113152 |
| acsac2017 | Kakute: A Precise, Unified Information Flow Analysis System For Big-data Security | not predominantly Python/R (Scala, py/R 11%) | https://github.com/hku-systems/kakute |
| acsac2017 | Spinner: Semi-automatic Detection Of Pinning Without Hostname Verification (or Why 10m Bank Users Were Vulnerable) | not predominantly Python/R (Java, py/R 0%) | https://github.com/ChrisMcMStone/spinner |
| acsac2017 | Supplementing Modern Software Defenses With Stack-pointer Sanity | not predominantly Python/R (C++, py/R 1%) | https://github.com/bingseclab/spiglass |
| acsac2018 | DeDoS: Defusing DoS with Dispersion Oriented Software | not predominantly Python/R (C, py/R 2%) | https://github.com/dedos-project/DeDOS |
| acsac2018 | Hiding in the Shadows: Empowering ARM for Stealthy Virtual Machine Introspection | not predominantly Python/R (None, py/R 0%) | https://github.com/drakvuf-on-arm/drakvuf-on-arm |
| acsac2018 | MicroWalk: A Framework for Finding Side Channels in Binaries | not predominantly Python/R (C#, py/R 0%) | https://github.com/UzL-ITS/Microwalk |
| acsac2018 | Obscuro: A Bitcoin Mixer using Trusted Execution Environments | not predominantly Python/R (C, py/R 0%) | https://github.com/BitObscuro/Obscuro |
| acsac2018 | Osiris: Hunting for Integer Bugs in Ethereum Smart Contracts | not predominantly Python/R (None, py/R 0%) | https://github.com/christoftorres/Osiris |
| acsac2018 | Practical Integrity Protection with Oblivious Hashing | not predominantly Python/R (LLVM, py/R 0%) | https://github.com/tum-i22/sip-oblivious-hashing/tree/acsac |
| acsac2018 | StateDroid: Stateful Detection of Stealthy Attacks in Android Apps via Horn-Clause Verification | not predominantly Python/R (Java, py/R 0%) | https://github.com/mohsinjuni/statedroid |
| acsac2018 | Towards Automated Generation of Exploitation Primitives for Web Browsers | not predominantly Python/R (JavaScript, py/R 0%) | https://github.com/RUB-SysSec/PrimGen |
| acsac2018 | Type-after-Type: Practical and Complete Type-Safe Memory Reuse | not predominantly Python/R (C++, py/R 9%) | https://github.com/vusec/type-after-type |
| acsac2019 | FuzzBuilder: Automated building greybox fuzzing environment for C/C++ library | not predominantly Python/R (C++, py/R 22%) | https://github.com/hksecurity/FuzzBuilder |
| acsac2019 | Koinonia: Verifiable E-Voting with Long-term Privacy | not predominantly Python/R (C, py/R 0%) | https://github.com/gehuangyi20/Koinonia |
| acsac2019 | Out of Control: Stealthy Attacks Against Robotic Vehicles Protected by Control-based Techniques | not predominantly Python/R (C, py/R 2%) | https://github.com/DependableSystemsLab/stealthy-attacks |
| acsac2019 | PDoT: Private DNS-over-TLS with TEE Support | not predominantly Python/R (C, py/R 0%) | https://github.com/sprout-uci/PDoT |
| acsac2019 | Proof of Aliveness | not predominantly Python/R (C, py/R 0%) | https://github.com/ChengluJin/Proof_of_Aliveness |
| acsac2019 | SIP Shaker: Software Integrity Protection Composition | not predominantly Python/R (None, py/R 0%) | https://github.com/mr-ma/sip-shaker-artifact/ |
| acsac2019 | SecDATAVIEW: A Secure Big Data Workflow Management System for Heterogeneous Computing Environments | not predominantly Python/R (Java, py/R 0%) | https://github.com/shiyonglu/SecDATAVIEW |
| acsac2019 | Speculator: A Tool to Analyze Speculative Execution Attacks and Mitigations | not predominantly Python/R (C, py/R 24%) | https://github.com/ibm-research/speculator |
| acsac2020 | Cupid : Automatic Fuzzer Selection for Collaborative Fuzzing | not predominantly Python/R (None, py/R 0%) | https://github.com/egueler/cupid-artifact-eval |
| acsac2020 | DPIFuzz: A Differential Fuzzing Framework to Detect DPI Elusion Strategies for QUIC | not predominantly Python/R (Go, py/R 0%) | https://github.com/piano-man/DPIFuzz |
| acsac2020 | Faulty Point Unit: ABI Poisoning Attacks on Intel SGX | not predominantly Python/R (C++, py/R 1%) | https://github.com/fritzalder/faulty-point-unit |
| acsac2020 | HeapExpo: Pinpointing Promoted Pointers to Prevent Use-After-Free Vulnerabilities | not predominantly Python/R (C++, py/R 1%) | https://github.com/messlabnyu/heap-expo/ |
| acsac2020 | Practical Over-Threshold Multi-Party Private Set Intersection | not predominantly Python/R (C++, py/R 1%) | https://github.com/cryspuwaterloo/OT-MP-PSI |
| acsac2020 | Privacy-Preserving Production Process Parameter Exchange | not predominantly Python/R (C++, py/R 49%) | https://github.com/COMSYS/parameter-exchange |
| acsac2020 | RusTEE: Developing Memory-Safe ARM TrustZone Applications | not predominantly Python/R (Rust, py/R 0%) | https://github.com/sccommunity/rust-optee-trustzone-sdk |
| acsac2020 | Session Key Distribution Made Practical for CAN and CAN-FD Message Authentication | not predominantly Python/R (C++, py/R 7%) | https://github.com/yang-sec/CAN-SessionKey |
| acsac2020 | Up2Dep: Android Tool Support to Fix Insecure Code Dependencies | not predominantly Python/R (Java, py/R 4%) | https://github.com/ngcuongst/up2dep |
| acsac2020 | ZeroAUDIT | not predominantly Python/R (None, py/R 0%) | https://github.com/xfu2006/ACSAC20_Artifact |
| acsac2021 | A formal analysis of IKEv2's post-quantum extension | not predominantly Python/R (None, py/R 0%) | https://github.com/mnm-team/tamarin-ikev2 |
| acsac2021 | FlexFilt: Towards Flexible Instruction Filtering for Security | not predominantly Python/R (C, py/R 5%) | https://github.com/bu-icsg/FlexFilt |
| acsac2021 | ReCFA: Resilient Control-Flow Attestation | not predominantly Python/R (C++, py/R 0%) | https://github.com/suncongxd/ReCFA |
| acsac2021 | Repack Me If You Can: An Anti-Repackaging Solution Based on Android Virtualization | not predominantly Python/R (Java, py/R 0%) | https://github.com/totoR13/MARVEL |
| acsac2021 | Reproducible and Adaptable Log Data Generation for Sound Cybersecurity Experiments | not predominantly Python/R (None, py/R 0%) | https://github.com/ru37z/socbed-acsac-2021 |
| acsac2021 | RingRAM: A Unified Hardware SecurityPrimitive for IoT Devices that Gets Better with Age | not predominantly Python/R (Verilog, py/R 38%) | https://github.com/FoRTE-Research/RingRAM |
| acsac2021 | Rocky: Replicating Block Devices for Tamper and Failure Resistant Edge-based Virtualized Desktop Infrastructure | not predominantly Python/R (Java, py/R 2%) | https://github.com/Kaelus/Rocky |
| acsac2021 | Sipster: Settling IOU Privately and Quickly with Smart Meters | not predominantly Python/R (C, py/R 0%) | https://github.com/MobiSec-CSE-UTA/Sipster |
| acsac2021 | TEEKAP: Self-Expiring Data Capsule using Trusted Execution Environment | not predominantly Python/R (C++, py/R 0%) | https://github.com/MingyuanGao/TEEKAP |
| acsac2021 | VIA: Analyzing Device Interfaces of Protected Virtual Machines | not predominantly Python/R (None, py/R 0%) | https://github.com/file-citas/via |
| acsac2022 | Analysis of Payment Service Provider SDKs in Android | not predominantly Python/R (Scala, py/R 8%) | https://github.com/wspr-ncsu/aardroid |
| acsac2022 | Cloak: Transitioning States on Legacy Blockchains Using Secure and Publicly Verifiable Off-Chain Multi-Party Computation | not predominantly Python/R (C++, py/R 3%) | https://github.com/OxHainan/cloak.git |
| acsac2022 | DeView: Confining Progressive Web Applications by Debloating Web APIs | not predominantly Python/R (C++, py/R 1%) | https://github.com/shivamidow/deview |
| acsac2022 | Designing a Provenance Analysis for SGX Enclaves | not predominantly Python/R (Assembly, py/R 1%) | https://github.com/tregua87/sgxmonitor-artifact |
| acsac2022 | Formal Modeling and Security Analysis for Intra-level Privilege Separation | not predominantly Python/R (None, py/R 0%) | https://github.com/gyg128/Privilege-Centric-Model |
| acsac2022 | From Hindsight to Foresight: Enhancing Design Artifacts for Business Logic Flaw Discovery | not predominantly Python/R (Gherkin, py/R 0%) | https://github.com/cheh2/ACSAC-Artifacts |
| acsac2022 | Parallel Small Polynomial Multiplication for Dilithium: A Faster Design and Implementation | not predominantly Python/R (C, py/R 0%) | https://github.com/zhengjieyu/Dilithium-smallpoly |
| acsac2022 | Privacy-Preserving Trajectory Matching on Autonomous Unmanned Aerial Vehicles | not predominantly Python/R (None, py/R 0%) | https://github.com/DominikRoy/PPTM |
| acsac2022 | Randezvous: Making Randomization Effective on MCUs | not predominantly Python/R (C, py/R 1%) | https://github.com/URSec/Randezvous |
| acsac2022 | SLOPT: Bandit Optimization Framework for Mutation-Based Fuzzing | not predominantly Python/R (C, py/R 3%) | https://github.com/RICSecLab/SLOPTAFLpp |
| acsac2022 | Stopping Silent Sneaks: Defending against Malicious Mixes with Topological Engineering | not predominantly Python/R (None, py/R 0%) | https://github.com/sus0pid/BowTie-Artifacts |
| acsac2022 | Trebiz: Byzantine Fault Tolerance with Byzantine Merchants | not predominantly Python/R (Go, py/R 2%) | https://github.com/treble-h/trebiz |
| acsac2022 | TyPro: Forward CFI for C-Style Indirect Function Calls Using Type Propagation | not predominantly Python/R (C++, py/R 1%) | https://github.com/typro-type-propagation/TyPro-CFI/blob/master/ArtifactEvaluation.md |
| acsac2022 | User Perceptions of Five-Word Passwords | not predominantly Python/R (JavaScript, py/R 18%) | https://github.com/gwusec/2022-ACSAC-Five-word-Passwords |
| acsac2023 | Attack of the Knights: Non Uniform Cache Side Channel Attack | not predominantly Python/R (C++, py/R 2%) | https://github.com/farabimahmud/aok_ae |
| acsac2023 | Delegation of TLS Authentication to CDNs using Revocable Delegated Credentials | not predominantly Python/R (Go, py/R 0%) | https://github.com/revtls/revtls |
| acsac2023 | Detecting Weak Keys in Manufacturing Certificates: A Case Study | not predominantly Python/R (C++, py/R 2%) | https://github.com/andrewchi/mercury/blob/acsac23/doc/batch-gcd.md |
| acsac2023 | FLEDGE: Ledger-based Federated Learning Resilient to Inference and Backdoor Attacks | not predominantly Python/R (JavaScript, py/R 13%) | https://github.com/anonymouspapers8/FLEDGE |
| acsac2023 | Hades: Practical Decentralized Identity with Full Accountability and Fine-grained Sybil-resistance | not predominantly Python/R (Circom, py/R 0%) | https://github.com/didnet/Hades |
| acsac2023 | Mostree: Malicious Secure Private Decision Tree Evaluation with Sublinear Communication | not predominantly Python/R (C++, py/R 7%) | https://github.com/Jbai795/Mostree-pub |
| acsac2023 | No Forking Way: Detecting Cloning Attacks on Intel SGX Applications | not predominantly Python/R (C++, py/R 9%) | https://github.com/SamiraBriongos/Clonebuster/ |
| acsac2023 | PAVUDI: Patch-based Vulnerability Discovery using Machine Learning | not predominantly Python/R (C, py/R 8%) | https://github.com/SAP-samples/security-research-taintgraphs/tree/main/PAVUDI |
| acsac2023 | Remote Attestation of Confidential VMs Using Ephemeral vTPMs | not predominantly Python/R (Gnuplot, py/R 0%) | https://github.com/svsm-vtpm/SVSM-vTPM-artifacts |
| acsac2023 | Remote Attestation with Constrained Disclosure | not predominantly Python/R (C, py/R 0%) | https://github.com/DominikRoy/RACD/tree/master |
| acsac2023 | Secure MLaaS with Temper: Trusted and Efficient Model Partitioning and Enclave Reuse | not predominantly Python/R (Rust, py/R 37%) | https://github.com/tsinghua-ideal/TEMPER-Secure-MLaaS.git |
| acsac2023 | Secure Softmax/Sigmoid for Machine-learning Computation | not predominantly Python/R (Terra, py/R 39%) | https://github.com/alipay/Antchain-MPC/tree/sec_softmoid |
| acsac2023 | Secure and Lightweight ECU Attestations for Resilient Over-the-Air Updates in Connected Vehicles | not predominantly Python/R (None, py/R 0%) | https://github.com/cplappert/update-reporting |
| acsac2023 | Secure and Lightweight Over-the-Air Software Update Distribution for Connected Vehicles | not predominantly Python/R (C, py/R 2%) | https://github.com/cplappert/update-distribution |
| acsac2023 | When Push Comes to Shove: Empirical Analysis of Web Push Implementations in the Wild | not predominantly Python/R (Python, py/R 48%) | https://github.com/wpctsh/artifacts |
| atc2026 | Breaking Host-Wide Blocking: Boundary-Closure SCSI Error Handling | not predominantly Python/R (C, py/R 0%) | https://doi.org/10.5281/zenodo.23200821 |
| atc2026 | Chameleon: Adaptive Memory Overcommitment for Disaggregated Virtual Machines | not predominantly Python/R (C, py/R 2%) | https://doi.org/10.5281/zenodo.23093805 |
| atc2026 | FLASH: Fault-Led, Attested, Secure Hybrid-Key Checkpoint for Confidential GPU | not predominantly Python/R (C, py/R 2%) | https://doi.org/10.5281/zenodo.23199897 |
| atc2026 | FM2: Fast Live VM Migration in CXL Memory Pools | not predominantly Python/R (C, py/R 0%) | https://doi.org/10.5281/zenodo.23147401 |
| atc2026 | Faastion: Elastic and Scalable Native Library Isolation for High-Density Serverless Platforms | not predominantly Python/R (C, py/R 2%) | https://doi.org/10.5281/zenodo.23351569 |
| atc2026 | LifeSketch: Lifecycle-aware Active Flow Counting on Programmable Switches | not predominantly Python/R (C++, py/R 28%) | https://doi.org/10.5281/zenodo.23201840 |
| atc2026 | Lightweight Memory Tracing via General Binary Analysis and Offline Reconstruction | not predominantly Python/R (C++, py/R 29%) | https://doi.org/10.5281/zenodo.23424873 |
| atc2026 | Premature Hardware Binding is the Root of all Evil! Making Serverless Performance-Portable with Lazy Hardware Binding | not predominantly Python/R (Rust, py/R 20%) | https://doi.org/10.5281/zenodo.23319839 |
| atc2026 | Rattan: An Extensible and Scalable Modular Internet Path Emulator | not predominantly Python/R (Rust, py/R 1%) | https://doi.org/10.5281/zenodo.23267892 |
| atc2026 | Starfish: Fault-Tolerant Far Memory with Low Resource and Performance Overhead | not predominantly Python/R (C++, py/R 4%) | https://doi.org/10.5281/zenodo.23062383 |
| atc2026 | SubSan: Efficient Memory Error Detection via Subtraction Checks | not predominantly Python/R (C++, py/R 2%) | https://doi.org/10.5281/zenodo.23201930 |
| atc2026 | SwiftBaton: Dependency-Aware Staged Reconstruction for Live Migration of Stateful Containers | not predominantly Python/R (Go, py/R 1%) | https://doi.org/10.5281/zenodo.23203332 |
| cais2026 | How To Steer Your Multi-Agent System: Human-LLM Collaborative Planning | not predominantly Python/R (JavaScript, py/R 47%) | https://github.com/megagonlabs/ambipom |
| cais2026 | Securing Agents With Tracked Capabilities | not predominantly Python/R (Scala, py/R 0%) | https://github.com/lampepfl/tacit |
| cais2026 | Vista: Verifier-in-the-Loop Agentic Reinforcement Learning for Quantum Program Synthesis | not predominantly Python/R (OpenQASM, py/R 6%) | https://github.com/benyucong/rl-quantum |
| eurosys2021 | Accelerating Graph Sampling for Graph Machine Learning using GPUs | not predominantly Python/R (Cuda, py/R 13%) | https://github.com/abhijangda/nextdoor-experiments/ |
| eurosys2021 | Characterizing, Exploiting, and Detecting DMA Code Injection Vulnerabilities in the Presence of an IOMMU | not predominantly Python/R (Perl, py/R 0%) | https://github.com/Markuze/mmo-static |
| eurosys2021 | Confidential Computing for OpenPOWER | not predominantly Python/R (None, py/R 0%) | https://github.com/mvle/eurosys2021_PEF_OpenPOWER |
| eurosys2021 | DGCL: An Efficient Communication Library for Distributed GNN Training | not predominantly Python/R (C++, py/R 1%) | https://github.com/czkkkkkk/gccl |
| eurosys2021 | DZiG: Sparsity-Aware Incremental Processing of Streaming Graphs | not predominantly Python/R (C++, py/R 0%) | https://github.com/pdclab/graphbolt/tree/eurosys21-artifact |
| eurosys2021 | Finding Heterogeneous-Unsafe Configuration Parameters in Cloud Systems | not predominantly Python/R (None, py/R 0%) | https://github.com/StarThinking/ZebraConf |
| eurosys2021 | Home, SafeHome: Smart Home Reliability with Visibility and Atomicity | not predominantly Python/R (Java, py/R 12%) | https://github.com/Shegufta/SafeHomeFramework |
| eurosys2021 | Tahoe: Tree Structure-Aware High Performance Inference Engine for Decision Tree Ensemble on GPU | not predominantly Python/R (Cuda, py/R 0%) | https://github.com/zhen-xie/Tahoe |
| eurosys2021 | Understanding and Dealing with Hard Faults in Persistent Memory Systems | not predominantly Python/R (C++, py/R 1%) | https://github.com/OrderLab/Arthas/blob/master/artifactREADME.md |
| eurosys2021 | Unikraft: Fast, Specialized Unikernels the Easy Way | not predominantly Python/R (Assembly, py/R 2%) | https://github.com/unikraft/eurosys21-artifacts |
| eurosys2021 | Zeus: Locality-aware Distributed Transactions | not predominantly Python/R (TLA, py/R 0%) | https://github.com/ease-lab/Zeus |
| eurosys2021 | rkt-io: A Direct I/O Stack for Shielded Execution | not predominantly Python/R (C, py/R 18%) | https://github.com/Mic92/rkt-io |
| eurosys2022 | A New Benchmark Harness for Systematic and Robust Evaluation of Streaming State Stores | not predominantly Python/R (C, py/R 1%) | https://doi.org/10.5281/zenodo.6347736 |
| eurosys2022 | Beating the I/O bottleneck: A case for log-structured virtual disks | not predominantly Python/R (Go, py/R 7%) | https://doi.org/10.5281/zenodo.6354775 |
| eurosys2022 | Characterizing the Performance of Intel Optane Persistent Memory -- A Close Look at its On-DIMM Buffering | not predominantly Python/R (C++, py/R 3%) | https://doi.org/10.5281/zenodo.6342303 |
| eurosys2022 | DeepRest: Deep Resource Estimation for Interactive Microservices | not predominantly Python/R (Go, py/R 7%) | https://doi.org/10.5281/zenodo.6335690 |
| eurosys2022 | GNNLab: A Factored System for Sample-based GNN Training over GPUs | not predominantly Python/R (Python, py/R 47%) | https://doi.org/10.5281/zenodo.6347456 |
| eurosys2022 | Isolating at the Hardware Limit with Virtines | not predominantly Python/R (C, py/R 0%) | https://doi.org/10.5281/zenodo.6350453 |
| eurosys2022 | Jiffy: Elastic Far-Memory for Stateful Serverless Analytics. | not predominantly Python/R (C, py/R 18%) | https://doi.org/10.5281/zenodo.6349596 |
| eurosys2022 | Kite: Lightweight Critical Service Domains full strip note | not predominantly Python/R (C, py/R 0%) | https://doi.org/10.5281/zenodo.6348174 |
| eurosys2022 | Memory Deduplication for Serverless Computing with Medes | not predominantly Python/R (C++, py/R 28%) | https://doi.org/10.5281/zenodo.6374411 |
| eurosys2022 | Narwhal and Tusk: A DAG-based Mempool and Efficient BFT Consensus | not predominantly Python/R (Rust, py/R 29%) | https://doi.org/10.5281/zenodo.6353717 |
| eurosys2022 | OPEC: Operation-based Security Isolation for Bare-metal Embedded Systems | not predominantly Python/R (C, py/R 0%) | https://doi.org/10.5281/zenodo.6336200 |
| eurosys2022 | Optimizing the Interval-centric Distributed Computing Model for Temporal Graph Algorithms | not predominantly Python/R (Java, py/R 2%) | https://doi.org/10.5281/zenodo.6337790 |
| eurosys2022 | Out-Of-Order BackProp: An Effective Scheduling Technique for Deep Learning | not predominantly Python/R (C++, py/R 31%) | https://doi.org/10.5281/zenodo.6345869 |
| eurosys2022 | Rolis: a software approach to efficiently replicating multi-core transactions | not predominantly Python/R (C++, py/R 18%) | https://doi.org/10.5281/zenodo.6335844 |
| eurosys2022 | SafePM: A Sanitizer for Persistent Memory | not predominantly Python/R (C, py/R 30%) | https://doi.org/10.5281/zenodo.6338745 |
| eurosys2022 | Sharing is Caring: Secure and Efficient Shared Memory Support for MVEEs | not predominantly Python/R (C++, py/R 6%) | https://doi.org/10.5281/zenodo.6360235 |
| eurosys2022 | State Machine Replication Scalability Made Simple | not predominantly Python/R (Go, py/R 5%) | https://doi.org/10.5281/zenodo.6344032 |
| eurosys2022 | Tebis: Index Shipping for Efficient Replication in LSM Key-Value Stores | not predominantly Python/R (C, py/R 3%) | https://doi.org/10.5281/zenodo.6349594 |
| eurosys2022 | Varuna: Scalable, Low-cost Training of Massive Deep Learning Models | not predominantly Python/R (Python, py/R 48%) | https://doi.org/10.5281/zenodo.6359660 |
| eurosys2022 | p2KVS: a Portable 2-Dimensional Parallelizing Framework to Improve Scalability of Key-value Stores on SSDs | not predominantly Python/R (C++, py/R 2%) | https://doi.org/10.5281/zenodo.6336196 |
| eurosys2023 | All-Flash Array Key-Value Cache for Large Objects | not predominantly Python/R (C, py/R 0%) | https://doi.org/10.5281/zenodo.7703583 |
| eurosys2023 | DiLOS: Do Not Trade Compatibility for Performance in Memory Disaggregation | not predominantly Python/R (C, py/R 3%) | https://doi.org/10.5281/zenodo.7704577 |
| eurosys2023 | Diablo: A Benchmark Suite for Blockchains | not predominantly Python/R (Go, py/R 5%) | https://doi.org/10.5281/zenodo.7707312 |
| eurosys2023 | Dissecting BFT Consensus: In Trusted Components we Trust! | not predominantly Python/R (C++, py/R 0%) | https://doi.org/10.5281/zenodo.7734495 |
| eurosys2023 | Fast and Efficient Model Serving Using Multi-GPUs with Direct-Host-Access | not predominantly Python/R (Python, py/R 42%) | https://doi.org/10.5281/zenodo.7703530 |
| eurosys2023 | FlexPass: A Case for Flexible Credit-based Transport for Datacenter Networks | not predominantly Python/R (C++, py/R 0%) | https://doi.org/10.5281/zenodo.7737994 |
| eurosys2023 | FrozenHot Cache: Rethinking Cache Management for Modern Software | not predominantly Python/R (C, py/R 35%) | https://doi.org/10.5281/zenodo.7733671 |
| eurosys2023 | LogGrep: Fast and Cheap Cloud Log Storage by Exploiting both Static and Runtime Patterns | not predominantly Python/R (C, py/R 5%) | https://doi.org/10.5281/zenodo.7709922 |
| eurosys2023 | MariusGNN: Resource-Efficient Out-of-Core Training of Graph Neural Networks | not predominantly Python/R (C++, py/R 38%) | https://doi.org/10.5281/zenodo.7114594 |
| eurosys2023 | Model Checking Guided Testing for Distributed Systems | not predominantly Python/R (Java, py/R 6%) | https://doi.org/10.5281/zenodo.7654817 |
| eurosys2023 | Mumak: efficient and black-box bug detection for Persistent Memory | not predominantly Python/R (C, py/R 0%) | https://doi.org/10.5281/zenodo.7737117 |
| eurosys2023 | NearPM: A Near-memory Processing Prototype for Storage-class Workloads | not predominantly Python/R (Verilog, py/R 0%) | https://doi.org/10.5281/zenodo.7738934 |
| eurosys2023 | Nephele: Extending Virtualization Environments for Cloning Unikernel-based VMs | not predominantly Python/R (C, py/R 0%) | https://doi.org/10.5281/zenodo.7732935 |
| eurosys2023 | Omni-Paxos: Breaking the Barriers of Partial Connectivity | not predominantly Python/R (Rust, py/R 36%) | https://doi.org/10.5281/zenodo.7737776 |
| eurosys2023 | WAFL: Exposing Memory Ordering Bugs Efficiently with Active Delay Injection | not predominantly Python/R (C#, py/R 0%) | https://doi.org/10.5281/zenodo.7710637 |
| eurosys2024 | Atlas: Hybrid Cloud Migration Advisor for Interactive Microservices | not predominantly Python/R (Lua, py/R 14%) | http://doi.org/10.5281/zenodo.10080460 |
| eurosys2024 | Characterization and Reclamation of Frozen Garbage in Managed FaaS Workloads | not predominantly Python/R (Java, py/R 1%) | http://doi.org/10.5281/zenodo.10103366 |
| eurosys2024 | Concealing Compression-accelerated I/O for HPC Applications through In Situ Task Scheduling | not predominantly Python/R (C, py/R 1%) | http://doi.org/10.5281/zenodo.8394044 |
| eurosys2024 | Effective Bug Detection with Unused Definitions | not predominantly Python/R (C++, py/R 1%) | http://doi.org/10.5281/zenodo.10081116 |
| eurosys2024 | FLOWS: Balanced MRC Profiling for Heterogeneous Object-Size Cache | not predominantly Python/R (C, py/R 20%) | http://doi.org/10.5281/zenodo.10783932 |
| eurosys2024 | HAP: SPMD DNN Training on Heterogeneous GPU Clusters with Automated Program Synthesis | not predominantly Python/R (Rust, py/R 35%) | http://doi.org/10.5281/zenodo.10089643 |
| eurosys2024 | Minuet: Accelerating 3D Sparse Convolutions on GPUs | not predominantly Python/R (CUDA, py/R 43%) | http://doi.org/10.5281/zenodo.8393982 |
| eurosys2024 | TTLs Matter: Efficient Cache Sizing with TTL-Aware Miss Ratio Curves and Working Set Sizes | not predominantly Python/R (C#, py/R 0%) | http://doi.org/10.5281/zenodo.10783873 |
| eurosys2024 | Transparent Multicore Scaling of Single-Threaded Network Functions | not predominantly Python/R (C, py/R 3%) | http://doi.org/10.5281/zenodo.10087198 |
| eurosys2025 | A Hardware-Software Co-Design for Efficient Secure Containers | not predominantly Python/R (C, py/R 11%) | https://github.com/shijc-sjtu/cki-artifact |
| eurosys2025 | AlloyStack: A Library Operating System for Serverless Workflow Applications | not predominantly Python/R (Rust, py/R 23%) | https://github.com/tanksys/AlloyStack |
| eurosys2025 | CRAVE: Analyzing Cross-Resource Interaction to Improve Energy Efficiency in Systems-on-Chip | not predominantly Python/R (C, py/R 0%) | https://github.com/dipayan2/CRAVE_Artifact_EuroSys |
| eurosys2025 | Chrono: Meticulous Hotness Measurement and Flexible Page Migration for Memory Tiering | not predominantly Python/R (C, py/R 0%) | https://doi.org/10.5281/zenodo.14875828 |
| eurosys2025 | Collaborative Text Editing with Eg-walker: Better, Faster, Smaller | not predominantly Python/R (Rust, py/R 0%) | https://doi.org/10.5281/zenodo.13823409 |
| eurosys2025 | Comprehensive Deadlock Prevention for GPU Collective Communication | not predominantly Python/R (C++, py/R 2%) | https://doi.org/10.5281/zenodo.14871978 |
| eurosys2025 | Daredevil: Rescue Your Flash Storage from Inflexible Kernel Storage Stack | not predominantly Python/R (C, py/R 2%) | https://github.com/HKU-System-Security-Lab/Daredevil |
| eurosys2025 | Erebor: A Drop-In Sandbox Solution for Private Data Processing in Untrusted Confidential Virtual Machines | not predominantly Python/R (C, py/R 0%) | https://github.com/ASTERISC-Release/Erebor |
| eurosys2025 | HawkSet: Automatic, Application-Agnostic, and Efficient Concurrent PM Bug Detection | not predominantly Python/R (C++, py/R 24%) | https://doi.org/10.5281/zenodo.14917473 |
| eurosys2025 | Heimdall: Optimizing Storage I/O Admission with Extensive Machine Learning Pipeline | not predominantly Python/R (C, py/R 0%) | https://github.com/ucare-uchicago/Heimdall |
| eurosys2025 | Hey Hey, My My, Skewness Is Here to Stay: Challenges and Opportunities in Cloud Block Store Traffic | not predominantly Python/R (None, py/R 0%) | https://github.com/Nan-Ge/eurosys25_ae |
| eurosys2025 | Impeller: Stream Processing on Shared Logs | not predominantly Python/R (None, py/R 0%) | https://github.com/ut-osa/impeller-artifact |
| eurosys2025 | Jupiter: Pushing Speed and Scalability Limitations for Subgraph Matching on Multi-GPUs | not predominantly Python/R (Cuda, py/R 0%) | https://github.com/AnySparse/Jupiter |
| eurosys2025 | LOFT: A Lock-free and Adaptive Learned Index with High Scalability for Dynamic Workloads | not predominantly Python/R (C++, py/R 0%) | https://github.com/yuxuanMo/LOFT/tree/main |
| eurosys2025 | Multi-Grained Specifications for Distributed System Model Checking and Verification | not predominantly Python/R (Java, py/R 2%) | https://doi.org/10.5281/zenodo.13738672 |
| eurosys2025 | Occamy: A Preemptive Buffer Management for On-chip Shared-memory Switches | not predominantly Python/R (C++, py/R 3%) | https://doi.org/10.5281/zenodo.14871138 |
| eurosys2025 | Overcoming the Last Mile between Log-Structured File Systems and Persistent Memory via Scatter Logging | not predominantly Python/R (C, py/R 34%) | https://github.com/fg-sn2003/slotfs-eurosys |
| eurosys2025 | Solid State Drive Targeted Memory-Efficient Indexing for Universal I/O Patterns and Fragmentation Degrees | not predominantly Python/R (C, py/R 0%) | https://github.com/dgist-datalab/AppL |
| eurosys2025 | SpInfer: Leveraging Low-Level Sparsity for Efficient Large Language Model Inference on GPUs | not predominantly Python/R (Cuda, py/R 21%) | https://github.com/xxyux/SpInfer |
| eurosys2025 | Understanding the Linux Kernel, Visually | not predominantly Python/R (C, py/R 37%) | https://doi.org/10.5281/zenodo.13710732 |
| eurosys2026 | 2DIO: Configurable and Cache-Accurate Trace Generation for Storage Benchmarking | not predominantly Python/R (C++, py/R 42%) | https://github.com/Effygal/trace-gen |
| eurosys2026 | A Logically Disaggregated Cache for Replicated Storage Systems | not predominantly Python/R (C++, py/R 18%) | https://github.com/dassl-uiuc/Logically-Disaggregated-Caches |
| eurosys2026 | Arena: Efficiently Training Large Models via Dynamic Scheduling and Adaptive Parallelism Co-Design | not predominantly Python/R (None, py/R 0%) | https://github.com/sjtu-epcc/arena/tree/ae-eurosys |
| eurosys2026 | BASK: Batch And SmartNIC-offloaded KSM | not predominantly Python/R (C, py/R 2%) | https://github.com/casys-kaist/bask_ae_eurosys26 |
| eurosys2026 | CSnake: Detecting Self-Sustaining Cascading Failure via Causal Stitching of Fault Propagations | not predominantly Python/R (None, py/R 0%) | https://github.com/Purdue-PFL/CSnake |
| eurosys2026 | Chimera: Transparent and High-Performance ISAX Heterogeneous Computing via Binary Rewriting | not predominantly Python/R (C, py/R 5%) | https://github.com/Eurosys26p57/Chimera |
| eurosys2026 | DROPS: Managing Resource Pools in a Large-Scale Commercial Serverless Platform | not predominantly Python/R (None, py/R 0%) | https://github.com/UWASL/DROPS.git |
| eurosys2026 | Efficient ML Model Updates for Deeply Embedded Microcontrollers | not predominantly Python/R (C, py/R 1%) | https://github.com/ShishirPatil/minerva |
| eurosys2026 | Efficient and Adaptable Overlapping for Computation and Communication via Signaling and Reordering | not predominantly Python/R (Python, py/R 43%) | https://github.com/infinigence/FlashOverlap |
| eurosys2026 | Fast and Parallelized Crash Consistency with Opportunistic Order Elimination | not predominantly Python/R (C, py/R 0%) | https://github.com/Straho-Chen/ChimeraFS |
| eurosys2026 | FicusDB: Scalable Multi-Versioned Authenticated Storage | not predominantly Python/R (Go, py/R 0%) | https://github.com/0x486F626F/ficusdb-evaluation |
| eurosys2026 | Fix: externalizing network I/O in serverless computing | not predominantly Python/R (C++, py/R 3%) | https://github.com/fix-project/fix |
| eurosys2026 | GeDES: GPU-Driven Discrete Event Network Simulator | not predominantly Python/R (C++, py/R 5%) | https://github.com/mobinets/GeDES |
| eurosys2026 | LightDSA: Enabling Efficient DSA Through Hardware-Aware Transparent Optimization | not predominantly Python/R (None, py/R 0%) | https://github.com/izumihanako/LightDSA-AE |
| eurosys2026 | MTTM: Dynamic Fast Memory Partitioning with Bandwidth Optimization for Multi-tenant Cloud | not predominantly Python/R (C, py/R 0%) | https://github.com/casys-kaist/MTTM_ae_EuroSys26 |
| eurosys2026 | NecoFuzz: Effective Fuzzing of Nested Virtualization via Fuzz-Harness Virtual Machines | not predominantly Python/R (C, py/R 5%) | https://github.com/shina-lab/artifact_NecoFuzz |
| eurosys2026 | Not A DPU in Name Only! Unleashing RDMA-capable DPUs in Multi-Tenant Serverless Clouds with NADINO | not predominantly Python/R (None, py/R 0%) | https://github.com/ucr-serverless/NADINO.git |
| eurosys2026 | OptiLog: Assigning Roles in Byzantine Consensus | not predominantly Python/R (Go, py/R 1%) | https://github.com/relab/optilog_artifacts |
| eurosys2026 | PaCaR: Improved Buffered I/O Locality on NUMA Systems with Page Cache Replication | not predominantly Python/R (Python, py/R 45%) | https://github.com/jeromecst/pacar_artifact_eurosys_26 |
| eurosys2026 | PatternSketch: General and Runtime Reconfigurable Time-series Network Traffic Pattern Detection | not predominantly Python/R (C++, py/R 0%) | https://github.com/duyang92/patternsketch/ |
| eurosys2026 | Prediction-Informed Power Management for General-Purpose Compute Servers | not predominantly Python/R (C, py/R 5%) | https://github.com/jonggyup/PIP |
| eurosys2026 | Proteus: Heterogeneous FPGA Virtualization | not predominantly Python/R (None, py/R 0%) | https://github.com/TUM-DSE/proteus.git |
| eurosys2026 | REPS: Recycled Entropy Packet Spraying for Adaptive Load Balancing and Failure Mitigation | not predominantly Python/R (C++, py/R 23%) | https://github.com/tommasobo/REPS_EuroSys_Artifact |
| eurosys2026 | Reducing the GPU Memory Bottleneck with Lossless Compression for ML | not predominantly Python/R (Cuda, py/R 34%) | https://github.com/AKKamath/InvariantBitPacking |
| eurosys2026 | RoPeerTo: A Datacenter-Scale Architecture for Peer-To-Peer DMA between GPUs and FPGAs | not predominantly Python/R (C++, py/R 10%) | https://github.com/necst/ropeerto |
| eurosys2026 | Rose: Reproducing External-Fault-Induced Failures in Distributed Systems with Lighweight Instrumentation | not predominantly Python/R (C, py/R 1%) | https://github.com/sebastiaoamaro/rose |
| eurosys2026 | SCServe: Accelerating Multi-SLO LLM Serving with SLO-Customized Speculative Decoding | not predominantly Python/R (None, py/R 0%) | https://github.com/zikun-li/AdaServe-Artifact-Evaluation |
| eurosys2026 | STAlloc: Enhancing Memory Efficiency in Large-Scale Model Training with Spatio-Temporal Planning | not predominantly Python/R (C++, py/R 44%) | https://github.com/infinigence/STAlloc |
| eurosys2026 | Scheduling Cloud Block Storage Proactively and Reactively with Omar | not predominantly Python/R (C++, py/R 48%) | https://github.com/Master-Chen-Xin-Qi/EuroSys26_AE |
| eurosys2026 | SmartNS: Enabling Line-rate and Flexible Network Stack with SmartNIC | not predominantly Python/R (C++, py/R 8%) | https://github.com/RC4ML/SmartNS |
| eurosys2026 | TCO-driven Storage Provisioning for Exascale Data Centers | not predominantly Python/R (Java, py/R 27%) | https://github.com/Thesys-lab/storage-tco-eurosys-2026-artifact |
| eurosys2026 | TierScape: Harnessing Multiple Compressed Tiers to Tame Server Memory TCO | not predominantly Python/R (C++, py/R 37%) | https://github.com/intellabs/tierscape |
| eurosys2026 | Turnstile: Hybrid Information Flow Control Framework for Managing Privacy in Internet-of-Things Applications | not predominantly Python/R (JavaScript, py/R 1%) | https://github.com/DependableSystemsLab/Turnstile-EuroSys26 |
| sc2021 | 3D Acoustic-Elastic Coupling with Gravity: The Dynamics of the 2018 Palu, Sulawesi Earthquake and Tsunami | not predominantly Python/R (C, py/R 7%) | https://doi.org/10.5281/zenodo.5159333 |
| sc2021 | A Next-Generation Discontinuous Galerkin Fluid Dynamics Solver with Application to High-Resolution Lung Airflow Simulations | not predominantly Python/R (C++, py/R 1%) | https://doi.org/10.5281/zenodo.5176507 |
| sc2021 | APNN-TC: Accelerating Arbitrary Precision Neural Networks on Ampere GPU Tensor Cores | not predominantly Python/R (CUDA, py/R 0%) | https://doi.org/10.5281/zenodo.5144378 |
| sc2021 | Accelerating Applications using Edge Tensor Processing Units | not predominantly Python/R (C++, py/R 17%) | https://doi.org/10.5281/zenodo.5156431 |
| sc2021 | Accelerating XOR-based Erasure Coding using Program Optimization Techniques | not predominantly Python/R (Rust, py/R 0%) | https://doi.org/10.5281/zenodo.5167006 |
| sc2021 | Accelerating large scale de novo metagenome assembly using GPUs | not predominantly Python/R (C++, py/R 3%) | https://doi.org/10.5281/zenodo.5165333 |
| sc2021 | Bootstrapping In-situ Workflow Auto-Tuning via Combining Performance Models of Component Applications full strip note | not predominantly Python/R (Swift, py/R 49%) | https://doi.org/10.5281/zenodo.5003901 |
| sc2021 | Cuttlefish: Library for Achieving Energy Efficiency in Multicore Parallel Programs | not predominantly Python/R (C, py/R 2%) | https://doi.org/10.5281/zenodo.5167629 |
| sc2021 | DeltaFS: A Scalable No-Ground-Truth Filesystem For Massively-Parallel Computing | not predominantly Python/R (C++, py/R 0%) | https://doi.org/10.5281/zenodo.4884852 |
| sc2021 | Flare: Flexible In-Network Allreduce | not predominantly Python/R (C++, py/R 0%) | https://doi.org/10.5281/zenodo.4836022 |
| sc2021 | HPAC: Evaluating Approximate Computing Techniques on HPC OpenMP Applications. | not predominantly Python/R (C++, py/R 2%) | https://doi.org/10.5281/zenodo.5167980 |
| sc2021 | Hardware-supported Remote Persistence for Distributed Persistent Memory | not predominantly Python/R (C, py/R 0%) | https://doi.org/10.5281/zenodo.5162688 |
| sc2021 | In-Depth Analyses of Unified Virtual Memory System for GPU Accelerated Computing | not predominantly Python/R (C, py/R 1%) | https://doi.org/10.5281/zenodo.5148930 |
| sc2021 | Krill: A Compiler and Runtime System for Concurrent Graph Processing | not predominantly Python/R (C, py/R 16%) | https://doi.org/10.5281/zenodo.5165762 |
| sc2021 | LCCG: A Locality-Centric Hardware Accelerator for High Throughput of Concurrent Graph Processing | not predominantly Python/R (C, py/R 0%) | https://doi.org/10.5281/zenodo.5155769 |
| sc2021 | LMFF: Efficient and Scalable Layered Materials Force Field on Heterogeneous Many-Core Processors | not predominantly Python/R (C++, py/R 0%) | https://doi.org/10.5281/zenodo.5203181 |
| sc2021 | LibShalom: Optimizing Small and Irregular-shaped Matrix Multiplications on ARMv8 Multi-Cores | not predominantly Python/R (C, py/R 0%) | https://doi.org/10.5281/zenodo.5201457 |
| sc2021 | LogECMem: Coupling Erasure-Coded In-Memory Key-Value Stores with Parity Logging | not predominantly Python/R (C++, py/R 0%) | https://doi.org/10.5281/zenodo.5092219 |
| sc2021 | Lunule: An Agile and Judicious Metadata Load Balancer for CephFS | not predominantly Python/R (C++, py/R 2%) | https://doi.org/10.5281/zenodo.5171833 |
| sc2021 | On the Parallel I/O Optimality of Linear Algebra Kernels: Near-Optimal Matrix Factorizations | not predominantly Python/R (C++, py/R 6%) | https://doi.org/10.5281/zenodo.5168027 |
| sc2021 | Online Optimization of File Transfers in High-Speed Networks | not predominantly Python/R (Java, py/R 23%) | https://doi.org/10.5281/zenodo.5164277 |
| sc2021 | Parallel Construction of Module Networks | not predominantly Python/R (C++, py/R 0%) | https://doi.org/10.5281/zenodo.5144438 |
| sc2021 | Pinpointing Crash-Consistency Bugs in the HPC I/O Stack: A Cross-Layer Approach | not predominantly Python/R (C, py/R 15%) | https://doi.org/10.5281/zenodo.5168471 |
| sc2021 | Preparing an Incompressible-Flow Fluid Dynamics Code for Exascale-Class Wind Energy Simulations | not predominantly Python/R (C, py/R 0%) | https://doi.org/10.5281/zenodo.4899910 |
| sc2021 | Reducing Redundancy in Data Organization and Arithmetic Calculation for Stencil Computations | not predominantly Python/R (C, py/R 0%) | https://doi.org/10.5281/zenodo.3881644 |
| sc2021 | Reverse-Mode Automatic Differentiation and Optimization of GPU Kernels via Enzyme full strip note | not predominantly Python/R (CUDA, py/R 3%) | https://doi.org/10.5281/zenodo.5147573 |
| sc2021 | STM-Multifrontal QR: Streaming Task Mapping Multifrontal QR Factorization Empowered by GCN | not predominantly Python/R (C, py/R 24%) | https://doi.org/10.5281/zenodo.5144709 |
| sc2021 | SV-Sim: Scalable PGAS-based State Vector Simulation of Quantum Circuits | not predominantly Python/R (C++, py/R 13%) | https://doi.org/10.5281/zenodo.5206960 |
| sc2021 | Scalable adaptive PDE solvers in arbitrary domains | not predominantly Python/R (C++, py/R 2%) | https://doi.org/10.5281/zenodo.5168313 |
| sc2021 | Simurgh: A Fully Decentralized and Secure NVMM User Space File System | not predominantly Python/R (C, py/R 1%) | https://doi.org/10.5281/zenodo.5163624 |
| sc2021 | Temporal Vectorization for Stencils | not predominantly Python/R (C, py/R 0%) | https://doi.org/10.5281/zenodo.5151491 |
| sc2021 | Understanding, Predicting and Scheduling Serverless Workloads under Partial Interference | not predominantly Python/R (JavaScript, py/R 1%) | https://doi.org/10.5281/zenodo.5147569 |
| sc2021 | Whale: Efficient One-to-Many Data Partitioning in RDMA-assisted Distributed Stream Processing Systems | not predominantly Python/R (Java, py/R 5%) | https://doi.org/10.5281/zenodo.4897500 |
| sc2021 | cuTS: Scaling Subgraph Isomorphism on Distributed Multi-GPU Systems Using Trie Based Data Structure | not predominantly Python/R (CUDA, py/R 4%) | https://doi.org/10.5281/zenodo.5154114 |
| sc2021 | ndzip-gpu: Efficient Lossless Compression of Scientific Floating-Point Data on GPUs | not predominantly Python/R (C++, py/R 7%) | https://doi.org/10.5281/zenodo.5144874 |
| sosp2019 | An Analysis of Performance Evolution of Linux's Core Operations | not predominantly Python/R (C, py/R 18%) | https://github.com/LinuxPerfStudy/LEBench |
| sosp2019 | Efficient and Scalable Thread-Safety Violation Detection --- Finding thousands of concurrency bugs during testing | not predominantly Python/R (C#, py/R 0%) | https://github.com/SpectrumLi/TSVDExamples |
| sosp2019 | File Systems Unfit as Distributed Storage Backends: Lessons from 10 Years of Ceph Evolution | not predominantly Python/R (C++, py/R 4%) | https://github.com/mkuchnik/CephEvolution |
| sosp2019 | Finding Semantic Bugs in File Systems with an Extensible Fuzzing Framework | not predominantly Python/R (C, py/R 13%) | https://github.com/sslab-gatech/hydra |
| sosp2019 | Nexus: A GPU Cluster Engine for Accelerating Neural Networks Based Video Analysis | not predominantly Python/R (C++, py/R 5%) | https://github.com/uwsampl/nexus |
| sosp2019 | Optimizing Data Intensive Computations in Existing Libraries with Split Annotations | not predominantly Python/R (Python, py/R 47%) | https://github.com/weld-project/split-annotations |
| sosp2019 | Optimizing Deep Learning Computation with Automated Generation of Graph Substitutions | not predominantly Python/R (C++, py/R 24%) | https://github.com/jiazhihao/sosp19ae |
| sosp2019 | Parity Models: Erasure-Coded Resilience for Prediction Serving Systems | not predominantly Python/R (C++, py/R 37%) | https://github.com/Thesys-lab/parity-models |
| sosp2019 | RECIPE: Converting Concurrent DRAM Indexes to Persistent-Memory Indexes | not predominantly Python/R (C++, py/R 1%) | https://github.com/utsaslab/RECIPE |
| sosp2019 | Replication in the era of microservices | not predominantly Python/R (Java, py/R 1%) | https://github.com/GLaDOS-Michigan/Aegean |
| sosp2019 | Risk-based planning for evolving data-center networks | not predominantly Python/R (C, py/R 6%) | https://github.com/SiGe/Janus |
| sosp2019 | Scalable and Practical Locking with Shuffling | not predominantly Python/R (C, py/R 0%) | https://github.com/sslab-gatech/shfllock |
| sosp2019 | ShortCut: Accelerating Mostly-Deterministic Code Regions | not predominantly Python/R (C, py/R 0%) | https://github.com/dozenow/shortcut |
| sosp2019 | SplitFS: Reducing Software Overhead in File Systems for Persistent Memory | not predominantly Python/R (C, py/R 0%) | https://github.com/utsaslab/SplitFS |
| sosp2019 | Teechain: A Secure Payment Network with Asynchronous Blockchain Access | not predominantly Python/R (C++, py/R 0%) | https://github.com/lsds/Teechain |
| sosp2019 | Verifying Software Network Functions with No Verification Expertise | not predominantly Python/R (C, py/R 3%) | https://github.com/vigor-nf/vigor |
| sosp2019 | Verifying concurrent, crash-safe systems with Perennial | not predominantly Python/R (Lean, py/R 1%) | https://github.com/mit-pdos/perennial/tree/master/artifact |
| sosp2021 | Automated SmartNIC Offloading Insights for Network Functions | not predominantly Python/R (LLVM, py/R 1%) | https://github.com/824728350/Clara |
| sosp2021 | BIDL: A High-throughput, Low-latency Permissioned Blockchain Framework for Datacenter Networks | not predominantly Python/R (Go, py/R 11%) | https://github.com/hku-systems/bidl |
| sosp2021 | Boki: Stateful Serverless Computing with Shared Logs | not predominantly Python/R (Go, py/R 9%) | https://github.com/ut-osa/boki-benchmarks |
| sosp2021 | Caracal: Contention Management with Deterministic Concurrency Control | not predominantly Python/R (C++, py/R 1%) | https://github.com/uoft-felis/felis |
| sosp2021 | Coeus: A system for oblivious document ranking and retrieval | not predominantly Python/R (C++, py/R 20%) | https://github.com/ishtiyaque/Coeus_artifact |
| sosp2021 | Crash Consistent Non-Volatile Memory Express | not predominantly Python/R (C, py/R 0%) | https://github.com/thustorage/ccnvme |
| sosp2021 | Forerunner: Constraint-based Speculative Transaction Execution for Ethereum | not predominantly Python/R (Go, py/R 0%) | https://github.com/microsoft/Forerunner/ |
| sosp2021 | Geometric Partitioning: Explore the Boundary of Optimal Erasure Code Repair | not predominantly Python/R (Go, py/R 0%) | https://github.com/rcstor/rcstor |
| sosp2021 | HEALER: Relation Learning Guided Kernel Fuzzing | not predominantly Python/R (Rust, py/R 0%) | https://github.com/SunHao-0/healer |
| sosp2021 | IODA: A Host/Device Co-Design for Strong Predictability Contract on Modern Flash Storage | not predominantly Python/R (C, py/R 0%) | https://github.com/huaicheng/IODA-SOSP21-AE |
| sosp2021 | J-NVM: Off-heap Persistent Objects in Java | not predominantly Python/R (Java, py/R 0%) | https://github.com/jnvm-project/jnvm |
| sosp2021 | Kangaroo: Caching Billions of Tiny Objects on Flash | not predominantly Python/R (C++, py/R 7%) | https://github.com/saramcallister/Kangaroo |
| sosp2021 | Kauri: Scalable BFT Consensus with Pipelined Tree-Based Dissemination and Aggregation | not predominantly Python/R (C++, py/R 1%) | https://github.com/Raycoms/Kauri-Public |
| sosp2021 | LineFS: Efficient SmartNIC Offload of a Distributed File System with Pipeline Parallelism | not predominantly Python/R (C, py/R 1%) | https://github.com/casys-kaist/LineFS |
| sosp2021 | MIND: In-Network Memory Management for Disaggregated Data Centers | not predominantly Python/R (C, py/R 0%) | https://github.com/shsym/mind |
| sosp2021 | Mycelium: Large-Scale Distributed Graph Queries with Differential Privacy | not predominantly Python/R (C++, py/R 40%) | https://github.com/karannewatia/Mycelium |
| sosp2021 | PACTree: A High Performance Persistent Range Index Using PAC Guidelines | not predominantly Python/R (C++, py/R 2%) | https://github.com/cosmoss-vt/pactree |
| sosp2021 | Rabia: Simplifying State-Machine Replication Through Randomization | not predominantly Python/R (Go, py/R 21%) | https://github.com/haochenpan/rabia/ |
| sosp2021 | Random Walks on Huge Graphs at Cache Efficiency | not predominantly Python/R (C++, py/R 0%) | https://github.com/flashmobwalk/flashmob/tree/sosp21-ae |
| sosp2021 | Regular Sequential Serializability and Regular Sequential Consistency | not predominantly Python/R (Java, py/R 0%) | https://github.com/jmhelt/tapir/tree/spanner |
| sosp2021 | Rudra: Finding Memory Safety Bugs in Rust at the Ecosystem Scale | not predominantly Python/R (Rust, py/R 0%) | https://github.com/sslab-gatech/Rudra-Artifacts |
| sosp2021 | Scale and Performance in a Filesystem Semi-Microkernel | not predominantly Python/R (C, py/R 2%) | https://github.com/WiscADSL/uFS |
| sosp2021 | Snoopy: Surpassing the Scalability Bottleneck of Oblivious Storage | not predominantly Python/R (C, py/R 7%) | https://github.com/ucbrise/snoopy |
| sosp2021 | Snowboard: Finding Kernel Concurrency Bugs through Systematic Inter-thread Communication Analysis | not predominantly Python/R (C, py/R 1%) | https://github.com/rssys/snowboard |
| sosp2021 | The Aurora Single Level Store Operating System | not predominantly Python/R (C, py/R 0%) | https://github.com/rcslab/aurora-12.1/ |
| sosp2021 | The Demikernel Library OS Architecture for Microsecond, Kernel-Bypass Datacenter Systems | not predominantly Python/R (Rust, py/R 4%) | https://github.com/demikernel/demikernel |
| sosp2021 | Understanding and Detecting Software Upgrade Failures in Distributed Systems | not predominantly Python/R (None, py/R 0%) | https://github.com/zlab-purdue/ds-upgrade |
| sosp2021 | When Idling is Ideal: Optimizing Tail-Latency for Highly-Dispersed Datacenter Workloads with Perséphone | not predominantly Python/R (C++, py/R 22%) | https://github.com/maxdml/psp |
| sosp2021 | WineFS: a hugepage-aware file system for persistent memory that ages gracefully | not predominantly Python/R (C, py/R 0%) | https://github.com/rohankadekodi/WineFS |
| sosp2021 | Witcher: Systematic Crash Consistency Testing for Non-Volatile Memory Key-Value Stores | not predominantly Python/R (C, py/R 8%) | https://github.com/cosmoss-vt/witcher |
| sosp2021 | iGUARD: In-GPU Advanced Race Detection | not predominantly Python/R (Cuda, py/R 0%) | https://github.com/csl-iisc/iGUARD |
| sosp2023 | Cornflakes: Zero-Copy Serialization for Microsecond-Scale Networking | not predominantly Python/R (Rust, py/R 10%) | https://github.com/deeptir18/cornflakes |
| sosp2023 | Ditto: An Elastic and Adaptive Memory-Disaggregated Caching System | not predominantly Python/R (C++, py/R 27%) | https://github.com/dmemsys/Ditto |
| sosp2023 | Edna: Disguising and Revealing User Data in Web Applications | not predominantly Python/R (C, py/R 0%) | https://github.com/tslilyai/edna/tree/artifact |
| sosp2023 | Enabling High-Performance and Secure Userspace NVM File Systems with the Trio Architecture | not predominantly Python/R (C, py/R 0%) | https://github.com/vmexit/trio-sosp23-ae |
| sosp2023 | FIFO Queues are All You Need for Cache Eviction | not predominantly Python/R (C, py/R 12%) | https://github.com/Thesys-lab/sosp23-s3fifo |
| sosp2023 | Falcon: Fast OLTP Engine for Persistent Cache and Non-Volatile Memory | not predominantly Python/R (Rust, py/R 18%) | https://github.com/madsys-dev/Falcon |
| sosp2023 | Halfmoon: Log-Optimal Fault-Tolerant Stateful Serverless Computing | not predominantly Python/R (Go, py/R 11%) | https://github.com/pkusys/Halfmoon-bench |
| sosp2023 | MEMTIS: Efficient Memory Tiering with Dynamic Page Classification and Page Size Determination | not predominantly Python/R (C, py/R 0%) | https://github.com/cosmoss-jigu/memtis |
| sosp2023 | Paella: Low-latency Model Serving with Software-defined GPU Scheduling | not predominantly Python/R (C++, py/R 11%) | https://github.com/eniac/paella/tree/sosp23_artifact |
| sosp2023 | Partial Failure Resilient Memory Management System for (CXL-based) Distributed Shared Memory | not predominantly Python/R (C++, py/R 3%) | https://github.com/madsys-dev/sosp-paper19-ae |
| sosp2023 | QuePaxa: Escaping the tyranny of timeouts in consensus | not predominantly Python/R (Go, py/R 31%) | https://github.com/dedis/quepaxa/ |
| sosp2023 | RackBlox: A Software-Defined Rack-Scale Storage System with Network-Storage Co-Design | not predominantly Python/R (C, py/R 0%) | https://github.com/breidys2/RackBlox |
| sosp2023 | SPFresh: Incremental In-Place Update for Billion-Scale Vector Search | not predominantly Python/R (C++, py/R 6%) | https://github.com/SPFresh/SPFresh |
| sosp2023 | Siloz: Leveraging DRAM Isolation Domains to Prevent Inter-VM Rowhammer | not predominantly Python/R (C, py/R 0%) | https://github.com/efeslab/siloz |
| sosp2023 | Snowcat: Efficient Kernel Concurrency Testing using a Learned Coverage Predictor | not predominantly Python/R (None, py/R 0%) | https://github.com/rssys/snowcat |
| sosp2023 | UGACHE: A Unified GPU Cache for Embedding-based Deep Learning | not predominantly Python/R (C++, py/R 30%) | https://github.com/SJTU-IPADS/ugache-artifacts |
| sosp2024 | Aceso: Achieving Efficient Fault Tolerance in Memory-Disaggregated Key-Value Stores | not predominantly Python/R (None, py/R 0%) | https://github.com/huzhisheng/Aceso |
| sosp2024 | BIZA: Design of Self-Governing Block-Interface ZNS AFA for Endurance and Performance | not predominantly Python/R (C, py/R 0%) | https://github.com/ChaseLab-PKU/BIZA |
| sosp2024 | CHIME: A Cache-Efficient and High-Performance Hybrid Index on Disaggregated Memory | not predominantly Python/R (C++, py/R 29%) | https://github.com/dmemsys/CHIME |
| sosp2024 | Dirigent: Lightweight Serverless Orchestration | not predominantly Python/R (Go, py/R 9%) | https://github.com/eth-easl/dirigent |
| sosp2024 | Fast Core Scheduling with Userspace Process Abstraction | not predominantly Python/R (Assembly, py/R 2%) | https://github.com/neolinsu/sosp24ae |
| sosp2024 | Fast, Flexible, and Practical Kernel Extensions | not predominantly Python/R (C, py/R 0%) | https://github.com/rs3lab/KFlex |
| sosp2024 | LazyLog: A New Shared Log Abstraction for Low-Latency Applications | not predominantly Python/R (C++, py/R 5%) | https://github.com/dassl-uiuc/LazyLog-Artifact |
| sosp2024 | Modular Verification of Secure and Leakage-Free Systems: From Application Specification to Circuit-Level Implementation | not predominantly Python/R (Racket, py/R 0%) | https://github.com/anishathalye/parfait-hsm |
| sosp2024 | Ozz: Identifying Kernel Out-of-Order Concurrency Bugs with In-Vivo Memory Access Reordering | not predominantly Python/R (None, py/R 0%) | https://github.com/casys-kaist/ozz_artifact |
| sosp2024 | Practical Verification of System-Software Components Written in Standard C | not predominantly Python/R (C, py/R 2%) | https://github.com/dslab-epfl/tpot |
| sosp2024 | SilvanForge: A Schedule-Guided Retargetable Compiler for Decision Tree Inference | not predominantly Python/R (C++, py/R 6%) | https://github.com/asprasad/treebeard/tree/silvanforge |
| sosp2024 | Skyloft: A General High-Efficient Scheduling Framework in User Space | not predominantly Python/R (None, py/R 0%) | https://github.com/yhtzd/skyloft-sosp24-ae |
| sosp2024 | Tenplex: Dynamic Parallelism for Deep Learning using Parallelizable Tensor Collections | not predominantly Python/R (Go, py/R 28%) | https://github.com/kungfu-team/tenplex/tree/sosp24-artifact |
| sosp2024 | Tiered Memory Management: Access Latency is the Key! | not predominantly Python/R (C, py/R 0%) | https://github.com/host-architecture/colloid |
| sosp2024 | Uncovering Nested Data Parallelism and Data Reuse in DNN Computation with FractalTensor | not predominantly Python/R (C++, py/R 13%) | https://github.com/microsoft/nnfusion |
| sosp2024 | Verus: A Practical Foundation for Systems Verification | not predominantly Python/R (Rust, py/R 10%) | https://github.com/verus-lang/paper-sosp24-artifact/blob/main/site/guide.md |
| sosp2024 | vSoC: Efficient Virtual System-on-Chip on Heterogeneous Hardware | not predominantly Python/R (C, py/R 3%) | https://github.com/VirtualSoC/vsoc/tree/artifact |
| sosp2025 | Aeolia: A Fast and Secure Userspace Interrupt-Based Storage Stack | not predominantly Python/R (C, py/R 0%) | https://github.com/TELOS-syslab/Aeolia |
| sosp2025 | Analyzing and Enhancing ArckFS: An Anecdotal Example of Benefits of Artifact Evaluation | not predominantly Python/R (C, py/R 0%) | https://github.com/vmexit/trio-sosp23-ae/tree/sosp25ae-snapshot |
| sosp2025 | Atmosphere: Practical Verified Kernels with Rust and Verus | not predominantly Python/R (Rust, py/R 0%) | https://github.com/mars-research/atmosphere-sosp25-artifact |
| sosp2025 | AutoMan: Facilitating Verified Distributed Systems Development Through Automatic Code Generation and Manual Optimizations | not predominantly Python/R (Dafny, py/R 1%) | https://github.com/stonysystems/automan |
| sosp2025 | CHERIoT RTOS: An OS for Fine-Grained Memory-Safe Compartments on Low-Cost Embedded Devices | not predominantly Python/R (C++, py/R 0%) | https://github.com/CHERIoT-Platform/cheriot-rtos |
| sosp2025 | CortenMM: Efficient Memory Management with Strong Correctness Guarantees | not predominantly Python/R (Rust, py/R 1%) | https://github.com/TELOS-syslab/CortenMM-Artifact |
| sosp2025 | Coyote v2: Raising the Level of Abstraction for Data Center FPGAs | not predominantly Python/R (SystemVerilog, py/R 3%) | https://github.com/fpgasystems/Coyote |
| sosp2025 | Fast End-to-End Performance Simulation of Accelerated Hardware-Software Stacks | not predominantly Python/R (C++, py/R 15%) | https://github.com/dslab-epfl/NEXDSIM_AE |
| sosp2025 | HedraRAG: Co-Optimizing Generation and Retrieval for Heterogeneous RAG Workflows | not predominantly Python/R (C++, py/R 26%) | https://github.com/Leo9660/HedraRAG_AE |
| sosp2025 | How to Copy Memory? Coordinated Asynchronous Copy as a First-Class OS Service | not predominantly Python/R (C, py/R 0%) | https://github.com/SJTU-IPADS/Copier |
| sosp2025 | KNighter: Transforming Static Analysis with LLM-Synthesized Checkers | not predominantly Python/R (C++, py/R 15%) | https://github.com/ise-uiuc/KNighter |
| sosp2025 | Loom: Efficient Capture and Querying of High-Frequency Telemetry | not predominantly Python/R (Rust, py/R 0%) | https://github.com/fsolleza/loom |
| sosp2025 | Mitigating Application Resource Overload with Targeted Task Cancellation | not predominantly Python/R (C, py/R 0%) | https://github.com/OrderLab/Atropos |
| sosp2025 | ORQ: Complex Analytics on Private Data with Strong Security Guarantees | not predominantly Python/R (C++, py/R 11%) | https://github.com/CASP-Systems-BU/orq |
| sosp2025 | Orthrus: Efficient and Timely Detection of Silent User Data Corruption in the Cloud with Resource-Adaptive Computation Validation | not predominantly Python/R (C++, py/R 2%) | https://github.com/ICTPLSys/Orthrus |
| sosp2025 | Pesto: Cooking up High Performance BFT Queries | not predominantly Python/R (C++, py/R 1%) | https://github.com/fsuri/Pequin-Artifact |
| sosp2025 | Pie: A Programmable Serving System for Emerging LLM Applications | not predominantly Python/R (Rust, py/R 3%) | https://github.com/pie-project/pie |
| sosp2025 | Prove It to the Kernel: Precise Extension Analysis via Proof-Guided Abstraction Refinement | not predominantly Python/R (C, py/R 3%) | https://github.com/SunHao-0/BCF/tree/artifact-evaluation |
| sosp2025 | Quilt: Resource-aware Merging of Serverless Workflows | not predominantly Python/R (C, py/R 6%) | https://github.com/eniac/quilt |
| sosp2025 | Scalable Address Spaces using Concurrent Interval Skiplist | not predominantly Python/R (C, py/R 1%) | https://github.com/kaist-cp/interval-vm.git |
| sosp2025 | Scalable Far Memory: Balancing Faults and Evictions | not predominantly Python/R (C, py/R 1%) | https://github.com/rs3lab/mage-artifact |
| sosp2025 | TRIP: Coercion-resistant Registration for E-Voting with Verifiability and Usability in Votegral | not predominantly Python/R (Go, py/R 0%) | https://github.com/dedis/votegral |
| sosp2025 | The Design and Implementation of a Virtual Firmware Monitor | not predominantly Python/R (Rust, py/R 0%) | https://github.com/epfl-dcsl/miralis-sosp25-artifact |
| sosp2025 | Tiga: Accelerating Geo-Distributed Transactions with Synchronized Clocks | not predominantly Python/R (C++, py/R 10%) | https://github.com/New-Consensus-Concurrency-Control/Tiga |
| sosp2025 | Unlocking True Elasticity for the Cloud-Native Era with Dandelion | not predominantly Python/R (Rust, py/R 0%) | https://github.com/eth-easl/dandelion |
| sosp2025 | WASIT: Deep and Continuous Differential Testing of WebAssembly System Interface Implementations | not predominantly Python/R (Rust, py/R 0%) | https://github.com/yagehu/wasit |
| sosp2025 | eBPF Misbehavior Detection: Fuzzing with a Specification-Based Oracle | not predominantly Python/R (C, py/R 1%) | https://github.com/rs3lab/veritas |
| sosp2026 | AgileLog: A Forkable Shared Log for Agents on Data Streams | not predominantly Python/R (C++, py/R 17%) | https://doi.org/10.5281/zenodo.22102576 |
| sosp2026 | Anchor: Mitigating Shallow Disruptions with Decoupled Memory | not predominantly Python/R (C++, py/R 18%) | https://doi.org/10.5281/zenodo.22128753 |
| sosp2026 | Borges: A Low-Latency Distributed Shared Log on a CXL Memory/SSD Hybrid | not predominantly Python/R (C, py/R 0%) | https://doi.org/10.5281/zenodo.22100507 |
| sosp2026 | Cohort: Decentralized PIR | not predominantly Python/R (Go, py/R 16%) | https://doi.org/10.5281/zenodo.21929085 |
| sosp2026 | Compositional Verification of Cluster Control Planes | not predominantly Python/R (Rust, py/R 1%) | https://doi.org/10.5281/zenodo.21763916 |
| sosp2026 | Computation is Fast, Use Threadlet! | not predominantly Python/R (Verilog, py/R 1%) | https://doi.org/10.5281/zenodo.22097300 |
| sosp2026 | CryptDough: A unified analytics engine for secure multiparty computation | not predominantly Python/R (C, py/R 8%) | https://zenodo.org/records/22077010 |
| sosp2026 | GAZE: Rearchitecting Log-Structured Filesystem for Garbage Collection on Zoned Namespace Flash Storage | not predominantly Python/R (C, py/R 0%) | https://doi.org/10.5281/zenodo.22119686 |
| sosp2026 | Information and Control in Agent-Native Filesystems | not predominantly Python/R (Rust, py/R 4%) | https://zenodo.org/records/21959241 |
| sosp2026 | Multi-LLM Serving at Production Scale | not predominantly Python/R (C++, py/R 5%) | https://doi.org/10.5281/zenodo.22049568 |
| sosp2026 | PirateShip: Append-Only Ledgers for (Mostly) Trusted Execution Environments | not predominantly Python/R (Rust, py/R 29%) | https://doi.org/10.5281/zenodo.22117853 |
| sosp2026 | Scheduling Linux Threads under I/O Chiplet Wall Using cSwitch | not predominantly Python/R (Rust, py/R 0%) | https://doi.org/10.5281/zenodo.22097791 |
| sosp2026 | Smaran: Serving Authenticated Time Travel Queries | not predominantly Python/R (Go, py/R 28%) | https://doi.org/10.5281/zenodo.22106501 |
| sosp2026 | StarfishOS: Revisiting Single System Image on CXL with State-Partitioned Microkernel | not predominantly Python/R (C, py/R 3%) | https://doi.org/10.5281/zenodo.22096448 |
| sosp2026 | Tamias: Feedback-Guided Systematic Concurrency Exploration for Transparent Distributed System Fuzzing | not predominantly Python/R (C, py/R 4%) | https://doi.org/10.5281/zenodo.22122309 |
| sosp2026 | Testing Custom Control Planes Without the Cluster | not predominantly Python/R (Go, py/R 5%) | https://doi.org/10.5281/zenodo.22109592 |
| sosp2026 | VoliStorM: A crash-consistent I/O cache with two kernels instead of two caches | not predominantly Python/R (C, py/R 16%) | https://doi.org/10.5281/zenodo.22132413 |
| sosp2026 | XTRA: Unifying Cache Coherence and Concurrency Control for Distributed Transactions in a CXL Pod | not predominantly Python/R (C, py/R 11%) | https://doi.org/10.5281/zenodo.22090243 |
| atc2026 | Janus: Practical Load Balancing for Confidential Cloud Services | docker-compose at repo root | https://doi.org/10.5281/zenodo.23240477 |
| eurosys2021 | Achieving Low Tail-latency and High Scalability for Serializable Transactions in Edge Computing | docker-compose at repo root | https://github.com/hku-systems/dast |
| eurosys2022 | Unicorn: Reasoning about Configurable System Performance through the lens of Causality | docker-compose at repo root | https://doi.org/10.5281/zenodo.6360540 |
| sosp2023 | Turbo: Effective Caching in Differentially-Private Databases | docker-compose at repo root | https://github.com/columbia/turbo-artifact |
| acsac2019 | Model Inversion Attacks Against Collaborative Inference | GPU keyword in README/dependency file (README.md) | https://github.com/zechenghe/Inverse_Collaborative_Inference |
| acsac2020 | DeepSIM: GPS Spoofing Detection on UAVs using Satellite Imagery Matching | GPU keyword in README/dependency file (README.md) | https://github.com/wangxiaodiu/DeepSim |
| acsac2021 | Detecting Audio Adversarial Examples with Logit Noising | GPU keyword in README/dependency file (README.md) | https://github.com/namgyupark22/_Detecting_Audio_Adversarial_Examples_with_Logit_Noising |
| acsac2021 | Two Souls in an Adversarial Image: Towards Universal Adversarial Example Detection using Multi-view Inconsistency | GPU keyword in README/dependency file (ReadMe.md) | https://github.com/sohaib730/Argus-Adversarial_Detection |
| acsac2022 | Better Together: Attaining the Triad of Byzantine-robust Federated Learning via Local Update Amplification | GPU keyword in README/dependency file (readme.md) | https://github.com/AgrAmplifierAE/AgrAmp |
| acsac2022 | DF-SCA: Dynamic Frequency Side Channel Attacks are Practical | GPU keyword in README/dependency file (README.md) | https://github.com/Diptakuet/DF-SCA-Dynamic-Frequency-Side-Channel-Attacks-are-Practical |
| acsac2022 | Reconstruction Attack on Differential Private Trajectory Protection Mechanisms | GPU keyword in README/dependency file (README.md) | https://github.com/erik-buchholz/RAoPT |
| acsac2022 | Squeezing More Utility via Adaptive Clipping on Differentially Private Gradients in Federated Meta-Learning | GPU keyword in README/dependency file (README.md,environment.yml) | https://github.com/ning-wang1/DPFedMeta |
| acsac2023 | DeepTaster: Adversarial Perturbation-Based Fingerprinting to Identify Proprietary Dataset Use in Deep Neural Networks | GPU keyword in README/dependency file (README.md,environment.yaml) | https://github.com/qkrtjsgp08/DeepTaster |
| acsac2023 | DefWeb: Defending User Privacy against Cache-based Website Fingerprinting Attacks with Intelligent Noise Injection | GPU keyword in README/dependency file (README.md) | https://github.com/hunie-son/DefWeb |
| acsac2023 | Poisoning Network Flow Classifiers | GPU keyword in README/dependency file (README.md) | https://github.com/ClonedOne/poisoning_network_flow_classifiers |
| atc2026 | AutoTailor: Automatic and Efficient Adaptive Model Deployment for Diverse Edge Devices | GPU keyword in README/dependency file (README.md) | https://doi.org/10.5281/zenodo.22984628 |
| atc2026 | DiGiT: Co-Designing GNN Algorithms and Storage I/O for Out-of-Core Training Acceleration | GPU keyword in README/dependency file (README.md) | https://doi.org/10.5281/zenodo.23227584 |
| atc2026 | ELDR: Expert-Locality-Aware Decode Routing for PD-Disaggregated MoE Serving | GPU keyword in README/dependency file (README.md,pyproject.toml,setup.py) | https://doi.org/10.5281/zenodo.23310788 |
| atc2026 | HACK: Homomorphic Acceleration via Compression of the Key-Value Cache for Disaggregated LLM Inference | GPU keyword in README/dependency file (README.md) | https://doi.org/10.5281/zenodo.22982680 |
| atc2026 | Rocks, Pebbles and Sand: Modality-aware Scheduling for Multimodal Large Language Model Inference | GPU keyword in README/dependency file (README.md,requirements.txt) | https://doi.org/10.5281/zenodo.23134685 |
| atc2026 | Storage Evolution for Exascale Supercomputers: Lessons from Sunway TaihuLight to OceanLight | GPU keyword in README/dependency file (README.md) | https://doi.org/10.5281/zenodo.23201065 |
| cais2026 | AI Realtor: Towards Grounded Persuasive Language Generation for Automated Copywriting | GPU keyword in README/dependency file (README.md) | https://github.com/yangalan123/AI-Realtor-Codebase |
| cais2026 | AgentStop: Terminating Local AI Agents Early to Save Energy in Consumer Devices | GPU keyword in README/dependency file (README.md) | https://github.com/brave-experiments/AgentStop |
| cais2026 | Do Agents Need to Plan Step-by-Step? Rethinking Planning Horizon in Data-Centric Tool Calling | GPU keyword in README/dependency file (README.md) | https://github.com/megagonlabs/cais26-planning-horizon |
| cais2026 | Exploring and Developing a Pre-Model Safeguard with Draft Models | GPU keyword in README/dependency file (README.md) | https://github.com/standbyme/proj-jailbreak-sandbox |
| cais2026 | Glia: A Human-Inspired AI for Automated Systems Design and Optimization | GPU keyword in README/dependency file (README.md) | https://github.com/mit-nms/Engram |
| cais2026 | Improving Coherence and Persistence in Agentic AI for System Optimization | GPU keyword in README/dependency file (README.md) | https://github.com/mit-nms/Engram |
| cais2026 | Learning from Supervision with Semantic and Episodic Memory: A Reflective Approach to Agent Adaptation | GPU keyword in README/dependency file (README.md) | https://github.com/megagonlabs/critique-learning |
| cais2026 | OpaqueToolsBench: Learning Nuances of Tool Behavior Through Interaction | GPU keyword in README/dependency file (README.md,pyproject.toml) | https://github.com/shallinan1/OpaqueToolsBench/ |
| cais2026 | Retrieval-Augmented LLMs for Security Incident Analysis | GPU keyword in README/dependency file (README.md) | https://github.com/neu-nds2/llm-sec-incident-analysis |
| cais2026 | Robust Batch-Level Query Routing for Large Language Models under Cost and Capacity Constraints | GPU keyword in README/dependency file (README.md) | https://github.com/CoreAI-Optimization/robust-llm-router |
| cais2026 | Securing the Agent: Vendor-Neutral, Multitenant Enterprise Retrieval and Tool Use | GPU keyword in README/dependency file (README.md) | https://github.com/varshaprasad96/ogx-evals |
| cais2026 | Who Decides the Trade-off? Resolution Policy as Delegation Governance in Autonomous Agents | GPU keyword in README/dependency file (README.md) | https://doi.org/10.5281/zenodo.20043263 |
| eurosys2021 | Seastar: Vertex-Centric Programming for Graph Neural Networks | GPU keyword in README/dependency file (README.md) | https://zenodo.org/records/4988602 |
| eurosys2022 | D3: A Dynamic Deadline-Driven Approach for Building Autonomous Vehicles | GPU keyword in README/dependency file (README.md) | https://doi.org/10.5281/zenodo.6345350 |
| eurosys2022 | LiteReconfig: Cost and Content Aware Reconfiguration of Video Object Detection Systems for Mobile GPUs | GPU keyword in README/dependency file (README.md) | https://doi.org/10.5281/zenodo.6345733 |
| eurosys2023 | Pocket: ML Serving from the Edge | GPU keyword in README/dependency file (README.md) | https://doi.org/10.5281/zenodo.7734815 |
| eurosys2023 | REFL: Resource-Efficient Federated Learning | GPU keyword in README/dependency file (README.md) | https://doi.org/10.5281/zenodo.7141105 |
| eurosys2024 | Aceso: Efficient Parallel DNN Training through Iterative Bottleneck Alleviation | GPU keyword in README/dependency file (README.md) | http://doi.org/10.5281/zenodo.10077042 |
| eurosys2024 | GMorph: Accelerating Multi-DNN Inference via Model Fusion | GPU keyword in README/dependency file (README.md) | http://doi.org/10.5281/zenodo.10783786 |
| eurosys2024 | Orion: Interference-aware, Fine-grained GPU Sharing for ML Applications | GPU keyword in README/dependency file (README.md) | http://doi.org/10.5281/zenodo.10084464 |
| eurosys2024 | WiseGraph: Optimizing GNN with Joint Workload Partition of Graph and Operations | GPU keyword in README/dependency file (README.md) | http://doi.org/10.5281/zenodo.10791372 |
| eurosys2025 | DeltaZip: Efficient Serving of Multiple Full-Model-Tuned LLMs | GPU keyword in README/dependency file (README.md) | https://github.com/eth-easl/deltazip-ae |
| eurosys2025 | Efficiently Packing Privacy Budget with DPack | GPU keyword in README/dependency file (README.md) | https://doi.org/10.5281/zenodo.13731043 |
| eurosys2025 | Eva: Cost-Efficient Cloud-Based Cluster Scheduling | GPU keyword in README/dependency file (README.md) | https://github.com/uw-mad-dash/Eva |
| eurosys2025 | Mist: Efficient Distributed Training of Large Language Models via Memory-Parallelism Co-Optimization | GPU keyword in README/dependency file (README.md) | https://github.com/dazz993/mist |
| eurosys2025 | NeuStream: Bridging Deep Learning Serving and Stream Processing | GPU keyword in README/dependency file (README.md) | https://github.com/Fjallraven-hc/NeuStream-AE |
| eurosys2025 | Samoyeds: Accelerating MoE Models with Structured Sparsity Leveraging Sparse Tensor Cores | GPU keyword in README/dependency file (README.md,setup.py) | https://github.com/guqiqi/Samoyeds |
| eurosys2025 | SpotHedge: Serving AI Models on Spot Instances | GPU keyword in README/dependency file (README.md) | https://github.com/MaoZiming/spothedge_ae |
| eurosys2025 | You Only Prefill Once: Combining Cached Knowledge for Large Language Model Serving with CacheBlend | GPU keyword in README/dependency file (README.md) | https://github.com/YaoJiayi/CacheBlend.git |
| eurosys2026 | FlashPS: Efficient Generative Image Editing with Mask-aware Caching and Scheduling | GPU keyword in README/dependency file (README.md,requirements.txt) | https://github.com/Sylvia-16/FlashPS |
| eurosys2026 | HARP: Orchestrating Automated Parallel Training on Heterogeneous GPU Clusters | GPU keyword in README/dependency file (README.md) | https://github.com/Lssyes/harp |
| eurosys2026 | MinatoLoader: Accelerating Machine Learning Training Through Efficient Data Preprocessing | GPU keyword in README/dependency file (README.md,requirements.txt) | https://github.com/Rahm-no/MinatoLoader |
| eurosys2026 | SkyWalker: A Locality-Aware Cross-Region Load Balancer for LLM Inference | GPU keyword in README/dependency file (README.md) | https://github.com/skypilot-org/skypilot |
| eurosys2026 | SwiftFL: Enabling Speculative Training for On-Device Federated Deep Learning | GPU keyword in README/dependency file (README.md) | https://github.com/grueyg/swiftfl_ae |
| eurosys2026 | Taming Latency-Memory Trade-Off in MoE-Based LLM Serving via Fine-Grained Expert Offloading | GPU keyword in README/dependency file (README.md,pyproject.toml) | https://github.com/IntelliSys-Lab/FineMoE-EuroSys26 |
| sc2021 | AgEBO-Tabular: Joint Neural Architecture and Hyperparameter Search with Autotuned Data-Parallel Training for Tabular Data full strip note | GPU keyword in README/dependency file (setup.py) | https://doi.org/10.5281/zenodo.4094667 |
| sc2021 | Characterization and Prediction of Deep Learning Workloads in Large-Scale GPU Datacenters | GPU keyword in README/dependency file (README.md) | https://doi.org/10.5281/zenodo.5116412 |
| sc2021 | Efficient Large-Scale Language Model Training on GPU Clusters | GPU keyword in README/dependency file (README.md,setup.py) | https://doi.org/10.5281/zenodo.5181820 |
| sc2021 | Enable Simultaneous DNN Services Based on Deterministic Operator Overlap and Precise Latency Prediction | GPU keyword in README/dependency file (readme.md) | https://doi.org/10.5281/zenodo.5176097 |
| sc2021 | KAISA: An Adaptive Second-order Optimizer Framework for Deep Neural Networks | GPU keyword in README/dependency file (README.md,environment.yml,requirements.txt) | https://doi.org/10.5281/zenodo.4895203 |
| sc2021 | Productivity, Portability, Performance: Data-Centric Python | GPU keyword in README/dependency file (README.md) | https://doi.org/10.5281/zenodo.5155509 |
| sc2021 | ZeRO-Infinity: Breaking the GPU Memory Wall for Extreme Scale Deep Learning | GPU keyword in README/dependency file (README.md,setup.py) | https://doi.org/10.5281/zenodo.5156596 |
| sosp2019 | Generalized Pipeline Parallelism for DNN Training | GPU keyword in README/dependency file (README.md,requirements.txt) | https://github.com/msr-fiddle/pipedream |
| sosp2023 | Bagpipe: Accelerating Deep Recommendation Model Training | GPU keyword in README/dependency file (README.md) | https://github.com/iidsample/bagpipe_artifacts |
| sosp2023 | Efficient Memory Management for Large Language Model Serving with PagedAttention | GPU keyword in README/dependency file (README.md,requirements.txt) | https://github.com/sosp-ae-39/sosp-ae-astra |
| sosp2023 | GEMINI: Fast Failure Recovery in Distributed Training with In-Memory Checkpoints | GPU keyword in README/dependency file (README.md,setup.py) | https://github.com/zhuangwang93/SOSP-30_AE |
| sosp2023 | Oobleck: Resilient Distributed Training of Large Models Using Pipeline Templates | GPU keyword in README/dependency file (README.md) | https://github.com/SymbioticLab/oobleck |
| sosp2023 | PIT: Optimization of Dynamic Sparse Deep Learning Models via Permutation Invariant Transformation | GPU keyword in README/dependency file (README.md,requirements.txt,setup.py) | https://github.com/microsoft/SparTA/tree/pit_artifact |
| sosp2023 | Private Web Search with Tiptoe | GPU keyword in README/dependency file (README.md) | https://github.com/ahenzinger/tiptoe |
| sosp2023 | Sia: Heterogeneity-Aware, Goodput-Optimized ML-Cluster Scheduling | GPU keyword in README/dependency file (README.md) | https://github.com/siasosp23/artifacts/tree/main |
| sosp2023 | gSampler: General and Efficient GPU-based Graph Sampling for Graph Learning | GPU keyword in README/dependency file (README.md) | https://github.com/gpzlx1/gsampler-artifact-evaluation |
| sosp2024 | Reducing Energy Bloat in Large Model Training | GPU keyword in README/dependency file (README.md,pyproject.toml) | https://github.com/ml-energy/zeus/tree/kronos |
| sosp2025 | DiffKV: Differentiated Memory Management for Large Language Models with Parallel KV Compaction | GPU keyword in README/dependency file (README.md,pyproject.toml,requirements.txt,setup.py) | https://github.com/zyqCSL/DiffKV |
| sosp2025 | Jenga: Effective Memory Management for Serving LLM with Heterogeneity | GPU keyword in README/dependency file (README.md) | https://github.com/heheda12345/Jenga-SOSP25-AE |
| sosp2025 | KTransformers: Unleashing the Full Potential of CPU/GPU Hybrid Inference for MoE Models | GPU keyword in README/dependency file (README.md,README_ZH.md,pyproject.toml) | https://github.com/kvcache-ai/ktransformers |
| sosp2025 | Mercury: Unlocking Multi-GPU Operator Optimization for LLMs via Remote Memory Scheduling | GPU keyword in README/dependency file (README.md) | https://github.com/ChandlerGuan/mercury_artifact |
| sosp2025 | PrefillOnly: An Inference Engine for Prefill-only Workloads in Large Language Model Applications | GPU keyword in README/dependency file (README.md,pyproject.toml,requirements-cuda.txt,requirements-openvino.txt,requirements-rocm.txt,requirements-test.txt,setup.py) | https://doi.org/10.5281/zenodo.16972909 |
| sosp2025 | Sailor: Automating Distributed Training over Dynamic, Heterogeneous, and Geo-distributed Clusters | GPU keyword in README/dependency file (README.md) | https://github.com/eth-easl/sailor |
| sosp2025 | Spirit: Fair Allocation of Interdependent Resources in Remote Memory Systems | GPU keyword in README/dependency file (README.md) | https://github.com/yale-nova/spirit |
| sosp2025 | Tempo: Compiled Dynamic Deep Learning with Symbolic Dependence Graphs | GPU keyword in README/dependency file (README.md) | https://github.com/LSDS/Tempo |
| sosp2026 | DiFlow: A System for Micro-Serving Text-to-Image Diffusion Workflows | GPU keyword in README/dependency file (README.md,requirements.txt,setup.py) | https://doi.org/10.5281/zenodo.22098454 |
| sosp2026 | Guiding Agentic GPU Kernel Optimization with Data Flow Invariants | GPU keyword in README/dependency file (README.md) | https://doi.org/10.5281/zenodo.22133069 |
| sosp2026 | MCon: Building an Elastic Cloud for Android Applications with Framework Consolidation | GPU keyword in README/dependency file (README.md) | https://doi.org/10.5281/zenodo.22113077 |
