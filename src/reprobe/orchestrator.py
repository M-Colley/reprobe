"""The 6-stage pipeline. This is the only trusted controller; it never executes
author code in-process — every analysis goes through docker_exec into a
sandboxed container. It owns work dirs and writes the report artifacts.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .config import Config, load_config
from .detect import detect as detect_artifacts
from .detect.manifest import declared_data_sources
from .docker_exec import DAEMON_DOWN_ERRORS, run_container
from .docker_exec import image_digest as _image_digest
from .envbuild import plan as plan_env
from .fetch import FetchError
from .fetch import configure as configure_fetchers
from .fetch import fetch as fetch_ref
from .llm import from_config as llm_from_config
from .llm import roles as llm_roles
from .models import (
    ContainerSpec,
    EnvPlan,
    FetchResult,
    Mount,
    Pin,
    Report,
    RunResult,
)
from .report import badges as badge_rules
from .report import html as html_report
from .report import markdown as md_report
from .runners import RunContext, load_runners, runner_for
from .runners.base import snapshot


def _slug(ref: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", ref).strip("-").lower()
    # Strip AFTER the slice, not only before it. Keeping the TAIL is deliberate —
    # it holds the part that distinguishes one submission from another, where the
    # head is a host prefix every submission shares — but the cut lands mid-token
    # and can re-expose a leading "-", and a directory named "-github-com-..." is
    # read as flags by every tool that later touches work/ (`rm -github-com-...`).
    return s[-28:].strip("-") or "submission"


def submission_id(ref: str) -> str:
    return f"{_slug(ref)}-{hashlib.sha1(ref.encode()).hexdigest()[:6]}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fresh_dir(work: Path, target: Path, stem: str) -> Path:
    """Remove a previous per-run directory so this run starts from a pristine
    tree; if Windows file locks defeat rmtree, fall back to a fresh
    uniquely-named sibling instead of crashing mid-batch.

    Re-running the same submission MUST NOT inherit the last run's files: a stale
    ``src/`` makes ``git clone`` fail outright ("destination path already exists
    and is not an empty directory"), and the local fetcher
    (``copytree(dirs_exist_ok=True)``) would silently merge the old tree into the
    new one."""
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    if target.exists():
        for n in range(1, 100):
            cand = work / f"{stem}-{n}"
            if not cand.exists():
                return cand
    return target


class Orchestrator:
    def __init__(self, config: Optional[Config] = None, *, workroot: str | Path = "work"):
        self.config = config or load_config()
        self.workroot = Path(workroot)
        configure_fetchers(self.config.fetch_cfg)   # extra_git_hosts / dataverse_hosts from pins.yaml
        enabled = {row["id"] for row in self.config.runner_rows if row.get("enabled", True)}
        self._runner_load_errors: list[str] = []
        self.runners = load_runners(enabled_ids=enabled or None, rows=self.config.runner_rows,
                                    errors=self._runner_load_errors)

    # ------------------------------------------------------------------ #
    def run(
        self,
        ref: str,
        *,
        do_run: bool = True,
        functional: bool = True,
        use_llm: bool = True,
        allow_repo2docker: bool = False,
        allow_net: Optional[list[str]] = None,
        allow_lfs: bool = False,
        install: bool = True,
        dry_run: bool = False,
        timeout_s: Optional[int] = None,
        sid: Optional[str] = None,
        data_sources: Optional[list[str]] = None,
        reuse_downloads: bool = False,
    ) -> Report:
        sid = sid or submission_id(ref)
        # Files merged in from a data deposit, relative to srcdir. They are in the
        # tree but they are not the submission, so anything that reasons about
        # what the artifact *contains* has to be able to exclude them.
        self._merged_data_paths: set[str] = set()
        work = self.workroot / sid
        rundir = work / "run"
        outdir = work / "out"
        logdir = work / "logs"
        # A fetch must land in a pristine tree, so re-running a submission (or a
        # batch --resume retry) never inherits the previous fetch's files.
        srcdir = fresh_dir(work, work / "src", "src")
        for d in (srcdir, outdir, logdir):
            d.mkdir(parents=True, exist_ok=True)

        report = Report(submission_id=sid, harness_version=f"reprobe {__version__}", timestamp=_now())
        report.provenance = self._provenance()

        llm_client = None
        if use_llm:
            llm_client = llm_from_config(self.config.llm)
            if llm_client is not None:
                status = llm_client.status()
                if not status.get("ok"):
                    # enabled-but-broken is not the same as --no-llm; say why.
                    report.llm = {"status": "enabled-but-unavailable",
                                  "model": self.config.llm.get("model"),
                                  "detail": status.get("detail")}
                    llm_client = None
                else:
                    report.llm = {"model": self.config.llm.get("model"), "source": "llm-advisory"}

        # -- (1) FETCH (network on; code NOT run) ----------------------- #
        try:
            fetch_res = fetch_ref(ref, srcdir, allow_lfs=allow_lfs)
        except FetchError as e:
            report.source = _failed_source_section(ref, str(e))
            report.not_verified.append(f"fetch failed ({e}); nothing about the artifact was checked")
            report.verdict = {"overall": "fetch-failed", "human_review_required": True}
            # The code half failing says nothing about the data half. Record what
            # the supplied deposits report about themselves before giving up: a
            # deposit that exists under an embargo with a known lift date is a
            # different availability finding from one that was never made, and
            # losing that distinction is losing the only finding still available.
            self._probe_unfetched_data(data_sources, report)
            self._write(outdir, report)
            return report
        report.source = _source_section(fetch_res)

        # -- (1b) SECONDARY DATA SOURCES -------------------------------- #
        # Before detection, so a deposit's files are part of what gets
        # inventoried, planned and copied into the run tree.
        self._data_source_phase(data_sources, srcdir, work, report)

        # -- (2) DETECT ------------------------------------------------- #
        detect_res, manifest_meta = detect_artifacts(srcdir, use_llm=use_llm, llm_client=llm_client)
        report.detect = {
            "artifact_types": detect_res.artifact_types,
            "inventory": detect_res.inventory,
            "manifest": detect_res.manifest_path,
            "run_plan_source": detect_res.run_plan_source,
            "llm_confidence": detect_res.llm_confidence,
            "flags": detect_res.flags,
            "notes": detect_res.notes + self._runner_load_errors,
            "steps": [s.target for s in detect_res.steps],
        }
        self._note_prose_only_data(srcdir, report)

        # -- (3) PLAN ENV ----------------------------------------------- #
        env_plan = plan_env(detect_res, manifest_meta, self.config, srcdir,
                            allow_repo2docker=allow_repo2docker)
        report.environment = _env_section(env_plan)
        if timeout_s:
            # A non-default budget makes this run non-comparable to a default one;
            # that belongs on the record, not just in the operator's shell history.
            report.environment.setdefault("warnings", []).append(
                f"per-step timeout overridden to {timeout_s}s via --timeout "
                f"(config default: {self.config.limits.get('defaults', {}).get('timeout_s')}s)")

        functional_requested = functional and manifest_wants_functional(manifest_meta, functional)

        # -- (4) RUN (sandboxed) ---------------------------------------- #
        results: list[RunResult] = []
        used_images: set[str] = set()        # every distinct image that ran a container
        unity_section = None
        ran = False
        if do_run and detect_res.steps:
            ran = True
            rundir = fresh_dir(work, rundir, "run")
            shutil.copytree(srcdir, rundir)
            self._dataset_phase(manifest_meta, rundir, report, dry_run=dry_run)
            self._install_phase(env_plan, rundir, logdir, install=install, dry_run=dry_run,
                                report=report, reuse_downloads=reuse_downloads)

            allow_egress_runtime = bool(allow_net)
            if allow_egress_runtime:
                # a badge-confidence downgrade is a warning, not an FYI
                report.environment.setdefault("warnings", []).append(
                    "RAN WITH RUNTIME EGRESS (--allow-net " + ",".join(allow_net or []) + "); badge "
                    "confidence downgraded. NOTE: per-host allowlisting is not yet enforced — this grants "
                    "full egress for the run phase.")

            for i, step in enumerate(detect_res.steps):
                runner = runner_for(step, self.runners)
                if runner is None:
                    results.append(RunResult(runner=step.runner or "?", target=step.target,
                                             status="skipped", executed=False,
                                             diagnostics={"reason": "no runner",
                                                          "loaded_runners": sorted(self.runners)}))
                    continue
                image = None if getattr(runner, "host_only", False) else (
                    env_plan.image
                    if env_plan.env_provenance in ("author-specified", "fallback-generic")
                    else self.config.base_image(runner.image_key) or env_plan.image)
                step_limits = self.config.limits_for(runner.id)
                if timeout_s:
                    # docker_exec still clamps this to limits.yaml:max_timeout_s —
                    # the operator picks a budget, config keeps the ceiling.
                    step_limits["timeout_s"] = timeout_s
                ctx = RunContext(step=step, rundir=rundir, src_dir=srcdir, out_dir=outdir,
                                 image=image, config=self.config,
                                 limits=step_limits,
                                 pre_index=snapshot(rundir),
                                 conda_env_prefix=env_plan.conda_env_prefix)
                spec = runner.container_spec(ctx)
                if spec is None:                       # host-only runner (Unity T0)
                    res = runner.interpret(None, ctx)
                    res.executed = False               # authoritative: no author code ran
                    if runner.id == "unity":
                        unity_section = _unity_section(res)
                else:
                    if allow_egress_runtime:
                        spec = spec.model_copy(update={"network": "egress"})
                    if spec.image:
                        used_images.add(spec.image)
                    log_path = logdir / f"step{i:02d}-{runner.id}.log"
                    raw = run_container(spec, ctx.limits, log_path,
                                        allow_egress=allow_egress_runtime, dry_run=dry_run,
                                        work_root=rundir)
                    res = runner.interpret(raw, ctx)
                    self._diagnose(res, llm_client, env_plan, step, logdir, runner.image_key)
                results.append(res)
                if _daemon_down(res):
                    # Once the daemon is gone every remaining step fails in
                    # milliseconds for the same host reason, and each one reads in
                    # the report like an independent finding about the artifact.
                    # Record the truth — they were never attempted — and stop.
                    for rest in detect_res.steps[i + 1:]:
                        results.append(RunResult(
                            runner=rest.runner or "?", target=rest.target,
                            status="skipped", executed=False,
                            diagnostics={"reason": "not attempted — the Docker daemon became "
                                                   f"unreachable during `{step.target}`"}))
                    break
            self._collect_artifacts(results, rundir, outdir)
            # Pin the exact image bytes that ran (pins.yaml carries a mutable tag).
            # A mixed python+R run uses more than one base, so record a digest per
            # distinct image actually used — not just the single env image (which
            # would silently omit the R base). Resolved from the real daemon; an
            # absent image yields no digest and is skipped — never faked.
            if not dry_run and used_images:
                digests = {im: d for im in sorted(used_images) if (d := _image_digest(im))}
                if digests:
                    report.environment["base_image_digests"] = digests
                    primary = report.environment.get("image")
                    # Keep the scalar base_image_digest for back-compat: prefer the
                    # primary env image, else any one resolved digest.
                    chosen = digests.get(primary) or next(iter(digests.values()))
                    env_plan.base_image_digest = chosen
                    report.environment["base_image_digest"] = chosen
        report.steps = results
        report.unity = unity_section

        # -- (5) BADGES + VERDICT --------------------------------------- #
        # An install phase that never finished makes every downstream step
        # unjudgeable, so both the badge and the verdict have to hear about it.
        install_err = badge_rules.install_error(report.environment)
        report.badges = badge_rules.decide(
            fetch_res, results, detect_res,
            badges_cfg=self.config.badges, functional_requested=functional_requested, ran=ran,
            install_error=install_err)
        report.verdict = badge_rules.verdict(results, ran, install_err)
        report.not_verified = sorted(
            {x for r in results for x in r.not_verified} | set(report.not_verified))

        # -- (6) RESULTS CHECK vs THE PAPER (advisory) ------------------- #
        if llm_client is not None and ran and results:
            self._results_check(report, results, srcdir, work, manifest_meta,
                                fetch_res, llm_client)

        # -- (7) LLM SUMMARY (advisory) --------------------------------- #
        if llm_client is not None:
            summary = llm_roles.summarize(llm_client, report.model_dump(mode="json"))
            if summary:
                report.llm["summary"] = summary

        self._write(outdir, report)
        return report

    # ------------------------------------------------------------------ #
    def _note_prose_only_data(self, srcdir: Path, report: Report) -> None:
        """Say so when the artifact's data lives somewhere its README only names
        in prose. Without this the run fails at the first missing input and the
        report reads like broken code, when the real finding is that the data was
        never declared anywhere a harness can act on."""
        if report.source.get("data_sources"):
            return                       # a deposit was supplied; nothing to advise
        from .fetch.data_source import referenced_deposits

        hints = referenced_deposits(srcdir)
        if not hints:
            return
        report.detect.setdefault("notes", []).append(
            "the documentation links a data repository but the artifact declares no "
            "`data_sources:` in a manifest, so the harness has no way to fetch it: "
            + ", ".join(hints)
            + ". If a step below fails on a missing input, re-run with "
            + " ".join(f"--data {h}" for h in hints[:2]))
        report.not_verified.append(
            "whether the artifact runs WITH its external data — the data is linked in prose "
            "only and was not fetched")

    def _probe_unfetched_data(self, specs: Optional[list[str]], report: Report) -> None:
        """Record the state of every ``--data`` deposit when the code fetch failed.

        Only the operator's sources are known at this point — the manifest's
        ``data_sources:`` live inside the tree that could not be fetched."""
        if not specs:
            return
        from .fetch.data_source import parse_ref, probe_data_source

        records = []
        for spec in specs:
            url, _ = parse_ref(str(spec))
            if url:
                records.append(probe_data_source(url))
        if not records:
            return
        report.source["data_sources"] = records
        for r in records:
            report.not_verified.append(
                f"data deposit '{r['input']}' was not fetched (the code fetch failed first) — "
                f"{r['status']}: {r['detail']}")

    def _data_source_phase(self, specs: Optional[list[str]], srcdir: Path, work: Path,
                           report: Report) -> None:
        """Fetch secondary data deposits and merge them into the artifact tree.

        The "code in git, data on OSF" split is the common shape of an artifact,
        and fetching one half alone says nothing: the code half dies at the first
        ``read_csv`` and the data half has nothing to run. Sources come from
        ``--data`` and from the manifest's ``data_sources``; the operator's are
        merged first, so a deposit can never displace what the chair asked for."""
        specs = list(specs or []) + declared_data_sources(srcdir)
        if not specs:
            return
        from .fetch.data_source import fetch_data_source, merge_into, parse_ref

        records: list[dict[str, Any]] = []
        notes: list[str] = []
        merged: set[str] = self._merged_data_paths
        for i, spec in enumerate(specs):
            url, into = parse_ref(str(spec))
            if not url:
                continue
            rec: dict[str, Any] = {"input": url, "into": into or "."}
            stage = fresh_dir(work, work / f"data{i:02d}", f"data{i:02d}")
            try:
                fr = fetch_data_source(url, stage)
                copied, collisions = merge_into(stage, srcdir, into)
            except FetchError as e:
                rec.update(status="failed", error=str(e))
                records.append(rec)
                notes.append(f"data source '{url}' could not be fetched ({e}) — the artifact was "
                             f"checked WITHOUT it, so a missing-input failure below may be ours")
                continue
            finally:
                shutil.rmtree(stage, ignore_errors=True)
            merged.update(copied)
            rec.update(status="ok", resolved_type=fr.resolved_type, pin=fr.pin.model_dump(),
                       files=len(copied), collisions=collisions,
                       checksum_verified=fr.checksum_verified, warnings=fr.warnings)
            records.append(rec)
            if not copied:
                notes.append(f"data source '{url}' contributed no files")
            if collisions:
                # Never silently resolved: if a deposit could overwrite a script,
                # the code reviewed would not be the code submitted.
                notes.append(
                    f"data source '{url}': {len(collisions)} file(s) already existed in the "
                    f"artifact and were NOT overwritten ({', '.join(collisions[:5])}"
                    f"{', …' if len(collisions) > 5 else ''})")
        if not records:
            return
        report.source["data_sources"] = records
        if any(r.get("status") == "ok" for r in records):
            notes.append("data was merged from a separate deposit; it is author-controlled and "
                         "does NOT strengthen the Available badge, which is decided by the "
                         "primary source's pin alone")
        report.source.setdefault("warnings", []).extend(notes)

    def _dataset_phase(self, manifest_meta: dict[str, Any], rundir: Path, report: Report, *,
                       dry_run: bool) -> None:
        """Download author-declared datasets (manifest ``data[]``) into the run
        tree before the offline analysis runs. Host-side, but hardened: http(s)
        only, SSRF-guarded host, byte-capped stream, path-contained, and checksum
        honesty. Off unless the manifest declares data; skipped in dry-run."""
        data = (manifest_meta or {}).get("data") or []
        if not data or dry_run:
            return
        from .fetch.base import (
            assert_safe_url,
            checksum_verdict,
            download,
            new_checksum_stats,
            record_download,
            safe_join,
        )

        results: list[dict[str, Any]] = []
        stats = new_checksum_stats()
        notes: list[str] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("path") or "").strip()
            source = str(entry.get("source") or "").strip()
            checksum = str(entry.get("checksum") or "").strip()
            if not path or not source:
                notes.append(f"entry missing path/source: {entry!r}")
                continue
            if not source.lower().startswith(("http://", "https://")):
                # DOI/platform sources are not wired here yet — declare, don't fake.
                results.append({"path": path, "source": source, "status": "skipped-unsupported-source"})
                notes.append(f"'{path}': source '{source}' is not an http(s) URL "
                             "(DOI/platform dataset fetching not yet wired); NOT downloaded")
                continue
            try:
                assert_safe_url(source)
                target = safe_join(rundir, path)
            except FetchError as e:
                results.append({"path": path, "source": source, "status": "refused"})
                notes.append(f"'{path}': {e}")
                continue
            md5 = checksum if (checksum.lower().startswith("md5")
                               or re.fullmatch(r"[0-9a-fA-F]{32}", checksum)) else None
            ok, note = download(source, target, expected_md5=md5, restrict_public=True)
            record_download(stats, ok, note, had_checksum=bool(md5))
            results.append({"path": path, "source": source,
                            "status": "ok" if ok else "failed", "note": note})
            if not ok:
                notes.append(f"'{path}' download failed: {note}")
            elif checksum and md5 is None:
                notes.append(f"'{path}': checksum '{checksum}' is not md5 and was not verified")
        checksum_verdict(stats, notes)
        if results:
            report.environment["datasets"] = results
        for n in notes:
            report.environment.setdefault("notes", []).append(f"dataset: {n}")
        if any(r["status"] == "ok" for r in results):
            report.environment.setdefault("notes", []).append(
                "author-declared datasets were downloaded into the run tree (host-side, byte-capped + "
                "SSRF-guarded); this data is author-controlled and does NOT strengthen the Available badge")

    # ------------------------------------------------------------------ #
    def _install_phase(self, env_plan: EnvPlan, rundir: Path, logdir: Path, *,
                       install: bool, dry_run: bool, report: Report,
                       reuse_downloads: bool = False) -> None:
        if not install or not env_plan.install_commands:
            return
        # SEPARATE, sanctioned egress containers; the actual analysis runs offline.
        # Split commands by language so each runs on the right base image. The
        # install phase relaxes the rootfs/tmpfs hardening because installing &
        # compiling packages must write and exec — but it is still egress-only,
        # resource-capped, socket-free, and ephemeral.
        groups: dict[str, list[str]] = {"python": [], "r": []}
        for c in env_plan.install_commands:
            groups["r" if c.strip().startswith("Rscript") else "python"].append(c)

        relaxed = {**self.config.limits_for("python"),
                   "read_only_rootfs": False, "tmpfs_noexec": False, "tmpfs_size": "4g",
                   # compiling a large source dependency tree can exceed the 30-min
                   # author-code cap; give the harness-controlled install phase (no
                   # author code runs here) more wall-clock and build space. An hour
                   # was still short: PerceivedRisk (2026-08) spends the whole of it
                   # inside `micromamba create` — an environment.yml whose pip section
                   # reaches ultralytics, which pulls torch's CUDA wheels, ~8 GB of
                   # mostly small files written to a bind mount. The kill then lands
                   # before the first pip call of the import-scan install, so the step
                   # fails on exactly the import that install exists to supply.
                   "timeout_s": 7200}
        prep = ("set -e; mkdir -p /work/.reprobe_deps /work/.reprobe_Rlib; export HOME=/work; "
                "export R_LIBS_USER=/work/.reprobe_Rlib; export PYTHONPATH=/work/.reprobe_deps:$PYTHONPATH; ")

        # Package downloads land under HOME=/work, i.e. inside the run dir, which
        # is wiped before every run — so re-running one submission re-downloads
        # gigabytes it already had. --reuse-downloads points pip and micromamba at
        # a workroot-level cache instead, turning a 34-minute env build into
        # minutes on the second attempt.
        #
        # OFF by default, and it must stay that way. The cache is shared across
        # submissions, and an environment.yml chooses its own channels: artifact A
        # can seed the package cache with its own build of a common name, which
        # micromamba will then reuse for artifact B without going back to the
        # network. That is a cross-submission influence a review harness should
        # not take on unasked. Use it while iterating on ONE artifact; leave it off
        # for a season of submissions.
        cache_mounts: list[Mount] = []
        if reuse_downloads:
            cache = Path(self.workroot) / ".package-cache"
            for sub in ("pip", "conda", "renv"):
                (cache / sub).mkdir(parents=True, exist_ok=True)
            cache_mounts.append(Mount(source=cache.resolve().as_posix(),
                                      target="/reprobe-cache", read_only=False))
            prep += ("export PIP_CACHE_DIR=/reprobe-cache/pip; "
                     "export CONDA_PKGS_DIRS=/reprobe-cache/conda; "
                     "export MAMBA_PKGS_DIRS=/reprobe-cache/conda; "
                     "export RENV_PATHS_CACHE=/reprobe-cache/renv; ")
            report.environment.setdefault("warnings", []).append(
                "--reuse-downloads was set: pip/conda downloads came from a cache shared with "
                "other submissions in this workroot, so the bytes installed were not all "
                "fetched fresh for this review")

        install_results: dict[str, Any] = {}
        for key, cmds in groups.items():
            if not cmds:
                continue
            image = (env_plan.image if env_plan.env_provenance == "fallback-generic"
                     else self.config.base_image(key) or env_plan.image)
            spec = ContainerSpec(image=image, command=["bash", "-c", prep + " ; ".join(cmds)],
                                 workdir="/work",
                                 mounts=[Mount(source=rundir.as_posix(), target="/work", read_only=False),
                                         *cache_mounts],
                                 network="egress")
            log_path = logdir / f"install-{key}.log"
            raw = run_container(spec, relaxed, log_path, allow_egress=True, dry_run=dry_run,
                                work_root=rundir)
            ok = raw.exit_code == 0
            # Carry WHY it failed, not just that it did: a timeout is the harness
            # running out of clock, and the verdict logic has to be able to say so
            # rather than reprint an exit code the reader cannot interpret.
            install_results[key] = {"exit_code": raw.exit_code, "ok": ok,
                                    "timed_out": bool(raw.timed_out)}
            note = f"install[{key}] ({'ok' if ok else 'failed/skipped'}; egress phase)"
            report.environment.setdefault("notes", []).append(note)
            if not ok:
                cause = ("was killed at the install-phase wall-clock limit" if raw.timed_out
                         else f"failed (exit {raw.exit_code})")
                report.not_verified.append(
                    f"dependency install ({key}) {cause}; the steps below ran against an "
                    "environment the harness never finished building, so their failures are "
                    "ours and not the artifact's")
        report.environment["install_results"] = install_results
        # Hash the install logs (they end with pip freeze / installed.packages()) so
        # two runs of the same deposit are comparable without re-solving.
        digest = _hash_install_logs(logdir)
        if digest:
            env_plan.resolved_deps_digest = digest
            report.environment["resolved_deps_digest"] = digest

    # ------------------------------------------------------------------ #
    def _produced_text(self, results: list[RunResult], rundir: Path) -> str:
        """What the re-run actually reported, as text an LLM can compare against
        the paper: machine-readable outputs first (a declared .csv/.json/.txt is
        the author saying "these are my numbers"), then the console output, which
        for many analyses is where the statistics are actually printed."""
        from .runners.base import _tail

        parts: list[str] = []
        budget = 12_000
        for res in results:
            for rel in res.expected_met or []:
                p = rundir / rel
                if p.suffix.lower() not in (".csv", ".tsv", ".json", ".txt", ".md", ".yml", ".yaml"):
                    continue
                try:
                    if p.is_file() and p.stat().st_size <= 200_000:
                        body = p.read_text(encoding="utf-8", errors="replace")[:4000]
                        parts.append(f"--- produced file {rel} ---\n{body}")
                        budget -= len(body)
                except OSError:
                    continue
            if budget <= 0:
                break
        for res in results:
            tail = _tail(res.log_path, 120)
            if tail.strip():
                parts.append(f"--- console output of {res.target} ---\n{tail}")
        return "\n\n".join(parts)[:12_000]

    def _results_check(self, report: Report, results: list[RunResult], srcdir: Path,
                       work: Path, manifest_meta: dict[str, Any], fetch_res: FetchResult,
                       llm_client) -> None:
        """Advisory comparison of the paper's claims against the run's output.

        Never touches a badge: the SYSTEM prompt tells the model it cannot change
        a decision, and `results_reproduced` stays "not-evaluated" whatever comes
        back. This exists so the human confirmer sees the comparison instead of
        having to do it from scratch."""
        from . import paper as paper_mod

        rundir = work / "run"
        try:
            found = paper_mod.locate(srcdir, work, manifest_meta=manifest_meta,
                                     pin_value=fetch_res.pin.value or "",
                                     exclude=self._merged_data_paths)
        except Exception as e:                      # never let this break a run
            report.llm["results_check"] = {"status": "error",
                                           "detail": f"{type(e).__name__}: {e}"}
            return
        if found is None:
            report.llm["results_check"] = {
                "status": "no-paper",
                "detail": "no paper found to compare against (no PDF in the repo and no DOI "
                          "in the manifest, CITATION.cff, README, or the archival pin)"}
            return

        section: dict[str, Any] = {"source": found.source, "ref": found.ref,
                                   "coverage": found.coverage}
        if found.warnings:
            section["warnings"] = found.warnings
        produced = self._produced_text(results, rundir)
        if not found.text.strip() or not produced.strip():
            section["status"] = "not-compared"
            section["detail"] = ("the paper text could not be read" if not found.text.strip()
                                 else "the run produced no comparable text output")
            report.llm["results_check"] = section
            report.not_verified.append(
                "numerical results were NOT compared to the paper: " + section["detail"])
            return

        advice = llm_roles.compare_results(llm_client, paper=found.excerpt(),
                                           produced=produced, coverage=found.coverage)
        if advice is None:
            section["status"] = "not-compared"
            section["detail"] = "the advisory model returned no usable comparison"
        else:
            section["status"] = "compared"
            section.update(advice)
            counts: dict[str, int] = {}
            for c in advice["claims"]:
                counts[c["verdict"]] = counts.get(c["verdict"], 0) + 1
            section["counts"] = counts
            if counts.get("mismatch"):
                report.not_verified.append(
                    f"{counts['mismatch']} paper claim(s) look inconsistent with the re-run "
                    "(LLM-advisory — a human must confirm)")
        report.llm["results_check"] = section

    def _diagnose(self, res: RunResult, llm_client, env_plan: EnvPlan, step,
                  logdir: Path | None = None, image_key: str | None = None) -> None:
        if res.status in ("pass", "skipped"):
            return
        if res.diagnostics.get("infra"):
            # The harness established this failure itself (daemon gone, image
            # absent, sandbox violation) and the report states it verbatim. The
            # model has no artifact evidence to read here, so what it produces is
            # confident narration about a cause it cannot see.
            return
        from .runners.base import _tail
        run_tail = str(res.diagnostics.get("log_tail", ""))
        if not run_tail.strip() and res.log_path:
            # Runners only record a tail for the statuses they consider failures,
            # but the log file exists either way. Read it here so a "partial" step
            # (ran clean, produced nothing declared) is diagnosed from what it
            # actually printed rather than treated as having printed nothing.
            run_tail = _tail(res.log_path)
        # Recognised signatures first, and INDEPENDENTLY of the LLM — these
        # failures are exactly as diagnosable with --no-llm as with a model, and
        # the old early-return meant a deterministic run got no diagnosis at all.
        # When one matches, the model is not asked: we already know the cause, and
        # a small local model's second opinion beside a known fact can only
        # contradict it.
        known = known_failure_diagnosis(run_tail, res.exit_code)
        if known:
            res.diagnostics["harness_diagnosis"] = known
            return
        if llm_client is None:
            return
        # If a step failed and the dependency-install phase logged an error, feed
        # that in too — the real root cause (e.g. a version constraint) usually
        # lives in the install log, not the run log.
        log_tail = run_tail
        if logdir and image_key:
            ilog = logdir / f"install-{image_key}.log"
            if ilog.exists():
                itail = _tail(str(ilog), 25)
                if "error" in itail.lower():
                    log_tail = f"[dependency-install log]\n{itail}\n\n[run log]\n{run_tail}"
        if not log_tail.strip():
            # Never ask the model to explain nothing. Given an empty log it does
            # not say "no evidence" — it narrates the absence ("the snippet is
            # empty..."), quotes the harness's own untrusted-data fences back
            # into the report, and invents plausible fixes with zero support.
            # A deterministic, clearly-non-LLM note is strictly more honest.
            if not res.diagnostics.get("harness_error"):
                # ...unless the harness already recorded exactly why nothing ran,
                # which the report renders on its own and explains it better.
                res.diagnostics["harness_diagnosis"] = _no_log_diagnosis(res)
            return
        adv = llm_roles.diagnose_failure(
            llm_client, target=step.target, kind=step.kind,
            env=f"{env_plan.strategy}:{env_plan.image}", log_tail=log_tail)
        if adv:
            res.diagnostics["llm_advisory"] = adv

    def _collect_artifacts(self, results: list[RunResult], rundir: Path, outdir: Path) -> None:
        art_dir = outdir / "artifacts"
        for res in results:
            keep = set(res.expected_met) | {a for a in res.artifacts if a.endswith(".executed.ipynb")}
            for rel in keep:
                src = rundir / rel
                if src.is_file():
                    dst = art_dir / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(src, dst)
                    except OSError:
                        pass

    def _provenance(self) -> dict[str, Any]:
        prov: dict[str, Any] = {
            "pins_year": self.config.pins.get("year"),
            "pins_revision": self.config.pins.get("revision"),
        }
        for name in ("pins", "limits", "runners", "badges"):
            p = self.config.config_dir / f"{name}.yaml"
            if p.is_file():
                prov[f"{name}.yaml_sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        if self.config.llm.get("enabled"):
            prov["llm_model"] = self.config.llm.get("model")
            prov["llm_confidence_threshold"] = self.config.llm.get("confidence_threshold")
        return prov


    def _write(self, outdir: Path, report: Report) -> None:
        (outdir / "report.json").write_text(
            json.dumps(report.model_dump(mode="json"), indent=2), encoding="utf-8")
        (outdir / "report.md").write_text(md_report.render(report), encoding="utf-8")
        (outdir / "report.html").write_text(html_report.render(report), encoding="utf-8")


# ---------------------------------------------------------------------- #
def _daemon_down(res: RunResult) -> bool:
    """True when a step failed because the Docker daemon is gone — the one
    failure that makes every later step meaningless rather than informative."""
    diags = res.diagnostics if isinstance(res.diagnostics, dict) else {}
    return str(diags.get("harness_error", "")).startswith(DAEMON_DOWN_ERRORS)


_DETERMINISTIC = "harness (deterministic — not an LLM guess)"

#: R's error when a script calls rstudioapi outside the IDE. Fixed string, one
#: cause: the PACKAGE is installed (it is on CRAN and installs like any other),
#: what is absent is a running RStudio session — which no headless container has.
_RSTUDIO_RE = re.compile(r"RStudio not running", re.I)

#: argparse's usage error. exit 2 + a "usage:" block is argparse's signature and
#: nothing else's; the second pattern separates "you gave me no arguments" from
#: an unrelated exit 2.
_USAGE_RE = re.compile(r"^usage:", re.M)
_ARG_REQUIRED_RE = re.compile(
    r"error: the following arguments are required:\s*(?P<names>.+)$", re.M)


def known_failure_diagnosis(log_tail: str, exit_code: Optional[int]) -> Optional[dict[str, Any]]:
    """Name a failure whose console output has exactly one possible cause.

    These are worth recognising deterministically rather than leaving to the
    advisory model: the signature is a fixed string, the cause admits no
    ambiguity, and a chair reading `fail` learns nothing while a chair reading
    "this script only runs inside RStudio" learns the finding. Both cases below
    are real artifacts, not hypotheticals.

    Deliberately narrow. A pattern that fires on the wrong artifact would state
    the harness's guess with the authority of a fact, which is worse than the
    generic failure it replaces — so each entry matches a message with a single
    known producer, and anything else falls through to the LLM advisory."""
    if not log_tail:
        return None

    if _RSTUDIO_RE.search(log_tail):
        return {
            "source": _DETERMINISTIC,
            "likely_cause": (
                "the script calls rstudioapi (typically "
                "`setwd(dirname(getActiveDocumentContext()$path))`, to set the working "
                "directory to the script's own location), which asks a RUNNING RStudio IDE "
                "which file is open in its editor. The package itself installed fine — what "
                "is missing is the IDE, and a headless container cannot provide one. The "
                "artifact runs for its author because the author runs it inside RStudio; it "
                "does not run for anyone using `Rscript`."),
            "suggested_fixes": [
                "drop the two rstudioapi lines: the harness already runs the script with the "
                "working directory at the artifact root, which is what they were setting",
                "if a path anchor is genuinely needed, `here::here(...)` resolves without an IDE",
            ],
        }

    if exit_code == 2 and _USAGE_RE.search(log_tail):
        m = _ARG_REQUIRED_RE.search(log_tail)
        missing = f" ({m.group('names').strip()})" if m else ""
        return {
            "source": _DETERMINISTIC,
            "likely_cause": (
                f"the script requires command-line arguments{missing} and was invoked without "
                "them — argparse printed its usage block and exited 2. Nothing in the deposit "
                "states what to pass, so no harness can supply them: the missing piece is a "
                "machine-readable invocation, not a dependency."),
            "suggested_fixes": [
                "declare the invocation in the manifest so it is reproducible by anyone: a step "
                "with `args: [...]` is passed straight through to the script",
                "or give the arguments defaults, so the documented entry point runs as deposited",
            ],
        }
    return None


def _no_log_diagnosis(res: RunResult) -> dict[str, Any]:
    """Deterministic stand-in for the LLM diagnoser when a step left no log to
    read. States the one thing that is actually known and points at the next
    concrete action — no cause is invented."""
    if res.status == "timeout":
        limit = res.diagnostics.get("timeout_s")
        return {
            "source": "harness (deterministic — not an LLM guess)",
            "likely_cause": (
                f"the step was killed at the {limit}s budget without writing any console output, "
                "so how far it got is unknown. A notebook that logs nothing usually means the "
                "kernel never reached a printing cell — a long fit/search, a blocking prompt, "
                "or a network call that hangs under --network none."),
            "suggested_fixes": [
                f"re-run this step with a larger budget: `reprobe run <ref> --timeout {int(limit or 1800) * 2}`",
                "inspect the partially-executed notebook collected under out/artifacts/ — "
                "papermill checkpoints after every cell, so the last completed cell is the stall point",
            ],
        }
    return {
        "source": "harness (deterministic — not an LLM guess)",
        "likely_cause": (f"the step ended with status '{res.status}' but produced no console output, "
                         "so there is no evidence to diagnose from."),
        "suggested_fixes": [f"inspect the full log at {res.log_path or '(none recorded)'}"],
    }


def manifest_wants_functional(manifest_meta: dict[str, Any], default: bool) -> bool:
    claimed = (manifest_meta or {}).get("badges_claimed", []) or []
    if claimed:
        return "functional" in claimed
    return default


def _source_section(f: FetchResult) -> dict[str, Any]:
    return {
        "input": f.input, "resolved_type": f.resolved_type,
        "pin": f.pin.model_dump(), "fetch_layer": f.fetch_layer,
        "anonymized": f.anonymized, "checksum_verified": f.checksum_verified,
        "warnings": f.warnings, "metadata": f.metadata,
    }


def _failed_source_section(ref: str, error: str) -> dict[str, Any]:
    """A fetch failure still produces a renderable report. Keep the SAME shape
    as _source_section (with safe defaults) so every consumer — html, markdown,
    dashboard, badge logic — sees a consistent source object and never blows up
    on a missing key."""
    return {
        "input": ref, "resolved_type": None,
        "pin": Pin().model_dump(), "fetch_layer": None,
        "anonymized": False, "checksum_verified": False,
        "warnings": [], "metadata": {}, "error": error,
    }


def _env_section(p: EnvPlan) -> dict[str, Any]:
    return {
        "strategy": p.strategy, "image": p.image, "env_provenance": p.env_provenance,
        "install_commands": p.install_commands, "conda_env_prefix": p.conda_env_prefix,
        "repo2docker_version": p.repo2docker_version,
        "base_image_digest": p.base_image_digest, "resolved_deps_digest": p.resolved_deps_digest,
        "warnings": p.warnings,
    }


def _hash_install_logs(logdir: Path) -> Optional[str]:
    h = hashlib.sha256()
    found = False
    for log in sorted(logdir.glob("install-*.log")):
        try:
            h.update(log.read_bytes())
            found = True
        except OSError:
            pass
    return f"sha256:{h.hexdigest()[:32]}" if found else None


def _unity_section(res: RunResult) -> dict[str, Any]:
    d = dict(res.diagnostics)
    d["tier_reached"] = res.tier_reached
    d["status"] = res.status
    d["not_verified"] = res.not_verified
    return d
