"""reprobe command-line interface.

  reprobe run <url>          fetch, detect, run (sandboxed), report
  reprobe detect <url|path>  detection only (no execution)
  reprobe batch <csv>        run a whole review season -> dashboard
  reprobe doctor             self-check the harness + config
  reprobe version
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

# Windows consoles default to cp1252 and crash on non-Latin-1 glyphs when output
# is redirected. Force UTF-8 so reports and tables print/redirect cleanly.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from . import __version__
from .config import installed_non_editably, load_config
from .docker_exec import docker_available, image_present, pull_image, run_container
from .models import ContainerSpec
from .orchestrator import Orchestrator, fresh_dir, submission_id

app = typer.Typer(add_completion=False, help="Reusable artifact-reproducibility harness for AutoUI open data.")
console = Console()


def _orch(workroot: str, config_dir: Optional[str]) -> Orchestrator:
    return Orchestrator(config=load_config(config_dir), workroot=workroot)


@app.command()
def run(
    ref: str = typer.Argument(..., help="git URL, Zenodo DOI/record, anonymous.4open.science link, or local path"),
    workroot: str = typer.Option("work", help="where per-submission work dirs are created"),
    config_dir: Optional[str] = typer.Option(None, help="override config/ directory"),
    no_run: bool = typer.Option(False, "--no-run", help="fetch + detect + Available badge only; do not execute code"),
    no_llm: bool = typer.Option(False, "--no-llm", help="disable the local LLM (fully deterministic)"),
    no_functional: bool = typer.Option(False, "--no-functional", help="do not evaluate the Functional candidate"),
    no_install: bool = typer.Option(False, "--no-install", help="skip the dependency-install phase"),
    allow_repo2docker: bool = typer.Option(False, "--allow-repo2docker", help="permit repo2docker fallback build"),
    allow_net: Optional[list[str]] = typer.Option(None, "--allow-net", help="permit runtime egress (downgrades badge confidence)"),
    allow_lfs: bool = typer.Option(False, "--allow-lfs", help="pull git-lfs data during fetch (hardened, off by default)"),
    data: Optional[list[str]] = typer.Option(
        None, "--data", metavar="URL[::SUBDIR]",
        help="extra data deposit merged into the artifact tree (repeatable) — for artifacts "
             "whose code is in git and data on OSF/Zenodo/Dryad/figshare/Dataverse. Append "
             "'::subdir' to place it somewhere other than the tree root"),
    timeout: Optional[int] = typer.Option(None, "--timeout", min=1,
                                          help="per-step wall-clock budget in seconds; overrides the "
                                               "config default, clamped to limits.yaml:max_timeout_s"),
    dry_run: bool = typer.Option(False, "--dry-run", help="build argv but do not launch containers"),
):
    """Fetch, detect, run (sandboxed), and report on a single artifact."""
    orch = _orch(workroot, config_dir)
    report = orch.run(
        ref, do_run=not no_run, functional=not no_functional, use_llm=not no_llm,
        allow_repo2docker=allow_repo2docker, allow_net=allow_net, allow_lfs=allow_lfs,
        install=not no_install, dry_run=dry_run, timeout_s=timeout,
        data_sources=list(data or []),
    )
    _print_report_summary(report, Path(workroot) / report.submission_id / "out")


@app.command()
def detect(
    ref: str = typer.Argument(..., help="git URL / DOI / local path"),
    workroot: str = typer.Option("work"),
    config_dir: Optional[str] = typer.Option(None),
    no_llm: bool = typer.Option(False, "--no-llm"),
    data: Optional[list[str]] = typer.Option(
        None, "--data", metavar="URL[::SUBDIR]",
        help="extra data deposit merged into the tree before detecting (repeatable), "
             "same as `reprobe run --data`"),
):
    """Detection only — no code execution. Shows the run plan reprobe would use."""
    import shutil

    from .detect import detect as detect_artifacts
    from .detect.manifest import declared_data_sources
    from .fetch import FetchError, fetch as fetch_ref
    from .fetch.data_source import fetch_data_source, merge_into, parse_ref

    sid = submission_id(ref)
    work = Path(workroot) / sid
    # same freshness rule as `run`: a re-detect must not inherit the last fetch
    srcdir = fresh_dir(work, work / "src", "src")
    srcdir.mkdir(parents=True, exist_ok=True)
    fr = fetch_ref(ref, srcdir)
    # Detection must see the same tree `run` would build, or the preview shows a
    # different artifact from the one that gets checked.
    for i, spec in enumerate(list(data or []) + declared_data_sources(srcdir)):
        url, into = parse_ref(str(spec))
        stage = fresh_dir(work, work / f"data{i:02d}", f"data{i:02d}")
        try:
            dfr = fetch_data_source(url, stage)
            copied, collisions = merge_into(stage, srcdir, into)
            console.print(f"[bold]data source[/bold]: {url} → {into or '.'} · "
                          f"{dfr.resolved_type} · {len(copied)} file(s)"
                          + (f" · [yellow]{len(collisions)} not overwritten[/yellow]"
                             if collisions else ""))
        except FetchError as e:
            console.print(f"[red]data source failed[/red]: {url} — {e}")
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    cfg = load_config(config_dir)
    from .llm import from_config as llm_from_config
    client = None if no_llm else llm_from_config(cfg.llm)
    if client is not None and not client.available():
        client = None
    res, meta = detect_artifacts(srcdir, use_llm=not no_llm, llm_client=client)

    console.print(f"[bold]source[/bold]: {fr.resolved_type} · pin {fr.pin.kind}:{fr.pin.value[:50]}")
    console.print(f"[bold]artifact types[/bold]: {', '.join(res.artifact_types) or '(none)'}")
    if res.inventory:
        console.print("[bold]non-code files[/bold]: "
                      + " · ".join(f"{t} ×{n}" for t, n in sorted(res.inventory.items())))
    console.print(f"[bold]run plan source[/bold]: {res.run_plan_source}")
    t = Table("order", "runner", "target", "expected outputs")
    for i, s in enumerate(res.steps, 1):
        t.add_row(str(i), s.runner, s.target, ", ".join(s.expected_outputs) or "—")
    console.print(t)
    for n in res.notes:
        console.print(f"  - {n}")


@app.command()
def pull(
    config_dir: Optional[str] = typer.Option(None, help="override config/ directory"),
):
    """Pull the pinned base images + smoke image — one-command bootstrap on a
    new machine. The CI controller image is intentionally excluded (build it
    with `bash images/build-images.sh controller`; it is not published)."""
    cfg = load_config(config_dir)
    if not docker_available():
        console.print("[red]docker is not available[/red] — install/start Docker first "
                      "(Windows: Docker Desktop needs WSL 2 or admin-enabled Hyper-V; Linux: "
                      "start the daemon and join the 'docker' group). "
                      "`reprobe doctor` checks the full environment")
        raise typer.Exit(1)

    images: list[str] = []
    for key in ("python", "r"):
        img = cfg.base_image(key)
        if img and img not in images:
            images.append(img)
    smoke = cfg.pins.get("fetch", {}).get("smoke_image", "hello-world")
    if smoke and smoke not in images:
        images.append(smoke)

    ok = True
    t = Table("image", "status")
    for img in images:
        if image_present(img):
            t.add_row(img, "ok (already present)")
            continue
        console.print(f"pulling {img}…")
        pulled = pull_image(img)
        ok &= pulled
        t.add_row(img, "ok (pulled)" if pulled else "FAIL (not found, private, or network error)")
    console.print(t)
    raise typer.Exit(0 if ok else 1)


@app.command()
def batch(
    csv_path: str = typer.Argument(..., help="CSV with a 'url' column (or one URL per line)"),
    workroot: str = typer.Option("work"),
    out: str = typer.Option("out", help="dashboard output directory"),
    config_dir: Optional[str] = typer.Option(None),
    no_run: bool = typer.Option(False, "--no-run"),
    no_llm: bool = typer.Option(False, "--no-llm"),
    resume: bool = typer.Option(False, "--resume", help="reuse existing per-submission reports; "
                                "only fetch-failed/infra-error submissions are retried"),
):
    """Run a whole review season and emit a sortable dashboard."""
    import shutil

    from . import __version__ as _v
    from .models import Report
    from .report import dashboard as dash

    refs = _read_refs(csv_path)
    orch = _orch(workroot, config_dir)
    outdir = Path(out)
    outdir.mkdir(parents=True, exist_ok=True)
    reports = []
    for ref in refs:
        console.print(f"[cyan]>[/cyan] {ref}")
        sid = submission_id(ref)
        rep = _resumed_report(Path(workroot) / sid / "out" / "report.json") if resume else None
        if rep is not None:
            console.print("[dim]  resumed from existing report[/dim]")
        else:
            try:
                rep = orch.run(ref, do_run=not no_run, use_llm=not no_llm, sid=sid)
            except Exception as e:  # one bad submission must never abort the season
                console.print(f"[red]  harness error[/red]: {type(e).__name__}: {e}")
                rep = Report(submission_id=sid, harness_version=f"reprobe {_v}",
                             timestamp="", source={"input": ref, "error": f"{type(e).__name__}: {e}"},
                             verdict={"overall": "infra-error", "human_review_required": True,
                                      "note": "harness crashed on this submission — no statement about the artifact"})
        reports.append(rep.model_dump(mode="json"))
        # dashboard hrefs are <sid>/report.html — make out/ a self-contained bundle
        src_report = Path(workroot) / sid / "out" / "report.html"
        if src_report.is_file():
            dst = outdir / sid / "report.html"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_report, dst)
    (outdir / "dashboard.html").write_text(dash.render(reports), encoding="utf-8")
    (outdir / "badges.json").write_text(json.dumps(
        [{"submission_id": r["submission_id"], "badges": r["badges"]} for r in reports], indent=2), encoding="utf-8")
    (outdir / "badges.csv").write_text(_badges_csv(reports), encoding="utf-8")
    console.print(f"[green]ok[/green] dashboard: {outdir / 'dashboard.html'}  ({len(reports)} submissions)")


@app.command()
def doctor(
    config_dir: Optional[str] = typer.Option(None),
    smoke: bool = typer.Option(False, "--smoke", help="run a sandbox smoke test (hello-world)"),
    golden: bool = typer.Option(False, "--golden", help="regression-check the pipeline against "
                                "the bundled golden fixtures (repo checkout only)"),
):
    """Self-check: config, Docker, base images, Ollama, sandbox flags."""
    import shutil as _shutil

    cfg = load_config(config_dir)
    ok = True
    t = Table("check", "status", "detail")

    # Never vouch for a path without looking at it. This row read "ok" for a
    # directory that did not exist, so the one check that could have caught a
    # wrong config resolution instead confirmed it.
    cfg_dir_ok = cfg.config_dir.is_dir()
    ok &= cfg_dir_ok
    detail = str(cfg.config_dir)
    if not cfg_dir_ok:
        detail += " — does not exist"
        if installed_non_editably():
            # src-layout: config lives at <repo>/config, found via parents[2] of
            # this package. From site-packages that resolves to <prefix>/Lib/config,
            # which is nobody's config dir — the give-away for a plain `pip install`.
            detail += ("; reprobe was installed non-editably, so it cannot see the repo's "
                       "config/. Reinstall with `pip install -e .` from the checkout, or set "
                       "REPROBE_CONFIG_DIR")
        else:
            detail += "; pass --config-dir or set REPROBE_CONFIG_DIR"
    t.add_row("config dir", "ok" if cfg_dir_ok else "FAIL", detail)
    t.add_row("pins.yaml", "ok" if cfg.pins else "FAIL", f"year={cfg.pins.get('year')}")

    git_ok = _shutil.which("git") is not None
    ok &= git_ok
    t.add_row("git", "ok" if git_ok else "FAIL",
              "on PATH" if git_ok else "not found — install git before fetching repositories")

    dav = docker_available()
    ok &= dav
    t.add_row("docker", "ok" if dav else "FAIL",
              "daemon reachable" if dav else "not available — start Docker (Windows: Docker "
              "Desktop needs WSL 2 or admin-enabled Hyper-V; Linux: start the daemon and join "
              "the 'docker' group)")

    # Advisory only (never fails doctor): pins.yaml asks for @sha256 digests on
    # the upstream images it cannot rebuild, so year-old reports re-run exactly.
    mm = (cfg.pins.get("base_images") or {}).get("micromamba_base") or ""
    if mm and "@sha256:" not in mm:
        t.add_row("pin digest", "--", "micromamba_base is tag-only — add its @sha256: digest "
                                      "to pins.yaml after first pull")
    if cfg.llm.get("enabled"):
        oi = cfg.llm.get("ollama_image") or ""
        if oi and "@sha256:" not in oi:
            t.add_row("pin digest", "--", "llm.ollama_image is tag-only — add its @sha256: digest "
                                          "to pins.yaml after first pull")

    # Missing base images mean nothing can run — fail loudly, don't just advise.
    for key in ("python", "r"):
        img = cfg.base_image(key)
        present = image_present(img) if dav else False
        ok &= present
        t.add_row(f"base image [{key}]", "ok" if present else "FAIL",
                  f"{img} {'present' if present else f'(docker pull {img} — or build with images/build-images.sh)'}")

    from .llm import from_config as llm_from_config
    client = llm_from_config(cfg.llm)
    if client is None:
        t.add_row("LLM", "--", "disabled in pins.yaml")
    else:
        st = client.status()
        t.add_row("LLM (Ollama)", "ok" if st.get("ok") else "--",
                  f"{st.get('detail')} (optional; --no-llm works)")

    from .runners import RunnerLoadError, load_runners as _load_runners
    errs: list[str] = []
    try:
        loaded = _load_runners(rows=cfg.runner_rows, errors=errs)
        detail = ", ".join(sorted(loaded)) or "(none)"
        t.add_row("runners", "ok" if loaded else "FAIL", detail)
    except RunnerLoadError as e:
        ok = False
        t.add_row("runners", "FAIL", str(e))
    for msg in errs:
        t.add_row("runners", "--", msg)
    console.print(t)

    if smoke:
        if not dav:
            ok = False
            console.print("[yellow]smoke FAIL: docker unavailable[/yellow]")
        else:
            img = cfg.pins.get("fetch", {}).get("smoke_image", "hello-world")
            # The smoke image comes from the chair's own pins.yaml (trusted
            # config, tiny by convention) — pulling it here keeps `doctor
            # --smoke` working on a fresh machine. Author-code images are
            # never auto-pulled; that policy lives in docker_exec.
            if not image_present(img):
                console.print(f"[bold]sandbox smoke[/bold]: pulling {img}…")
                pull_image(img, timeout=300)
            console.print(f"[bold]sandbox smoke[/bold]: running {img} under full sandbox flags…")
            spec = ContainerSpec(image=img, command=[], network="none")
            raw = run_container(spec, cfg.limits_for("python"), Path("work") / "_smoke.log")
            passed = raw.exit_code == 0
            ok &= passed
            status = "ok passed" if passed else f"FAIL exit={raw.exit_code} ({raw.error or 'see log'})"
            console.print(f"  {status}  ({raw.duration_s}s)")
            if raw.argv_redacted:
                console.print(f"  [dim]{' '.join(raw.argv_redacted)}[/dim]")

    if golden:
        from .golden import GOLDEN_REL, compare as golden_compare, find_repo_root
        root = find_repo_root()
        if root is None or not (root / GOLDEN_REL).is_file():
            console.print("[yellow]golden skipped: fixtures / tests/golden/expected.json not found "
                          "— run from a repo checkout[/yellow]")
        else:
            console.print(f"[bold]golden regression[/bold]: {len(json.loads((root / GOLDEN_REL).read_text(encoding='utf-8')))} fixtures through the dry-run pipeline…")
            for name, good, detail in golden_compare(root, config=cfg):
                ok &= good
                console.print(f"  {'ok' if good else 'FAIL'} {name}" + ("" if good else f" — {detail}"))

    raise typer.Exit(0 if ok else 1)


@app.command("unity-refresh")
def unity_refresh():
    """Refresh the Unity image tag map (Phase 3 — not yet implemented)."""
    console.print("unity-refresh is not yet implemented (Phase 3) — nothing to do this year. "
                  "Unity projects are checked structurally (T0) without it.")


@app.command()
def version():
    """Print the harness version."""
    console.print(f"reprobe {__version__}")


# ---------------------------------------------------------------------- #
def _resumed_report(report_json: Path):
    """The existing report for --resume, or None to (re-)run. Fetch-failed and
    infra-error verdicts return None so transient failures are retried; an
    unreadable file also returns None (never abort the season on a bad cache)."""
    from .models import Report

    if not report_json.is_file():
        return None
    try:
        data = json.loads(report_json.read_text(encoding="utf-8"))
        if (data.get("verdict") or {}).get("overall") in ("fetch-failed", "infra-error"):
            return None
        return Report.model_validate(data)
    except Exception:
        return None


def _csv_safe(v) -> str:
    """Neutralize spreadsheet formula injection: a cell beginning with = + - @
    (or a tab/CR) is executed as a formula when a chair opens the CSV in
    Excel/Sheets. The submission id and input URL are author-influenced, so
    prefix any such cell with a single quote to force it to plain text."""
    s = "" if v is None else str(v)
    if s[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def _badges_csv(reports: list[dict]) -> str:
    """Flat per-submission summary for spreadsheet reconciliation (PCS etc.)."""
    import io

    from .report.dashboard import triage_flags

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["submission_id", "input", "verdict", "human_review_required",
                "available", "functional", "results_reproduced",
                "fair_findable", "fair_accessible", "fair_interoperable", "fair_reusable",
                "artifact_types", "flags"])
    for r in reports:
        acm = (r.get("badges") or {}).get("acm") or {}
        fair = (r.get("badges") or {}).get("fair") or {}
        verdict = r.get("verdict") or {}
        w.writerow([_csv_safe(c) for c in (
                    r.get("submission_id"), (r.get("source") or {}).get("input"),
                    verdict.get("overall"), verdict.get("human_review_required"),
                    acm.get("available"), acm.get("functional"), acm.get("results_reproduced"),
                    fair.get("findable"), fair.get("accessible"),
                    fair.get("interoperable"), fair.get("reusable"),
                    ";".join((r.get("detect") or {}).get("artifact_types") or []),
                    ";".join(triage_flags(r)))])
    return buf.getvalue()


def _read_refs(csv_path: str) -> list[str]:
    p = Path(csv_path)
    # utf-8-sig strips a leading BOM (Excel / PowerShell CSV exports carry one),
    # which would otherwise break header detection and the 'url' column lookup.
    text = p.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    if not lines:                       # empty file must not abort the whole season
        return []
    refs: list[str] = []
    if "," in lines[0] or lines[0].lower().startswith("url"):
        for row in csv.DictReader(lines):
            url = row.get("url") or row.get("URL") or next(iter(row.values()), None)
            if url and url.strip():
                refs.append(url.strip())
    else:
        refs = [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]
    return refs


def _print_report_summary(report, outdir: Path) -> None:
    acm = report.badges.get("acm", {})
    console.print()
    console.print(f"[bold]submission[/bold]: {report.submission_id}")
    console.print(f"[bold]verdict[/bold]: {report.verdict.get('overall')} "
                  f"(human review: {report.verdict.get('human_review_required')})")
    console.print(f"[bold]Available[/bold]: {acm.get('available')}   "
                  f"[bold]Functional[/bold]: {acm.get('functional')}")
    for n in acm.get("notes", []):
        console.print(f"  [dim]- {n}[/dim]")
    t = Table("target", "runner", "status", "time")
    for s in report.steps:
        t.add_row(s.target, s.runner, s.status, f"{s.duration_s}s")
    if report.steps:
        console.print(t)
    console.print(f"[green]reports[/green]: {outdir / 'report.html'} · {outdir / 'report.md'} · {outdir / 'report.json'}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
