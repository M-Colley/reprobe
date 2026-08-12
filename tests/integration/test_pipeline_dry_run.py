"""End-to-end pipeline test, daemon-free: the full orchestrator over the local
example fixture with --dry-run containers. Asserts the report artifacts are
written, sandbox flags reach docker argv, and no badge is ever granted for a
local (non-archival) pin."""

from __future__ import annotations

import json
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
    return o.run(str(ref), use_llm=False, dry_run=True, **kw)


def test_example_pipeline_writes_reports_and_never_grants(tmp_path, spy_run_container):
    report = _run(tmp_path, EXAMPLE)
    outdir = tmp_path / report.submission_id / "out"
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
    outdir = tmp_path / report.submission_id / "out"
    for name in ("report.json", "report.md", "report.html"):
        assert (outdir / name).is_file(), f"{name} not written on fetch failure"
    html_text = (outdir / "report.html").read_text(encoding="utf-8")
    assert "fetch failed" in html_text.lower()
    assert "<b>Badges</b>" not in html_text        # nothing fetched -> no badge chips


def test_broken_python_fixture_fails_cleanly(tmp_path, monkeypatch):
    """Dry-run fabricates exit 0, so force a nonzero exit to prove a failing
    step yields a clean FAIL verdict — never a traceback out of run()."""
    real = orch_mod.run_container

    def failing(spec, limits, log_path, **kw):
        raw = real(spec, limits, log_path, **kw)
        return raw.model_copy(update={"exit_code": 1})

    monkeypatch.setattr(orch_mod, "run_container", failing)
    report = _run(tmp_path, BROKEN)
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
        # timeout_s is a subprocess timeout, not an argv flag
        assert c["limits"].get("timeout_s") == 7200


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

    real = orch_mod.run_container
    attempted: list[str] = []

    def fake(spec, limits, log_path, **kw):
        if spec.network == "egress":                  # dependency-install phase
            return real(spec, limits, log_path, **kw)
        attempted.append(spec.image)
        return RawRunOutput(exit_code=125, duration_s=12.5, image=spec.image,
                            log_path=str(log_path),
                            error="docker-daemon-lost: the Docker daemon stopped responding")

    monkeypatch.setattr(orch_mod, "run_container", fake)
    report = _run(tmp_path / "wk", src)

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
    outdir = tmp_path / report.submission_id / "out"
    for name in ("report.md", "report.html"):
        assert "2026-09-21" in (outdir / name).read_text(encoding="utf-8"), name


def test_no_data_probe_when_no_deposit_was_supplied(tmp_path):
    report = _run(tmp_path, str(tmp_path / "no-such-repo"))
    assert report.verdict["overall"] == "fetch-failed"
    assert "data_sources" not in report.source
