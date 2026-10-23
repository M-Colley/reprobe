"""The 6-stage pipeline. This is the only trusted controller; it never executes
author code in-process — every analysis goes through docker_exec into a
sandboxed container. It owns work dirs and writes the report artifacts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .config import Config, load_config
from .detect import detect as detect_artifacts
from .detect.manifest import declared_data_sources
from .docker_exec import DAEMON_DOWN_ERRORS, reap_orphans, run_container
from .docker_exec import image_digest as _image_digest
from .envbuild import plan as plan_env
from .envbuild.base import INSTALL_HOME, describe_install_command
from .fetch import FetchError
from .fetch import configure as configure_fetchers
from .fetch import fetch as fetch_ref
from .fetch.base import RefusedSource
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


def run_options(*, run_mode: str = "full", functional: bool = True, use_llm: bool = True,
                install: bool = True, allow_net: Optional[list[str]] = None, allow_lfs: bool = False,
                allow_repo2docker: bool = False, timeout_s: Optional[int] = None,
                data_sources: Optional[list[str]] = None, reuse_downloads: bool = False) -> dict[str, Any]:
    """The run options that change what a report can say, in one canonical form.
    Recorded in every report's provenance and compared by `batch --resume`, so a
    report made under other conditions is re-run rather than reused."""
    return {"run_mode": run_mode, "functional": functional, "use_llm": use_llm,
            "install": install, "allow_net": sorted(allow_net or []), "allow_lfs": allow_lfs,
            "allow_repo2docker": allow_repo2docker, "timeout_s": timeout_s,
            "data_sources": list(data_sources or []), "reuse_downloads": reuse_downloads}


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
        outdir = work / "report"
        logdir = work / "logs"
        # A fetch must land in a pristine tree, so re-running a submission (or a
        # batch --resume retry) never inherits the previous fetch's files.
        srcdir = fresh_dir(work, work / "src", "src")
        # Logs and collected artifacts are per run too: an old install log was
        # being fed to the diagnoser, and an old .executed.ipynb sat in
        # report/artifacts/ beside this run's as if this run had made it.
        logdir = fresh_dir(work, logdir, "logs")
        shutil.rmtree(outdir / "artifacts", ignore_errors=True)
        for d in (srcdir, outdir, logdir):
            d.mkdir(parents=True, exist_ok=True)

        report = Report(submission_id=sid, harness_version=f"reprobe {__version__}", timestamp=_now())
        report.run_mode = "dry-run" if dry_run else ("full" if do_run else "no-run")
        report.provenance = self._provenance()
        # What was asked for, so `batch --resume` can tell a report made under the
        # same conditions from one that only shares a submission id.
        report.provenance["run_options"] = run_options(
            run_mode=report.run_mode, functional=functional, use_llm=use_llm, install=install,
            allow_net=allow_net, allow_lfs=allow_lfs, allow_repo2docker=allow_repo2docker,
            timeout_s=timeout_s, data_sources=data_sources, reuse_downloads=reuse_downloads)

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
            self._copy_run_tree(srcdir, rundir, report)
            if not dry_run:
                reaped = reap_orphans()
                if reaped:
                    report.environment.setdefault("notes", []).append(
                        f"removed {len(reaped)} container(s) left behind by an earlier, interrupted "
                        "reprobe run before starting this one")
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
                refused = None if getattr(runner, "host_only", False) else (
                    _step_target_problem(step.target, rundir) or runner.unsupported(step))
                if refused:
                    results.append(RunResult(runner=runner.id, target=step.target,
                                             status="skipped", executed=False,
                                             diagnostics={"reason": refused}))
                    continue
                image = None if getattr(runner, "host_only", False) else (
                    env_plan.image
                    if env_plan.env_provenance in ("author-specified", "fallback-generic")
                    else self.config.base_image(runner.image_key_for(step)) or env_plan.image)
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
                    self._diagnose(res, llm_client, env_plan, step, logdir, runner.image_key_for(step))
                    if (not dry_run and hasattr(runner, "assisted_rstudio_command")
                            and (res.diagnostics.get("harness_diagnosis") or {}).get("kind") == "rstudio"):
                        res.diagnostics["assisted_retry"] = _rstudio_retry(
                            runner, ctx, logdir / f"step{i:02d}-{runner.id}-assisted.log",
                            allow_egress=allow_egress_runtime)
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
        declared_err = badge_rules.declared_env_error(report.environment)
        report.badges = badge_rules.decide(
            fetch_res, results, detect_res,
            badges_cfg=self.config.badges, functional_requested=functional_requested,
            ran=ran and not dry_run, install_error=install_err, declared_env_error=declared_err)
        report.verdict = badge_rules.verdict(results, ran, install_err, declared_err, dry_run=dry_run)
        report.fix_list = _fix_list(report, results)
        report.not_verified = sorted(
            {x for r in results for x in r.not_verified} | set(report.not_verified))

        # -- (6) RESULTS CHECK vs THE PAPER (advisory) ------------------- #
        if ran and not dry_run and results:
            self._regenerated_outputs(report, results, srcdir, rundir)
        if llm_client is not None and ran and not dry_run and results:
            self._results_check(report, results, srcdir, rundir, manifest_meta,
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
        # Who named a source decides what it may be. The operator's `--data` may
        # point at a local directory or an ssh git remote; a source the AUTHOR
        # wrote into their manifest may not — `data_sources: [{source: ~/.ssh}]`
        # would otherwise copy the chair's own directory into the sandbox, whose
        # install phase has network. Author sources are fetched `untrusted`.
        tagged = [(str(sp), False) for sp in (specs or [])]
        tagged += [(str(sp), True) for sp in declared_data_sources(srcdir)]
        if not tagged:
            return
        from .fetch.data_source import fetch_data_source, merge_into, parse_ref

        records: list[dict[str, Any]] = []
        notes: list[str] = []
        merged: set[str] = self._merged_data_paths
        for i, (spec, untrusted) in enumerate(tagged):
            url, into = parse_ref(spec)
            if not url:
                continue
            rec: dict[str, Any] = {"input": url, "into": into or ".",
                                   "declared_by": "author's manifest" if untrusted else "operator"}
            stage = fresh_dir(work, work / f"data{i:02d}", f"data{i:02d}")
            skipped_links: list[str] = []
            try:
                fr = fetch_data_source(url, stage, untrusted=untrusted)
                copied, collisions = merge_into(stage, srcdir, into, skipped=skipped_links)
            except FetchError as e:
                if isinstance(e, RefusedSource):
                    # A decision, not a failure: the author's manifest named a source
                    # reprobe will not fetch on an author's say-so. The chair can still
                    # supply it deliberately.
                    rec.update(status="refused", error=str(e))
                    records.append(rec)
                    # `e` already names the source and the rule; repeating the URL buried it
                    notes.append(f"a data source named in the artifact's manifest was NOT fetched: {e}. "
                                 "If it is legitimate, supply it yourself with `--data`; until then "
                                 "the artifact was checked without it")
                    continue
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
            if skipped_links:
                rec["skipped_links"] = skipped_links
                notes.append(f"data source '{url}': {len(skipped_links)} symbolic link(s) in the "
                             f"deposit were not copied ({', '.join(skipped_links[:5])}"
                             f"{', …' if len(skipped_links) > 5 else ''})")
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
                first = Path(path.replace("\\", "/")).parts[:1]
                if first and first[0].casefold().startswith(".reprobe_"):
                    # the harness's own scratch names: a planted .reprobe_deps/
                    # sitecustomize.py would sit on the run phase's PYTHONPATH
                    raise FetchError(f"'{path}' is a path reserved for the harness")
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
        #
        # What runs here with network: the package managers, and the build steps of
        # the third-party packages they fetch (sdist setup.py, conda post-link
        # scripts) from the indexes the plan allows. What must NOT: the artifact's
        # own code. The plan therefore never executes install.R, drops `-e .`,
        # git+/URL requirements and custom indexes, runs pip as `python -I` (no
        # cwd on sys.path), and R without the author's .Rprofile — see envbuild.
        relaxed = {**self.config.limits_for("python"),
                   "read_only_rootfs": False, "tmpfs_noexec": False, "tmpfs_size": "4g",
                   # compiling a large source dependency tree can exceed the 30-min
                   # author-code cap; give the harness-controlled install phase (the
                   # artifact's own code is not run here) more wall-clock and build space. An hour
                   # was still short: PerceivedRisk (2026-08) spends the whole of it
                   # inside `micromamba create` — an environment.yml whose pip section
                   # reaches ultralytics, which pulls torch's CUDA wheels, ~8 GB of
                   # mostly small files written to a bind mount. The kill then lands
                   # before the first pip call of the import-scan install, so the step
                   # fails on exactly the import that install exists to supply.
                   "timeout_s": 7200}
        # R_PROFILE_USER/R_ENVIRON_USER: every Rscript here runs in /work, where an
        # author's .Rprofile (renv projects always ship one) would otherwise run —
        # in the one phase that has network egress.
        #
        # HOME is NOT /work here. Everything that reads user config reads it from HOME —
        # ~/.R/Makevars (make runs $(shell …) whenever a source package compiles),
        # ~/.pydistutils.cfg, ~/.condarc, pip's cache — and /work is the artifact's
        # own tree. The outputs go where they are told (/work/.reprobe_deps,
        # /work/.reprobe_Rlib, the conda prefix); only config and caches use HOME.
        # PYTHONPATH is not exported either: every harness Python runs -I, and an
        # installer's own subprocess must not import from /work/.reprobe_deps.
        #
        # pip reads ~/.config/pip/pip.conf, so an author's config could point it at
        # their own index; PIP_CONFIG_FILE=/dev/null is pip's documented way to load
        # no config files at all.
        prep = (f"set -e; mkdir -p /work/.reprobe_deps /work/.reprobe_Rlib {INSTALL_HOME}; "
                f"export HOME={INSTALL_HOME}; export R_LIBS_USER=/work/.reprobe_Rlib; "
                "export R_PROFILE_USER=/dev/null R_ENVIRON_USER=/dev/null R_MAKEVARS_USER=/dev/null; "
                "export PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1; "
                # renv installs into its global cache and SYMLINKS the project
                # library to it. That cache is under this container's HOME (or
                # the --reuse-downloads mount), neither of which the run phase
                # sees: every restored package would be a dangling link there,
                # failing as "there is no package called ..." — read as the
                # artifact's fault. Copies keep the library self-contained.
                "export RENV_CONFIG_CACHE_SYMLINKS=FALSE; "
                + ("" if reuse_downloads else "export RENV_CONFIG_CACHE_ENABLED=FALSE; "))

        # Package downloads land in a container-local HOME, which is gone when the
        # container exits — so re-running one submission re-downloads
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
        cache_roots: list[Path] = []
        if reuse_downloads:
            cache = Path(self.workroot) / ".package-cache"
            for sub in ("pip", "conda", "renv"):
                (cache / sub).mkdir(parents=True, exist_ok=True)
            cache_roots.append(cache.resolve())
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

        origins = list(env_plan.install_origins)
        if len(origins) != len(env_plan.install_commands):
            origins = ["unknown"] * len(env_plan.install_commands)
        by_key: dict[str, list[tuple[int, str, str]]] = {"python": [], "r": []}
        for i, (c, o) in enumerate(zip(env_plan.install_commands, origins, strict=True)):
            by_key["r" if c.strip().startswith("Rscript") else "python"].append((i, c, o))

        install_results: dict[str, Any] = {}
        for key, items in by_key.items():
            if not items:
                continue
            image = (env_plan.image if env_plan.env_provenance == "fallback-generic"
                     else self.config.base_image(key) or env_plan.image)
            spec = ContainerSpec(image=image, command=["bash", "-c", prep + _attributed_script(items)],
                                 workdir="/work",
                                 mounts=[Mount(source=rundir.as_posix(), target="/work", read_only=False),
                                         *cache_mounts],
                                 network="egress")
            log_path = logdir / f"install-{key}.log"
            raw = run_container(spec, relaxed, log_path, allow_egress=True, dry_run=dry_run,
                                work_root=rundir, extra_roots=cache_roots)
            if raw.dry_run:
                install_results[key] = {"ok": True, "dry_run": True, "exit_code": None}
                continue
            marks = _install_failures(log_path, items)
            failures = [f for f in marks if f["origin"] != "harness-record"]
            ok = (not raw.timed_out and not raw.error and not raw.limit_exceeded and not raw.oom_killed
                  and (raw.exit_code == 0 or (bool(marks) and not failures)))
            # Carry WHY it failed, not just that it did: a timeout is the harness
            # running out of clock, a failed command is attributed to whoever asked
            # for it, and the verdict has to be able to say which.
            row: dict[str, Any] = {"exit_code": raw.exit_code, "ok": ok,
                                   "timed_out": bool(raw.timed_out), "failures": failures}
            if raw.error:
                row["error"] = raw.error
            if raw.limit_exceeded:
                row["limit_exceeded"] = raw.limit_exceeded
            if raw.oom_killed:
                row["oom_killed"] = True
            # The import-scan install never FAILS (a mis-detected local module must not
            # fail the phase) — it prints what pip could not get. If its OWN output
            # shows the index was unreachable, that is a failure of the harness's install.
            sections = _install_sections(log_path)
            missing = sorted({n for sec in sections.values() for n in sec["missing"]})
            if missing:
                row["incomplete"] = missing
                if any(sec["missing"] and sec["network"] and sec["failed_exit"] is None
                       for sec in sections.values()):
                    ok = row["ok"] = False
                    failures.append({"index": -1, "origin": "harness-detected", "exit_code": 0,
                                     "network": True, "command": "",
                                     "what": "install of the imports found in the code"})
            # network trouble is a property of a failing command's own output
            if any(f.get("network") for f in failures):
                row["network_error"] = True
            if not ok and not failures and not (raw.timed_out or raw.error or raw.limit_exceeded
                                                or raw.oom_killed or row.get("network_error")):
                # a non-zero exit that no command explains: the container, or its shell,
                # died — a failure of ours, which must not be left looking like "no
                # finding" (and so, downstream, like the artifact's)
                row["unattributed"] = True
            install_results[key] = row
            note = f"install[{key}] ({'ok' if ok else 'failed'}; egress phase)"
            report.environment.setdefault("notes", []).append(note)
            if not ok:
                declared = [f for f in failures if f["origin"] == "author-declared" and not f.get("network")]
                if declared and not (raw.timed_out or raw.error or raw.limit_exceeded or raw.oom_killed):
                    report.not_verified.append(
                        f"the artifact's declared environment ({key}) did not install: "
                        + "; ".join(f"{f.get('what') or f['command'][:100]} exited {f['exit_code']}"
                                    for f in declared)
                        + " — the steps below ran without it")
                else:
                    cause = ("was killed at the install-phase wall-clock limit" if raw.timed_out
                             else f"failed (exit {raw.exit_code})")
                    report.not_verified.append(
                        f"dependency install ({key}) {cause}; the steps below ran against an "
                        "environment the harness never finished building, so their failures are "
                        "ours and not the artifact's")
        report.environment["install_results"] = install_results
        # Hash what the install phase RESOLVED (the pip freeze / installed.packages()
        # / micromamba list sections), never the whole log: the log carries a random
        # container name and the host path, so its hash differed on every run and
        # could not say whether two runs installed the same versions.
        digest = _hash_resolved_versions(logdir)
        if digest:
            env_plan.resolved_deps_digest = digest
            report.environment["resolved_deps_digest"] = digest

    # ------------------------------------------------------------------ #
    def _produced_text(self, results: list[RunResult], rundir: Path) -> str:
        """What the re-run actually reported, as text an LLM can compare against
        the paper: machine-readable outputs first (a declared .csv/.json/.txt is
        the author saying "these are my numbers"), then the console output, which
        for many analyses is where the statistics are actually printed."""
        from .runners.base import _tail, expand_expected

        parts: list[str] = []
        budget = 12_000
        for res in results:
            for rel in expand_expected(res.expected_met or [], res.artifacts or [])[:50]:
                p = _inside(rundir, rel)
                if p is None or p.suffix.lower() not in (".csv", ".tsv", ".json", ".txt", ".md",
                                                         ".yml", ".yaml"):
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
                       rundir: Path, manifest_meta: dict[str, Any], fetch_res: FetchResult,
                       llm_client) -> None:
        """Advisory comparison of the paper's claims against the run's output.

        Never touches a badge: the SYSTEM prompt tells the model it cannot change
        a decision, and `results_reproduced` stays "not-evaluated" whatever comes
        back. This exists so the human confirmer sees the comparison instead of
        having to do it from scratch."""
        from . import paper as paper_mod

        # rundir is the tree THIS run used — fresh_dir may have fallen back to
        # run-1 when Windows locked the old one, and reading work/"run" then
        # compared the paper against the previous run's outputs.
        work = rundir.parent
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
            if counts.get("mismatch") and advice.get("meets_threshold"):
                report.not_verified.append(
                    f"{counts['mismatch']} paper claim(s) look inconsistent with the re-run "
                    "(LLM-advisory — a human must confirm)")
            elif counts.get("mismatch"):
                # Below the configured confidence the comparison is shown in its own
                # section for transparency, but it does not get to put a statement
                # about someone's published numbers into the report's findings.
                section["threshold_note"] = advice.get("threshold_note")
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
        known = known_failure_diagnosis(run_tail, res.exit_code, step.target)
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
        from .runners.base import expand_expected

        art_dir = outdir / "artifacts"
        for res in results:
            keep = set(expand_expected(res.expected_met, res.artifacts)) \
                | {a for a in res.artifacts if a.endswith(".executed.ipynb")}
            for rel in keep:
                # /work is writable by author code, which can plant a link there to
                # any host path (`x.executed.ipynb -> /proc/self/environ`). Only a
                # regular file that really lives in the run tree is collected.
                src = _inside(rundir, rel)
                if src is None or not src.is_file():
                    continue
                dst = art_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(src, dst, follow_symlinks=False)
                except OSError:
                    pass

    def _copy_run_tree(self, srcdir: Path, rundir: Path, report: Report) -> None:
        """Copy the fetched tree into the throwaway run dir — without following links.

        ``copytree`` follows symlinks by default, so a committed ``leak -> ~/.aws``
        copied the host directory INTO the sandbox, and the install phase has
        network. In-tree relative links are kept as links; anything pointing out
        of the tree is dropped and named in the report. Author-supplied
        ``.reprobe_*`` paths go too: they are the harness's own scratch names, and
        a committed ``.reprobe_deps/sitecustomize.py`` would sit on PYTHONPATH."""
        dropped: list[str] = []
        root = srcdir.resolve()

        def ignore(d: str, names: list[str]) -> set[str]:
            out = set()
            for n in names:
                p = Path(d) / n
                if Path(d).resolve() == root and n.casefold().startswith(".reprobe_"):
                    out.add(n)
                    dropped.append(f"{n} (reserved harness path)")
                elif p.is_symlink():
                    try:
                        target = p.resolve()
                        inside = target.is_relative_to(root) and not os.path.isabs(os.readlink(p))
                    except (OSError, RuntimeError):
                        inside = False
                    if not inside:
                        out.add(n)
                        dropped.append(f"{p.relative_to(srcdir).as_posix()} (link out of the tree)")
            return out

        try:
            shutil.copytree(srcdir, rundir, symlinks=True, ignore=ignore)
        except shutil.Error as e:
            # e.g. Windows without symlink privilege: the in-tree links that could
            # not be recreated are named, everything else was copied.
            for src, _dst, why in (e.args[0] if e.args and isinstance(e.args[0], list) else []):
                dropped.append(f"{Path(src).name} (not copied: {why})")
        if dropped:
            report.environment.setdefault("warnings", []).append(
                f"{len(dropped)} path(s) in the artifact were not copied into the run: "
                + ", ".join(dropped[:10]) + (" …" if len(dropped) > 10 else ""))

    def _regenerated_outputs(self, report: Report, results: list[RunResult], srcdir: Path,
                             rundir: Path) -> None:
        """Compare every committed file this run regenerated against the committed
        copy — the most direct reproduction evidence an automated run can give.
        Evidence for the human reviewer only: it never sets a badge."""
        from . import compare

        # Only a step that FINISHED can have regenerated a file. A crashed or
        # timed-out step that truncated out.csv left a half-written file, and
        # comparing it reported "regenerated with different values".
        regenerated = sorted({a for r in results for a in r.artifacts
                              if r.executed and r.status in ("pass", "partial")
                              and (srcdir / a).is_file()})
        if not regenerated:
            return
        cfg = ((self.config.badges.get("acm") or {}).get("results_reproduced") or {})
        rows = compare.compare_outputs(
            srcdir, rundir, regenerated,
            rtol=float(cfg.get("rtol", 1e-6)), atol=float(cfg.get("atol", 1e-12)),
            max_bytes=int(cfg.get("max_bytes", 50_000_000)), max_files=int(cfg.get("max_files", 200)))
        summary = compare.summarize(rows)
        report.results_comparison = {"summary": summary, "files": rows,
                                     "rtol": cfg.get("rtol", 1e-6), "atol": cfg.get("atol", 1e-12)}
        n_diff = sum(1 for r in rows if r.get("status") == "differs")
        if n_diff:
            report.not_verified.append(
                f"{n_diff} committed output(s) were regenerated with different values — see "
                "results_comparison; nondeterminism (unseeded RNG, library versions) is one "
                "possible cause, so a human must judge")

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
def _special_files(dirpath: str, names: list[str]) -> set[str]:
    """copytree ignore hook: drop everything that is not a regular file, a
    directory or a symlink (FIFOs, sockets, device nodes)."""
    out = set()
    for n in names:
        try:
            mode = os.lstat(os.path.join(dirpath, n)).st_mode
        except OSError:
            out.add(n)
            continue
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
            out.add(n)
    return out


#: The retry copies the whole run directory; past this it is not attempted.
_RETRY_COPY_CAP = 2 * 1024 ** 3


def _rstudio_retry(runner, ctx: RunContext, log_path: Path, *, allow_egress: bool) -> dict[str, Any]:
    """Re-run an R script that failed only because no RStudio IDE was running,
    with the IDE's answer supplied, and say what happened.

    Supplementary evidence, never the verdict: the verdict is about the deposit
    as submitted, and that does not run without RStudio. What this adds is
    whether the IDE call was the only thing in the way — the difference between
    a one-line fix and a script that would fail next on something else. It runs
    in a scratch copy of the run directory, so no later step sees its outputs."""
    from .fetch.base import _dir_bytes_over
    from .runners.base import _tail
    change = ("rstudioapi's document-context calls answered with the script's own path, as "
              "RStudio would have; the script itself unchanged")
    out: dict[str, Any] = {"counted_in_verdict": False, "change": change}
    if _dir_bytes_over(ctx.rundir, _RETRY_COPY_CAP):
        out["status"] = "not-attempted"
        out["summary"] = ("Harness-assisted retry not attempted: the run directory is over "
                          f"{_RETRY_COPY_CAP // 1024 ** 3} GiB to copy.")
        return out
    scratch = ctx.rundir.parent / f"{ctx.rundir.name}-assisted"
    shutil.rmtree(scratch, ignore_errors=True)
    try:
        # Author code has just had write access to the run dir: it can leave a
        # FIFO or device node there, which copytree would block on or fail over.
        # Only regular files, directories and links are copied.
        try:
            shutil.copytree(ctx.rundir, scratch, symlinks=True, ignore=_special_files)
        except (shutil.Error, OSError) as e:
            out["status"] = "not-attempted"
            out["summary"] = ("Harness-assisted retry not attempted: the run directory could not be "
                              f"copied ({type(e).__name__}).")
            return out
        rctx = replace(ctx, rundir=scratch, pre_index=snapshot(scratch))
        spec = runner.container_spec(rctx).model_copy(
            update={"command": runner.assisted_rstudio_command(rctx)})
        raw = run_container(spec, ctx.limits, log_path, allow_egress=allow_egress, work_root=scratch)
        rr = runner.interpret(raw, rctx)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    out.update(status=rr.status, exit_code=rr.exit_code, duration_s=rr.duration_s,
               expected_met=rr.expected_met, log_path=str(log_path))
    head = "Harness-assisted retry (not counted in the verdict), with " + change + ": "
    if rr.status in ("pass", "partial"):
        made = f", producing {len(rr.expected_met)} declared output(s)" if rr.expected_met else ""
        out["summary"] = (head + f"the script ran to completion (exit 0){made}. The IDE call was "
                          "the only thing found in the way of running it with Rscript.")
    elif rr.status in ("timeout", "resource-limit"):
        out["summary"] = head + ("it got past the IDE call and then ran out of the sandbox's "
                                 "budget, so whether anything else is in the way is unknown.")
    else:
        tail = _tail(str(log_path))
        nxt = known_failure_diagnosis(tail, rr.exit_code, ctx.step.target)
        if nxt and nxt.get("kind") != "rstudio":
            out["next_diagnosis"] = nxt
            why = str(nxt.get("likely_cause", "")).split(". ")[0].rstrip(".")
        else:
            lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
            why = f"it ended with `{lines[-1][:200]}`" if lines else "no output"
        out["summary"] = (head + f"it still failed (exit {rr.exit_code}) — {why}. The IDE call is "
                          "not the only problem.")
    return out


def _step_target_problem(target: str, rundir: Path) -> Optional[str]:
    """Why a step's target must not be run, or None.

    Targets from an author manifest are verbatim strings. One that names a file
    outside the deposit (`/usr/lib/python3/this.py`, `../x.py`, a symlink out)
    would run code the deposit does not contain — and pass, crediting the
    artifact with a run of someone else's file. One that does not exist fails
    with a console error that reads like the artifact's own. A leading `-` is
    read by the interpreter as an option (`-mhttp.server`), not a file."""
    t = target.strip()
    if not t:
        return "the step names no file"
    if t.startswith("-"):
        return f"`{t}` would be read as an interpreter option, not a file"
    root = rundir.resolve()
    if Path(t).is_absolute() or Path(t).drive:
        return f"`{t}` is an absolute path; a step must name a file inside the deposit"
    p = (rundir / t).resolve()
    if not p.is_relative_to(root):
        return f"`{t}` resolves outside the deposit; refusing to run it"
    if not p.is_file():
        return (f"`{t}` does not exist in the deposit (check the step's path in the "
                "manifest; paths are relative to the repository root)")
    return None


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

#: R's error for a function whose package is installed but not attached. The
#: single most common failure across the benchmark artifacts (7 of them), and the
#: one most precisely fixable: the package IS present — the install phase put it
#: there — so the only missing thing is a `library()` line.
_NO_FUNCTION_RE = re.compile(r'could not find function "(?P<fn>[^"]+)"')

#: Curated, not inferred. Every entry is an unambiguous export of one package in
#: the families research R actually uses. A wrong entry would send an author to
#: add the wrong line, so anything shared between packages is left out and falls
#: through to the generic message below.
_R_FUNCTION_OWNER = {
    # tidyr — the reshape verbs, repeatedly used without attaching tidyr
    "gather": "tidyr", "spread": "tidyr", "pivot_longer": "tidyr",
    "pivot_wider": "tidyr", "drop_na": "tidyr", "replace_na": "tidyr",
    "separate": "tidyr", "unite": "tidyr",
    # easystats family — each function belongs to exactly one member package
    "report": "report", "report_table": "report", "report_text": "report",
    "bayesfactor_models": "bayestestR", "describe_posterior": "bayestestR",
    "bayesfactor": "bayestestR", "rope": "bayestestR", "hdi": "bayestestR",
    "model_parameters": "parameters", "standardize_parameters": "parameters",
    "model_performance": "performance", "check_model": "performance",
    "cohens_d": "effectsize", "eta_squared": "effectsize", "hedges_g": "effectsize",
    "estimate_means": "modelbased", "estimate_contrasts": "modelbased",
    # other staples of this corpus
    "art": "ARTool", "emmeans": "emmeans", "lmer": "lme4", "glmer": "lme4",
    "read_xlsx": "readxl", "read_excel": "readxl", "geom_signif": "ggsignif",
}

#: Offline by design: the analysis container runs with --network none, so any
#: runtime fetch fails here. That is the point — an artifact that downloads its
#: own code or data when it runs is not self-contained, and what it downloads can
#: change after publication.
_NO_NETWORK_RE = re.compile(
    r"Could not resolve host(?:name)?|Failed to connect to [^\s]+ port|"
    r"Temporary failure in name resolution|nodename nor servname provided", re.I)
_URL_IN_LOG_RE = re.compile(r"https?://[^\s\"'\)\]]+")

#: A directory the script writes into that git never carried: git does not track
#: empty directories, so `plots/` exists for the author and for nobody else.
#: ggsave's message is unambiguous: it is only ever about an output directory.
_NO_DIR_RE = re.compile(r"Cannot find directory '(?P<dir>[^']+)'")
#: R prints ONE message for every connection it cannot open — a read, a write, a
#: missing script — so the message alone says nothing about which. The mode in the
#: accompanying `In file(file, "rt")` line does: r* is a read, w*/a* a write.
_R_OPEN_RE = re.compile(r"cannot open file '(?P<path>[^']+)': No such file or directory")
_R_OPEN_MODE_RE = re.compile(r"""\bfile\([^,()]*,\s*["'](?P<mode>[rwa])[bt+]?["']""")
#: Rscript itself, when the step's script is not there at all.
_R_NO_SCRIPT_RE = re.compile(r"Fatal error: cannot open file '(?P<path>[^']+)'")
_R_SETWD_RE = re.compile(r"cannot change working directory")


def _missing_output_dir(missing: str) -> dict[str, Any]:
    return {
        "source": _DETERMINISTIC,
        "likely_cause": (
            f"the script writes into `{missing}`, which does not exist in a fresh clone. "
            "git does not track empty directories, so a folder that exists on the "
            "author's machine is simply absent for everyone else — including every "
            "reviewer, and this harness."),
        "suggested_fixes": [
            f'create it in the script: `dir.create("{missing}", recursive = TRUE, showWarnings = FALSE)` '
            "at the top, or `ggsave(..., create.dir = TRUE)` per call",
            f'or commit `{missing}/.gitkeep` so a clone carries the directory',
        ],
    }


def _rstudio_fixes(target: str) -> list[str]:
    """The idiom sets the working directory to the SCRIPT's directory. The harness
    runs every script from the repository root, so dropping the lines is the right
    advice only for a script that sits at the root."""
    folder = target.replace("\\", "/").rsplit("/", 1)[0] if "/" in target.replace("\\", "/") else ""
    if not folder:
        return ["drop the two rstudioapi lines: the harness runs the script from the repository "
                "root, which is the script's own directory here — exactly what they were setting",
                "if a path anchor is genuinely needed, `here::here(...)` resolves without an IDE"]
    return [f"replace them with `setwd(here::here(\"{folder}\"))`: the idiom set the working "
            f"directory to the script's own folder (`{folder}`), and without an IDE the script "
            "is run from the repository root instead",
            f"or rewrite the script's paths relative to the repository root (prefix them with "
            f"`{folder}/`), which also makes it runnable with plain `Rscript`"]


def known_failure_diagnosis(log_tail: str, exit_code: Optional[int],
                            target: str = "") -> Optional[dict[str, Any]]:
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
            "kind": "rstudio",
            "likely_cause": (
                "the script calls rstudioapi (typically "
                "`setwd(dirname(getActiveDocumentContext()$path))`, to set the working "
                "directory to the script's own location), which asks a RUNNING RStudio IDE "
                "which file is open in its editor. The package itself installed fine — what "
                "is missing is the IDE, and a headless container cannot provide one. The "
                "artifact runs for its author because the author runs it inside RStudio; it "
                "does not run for anyone using `Rscript`."),
            "suggested_fixes": _rstudio_fixes(target),
        }

    if (m := _NO_FUNCTION_RE.search(log_tail)):
        fn = m.group("fn")
        owner = _R_FUNCTION_OWNER.get(fn)
        where = (f"`{fn}()` is exported by **{owner}**" if owner
                 else f"`{fn}()` belongs to a package that is installed but not attached")
        fix = (f"add `library({owner})` near the other library() calls" if owner
               else f"add the `library()` call for whichever package exports `{fn}()`, "
                    "or namespace the call as `pkg::" + fn + "()`")
        return {
            "source": _DETERMINISTIC,
            "likely_cause": (
                f"{where}, and the script never attaches it. The package is present — the "
                "install phase put it there, which is why nothing failed earlier — so this is "
                "not a missing dependency. It works in the author's session because something "
                "else attached the package first; in a clean one, nothing has."),
            "suggested_fixes": [fix,
                                "the same file may use other functions from that package; "
                                "attaching it once at the top fixes them all"],
        }

    if _NO_NETWORK_RE.search(log_tail):
        urls = _URL_IN_LOG_RE.findall(log_tail)
        what = f" (it tried to reach {urls[0]})" if urls else ""
        return {
            "source": _DETERMINISTIC,
            "likely_cause": (
                f"the artifact fetches something over the network while it runs{what}. The "
                "analysis container is offline by design (`--network none`), so the fetch "
                "fails here — but the finding is not the failed request. An artifact that "
                "downloads its own code or data at run time is not self-contained: what it "
                "downloads can change after publication, and if that URL moves or goes "
                "private the artifact stops working permanently. The deposit does not "
                "contain what the results depend on."),
            "suggested_fixes": [
                "vendor it: commit the fetched file into the repository and load it locally, "
                "which also pins the exact version that produced the published numbers",
                "if it must stay remote, depend on an immutable reference (a tag or commit, "
                "never a moving branch) — but a vendored copy survives the source disappearing",
            ],
        }

    if (m := _R_NO_SCRIPT_RE.search(log_tail)):
        return {
            "source": _DETERMINISTIC,
            "likely_cause": (f"the step's script `{m.group('path')}` does not exist in the deposit, so "
                             "R could not start it. Nothing of the analysis ran."),
            "suggested_fixes": ["correct the step path in the manifest (it is relative to the "
                                "repository root), or commit the missing script"],
        }

    if _R_SETWD_RE.search(log_tail):
        return {
            "source": _DETERMINISTIC,
            "likely_cause": (
                "the script calls `setwd()` with a directory that does not exist here — typically "
                "an absolute path on the author's machine (`C:/Users/…`, `~/Dropbox/…`). Every "
                "relative path after it was written against that directory."),
            "suggested_fixes": [
                "delete the setwd() call and write paths relative to the repository root, which "
                "is the working directory the script is run from",
                "or anchor them with `here::here(...)`, which needs no machine-specific path",
            ],
        }

    if (m := _R_OPEN_RE.search(log_tail)):
        mode = _R_OPEN_MODE_RE.search(log_tail)
        path = m.group("path")
        if mode and mode.group("mode") == "r":
            # a READ of a file that is not here: two causes, and both are said
            return {
                "source": _DETERMINISTIC,
                "likely_cause": (
                    f"the script reads `{path}`, which is not in the run directory (the "
                    "repository root). Either the file is not in the deposit — data kept in a "
                    "separate repository, or never committed — or the path is written relative "
                    "to a different working directory than the repository root."),
                "suggested_fixes": [
                    f"if `{path}` lives in a data deposit (OSF, Zenodo, …), declare it with "
                    "`data_sources:` in the manifest (or re-run with `--data <deposit>`)",
                    "if it is in the repository under another directory, make the path relative "
                    "to the repository root",
                ],
            }
        if mode and mode.group("mode") in ("w", "a"):
            missing = path.rsplit("/", 1)[0] if "/" in path else path
            return _missing_output_dir(missing)
        # no mode line: which of the three it was is unknown — say nothing as fact

    if (m := _NO_DIR_RE.search(log_tail)):
        return _missing_output_dir(m.group("dir"))

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


def _fix_list(report: Report, results: list[RunResult]) -> list[dict[str, Any]]:
    """Every concrete change the author could make, in one ordered list.

    The verdict answers "did it run". This answers "what do I change", and that
    is the part an author acts on — but until now it was scattered one diagnosis
    per step, several screens apart, in a report whose failures often share a
    single root cause. A reader had to assemble it themselves.

    Deterministic findings come first: they are facts about the run, not guesses.
    The advisory model's suggestions follow, labelled, because a chair should be
    able to tell which is which at a glance."""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(where: str, why: str, fixes: list[str], source: str) -> None:
        key = f"{where}|{why[:80]}"
        if key in seen or not fixes:
            return
        seen.add(key)
        items.append({"where": where, "why": why, "fixes": fixes, "source": source})

    # Environment findings read off the files themselves — these need no run at
    # all, so they are the cheapest things to fix and come first.
    for w in (report.environment.get("warnings") or []):
        if "requirements.txt line" in str(w):
            add("requirements.txt", str(w),
                ["delete that line — the file installs nothing until it is gone"], "deterministic")

    for res in results:
        d = res.diagnostics if isinstance(res.diagnostics, dict) else {}
        known = d.get("harness_diagnosis")
        if isinstance(known, dict) and known.get("suggested_fixes"):
            add(res.target, str(known.get("likely_cause", "")),
                [str(f) for f in known["suggested_fixes"]], "deterministic")
        adv = d.get("llm_advisory")
        # Below the configured confidence, advice stays visible in its step's own
        # section but is never promoted into the author's to-do list.
        if isinstance(adv, dict) and adv.get("suggested_fixes") and adv.get("meets_threshold", True):
            add(res.target, str(adv.get("likely_cause", "")),
                [str(f) for f in adv["suggested_fixes"]], "llm-advisory")
    return items


def _no_log_diagnosis(res: RunResult) -> dict[str, Any]:
    """Deterministic stand-in for the LLM diagnoser when a step left no log to
    read. States the one thing that is actually known and points at the next
    concrete action — no cause is invented."""
    if res.status == "timeout":
        limit = res.diagnostics.get("timeout_s")
        ceiling = res.diagnostics.get("max_timeout_s") or 21600
        suggest = min(int(limit or 1800) * 2, int(ceiling))
        return {
            "source": "harness (deterministic — not an LLM guess)",
            "likely_cause": (
                f"the step was killed at the {limit}s budget without writing any console output, "
                "so how far it got is unknown. A notebook that logs nothing usually means the "
                "kernel never reached a printing cell — a long fit/search, a blocking prompt, "
                "or a network call that hangs under --network none."),
            "suggested_fixes": [
                f"re-run this step with a larger budget: `reprobe run <ref> --timeout {suggest}` "
                f"(the ceiling is limits.yaml:max_timeout_s = {ceiling}s)",
                "inspect the partially-executed notebook collected under report/artifacts/ — "
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


_MARK = "[reprobe-install]"
_MARK_RE = re.compile(r"^\[reprobe-install\] (begin|failed) (\d+) (\S+)(?: exit (\d+))?\s*$", re.M)


def _attributed_script(items: list[tuple[int, str, str]]) -> str:
    """The install commands as one bash script that records, per command, who
    asked for it and whether it failed.

    One exit code for a whole ``set -e`` chain cannot say whether the artifact's
    own requirements.txt failed (a finding about the artifact) or a package
    reprobe guessed from an import did (ours). Each command runs in its own
    subshell, so one failure no longer stops the rest — the steps then run with
    as much of the environment as could be built, and the log says what was not."""
    lines = ["__rc=0"]
    for i, cmd, origin in items:
        # `echo;` first: a command whose last output has no newline would otherwise
        # glue the marker onto that line, and the line-anchored parser would miss it
        lines.append(f"echo; echo '{_MARK} begin {i} {origin}'")
        # A fresh `bash -ec`, not `( set -e; … )`: bash IGNORES errexit inside a subshell
        # that is part of an `||` list, so `false; echo after` carried on past its
        # first failure. A new process honours -e whatever list its parent is in.
        lines.append(f"bash -ec {shlex.quote(cmd)} || {{ __e=$?; echo; "
                     f"echo \"{_MARK} failed {i} {origin} exit $__e\"; "
                     "[ $__rc -ne 0 ] || __rc=$__e; }")
    lines.append("exit $__rc")
    return "\n".join(lines)


def _log_lines(path: Path, max_line: int = 65_536):
    """Lines of a log in constant memory. The install log is the container's
    uncapped-by-line stdout (the byte cap is 1 GiB), written by third-party package
    builds; reading it whole — as the parsers once did, up to four times — let a
    hostile build make the host allocate gigabytes. A line longer than ``max_line``
    comes back in pieces."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            while True:
                line = fh.readline(max_line)
                if not line:
                    return
                yield line.rstrip("\r\n")
    except OSError:
        return


def _install_sections(log_path: Path) -> dict[int, dict[str, Any]]:
    """What each install command's OWN output says, keyed by command index:
    ``{"origin", "failed_exit" (None if it did not fail), "network", "missing"}``.

    Per command, not per log: a pip retry warning printed by one command used to
    mark the whole group as "network trouble", which excused a different
    command's genuinely broken requirements.txt. The network markers are read
    from the last lines of the failing command's own section only. Streams the
    log in constant memory."""
    import ast
    from collections import deque

    sections: dict[int, dict[str, Any]] = {}
    cur: Optional[dict[str, Any]] = None
    tail: deque[str] = deque(maxlen=200)       # lines cut to 512 chars: bounded memory

    def network_in_tail() -> bool:
        text = "\n".join(tail).lower()
        return any(mk in text for mk in badge_rules._NETWORK_MARKERS)

    def close_section() -> None:
        # the import install REPORTS instead of failing: judge its network trouble
        # from the end of its own section
        if cur is not None and cur["missing"] and cur["failed_exit"] is None:
            cur["network"] = network_in_tail()

    for line in _log_lines(log_path):
        m = _MARK_RE.match(line)
        if m:
            kind, idx, origin = m.group(1), int(m.group(2)), m.group(3)
            if kind == "begin":
                close_section()
                cur = sections.setdefault(idx, {"origin": origin, "failed_exit": None,
                                                "network": False, "missing": []})
                tail.clear()
            elif kind == "failed":
                sec = sections.setdefault(idx, {"origin": origin, "failed_exit": None,
                                                "network": False, "missing": []})
                sec["failed_exit"] = int(m.group(4) or 0)
                sec["network"] = network_in_tail()
                cur = None
            continue
        if cur is None:
            continue
        tail.append(line[:512])
        mm = _DETECTED_MISSING_RE.match(line)
        if mm:
            listed = mm.group(1).strip()
            names: Any = []
            if listed and listed != "none":
                try:
                    names = ast.literal_eval(listed)
                except (ValueError, SyntaxError):
                    names = []
            cur["missing"] = [n for n in names if isinstance(n, str)
                              and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\[\],-]*", n)][:50] \
                if isinstance(names, list) else []
    close_section()
    return sections


def _install_failures(log_path: Path, items: list[tuple[int, str, str]]) -> list[dict[str, Any]]:
    cmd_by_index = {i: c for i, c, _ in items}
    out = []
    for idx, sec in sorted(_install_sections(log_path).items()):
        if sec["failed_exit"] is None:
            continue
        cmd = cmd_by_index.get(idx, "")
        out.append({"index": idx, "origin": sec["origin"], "exit_code": sec["failed_exit"],
                    "network": sec["network"],
                    "command": cmd, "what": describe_install_command(cmd) if cmd else ""})
    return out


_DETECTED_MISSING_RE = re.compile(r"^reprobe: pip could NOT install \(reported, not faked\): (.*)$")


def _hash_resolved_versions(logdir: Path) -> Optional[str]:
    """sha256 over the version listings the install phase printed (the
    ``harness-record`` sections), normalised to LF — the part of the log that
    names what was installed, and nothing that changes from run to run."""
    h = hashlib.sha256()
    found = False
    for log in sorted(logdir.glob("install-*.log")):
        section = None                  # sha256 of the listing being read, or None
        idx = -1
        for line in _log_lines(log):
            m = _MARK_RE.match(line)
            if m:
                kind, i, origin = m.group(1), int(m.group(2)), m.group(3)
                if section is not None:
                    if kind == "failed" and i == idx:
                        # the listing itself FAILED: what it printed is an error message,
                        # and hashing it would call that "the resolved versions"
                        section = None
                        continue
                    h.update(section.digest())
                    found = True
                    section = None
                if kind == "begin" and origin == "harness-record":
                    section, idx = hashlib.sha256(), i
                continue
            if section is not None:
                section.update(line.rstrip().encode("utf-8", "replace") + b"\n")
        if section is not None:             # the log ended inside a listing
            h.update(section.digest())
            found = True
    return f"sha256:{h.hexdigest()[:32]}" if found else None


def _inside(root: Path, rel: str) -> Optional[Path]:
    """root/rel if it is a real (non-link) path inside root, else None."""
    p = root / rel
    try:
        if p.is_symlink() or not p.resolve().is_relative_to(root.resolve()):
            return None
    except (OSError, RuntimeError):
        return None
    return p


def _unity_section(res: RunResult) -> dict[str, Any]:
    d = dict(res.diagnostics)
    d["tier_reached"] = res.tier_reached
    d["status"] = res.status
    d["not_verified"] = res.not_verified
    return d
