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
import re
import sys
from pathlib import Path
from typing import Any, Optional

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
from .docker_exec import docker_available, image_present, run_container
from .models import ContainerSpec
from .orchestrator import Orchestrator, fresh_dir, run_options, submission_id

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
    reuse_downloads: bool = typer.Option(
        False, "--reuse-downloads",
        help="reuse pip/conda downloads across runs via a cache in the workroot. Makes a "
             "re-run of the same artifact minutes instead of an hour. OFF by default because "
             "the cache is shared between submissions: use it while iterating on one "
             "artifact, not for a season of them"),
    dry_run: bool = typer.Option(False, "--dry-run", help="build argv but do not launch containers"),
    strict: bool = typer.Option(False, "--strict",
                                help="exit 1 when the verdict needs human review (CI gating). "
                                     "Without it: 0 = a report was made, 3 = the harness failed"),
):
    """Fetch, detect, run (sandboxed), and report on a single artifact.

    Exit status: 0 a report was written; 1 (only with --strict) the verdict needs
    human review; 3 the harness failed (infra-error) and made no statement."""
    orch = _orch(workroot, config_dir)
    report = orch.run(
        ref, do_run=not no_run, functional=not no_functional, use_llm=not no_llm,
        allow_repo2docker=allow_repo2docker, allow_net=allow_net, allow_lfs=allow_lfs,
        install=not no_install, dry_run=dry_run, timeout_s=timeout,
        data_sources=list(data or []), reuse_downloads=reuse_downloads,
    )
    _print_report_summary(report, Path(workroot) / report.submission_id / "report")
    if code := _exit_code([report.verdict], strict=strict):
        raise typer.Exit(code)


# Exit codes. A finding about the artifact — it fails, it needs review — is what
# the tool exists to report, so it is not a failure of the tool. Only a run that
# made NO statement (the harness itself failed) is, and `--strict` adds the case
# a CI gate wants: anything a human still has to look at.
EXIT_OK, EXIT_NEEDS_REVIEW, EXIT_HARNESS_ERROR = 0, 1, 3


def _exit_code(verdicts: list[dict[str, Any]], *, strict: bool) -> int:
    if any(v.get("overall") == "infra-error" for v in verdicts):
        console.print(f"[yellow]exit {EXIT_HARNESS_ERROR}[/yellow]: the harness failed on at "
                      "least one submission (infra-error) — no statement about it was made")
        return EXIT_HARNESS_ERROR
    if strict and any(v.get("human_review_required") for v in verdicts):
        return EXIT_NEEDS_REVIEW
    return EXIT_OK


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
    from .fetch import FetchError
    from .fetch import fetch as fetch_ref
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
    res, _meta = detect_artifacts(srcdir, use_llm=not no_llm, llm_client=client)

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

    from .docker_exec import pull_image_detail

    ok = True
    errors: list[tuple[str, str]] = []
    t = Table("image", "status")
    for img in images:
        if image_present(img):
            t.add_row(img, "ok (already present)")
            continue
        console.print(f"pulling {img}…")
        pulled, why = pull_image_detail(img)
        ok &= pulled
        t.add_row(img, "ok (pulled)" if pulled else "FAIL (see below)")
        if not pulled:
            errors.append((img, why))
    console.print(t)
    for img, why in errors:              # untruncated: the table clips long image names
        console.print(f"[red]{img}[/red]: {why}")
    raise typer.Exit(0 if ok else 1)


@app.command()
def batch(
    csv_path: str = typer.Argument(..., help="CSV with a 'url' column, plus an optional 'data' "
                                             "column of deposit URLs (or one URL per line)"),
    workroot: str = typer.Option("work"),
    out: str = typer.Option("out", help="dashboard output directory"),
    config_dir: Optional[str] = typer.Option(None),
    no_run: bool = typer.Option(False, "--no-run"),
    no_llm: bool = typer.Option(False, "--no-llm"),
    no_functional: bool = typer.Option(False, "--no-functional",
                                       help="do not evaluate the Functional candidate"),
    no_install: bool = typer.Option(False, "--no-install", help="skip the dependency-install phase"),
    allow_lfs: bool = typer.Option(False, "--allow-lfs", help="pull git-lfs data during fetch"),
    timeout: Optional[int] = typer.Option(None, "--timeout", min=1,
                                          help="per-step wall-clock budget in seconds, for every "
                                               "submission in the season"),
    reuse_downloads: bool = typer.Option(False, "--reuse-downloads",
                                         help="share pip/conda downloads across the season — see "
                                              "`reprobe run --help` for why this is off by default"),
    resume: bool = typer.Option(False, "--resume", help="reuse existing per-submission reports made "
                                "by this harness version, config and these options; fetch-failed/"
                                "infra-error submissions are retried"),
    strict: bool = typer.Option(False, "--strict",
                                help="exit 1 when any submission needs human review"),
):
    """Run a whole review season and emit a sortable dashboard.

    Exit status: 0 the dashboard was written; 1 (only with --strict) a
    submission needs human review; 3 the harness failed on at least one
    submission (re-run with --resume to retry just those)."""
    import shutil

    from . import __version__ as _v
    from .models import Report
    from .report import dashboard as dash

    refs = _read_refs(csv_path)
    orch = _orch(workroot, config_dir)
    outdir = Path(out)
    outdir.mkdir(parents=True, exist_ok=True)
    reports = []
    for row in refs:
        ref, data = row["url"], row["data"]
        console.print(f"[cyan]>[/cyan] {ref}" + (f"  [dim]+{len(data)} deposit(s)[/dim]" if data else ""))
        sid = submission_id(ref)
        expected = {
            "harness_version": f"reprobe {_v}",
            "config": {k: v for k, v in orch._provenance().items() if k.endswith("_sha256")},
            "run_options": run_options(
                run_mode="full" if not no_run else "no-run", functional=not no_functional,
                use_llm=not no_llm, install=not no_install, allow_lfs=allow_lfs, timeout_s=timeout,
                data_sources=data, reuse_downloads=reuse_downloads),
        }
        rep = _resumed_report(Path(workroot) / sid / "report" / "report.json", expected) \
            if resume else None
        if rep is not None:
            console.print("[dim]  resumed from existing report[/dim]")
        else:
            try:
                rep = orch.run(ref, do_run=not no_run, use_llm=not no_llm, sid=sid,
                               functional=not no_functional, install=not no_install,
                               allow_lfs=allow_lfs, timeout_s=timeout,
                               reuse_downloads=reuse_downloads, data_sources=data)
            except Exception as e:  # one bad submission must never abort the season
                console.print(f"[red]  harness error[/red]: {type(e).__name__}: {e}")
                rep = Report(submission_id=sid, harness_version=f"reprobe {_v}",
                             timestamp="", source={"input": ref, "error": f"{type(e).__name__}: {e}"},
                             verdict={"overall": "infra-error", "human_review_required": True,
                                      "note": "harness crashed on this submission — no statement about the artifact"})
        reports.append(rep.model_dump(mode="json"))
        # dashboard hrefs are <sid>/report.html — make out/ a self-contained bundle
        src_report = Path(workroot) / sid / "report" / "report.html"
        if src_report.is_file():
            dst = outdir / sid / "report.html"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_report, dst)
    (outdir / "dashboard.html").write_text(dash.render(reports), encoding="utf-8")
    (outdir / "badges.json").write_text(json.dumps(
        [{"submission_id": r["submission_id"], "badges": r["badges"]} for r in reports], indent=2), encoding="utf-8")
    (outdir / "badges.csv").write_text(_badges_csv(reports), encoding="utf-8")
    console.print(f"[green]ok[/green] dashboard: {outdir / 'dashboard.html'}  ({len(reports)} submissions)")
    if code := _exit_code([r.get("verdict") or {} for r in reports], strict=strict):
        raise typer.Exit(code)


@app.command()
def doctor(
    config_dir: Optional[str] = typer.Option(None),
    smoke: bool = typer.Option(False, "--smoke", help="check the sandbox inside each pinned base "
                               "image: /work writable, rootfs read-only, not root, no network"),
    workroot: str = typer.Option("work", help="the workroot runs will use; --smoke mounts a "
                                 "directory from it, exactly as a run would"),
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

    from .runners import RunnerLoadError
    from .runners import load_runners as _load_runners
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
            ok &= _sandbox_smoke(cfg, Path(workroot))

    if golden:
        from .golden import GOLDEN_REL, find_repo_root
        from .golden import compare as golden_compare
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
def _resumed_report(report_json: Path, expected: Optional[dict] = None):
    """The existing report for --resume, or None to (re-)run. Fetch-failed and
    infra-error verdicts return None so transient failures are retried; an
    unreadable file also returns None (never abort the season on a bad cache).

    A report is only reused when it was made the way this batch would make it:
    same harness version, same config files, same run options. A --dry-run or
    --no-run report, or one from last year's pins, sitting in the workroot was
    otherwise folded into this season's dashboard as if it were a result."""
    from .models import Report

    if not report_json.is_file():
        return None
    try:
        data = json.loads(report_json.read_text(encoding="utf-8"))
        if (data.get("verdict") or {}).get("overall") in ("fetch-failed", "infra-error"):
            return None
        if data.get("run_mode", "full") == "dry-run":
            return None
        if expected is not None:
            prov = data.get("provenance") or {}
            if data.get("harness_version") != expected.get("harness_version"):
                return None
            if any(prov.get(k) != v for k, v in (expected.get("config") or {}).items()):
                return None
            if prov.get("run_options") != expected.get("run_options"):
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


def _read_refs(csv_path: str) -> list[dict[str, Any]]:
    """Submissions to review: ``{"url": ..., "data": [...]}`` per row.

    A ``data`` column carries the deposits ``--data`` would carry on a single run.
    That column is the whole reason batch needs one: an artifact CAN declare its
    deposit in a manifest, and when it does the harness finds it either way — but
    the split that `--data` exists for is the one where the README links the data
    in prose and the artifact declares nothing a machine can read. That case was
    reviewable one submission at a time and not at the scale a chair works at.
    Several deposits go in one cell, separated by whitespace or ';'.

    Comment lines (``#``) and blank lines are skipped in both forms. A header
    is recognised by its ``url`` column; a file without one is read as
    ``url[,data]`` rows — the first row is a submission, not a header. A header
    that has columns but no ``url`` is an error: taking its first column instead
    would batch a sheet of paper titles."""
    # utf-8-sig strips a leading BOM (Excel / PowerShell CSV exports carry one),
    # which would otherwise break header detection and the 'url' column lookup.
    text = Path(csv_path).read_text(encoding="utf-8-sig")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:                       # empty file must not abort the whole season
        return []
    rows = list(csv.reader(lines))
    header = [(c or "").strip().lower() for c in rows[0]]
    if "url" in header:
        i_url = header.index("url")
        i_data = header.index("data") if "data" in header else None
        body = rows[1:]
    elif len(rows[0]) == 1 or _looks_like_ref(rows[0][0]):
        if any(len(r) > 2 for r in rows):
            raise typer.BadParameter(
                f"{csv_path}: rows have more than two columns but no header; add a header "
                "row naming the 'url' (and optional 'data') column")
        i_url, i_data, body = 0, 1, rows
    else:
        raise typer.BadParameter(
            f"{csv_path}: the header row ({', '.join(c.strip() for c in rows[0])}) has no "
            "'url' column; name the column holding the submission links 'url'")
    refs: list[dict[str, Any]] = []
    for r in body:
        url = r[i_url].strip() if len(r) > i_url else ""
        if url:
            cell = r[i_data] if i_data is not None and len(r) > i_data else None
            refs.append({"url": url, "data": _split_data_cell(cell)})
    return refs


def _looks_like_ref(cell: str) -> bool:
    """Whether a first cell is a submission rather than a column name."""
    c = cell.strip()
    return "://" in c or c.lower().startswith(("doi:", "10.", "git@")) or Path(c).exists()


def _split_data_cell(cell: Optional[str]) -> list[str]:
    """Deposit URLs from one CSV cell. ',' is the column separator and '::' the
    subdirectory marker, so ';' and whitespace are what remain — and a URL can
    contain neither."""
    return [tok for tok in re.split(r"[;\s]+", (cell or "").strip()) if tok]


# What every author step relies on, checked inside each pinned base image under
# the real run-phase envelope. hello-world proved only that a container starts;
# a /work the mapped user cannot write (rootless Docker, SELinux labels) or an
# image that cannot start read-only fails EVERY artifact, and each failure reads
# like the artifact's own. One line per check, so a failure names itself.
_SMOKE_CHECKS = {
    "work dir writable": "echo x > /work/.reprobe_smoke && rm /work/.reprobe_smoke",
    "root filesystem read-only": "! touch /usr/.reprobe_smoke 2>/dev/null",
    "not running as root": '[ "$(id -u)" != 0 ]',
    "no network": "! timeout 5 bash -c 'exec 3<>/dev/tcp/1.1.1.1/53' 2>/dev/null",
}
_SMOKE_RUNTIME = {"python": "python -I -c 'import sys; print(sys.version)'",
                  "r": "Rscript --vanilla -e 'cat(R.version.string, \"\\n\")'"}


def _smoke_script(key: str) -> str:
    checks = {**_SMOKE_CHECKS, f"{key} runs": _SMOKE_RUNTIME[key]}
    return "; ".join(f"if {cmd}; then echo 'ok   {name}'; else echo 'FAIL {name}'; rc=1; fi"
                     for name, cmd in checks.items()).join(("rc=0; ", "; exit $rc"))


def _sandbox_smoke(cfg, workroot: Path = Path("work")) -> bool:
    """Run the checks with /work mounted from ``workroot`` — the directory real
    runs mount. Under the controller image (Docker-out-of-Docker) the host
    daemon resolves that path on the HOST, so a workroot that is not the same
    path inside and outside the controller fails "work dir writable" here
    instead of failing every artifact later."""
    import tempfile

    from .models import Mount
    images = [(k, cfg.base_image(k)) for k in ("python", "r")]
    present = [(k, img) for k, img in images if img and image_present(img)]
    if not present:
        console.print("[yellow]smoke FAIL: no pinned base image is present — `reprobe pull` first; "
                      "the smoke test checks the sandbox inside the images author code runs in[/yellow]")
        return False
    all_ok = True
    for key, img in present:
        console.print(f"[bold]sandbox smoke[/bold] [{key}]: {img} under the run-phase envelope…")
        workroot.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".smoke-", dir=workroot) as tmp:
            work = Path(tmp) / "work"
            work.mkdir()
            spec = ContainerSpec(image=img, command=["bash", "-c", _smoke_script(key)],
                                 workdir="/work", network="none",
                                 mounts=[Mount(source=work.resolve().as_posix(), target="/work",
                                               read_only=False)])
            log = Path(tmp) / "smoke.log"
            limits = dict(cfg.limits_for("python"), timeout_s=120)
            raw = run_container(spec, limits, log, work_root=work)
            out = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
        lines = [ln for ln in out.splitlines() if ln.startswith(("ok   ", "FAIL "))]
        passed = raw.exit_code == 0 and len(lines) == len(_SMOKE_CHECKS) + 1 \
            and not any(ln.startswith("FAIL") for ln in lines)
        all_ok &= passed
        for ln in lines:
            console.print(f"  {ln}")
        if not passed:
            console.print(f"  [red]FAIL[/red] exit={raw.exit_code} {raw.error or ''}".rstrip())
            if not lines:
                console.print("  " + "\n  ".join(out.splitlines()[-10:]))
    return all_ok


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


def _exit_on_sigterm(signum, frame):  # noqa: ARG001 — signal handler signature
    # SystemExit unwinds through run_container's `finally`, which kills and
    # removes the running author-code container. Python's default SIGTERM
    # action skips every finally block, so a cancelled CI job or `kill` left the
    # container running with nothing enforcing its timeout.
    raise SystemExit(143)


def main() -> None:
    import signal

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _exit_on_sigterm)
    app()


if __name__ == "__main__":
    main()
