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
from .config import load_config
from .docker_exec import docker_available, image_present, run_container
from .models import ContainerSpec
from .orchestrator import Orchestrator, submission_id

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
    dry_run: bool = typer.Option(False, "--dry-run", help="build argv but do not launch containers"),
):
    """Fetch, detect, run (sandboxed), and report on a single artifact."""
    orch = _orch(workroot, config_dir)
    report = orch.run(
        ref, do_run=not no_run, functional=not no_functional, use_llm=not no_llm,
        allow_repo2docker=allow_repo2docker, allow_net=allow_net, install=not no_install, dry_run=dry_run,
    )
    _print_report_summary(report, Path(workroot) / report.submission_id / "out")


@app.command()
def detect(
    ref: str = typer.Argument(..., help="git URL / DOI / local path"),
    workroot: str = typer.Option("work"),
    config_dir: Optional[str] = typer.Option(None),
    no_llm: bool = typer.Option(False, "--no-llm"),
):
    """Detection only — no code execution. Shows the run plan reprobe would use."""
    from .detect import detect as detect_artifacts
    from .fetch import fetch as fetch_ref

    sid = submission_id(ref)
    srcdir = Path(workroot) / sid / "src"
    srcdir.mkdir(parents=True, exist_ok=True)
    fr = fetch_ref(ref, srcdir)
    cfg = load_config(config_dir)
    from .llm import from_config as llm_from_config
    client = None if no_llm else llm_from_config(cfg.llm)
    if client is not None and not client.available():
        client = None
    res, meta = detect_artifacts(srcdir, use_llm=not no_llm, llm_client=client)

    console.print(f"[bold]source[/bold]: {fr.resolved_type} · pin {fr.pin.kind}:{fr.pin.value[:50]}")
    console.print(f"[bold]artifact types[/bold]: {', '.join(res.artifact_types) or '(none)'}")
    console.print(f"[bold]run plan source[/bold]: {res.run_plan_source}")
    t = Table("order", "runner", "target", "expected outputs")
    for i, s in enumerate(res.steps, 1):
        t.add_row(str(i), s.runner, s.target, ", ".join(s.expected_outputs) or "—")
    console.print(t)
    for n in res.notes:
        console.print(f"  - {n}")


@app.command()
def batch(
    csv_path: str = typer.Argument(..., help="CSV with a 'url' column (or one URL per line)"),
    workroot: str = typer.Option("work"),
    out: str = typer.Option("out", help="dashboard output directory"),
    config_dir: Optional[str] = typer.Option(None),
    no_run: bool = typer.Option(False, "--no-run"),
    no_llm: bool = typer.Option(False, "--no-llm"),
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
    console.print(f"[green]ok[/green] dashboard: {outdir / 'dashboard.html'}  ({len(reports)} submissions)")


@app.command()
def doctor(
    config_dir: Optional[str] = typer.Option(None),
    smoke: bool = typer.Option(False, "--smoke", help="run a sandbox smoke test (hello-world)"),
):
    """Self-check: config, Docker, base images, Ollama, sandbox flags."""
    import shutil as _shutil

    cfg = load_config(config_dir)
    ok = True
    t = Table("check", "status", "detail")

    t.add_row("config dir", "ok", str(cfg.config_dir))
    t.add_row("pins.yaml", "ok" if cfg.pins else "FAIL", f"year={cfg.pins.get('year')}")

    git_ok = _shutil.which("git") is not None
    ok &= git_ok
    t.add_row("git", "ok" if git_ok else "FAIL",
              "on PATH" if git_ok else "not found — install git before fetching repositories")

    dav = docker_available()
    ok &= dav
    t.add_row("docker", "ok" if dav else "FAIL", "daemon reachable" if dav else "not available")

    # Missing base images mean nothing can run — fail loudly, don't just advise.
    for key in ("python", "r"):
        img = cfg.base_image(key)
        present = image_present(img) if dav else False
        ok &= present
        t.add_row(f"base image [{key}]", "ok" if present else "FAIL",
                  f"{img} {'present' if present else '(build with images/build-images.sh)'}")

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
            console.print("[yellow]smoke skipped: docker unavailable[/yellow]")
        else:
            img = cfg.pins.get("fetch", {}).get("smoke_image", "hello-world")
            console.print(f"[bold]sandbox smoke[/bold]: running {img} under full sandbox flags…")
            spec = ContainerSpec(image=img, command=[], network="none")
            raw = run_container(spec, cfg.limits_for("python"), Path("work") / "_smoke.log")
            status = "ok passed" if raw.exit_code == 0 else f"FAIL exit={raw.exit_code} ({raw.error or 'see log'})"
            console.print(f"  {status}  ({raw.duration_s}s)")
            if raw.argv_redacted:
                console.print(f"  [dim]{' '.join(raw.argv_redacted)}[/dim]")

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
def _read_refs(csv_path: str) -> list[str]:
    p = Path(csv_path)
    text = p.read_text(encoding="utf-8")
    refs: list[str] = []
    if "," in text.splitlines()[0] or text.lower().startswith("url"):
        for row in csv.DictReader(text.splitlines()):
            url = row.get("url") or row.get("URL") or next(iter(row.values()), None)
            if url and url.strip():
                refs.append(url.strip())
    else:
        refs = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
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
