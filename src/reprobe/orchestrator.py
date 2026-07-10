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
from .docker_exec import image_digest as _image_digest, run_container
from .envbuild import plan as plan_env
from .fetch import FetchError, configure as configure_fetchers, fetch as fetch_ref
from .llm import from_config as llm_from_config, roles as llm_roles
from .models import (
    ContainerSpec,
    DetectResult,
    EnvPlan,
    FetchResult,
    Mount,
    RawRunOutput,
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
    return s[-28:] or "submission"


def submission_id(ref: str) -> str:
    return f"{_slug(ref)}-{hashlib.sha1(ref.encode()).hexdigest()[:6]}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        install: bool = True,
        dry_run: bool = False,
        sid: Optional[str] = None,
    ) -> Report:
        sid = sid or submission_id(ref)
        work = self.workroot / sid
        srcdir = work / "src"
        rundir = work / "run"
        outdir = work / "out"
        logdir = work / "logs"
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
            fetch_res = fetch_ref(ref, srcdir)
        except FetchError as e:
            report.source = {"input": ref, "error": str(e)}
            report.verdict = {"overall": "fetch-failed", "human_review_required": True}
            self._write(outdir, report)
            return report
        report.source = _source_section(fetch_res)

        # -- (2) DETECT ------------------------------------------------- #
        detect_res, manifest_meta = detect_artifacts(srcdir, use_llm=use_llm, llm_client=llm_client)
        report.detect = {
            "artifact_types": detect_res.artifact_types,
            "manifest": detect_res.manifest_path,
            "run_plan_source": detect_res.run_plan_source,
            "llm_confidence": detect_res.llm_confidence,
            "flags": detect_res.flags,
            "notes": detect_res.notes + self._runner_load_errors,
            "steps": [s.target for s in detect_res.steps],
        }

        # -- (3) PLAN ENV ----------------------------------------------- #
        env_plan = plan_env(detect_res, manifest_meta, self.config, srcdir,
                            allow_repo2docker=allow_repo2docker)
        report.environment = _env_section(env_plan)

        functional_requested = functional and manifest_wants_functional(manifest_meta, functional)

        # -- (4) RUN (sandboxed) ---------------------------------------- #
        results: list[RunResult] = []
        unity_section = None
        ran = False
        if do_run and detect_res.steps:
            ran = True
            rundir = self._fresh_rundir(work, rundir)
            shutil.copytree(srcdir, rundir)
            self._install_phase(env_plan, rundir, logdir, install=install, dry_run=dry_run, report=report)

            allow_egress_runtime = bool(allow_net)
            if allow_egress_runtime:
                report.environment.setdefault("notes", []).append(
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
                ctx = RunContext(step=step, rundir=rundir, src_dir=srcdir, out_dir=outdir,
                                 image=image, config=self.config,
                                 limits=self.config.limits_for(runner.id),
                                 pre_index=snapshot(rundir))
                spec = runner.container_spec(ctx)
                if spec is None:                       # host-only runner (Unity T0)
                    res = runner.interpret(None, ctx)
                    res.executed = False               # authoritative: no author code ran
                    if runner.id == "unity":
                        unity_section = _unity_section(res)
                else:
                    if allow_egress_runtime:
                        spec = spec.model_copy(update={"network": "egress"})
                    log_path = logdir / f"step{i:02d}-{runner.id}.log"
                    raw = run_container(spec, ctx.limits, log_path,
                                        allow_egress=allow_egress_runtime, dry_run=dry_run,
                                        work_root=rundir)
                    res = runner.interpret(raw, ctx)
                    self._diagnose(res, llm_client, env_plan, step, logdir, runner.image_key)
                results.append(res)
            self._collect_artifacts(results, rundir, outdir)
            # Pin the exact image bytes that ran (pins.yaml carries a mutable tag).
            # Resolve from the real daemon; None if absent/dry-run — never faked.
            if not dry_run and any(r.executed for r in results):
                img = report.environment.get("image")
                digest = _image_digest(img) if img else None
                if digest:
                    env_plan.base_image_digest = digest
                    report.environment["base_image_digest"] = digest
        report.steps = results
        report.unity = unity_section

        # -- (5) BADGES + VERDICT --------------------------------------- #
        report.badges = badge_rules.decide(
            fetch_res, results, detect_res,
            badges_cfg=self.config.badges, functional_requested=functional_requested, ran=ran)
        report.verdict = badge_rules.verdict(results, ran)
        report.not_verified = sorted(
            {x for r in results for x in r.not_verified} | set(report.not_verified))

        # -- (6) LLM SUMMARY (advisory) --------------------------------- #
        if llm_client is not None:
            summary = llm_roles.summarize(llm_client, report.model_dump(mode="json"))
            if summary:
                report.llm["summary"] = summary

        self._write(outdir, report)
        return report

    # ------------------------------------------------------------------ #
    def _install_phase(self, env_plan: EnvPlan, rundir: Path, logdir: Path, *,
                       install: bool, dry_run: bool, report: Report) -> None:
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
                   "read_only_rootfs": False, "tmpfs_noexec": False, "tmpfs_size": "2g"}
        prep = ("set -e; mkdir -p /work/.reprobe_deps /work/.reprobe_Rlib; export HOME=/work; "
                "export R_LIBS_USER=/work/.reprobe_Rlib; export PYTHONPATH=/work/.reprobe_deps:$PYTHONPATH; ")

        install_results: dict[str, Any] = {}
        for key, cmds in groups.items():
            if not cmds:
                continue
            image = (env_plan.image if env_plan.env_provenance == "fallback-generic"
                     else self.config.base_image(key) or env_plan.image)
            spec = ContainerSpec(image=image, command=["bash", "-c", prep + " ; ".join(cmds)],
                                 workdir="/work",
                                 mounts=[Mount(source=rundir.as_posix(), target="/work", read_only=False)],
                                 network="egress")
            log_path = logdir / f"install-{key}.log"
            raw = run_container(spec, relaxed, log_path, allow_egress=True, dry_run=dry_run,
                                work_root=rundir)
            ok = raw.exit_code == 0
            install_results[key] = {"exit_code": raw.exit_code, "ok": ok}
            note = f"install[{key}] ({'ok' if ok else 'failed/skipped'}; egress phase)"
            report.environment.setdefault("notes", []).append(note)
            if not ok:
                report.not_verified.append(
                    f"dependency install ({key}) failed or was skipped; step failures may be "
                    "environmental rather than the artifact's fault")
        report.environment["install_results"] = install_results
        # Hash the install logs (they end with pip freeze / installed.packages()) so
        # two runs of the same deposit are comparable without re-solving.
        digest = _hash_install_logs(logdir)
        if digest:
            env_plan.resolved_deps_digest = digest
            report.environment["resolved_deps_digest"] = digest

    def _diagnose(self, res: RunResult, llm_client, env_plan: EnvPlan, step,
                  logdir: Path | None = None, image_key: str | None = None) -> None:
        if res.status in ("pass", "skipped") or llm_client is None:
            return
        run_tail = str(res.diagnostics.get("log_tail", ""))
        # If a step failed and the dependency-install phase logged an error, feed
        # that in too — the real root cause (e.g. a version constraint) usually
        # lives in the install log, not the run log.
        log_tail = run_tail
        if logdir and image_key:
            ilog = logdir / f"install-{image_key}.log"
            if ilog.exists():
                from .runners.base import _tail
                itail = _tail(str(ilog), 25)
                if "error" in itail.lower():
                    log_tail = f"[dependency-install log]\n{itail}\n\n[run log]\n{run_tail}"
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

    @staticmethod
    def _fresh_rundir(work: Path, rundir: Path) -> Path:
        """Remove the previous run copy; if Windows file locks defeat rmtree,
        fall back to a fresh uniquely-named dir instead of crashing mid-batch."""
        if rundir.exists():
            shutil.rmtree(rundir, ignore_errors=True)
        if rundir.exists():
            for n in range(1, 100):
                cand = work / f"run-{n}"
                if not cand.exists():
                    return cand
        return rundir

    def _write(self, outdir: Path, report: Report) -> None:
        (outdir / "report.json").write_text(
            json.dumps(report.model_dump(mode="json"), indent=2), encoding="utf-8")
        (outdir / "report.md").write_text(md_report.render(report), encoding="utf-8")
        (outdir / "report.html").write_text(html_report.render(report), encoding="utf-8")


# ---------------------------------------------------------------------- #
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


def _env_section(p: EnvPlan) -> dict[str, Any]:
    return {
        "strategy": p.strategy, "image": p.image, "env_provenance": p.env_provenance,
        "install_commands": p.install_commands, "repo2docker_version": p.repo2docker_version,
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
