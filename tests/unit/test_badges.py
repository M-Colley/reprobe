from reprobe.config import load_config
from reprobe.models import DetectResult, FetchResult, Pin, RunResult
from reprobe.report import badges


def _decide(fetch, steps, functional_requested=True, ran=True):
    cfg = load_config()
    detect = DetectResult(artifact_types=["python"])
    return badges.decide(fetch, steps, detect, badges_cfg=cfg.badges,
                         functional_requested=functional_requested, ran=ran)


def test_git_sha_is_not_archival_available_is_candidate():
    f = FetchResult(input="gh", resolved_type="git", src_dir="/x",
                    pin=Pin(kind="git_sha", value="abc123"))
    out = _decide(f, [])
    assert out["acm"]["available"] == "candidate"
    assert any("archival" in n or "Software Heritage" in n for n in out["acm"]["notes"])


def test_version_doi_with_checksum_grants_available():
    f = FetchResult(input="zenodo", resolved_type="zenodo", src_dir="/x",
                    pin=Pin(kind="version_doi", value="10.5281/zenodo.1"), checksum_verified=True)
    out = _decide(f, [])
    assert out["acm"]["available"] == "granted"


def test_functional_is_candidate_not_granted_when_passing():
    f = FetchResult(input="zenodo", resolved_type="zenodo", src_dir="/x",
                    pin=Pin(kind="version_doi", value="10.5281/zenodo.1"), checksum_verified=True)
    step = RunResult(runner="python", target="a.py", status="pass", expected_met=["out.csv"])
    out = _decide(f, [step])
    assert out["acm"]["functional"] == "candidate"   # never auto-"granted"


def test_functional_not_evaluated_when_opt_out():
    f = FetchResult(input="zenodo", resolved_type="zenodo", src_dir="/x",
                    pin=Pin(kind="version_doi", value="10.5281/zenodo.1"), checksum_verified=True)
    step = RunResult(runner="python", target="a.py", status="pass", expected_met=["out.csv"])
    out = _decide(f, [step], functional_requested=False)
    assert out["acm"]["functional"] == "not-evaluated"
