"""End-to-end pipeline test, daemon-free: the full orchestrator over the local
example fixture with --dry-run containers. Asserts the report artifacts are
written, sandbox flags reach docker argv, and no badge is ever granted for a
local (non-archival) pin."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import reprobe.orchestrator as orch_mod
from reprobe.orchestrator import Orchestrator

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "example-python"
BROKEN = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "broken-python"


@pytest.fixture()
def spy_run_container(monkeypatch):
    """Delegate to the real dry-run implementation while capturing every argv."""
    calls: list[dict] = []
    real = orch_mod.run_container

    def spy(spec, limits, log_path, **kw):
        raw = real(spec, limits, log_path, **kw)
        calls.append({"spec": spec, "argv": raw.argv_redacted, "kw": kw, "limits": limits})
        return raw

    monkeypatch.setattr(orch_mod, "run_container", spy)
    return calls


def _run(tmp_path, ref, spy=None, **kw):
    o = Orchestrator(workroot=tmp_path)
    kw.setdefault("dry_run", True)
    return o.run(str(ref), use_llm=False, **kw)


def _simulate(monkeypatch, run_phase, install_phase=None):
    """Replace the container layer with canned results so the orchestrator runs
    for real (not as a dry run) without a daemon. ``run_phase(spec, log_path)``
    returns the RawRunOutput of an offline step; the install phase succeeds
    unless ``install_phase`` says otherwise."""
    from reprobe.models import RawRunOutput

    def fake(spec, limits, log_path, **kw):
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        if spec.network == "egress":
            if install_phase is not None:
                return install_phase(spec, log_path)
            Path(log_path).write_text("installed\n", encoding="utf-8")
            return RawRunOutput(exit_code=0, duration_s=0.1, image=spec.image, log_path=str(log_path))
        return run_phase(spec, log_path)

    monkeypatch.setattr(orch_mod, "run_container", fake)
    monkeypatch.setattr(orch_mod, "reap_orphans", lambda: [])
    monkeypatch.setattr(orch_mod, "_image_digest", lambda image: None)


def test_example_pipeline_writes_reports_and_never_grants(tmp_path, spy_run_container):
    report = _run(tmp_path, EXAMPLE)
    outdir = tmp_path / report.submission_id / "report"
    for name in ("report.json", "report.md", "report.html"):
        assert (outdir / name).is_file(), f"{name} not written"

    data = json.loads((outdir / "report.json").read_text(encoding="utf-8"))
    # a local path has no archival PID: Available must never be granted
    assert data["badges"]["acm"]["available"] != "granted"
    assert data["verdict"]["human_review_required"] is True
    # provenance records the pins so the run is re-checkable next year
    assert data["provenance"]["pins_year"] is not None
    assert "pins.yaml_sha256" in data["provenance"]


def test_run_phase_argv_is_hardened_and_install_is_not_readonly(tmp_path, spy_run_container):
    _run(tmp_path, EXAMPLE)
    assert spy_run_container, "no containers were launched"
    run_calls = [c for c in spy_run_container if c["spec"].network == "none"]
    install_calls = [c for c in spy_run_container if c["spec"].network == "egress"]
    assert run_calls, "no offline run-phase container"
    for c in run_calls:
        argv = c["argv"]
        assert "--network" in argv and "none" in argv
        assert "--read-only" in argv
        assert ["--cap-drop", "ALL"] == argv[argv.index("--cap-drop"):argv.index("--cap-drop") + 2]
    # the example declares no deps, so an install phase may legitimately not run
    for c in install_calls:
        assert "--read-only" not in c["argv"]


def test_failed_source_section_matches_success_shape():
    """A fetch failure must not under-populate report.source, or the report
    renderers blow up on missing keys (pin, resolved_type, ...). The failure
    shape must carry every key the success shape does."""
    from reprobe.models import FetchResult, Pin
    ok = orch_mod._source_section(FetchResult(
        input="u", resolved_type="git", src_dir="/x", pin=Pin(kind="git_sha", value="abc")))
    failed = orch_mod._failed_source_section("u", "boom")
    assert set(ok).issubset(set(failed)), f"missing keys: {set(ok) - set(failed)}"
    assert failed["pin"]["kind"] == "none" and failed["error"] == "boom"


def test_unresolvable_ref_yields_renderable_fetch_failed_report(tmp_path):
    # nonexistent local path: fails fast through the local fetcher, no network.
    report = _run(tmp_path, str(tmp_path / "does-not-exist-anywhere"))
    assert report.verdict["overall"] == "fetch-failed"
    assert report.verdict["human_review_required"] is True
    outdir = tmp_path / report.submission_id / "report"
    for name in ("report.json", "report.md", "report.html"):
        assert (outdir / name).is_file(), f"{name} not written on fetch failure"
    html_text = (outdir / "report.html").read_text(encoding="utf-8")
    assert "fetch failed" in html_text.lower()
    assert "<b>Badges</b>" not in html_text        # nothing fetched -> no badge chips


def test_broken_python_fixture_fails_cleanly(tmp_path, monkeypatch):
    """A failing step yields a clean FAIL verdict — never a traceback out of run()."""
    from reprobe.models import RawRunOutput

    def failing(spec, log_path):
        Path(log_path).write_text("Traceback ...\nZeroDivisionError\n", encoding="utf-8")
        return RawRunOutput(exit_code=1, duration_s=0.1, image=spec.image, log_path=str(log_path))

    _simulate(monkeypatch, failing)
    report = _run(tmp_path, BROKEN, dry_run=False)
    statuses = {s.status for s in report.steps if s.executed}
    assert statuses and statuses <= {"fail", "partial", "error"}
    assert report.verdict["overall"] in ("runs-with-failures", "infra-error")
    assert report.badges["acm"]["functional"] in ("not-met", "not-evaluated")


def test_dataset_phase_downloads_public_and_guards_internal(tmp_path, monkeypatch):
    """Author-declared data[]: a public http(s) URL is downloaded into the run
    tree; an internal/SSRF host is refused; a non-http(s) source is skipped —
    all without ever downloading author-controlled bytes to an internal host."""
    import reprobe.fetch.base as fbase
    from reprobe.models import Report

    calls = []

    def fake_download(url, dest, *, expected_md5=None, **kw):
        calls.append((url, kw))
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_text("data")
        return True, "downloaded (no checksum provided)"

    monkeypatch.setattr(fbase, "download", fake_download)

    o = Orchestrator(workroot=tmp_path)
    rundir = tmp_path / "run"
    rundir.mkdir()
    report = Report(submission_id="x", harness_version="t", timestamp="")
    report.environment = {}
    meta = {"data": [
        {"path": "data/x.csv", "source": "https://93.184.216.34/x.csv"},   # public IP -> ok
        {"path": "y.csv", "source": "http://169.254.169.254/y"},           # metadata IP -> refused
        {"path": "z.csv", "source": "doi:10.5281/zenodo.1"},               # non-http(s) -> skipped
    ]}
    o._dataset_phase(meta, rundir, report, dry_run=False)

    status = {d["path"]: d["status"] for d in report.environment["datasets"]}
    assert status["data/x.csv"] == "ok"
    assert status["y.csv"] == "refused"
    assert status["z.csv"] == "skipped-unsupported-source"
    assert (rundir / "data" / "x.csv").read_text() == "data"
    assert [u for u, _ in calls] == ["https://93.184.216.34/x.csv"]   # only the public URL fetched
    # restrict_public is what turns on per-redirect-hop SSRF re-validation and the
    # DNS pin; without it a public host could 302 to an internal one.
    assert all(kw.get("restrict_public") is True for _, kw in calls)


def test_install_phase_container_gets_the_relaxed_build_envelope(tmp_path, spy_run_container):
    """The install phase must reach docker with an EXECUTABLE tmpfs (source
    packages run ./configure), a writable rootfs, and the long build timeout —
    asserted on the real orchestrator wiring, not a hand-built limits dict."""
    RMIX = Path(__file__).resolve().parents[1] / "fixtures" / "notebook-r-mix"
    _run(tmp_path, RMIX)
    install = [c for c in spy_run_container if c["spec"].network == "egress"]
    assert install, "no egress install container was launched"
    for c in install:
        argv = " ".join(c["argv"])
        assert "--tmpfs /tmp:rw,exec,nosuid,size=4g" in argv, argv
        assert "--read-only" not in c["argv"]
        assert c["kw"].get("allow_egress") is True
        # timeout_s is a subprocess timeout, not an argv flag; the phase's 7200 s are
        # shared by its commands (one container each), so each gets what is left
        assert 0 < c["limits"].get("timeout_s") <= 7200
    assert install[0]["limits"].get("timeout_s") == 7200


def test_rerunning_same_submission_starts_from_a_pristine_src(tmp_path):
    """Re-running a submission must not inherit the previous fetch. A stale src/
    made `git clone` fail outright ("destination path already exists"), and the
    local fetcher (copytree dirs_exist_ok=True) silently merged the old tree in."""
    o = Orchestrator(workroot=tmp_path)
    first = o.run(str(EXAMPLE), use_llm=False, dry_run=True)
    srcdir = tmp_path / first.submission_id / "src"
    stale = srcdir / "stale_from_previous_run.py"
    stale.write_text("print('should not survive a re-fetch')\n")

    second = o.run(str(EXAMPLE), use_llm=False, dry_run=True)
    assert second.submission_id == first.submission_id
    assert second.verdict["overall"] != "fetch-failed", second.source.get("error")
    assert not stale.exists(), "stale file survived the re-fetch"
    assert (srcdir / "01_analyze.py").is_file(), "fresh fetch did not land"


def test_fresh_dir_falls_back_when_removal_fails(tmp_path, monkeypatch):
    # Windows file locks can defeat rmtree; rather than crash mid-batch we fall
    # back to a uniquely-named sibling.
    import reprobe.orchestrator as om

    target = tmp_path / "src"
    target.mkdir()
    monkeypatch.setattr(om.shutil, "rmtree", lambda *a, **k: None)   # pretend it failed
    out = om.fresh_dir(tmp_path, target, "src")
    assert out != target and out.name == "src-1"


def test_dataset_phase_noop_in_dry_run(tmp_path):
    from reprobe.models import Report
    o = Orchestrator(workroot=tmp_path)
    report = Report(submission_id="x", harness_version="t", timestamp="")
    report.environment = {}
    meta = {"data": [{"path": "x.csv", "source": "https://93.184.216.34/x.csv"}]}
    o._dataset_phase(meta, tmp_path, report, dry_run=True)
    assert "datasets" not in report.environment      # dry-run downloads nothing


def test_daemon_loss_stops_the_pipeline_instead_of_inventing_more_failures(tmp_path, monkeypatch):
    """When the engine dies mid-run, every later step fails in milliseconds for
    the same host reason. Those rows read like independent findings about the
    artifact (and each used to draw its own LLM diagnosis), so the run must stop
    and say the remaining steps were never attempted."""
    from reprobe.models import RawRunOutput

    src = tmp_path / "three-step"
    src.mkdir()
    for name in ("01_prep.py", "02_fit.py", "03_plot.py"):
        (src / name).write_text("print('hi')\n", encoding="utf-8")

    attempted: list[str] = []

    def fake(spec, log_path):
        attempted.append(spec.image)
        return RawRunOutput(exit_code=125, duration_s=12.5, image=spec.image,
                            log_path=str(log_path),
                            error="docker-daemon-lost: the Docker daemon stopped responding")

    _simulate(monkeypatch, fake)
    report = _run(tmp_path / "wk", src, dry_run=False)

    assert len(attempted) == 1, f"kept launching containers after the daemon vanished: {attempted}"
    statuses = [s.status for s in report.steps]
    assert statuses == ["error", "skipped", "skipped"], statuses
    for s in report.steps[1:]:
        assert "not attempted" in str(s.diagnostics.get("reason", ""))
        assert s.executed is False
    # the harness failed, so the report still makes no claim about the artifact
    assert report.verdict["overall"] == "infra-error"


def test_data_source_is_merged_before_detection(tmp_path):
    """The "code in git, data on OSF" artifact. The deposit must land in the tree
    BEFORE detect, or the data is missing from the inventory, from the run plan,
    and from the copy that reaches the container — and the run fails on inputs
    the harness was told about."""
    code = tmp_path / "code"
    code.mkdir()
    (code / "01_analyze.py").write_text("import pandas\n", encoding="utf-8")
    deposit = tmp_path / "deposit"
    (deposit / "Study Data").mkdir(parents=True)
    (deposit / "Study Data" / "p01.csv").write_text("id,risk\n1,0.5\n", encoding="utf-8")

    o = Orchestrator(workroot=tmp_path / "wk")
    report = o.run(str(code), use_llm=False, dry_run=True,
                   data_sources=[f"{deposit}::dataset"])

    src = tmp_path / "wk" / report.submission_id / "src"
    assert (src / "dataset" / "Study Data" / "p01.csv").is_file()
    assert (src / "01_analyze.py").is_file(), "the code source was disturbed"

    ds = report.source["data_sources"]
    assert len(ds) == 1 and ds[0]["status"] == "ok" and ds[0]["files"] == 1
    assert ds[0]["into"] == "dataset"
    # a deposit is author-controlled bytes: it must never carry an archival pin
    assert ds[0]["pin"]["kind"] == "none"
    assert any("does NOT strengthen the Available badge" in w
               for w in report.source["warnings"])
    # detection saw it
    assert report.detect["inventory"].get("dataset", 0) >= 1
    # and the run tree got it
    assert (tmp_path / "wk" / report.submission_id / "run" / "dataset" / "Study Data" / "p01.csv").is_file()


def test_data_source_never_overwrites_the_code(tmp_path):
    code = tmp_path / "code"
    code.mkdir()
    (code / "01_analyze.py").write_text("REAL = 1\n", encoding="utf-8")
    deposit = tmp_path / "deposit"
    deposit.mkdir()
    (deposit / "01_analyze.py").write_text("REAL = 'tampered'\n", encoding="utf-8")

    o = Orchestrator(workroot=tmp_path / "wk")
    report = o.run(str(code), use_llm=False, dry_run=True, data_sources=[str(deposit)])

    src = tmp_path / "wk" / report.submission_id / "src"
    assert (src / "01_analyze.py").read_text() == "REAL = 1\n"
    assert report.source["data_sources"][0]["collisions"] == ["01_analyze.py"]
    assert any("NOT overwritten" in w for w in report.source["warnings"])


def test_failed_data_source_is_stated_not_swallowed(tmp_path):
    """If the harness could not fetch declared data, a later missing-input
    failure may be the harness's fault — the report has to say so."""
    code = tmp_path / "code"
    code.mkdir()
    (code / "01_analyze.py").write_text("x = 1\n", encoding="utf-8")

    o = Orchestrator(workroot=tmp_path / "wk")
    report = o.run(str(code), use_llm=False, dry_run=True,
                   data_sources=[str(tmp_path / "does-not-exist")])

    rec = report.source["data_sources"][0]
    assert rec["status"] == "failed"
    assert any("could not be fetched" in w and "WITHOUT it" in w
               for w in report.source["warnings"])


def test_prose_only_data_link_becomes_an_actionable_note(tmp_path):
    """An artifact that says "download the data from OSF" has declared it in a
    way no harness can act on. Say so, with the command that fixes it."""
    code = tmp_path / "code"
    code.mkdir()
    (code / "01_analyze.py").write_text("x = 1\n", encoding="utf-8")
    (code / "README.md").write_text(
        "Download the logs ([OSF](https://osf.io/cwd6h/))\n", encoding="utf-8")

    o = Orchestrator(workroot=tmp_path / "wk")
    report = o.run(str(code), use_llm=False, dry_run=True)

    note = " ".join(report.detect["notes"])
    assert "data_sources" in note and "https://osf.io/cwd6h/" in note
    assert "--data https://osf.io/cwd6h/" in note
    assert any("linked in prose only" in x for x in report.not_verified)


def test_no_hint_once_a_data_source_was_supplied(tmp_path):
    code = tmp_path / "code"
    code.mkdir()
    (code / "01_analyze.py").write_text("x = 1\n", encoding="utf-8")
    (code / "README.md").write_text("Data: https://osf.io/cwd6h/\n", encoding="utf-8")
    deposit = tmp_path / "deposit"
    deposit.mkdir()
    (deposit / "p01.csv").write_text("a\n", encoding="utf-8")

    o = Orchestrator(workroot=tmp_path / "wk")
    report = o.run(str(code), use_llm=False, dry_run=True, data_sources=[str(deposit)])

    assert not any("no way to fetch it" in n for n in report.detect["notes"])


def test_submission_dir_never_starts_with_a_dash():
    """The slug keeps the TAIL of the ref — that is what distinguishes one
    submission from another — but the cut lands mid-token and used to re-expose a
    leading "-". A directory named "-github-com-..." is read as flags by every
    tool that later touches work/ (`rm -github-com-...`)."""
    sid = orch_mod.submission_id("https://github.com/ammarjamal/ARena")
    assert not sid.startswith("-")
    assert "github-com-ammarjamal-arena" in sid
    # the tail is still what survives truncation, not the shared host prefix
    long_sid = orch_mod.submission_id("https://github.com/some-very-long-org-name/the-repo")
    assert long_sid.startswith("the") or "the-repo" in long_sid
    assert not long_sid.startswith("-")


def test_downloads_are_not_shared_between_submissions_by_default(tmp_path, spy_run_container):
    """The cache crosses submission boundaries, so it is opt-in. Default runs must
    mount nothing but the run dir."""
    RMIX = Path(__file__).resolve().parents[1] / "fixtures" / "notebook-r-mix"
    _run(tmp_path, RMIX)
    install = [c for c in spy_run_container if c["spec"].network == "egress"]
    assert install, "no egress install container was launched"
    for c in install:
        assert [m.target for m in c["spec"].mounts] == ["/work"]
        assert "PIP_CACHE_DIR" not in " ".join(c["spec"].command)


def test_reuse_downloads_mounts_one_cache_and_says_so(tmp_path, spy_run_container):
    """Re-running one artifact re-downloaded gigabytes it already had, because the
    caches live under HOME=/work — inside the run dir, which is wiped every run."""
    RMIX = Path(__file__).resolve().parents[1] / "fixtures" / "notebook-r-mix"
    report = _run(tmp_path, RMIX, reuse_downloads=True)
    install = [c for c in spy_run_container if c["spec"].network == "egress"]
    assert install, "no egress install container was launched"
    for c in install:
        targets = [m.target for m in c["spec"].mounts]
        assert "/reprobe-cache" in targets
        cmd = " ".join(c["spec"].command)
        assert "PIP_CACHE_DIR=/reprobe-cache/pip" in cmd
        assert "MAMBA_PKGS_DIRS=/reprobe-cache/conda" in cmd
    assert (tmp_path / ".package-cache" / "pip").is_dir()
    # the report must not let a cached install pass for a fresh one
    assert any("shared with other submissions" in w
               for w in report.environment.get("warnings", []))


def test_a_failed_code_fetch_still_reports_what_the_data_deposit_says(tmp_path, monkeypatch):
    """The composite artifact: code in git, data in a deposit. When the code half
    cannot be fetched the run stops there — but "the data is deposited and its
    embargo lifts on 2026-09-21" is the availability finding that survives, and it
    is the opposite conclusion from "nothing was ever deposited". Dropping it left
    the chair with neither."""
    import reprobe.fetch.data_source as ds

    monkeypatch.setattr(ds, "probe_data_source", lambda url: {
        "input": url, "status": "embargoed",
        "detail": "Zenodo record 21095524 exists but is embargoed until 2026-09-21"})

    report = _run(tmp_path, str(tmp_path / "no-such-repo"),
                  data_sources=["https://zenodo.org/records/21095524"])
    assert report.verdict["overall"] == "fetch-failed"
    records = report.source["data_sources"]
    assert [r["status"] for r in records] == ["embargoed"]
    assert any("2026-09-21" in n for n in report.not_verified)

    # and it must reach the rendered report, not just the JSON
    outdir = tmp_path / report.submission_id / "report"
    for name in ("report.md", "report.html"):
        assert "2026-09-21" in (outdir / name).read_text(encoding="utf-8"), name


def test_no_data_probe_when_no_deposit_was_supplied(tmp_path):
    report = _run(tmp_path, str(tmp_path / "no-such-repo"))
    assert report.verdict["overall"] == "fetch-failed"
    assert "data_sources" not in report.source


def test_a_dry_run_report_says_it_is_a_dry_run(tmp_path, spy_run_container):
    """A dry run once wrote "ran to completion (exit 0)", verdict `runs` and a
    Functional candidate — a fabricated result that `batch --resume` then reused."""
    report = _run(tmp_path, EXAMPLE)
    assert report.run_mode == "dry-run"
    assert report.verdict["overall"] == "not-run" and "dry run" in report.verdict["note"]
    assert report.badges["acm"]["functional"] == "not-evaluated"
    executed = [s for s in report.steps if s.runner != "unity"]
    assert executed and all(s.status == "skipped" and not s.executed and not s.claims
                            for s in executed)
    assert spy_run_container, "the argv must still be built and checked"


def test_symlinks_out_of_the_tree_never_reach_the_run_dir(tmp_path, monkeypatch):
    """copytree follows links by default: a committed `leak -> ~/.aws` copied the
    host directory into the sandbox, where the install phase has network."""
    import os

    secret = tmp_path / "host-secret"
    secret.mkdir()
    (secret / "id_rsa").write_text("PRIVATE", encoding="utf-8")
    src = tmp_path / "artifact"
    src.mkdir()
    (src / "run.py").write_text("print(1)\n", encoding="utf-8")
    (src / ".reprobe_deps").mkdir()
    (src / ".reprobe_deps" / "sitecustomize.py").write_text("import os\n", encoding="utf-8")
    try:
        os.symlink(secret, src / "leak", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not creatable on this platform/user")

    from reprobe.models import Report
    o = Orchestrator(workroot=tmp_path / "wk")
    report = Report(submission_id="x", harness_version="t", timestamp="")
    rundir = tmp_path / "wk" / "run"
    o._copy_run_tree(src, rundir, report)
    assert (rundir / "run.py").is_file()
    assert not (rundir / "leak").exists() and not (rundir / "leak").is_symlink()
    assert not (rundir / ".reprobe_deps").exists()
    warning = " ".join(report.environment["warnings"])
    assert "leak" in warning and ".reprobe_deps" in warning


def test_install_failures_are_attributed_to_whoever_asked(tmp_path, monkeypatch):
    """One exit code for the whole install chain could not say whether the
    artifact's requirements.txt failed or a package reprobe guessed did."""
    from reprobe.models import EnvPlan, RawRunOutput, Report
    results = iter([("ERROR: No matching distribution found for itertools\n", 1),
                    ("pandas==2.2.0\n", 0)])

    def install(spec, log_path):
        text, code = next(results)
        Path(log_path).write_text(text, encoding="utf-8")
        return RawRunOutput(exit_code=code, duration_s=1.0, image=spec.image, log_path=str(log_path))

    _simulate(monkeypatch, run_phase=None, install_phase=install)
    o = Orchestrator(workroot=tmp_path)
    plan = EnvPlan(image="img", install_commands=["python -I -m pip install -r requirements.txt",
                                                  "python -I -m pip freeze"],
                   install_origins=["author-declared", "harness-record"])
    report = Report(submission_id="x", harness_version="t", timestamp="")
    rundir, logdir = tmp_path / "run", tmp_path / "logs"
    rundir.mkdir()
    logdir.mkdir()
    o._install_phase(plan, rundir, logdir, install=True, dry_run=False, report=report)

    row = report.environment["install_results"]["python"]
    assert not row["ok"] and row["failures"][0]["origin"] == "author-declared"
    from reprobe.report import badges
    assert badges.install_error(report.environment) is None
    assert "requirements.txt" in badges.declared_env_error(report.environment)
    assert any("declared environment (python) did not install" in n for n in report.not_verified)
    # only the version listing feeds the digest, so it is stable across runs
    assert report.environment["resolved_deps_digest"].startswith("sha256:")


def test_resolved_deps_digest_ignores_the_volatile_parts_of_the_log(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d, name in ((a, "reprobe-111"), (b, "reprobe-222")):
        d.mkdir()
        (d / "install-python.log").write_text(
            f"$ docker run --name {name} -v {d}:/work ...\n\n"
            "[reprobe-install] begin 0 author-declared\nCollecting pandas (took 3.2s)\n"
            "[reprobe-install] begin 1 harness-record\npandas==2.2.0\nnumpy==2.0.1\n",
            encoding="utf-8")
    assert orch_mod._hash_resolved_versions(a) == orch_mod._hash_resolved_versions(b)


def test_regenerated_committed_outputs_are_compared_with_the_authors_copy(tmp_path, monkeypatch):
    from reprobe.models import RawRunOutput

    art = tmp_path / "artifact"
    (art / "results").mkdir(parents=True)
    (art / "analyze.py").write_text("print(1)\n", encoding="utf-8")
    (art / "results" / "means.csv").write_text("group,mean\na,3.2100000000\nb,4.5\n", encoding="utf-8")
    (art / "results" / "counts.csv").write_text("group,n\na,35\nb,40\n", encoding="utf-8")

    def rerun(spec, log_path):
        work = Path(spec.mounts[0].source)
        # float noise in one file, a real change in the other
        (work / "results" / "means.csv").write_text("group,mean\na,3.2100000000001\nb,4.5\n",
                                                    encoding="utf-8")
        (work / "results" / "counts.csv").write_text("group,n\na,35\nb,41\n", encoding="utf-8")
        Path(log_path).write_text("done\n", encoding="utf-8")
        return RawRunOutput(exit_code=0, duration_s=0.1, image=spec.image, log_path=str(log_path))

    _simulate(monkeypatch, rerun)
    report = _run(tmp_path / "wk", art, dry_run=False)
    rows = {r["path"]: r["status"] for r in report.results_comparison["files"]}
    assert rows == {"results/counts.csv": "differs", "results/means.csv": "equivalent"}
    assert report.badges["acm"]["results_reproduced"] == "not-evaluated"   # evidence, not a badge
    assert any("regenerated with different values" in n for n in report.not_verified)


def _manifest_artifact(tmp_path, source):
    art = tmp_path / "artifact"
    art.mkdir()
    (art / "run.py").write_text("print(1)\n", encoding="utf-8")
    (art / "autoui-repro.yml").write_text(
        f"version: 1\ndata_sources:\n  - {{ source: {source!r}, into: loot }}\nrun:\n  steps: [run.py]\n",
        encoding="utf-8")
    return art


def test_a_manifest_cannot_pull_a_host_directory_into_the_sandbox(tmp_path):
    """`data_sources: [{source: <host path>}]` once reached the catch-all local
    fetcher, copying the chair's own directory into a tree whose install phase has
    network. Only the operator's --data may name a local path."""
    secret = tmp_path / "host-secret"
    secret.mkdir()
    (secret / "id_rsa").write_text("PRIVATE", encoding="utf-8")
    art = _manifest_artifact(tmp_path, str(secret))

    report = Orchestrator(workroot=tmp_path / "wk").run(str(art), use_llm=False, dry_run=True)
    src = tmp_path / "wk" / report.submission_id / "src"
    run = tmp_path / "wk" / report.submission_id / "run"
    assert not (src / "loot").exists() and not (run / "loot").exists()
    rec = report.source["data_sources"][0]
    assert rec["status"] == "refused" and rec["declared_by"] == "author's manifest"
    assert any("supply it yourself with `--data`" in w for w in report.source["warnings"])


@pytest.mark.parametrize("source", ["git@github.com:a/b.git", "ssh://git@host/x.git",
                                    "http://169.254.169.254/latest", ".", "..", "file:///etc"])
def test_manifest_sources_that_reach_credentials_or_internals_are_refused(tmp_path, source):
    art = _manifest_artifact(tmp_path, source)
    report = Orchestrator(workroot=tmp_path / "wk").run(str(art), use_llm=False, dry_run=True)
    assert report.source["data_sources"][0]["status"] == "refused"


def test_the_operators_own_data_flag_may_still_name_a_local_directory(tmp_path):
    deposit = tmp_path / "deposit"
    deposit.mkdir()
    (deposit / "p01.csv").write_text("a\n", encoding="utf-8")
    art = tmp_path / "artifact"
    art.mkdir()
    (art / "run.py").write_text("print(1)\n", encoding="utf-8")
    report = Orchestrator(workroot=tmp_path / "wk").run(
        str(art), use_llm=False, dry_run=True, data_sources=[f"{deposit}::dataset"])
    rec = report.source["data_sources"][0]
    assert rec["status"] == "ok" and rec["declared_by"] == "operator"


@pytest.mark.parametrize("target", ["/usr/lib/python3/this.py", "../outside.py", "missing.py",
                                    "-mrunpy.py", "link_out.py"])
def test_a_manifest_step_must_name_a_file_inside_the_deposit(tmp_path, monkeypatch, target):
    """A step outside the deposit would run someone else's code and pass; a
    missing one would fail with an error that reads like the artifact's own."""
    art = tmp_path / "artifact"
    art.mkdir()
    (art / "run.py").write_text("print(1)\n", encoding="utf-8")
    (tmp_path / "outside.py").write_text("print(1)\n", encoding="utf-8")
    try:
        (art / "link_out.py").symlink_to(tmp_path / "outside.py")
    except OSError:
        if target == "link_out.py":
            pytest.skip("symlinks need privileges on this platform")
    (art / "autoui-repro.yml").write_text(
        f"version: 1\nrun:\n  steps: [{target!r}]\n", encoding="utf-8")
    launched = []
    _simulate(monkeypatch, lambda spec, log: launched.append(spec))

    report = Orchestrator(workroot=tmp_path / "wk").run(str(art), use_llm=False)
    assert launched == []                                   # no container for the step
    [step] = report.steps
    assert step.status == "skipped" and step.executed is False
    assert report.verdict["overall"] == "nothing-executed"
    assert target.lstrip("-") in report.verdict["note"]


def test_a_refused_step_beside_a_passing_one_is_not_a_clean_run(tmp_path, monkeypatch):
    from reprobe.models import RawRunOutput
    art = tmp_path / "artifact"
    art.mkdir()
    (art / "run.py").write_text("print(1)\n", encoding="utf-8")
    (art / "autoui-repro.yml").write_text(
        "version: 1\nrun:\n  steps: [run.py, typo.py]\n", encoding="utf-8")

    def ok(spec, log):
        Path(log).write_text("1\n", encoding="utf-8")
        return RawRunOutput(exit_code=0, duration_s=0.1, image=spec.image, log_path=str(log))
    _simulate(monkeypatch, ok)

    report = Orchestrator(workroot=tmp_path / "wk").run(str(art), use_llm=False)
    assert [s.status for s in report.steps] == ["pass", "skipped"]
    assert report.verdict["overall"] == "runs-with-warnings"
    assert "typo.py" in report.verdict["note"] and "does not exist" in report.verdict["note"]


@pytest.mark.parametrize("reuse", [False, True])
def test_renv_restores_copies_not_links_into_a_cache_the_run_cannot_see(tmp_path, spy_run_container,
                                                                        reuse):
    """renv symlinks the project library into its global cache; that cache is in
    the install container's HOME (or the --reuse-downloads mount), and the run
    phase sees neither — every restored package was a dangling link."""
    RENV = Path(__file__).resolve().parents[1] / "e2e" / "fixtures" / "r-renv"
    _run(tmp_path, RENV, reuse_downloads=reuse)
    [install] = [c for c in spy_run_container if c["spec"].network == "egress"]
    script = install["spec"].command[-1]
    assert "renv::restore" in script
    assert "export RENV_CONFIG_CACHE_SYMLINKS=FALSE;" in script
    assert ("RENV_CONFIG_CACHE_ENABLED=FALSE" in script) is (not reuse)


def _rstudio_artifact(tmp_path):
    art = tmp_path / "artifact"
    art.mkdir()
    (art / "analysis.R").write_text(
        "setwd(dirname(rstudioapi::getActiveDocumentContext()$path))\nwriteLines('1', 'out.txt')\n",
        encoding="utf-8")
    (art / "autoui-repro.yml").write_text(
        "version: 1\nrun:\n  steps: [analysis.R]\nexpected_outputs: [out.txt]\n", encoding="utf-8")
    return art


def _rstudio_run(spec, log, assisted_outcome):
    """First attempt: R's error outside the IDE. The assisted retry: whatever the test says."""
    from reprobe.models import RawRunOutput
    work = Path(spec.mounts[0].source)
    if "harness-assisted retry" not in spec.command[-1]:
        Path(log).write_text("Error: RStudio not running\nExecution halted\n", encoding="utf-8")
        return RawRunOutput(exit_code=1, duration_s=0.1, image=spec.image, log_path=str(log))
    text, code = assisted_outcome
    if code == 0:
        (work / "out.txt").write_text("1\n", encoding="utf-8")
    Path(log).write_text(text, encoding="utf-8")
    return RawRunOutput(exit_code=code, duration_s=0.1, image=spec.image, log_path=str(log))


def test_an_rstudio_only_script_is_retried_with_the_ide_answer_but_still_fails(tmp_path, monkeypatch):
    """The deposit does not run with Rscript, and the verdict says so. The retry
    only adds whether the IDE call was the sole blocker — in a scratch copy, so
    nothing it writes reaches the run directory or a later step."""
    art = _rstudio_artifact(tmp_path)
    _simulate(monkeypatch, lambda spec, log: _rstudio_run(spec, log, ("done\n", 0)))
    report = Orchestrator(workroot=tmp_path / "wk").run(str(art), use_llm=False)
    [step] = report.steps
    assert step.status == "fail" and step.expected_met == []
    assert report.verdict["overall"] == "runs-with-failures"
    retry = step.diagnostics["assisted_retry"]
    assert retry["status"] == "pass" and retry["counted_in_verdict"] is False
    assert retry["expected_met"] == ["out.txt"]
    assert "the only thing found in the way" in retry["summary"]
    run = tmp_path / "wk" / report.submission_id / "run"
    assert not (run / "out.txt").exists()
    assert not (run.parent / "run-assisted").exists()
    md = (tmp_path / "wk" / report.submission_id / "report" / "report.md").read_text(encoding="utf-8")
    assert "Assisted retry:" in md


def test_an_assisted_retry_that_fails_next_names_the_next_problem(tmp_path, monkeypatch):
    art = _rstudio_artifact(tmp_path)
    nxt = ("Error in file(file, \"rt\") : cannot open the connection\n"
           "In addition: Warning message:\nIn file(file, \"rt\") :\n"
           "  cannot open file 'data/raw.csv': No such file or directory\nExecution halted\n", 1)
    _simulate(monkeypatch, lambda spec, log: _rstudio_run(spec, log, nxt))
    report = Orchestrator(workroot=tmp_path / "wk").run(str(art), use_llm=False)
    retry = report.steps[0].diagnostics["assisted_retry"]
    assert retry["status"] == "fail" and "not the only problem" in retry["summary"]
    assert "next_diagnosis" in retry


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
def test_the_retry_copy_skips_special_files_author_code_left_behind(tmp_path):
    """A FIFO planted in the run dir would block or fail copytree and crash the
    run with no report."""
    from reprobe.orchestrator import _special_files
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    (tmp_path / "d").mkdir()
    os.mkfifo(tmp_path / "pipe")
    assert _special_files(str(tmp_path), ["f.txt", "d", "pipe"]) == {"pipe"}


def _fake_containers(monkeypatch, step_phase):
    """Like _simulate, but tells the install container by its marker, not its
    network: under --allow-net the steps have network too."""
    from reprobe.models import RawRunOutput
    specs = []

    def fake(spec, limits, log_path, **kw):
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        specs.append(spec)
        if "reprobe-home" in spec.command[-1]:              # the install phase's own HOME
            Path(log_path).write_text("installed\n", encoding="utf-8")
            return RawRunOutput(exit_code=0, duration_s=0.1, image=spec.image, log_path=str(log_path))
        return step_phase(spec, log_path)

    monkeypatch.setattr(orch_mod, "run_container", fake)
    monkeypatch.setattr(orch_mod, "reap_orphans", lambda: [])
    monkeypatch.setattr(orch_mod, "_image_digest", lambda image: None)
    return specs


def test_the_assisted_retry_keeps_allow_net(tmp_path, monkeypatch):
    """It rebuilt its spec from the runner, offline, so a script that needed the
    network the operator had granted 'still failed' — on the network."""
    art = _rstudio_artifact(tmp_path)
    specs = _fake_containers(monkeypatch, lambda spec, log: _rstudio_run(spec, log, ("done\n", 0)))
    report = Orchestrator(workroot=tmp_path / "wk").run(str(art), use_llm=False, allow_net=["example.org"])
    retried = [s for s in specs if "harness-assisted retry" in s.command[-1]]
    assert retried and retried[0].network == "egress"
    assert report.steps[0].diagnostics["assisted_retry"]["status"] == "pass"


def test_a_harness_failure_during_the_retry_is_not_the_scripts(tmp_path, monkeypatch):
    from reprobe.models import RawRunOutput
    art = _rstudio_artifact(tmp_path)

    def phase(spec, log):
        if "harness-assisted retry" in spec.command[-1]:
            Path(log).write_text("", encoding="utf-8")
            return RawRunOutput(exit_code=None, duration_s=0.0, image=spec.image, log_path=str(log),
                                error="image-not-present")
        return _rstudio_run(spec, log, ("", 0))
    _fake_containers(monkeypatch, phase)
    report = Orchestrator(workroot=tmp_path / "wk").run(str(art), use_llm=False)
    retry = report.steps[0].diagnostics["assisted_retry"]
    assert retry["status"] == "harness-error"
    assert "says nothing about the script" in retry["summary"]
    assert "not the only problem" not in retry["summary"]


def test_an_r_install_never_runs_in_the_generic_python_fallback(tmp_path, monkeypatch):
    """With the pinned Python image missing, R installs went to the Python
    fallback image, failed on a missing Rscript, and read as the author's."""
    art = tmp_path / "artifact"
    art.mkdir()
    (art / "a.R").write_text("library(praise)\n", encoding="utf-8")
    (art / "b.py").write_text("print(1)\n", encoding="utf-8")
    (art / "autoui-repro.yml").write_text("version: 1\nrun:\n  steps: [a.R, b.py]\n", encoding="utf-8")
    from reprobe.models import RawRunOutput
    specs = _fake_containers(monkeypatch, lambda spec, log: RawRunOutput(
        exit_code=0, duration_s=0.1, image=spec.image, log_path=str(log)))
    o = Orchestrator(workroot=tmp_path / "wk")
    real_plan = orch_mod.plan_env

    def fallback(*a, **k):
        p = real_plan(*a, **k)
        return p.model_copy(update={"env_provenance": "fallback-generic", "image": "python:3.13-slim"})
    monkeypatch.setattr(orch_mod, "plan_env", fallback)
    o.run(str(art), use_llm=False)
    r_installs = [s for s in specs if "reprobe-home" in s.command[-1] and "Rscript" in s.command[-1]]
    assert r_installs and all(s.image != "python:3.13-slim" for s in r_installs)
