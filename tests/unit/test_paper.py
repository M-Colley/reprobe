"""Locating the paper an artifact reproduces, and the advisory comparison role.

Pure: no network, no Docker, no LLM. Network paths are exercised with stubbed
session/API responses so the SSRF and cap behaviour is asserted, not assumed.
"""


import os

import pytest

from reprobe import paper as P

# --- DOI discovery -----------------------------------------------------------

def test_doi_from_citation_cff(tmp_path):
    (tmp_path / "CITATION.cff").write_text(
        'cff-version: 1.2.0\ntitle: Shed Some Fear\ndoi: 10.1145/3613904.3641909\n')
    assert P.find_doi(tmp_path) == "10.1145/3613904.3641909"


def test_doi_from_readme_link_is_trimmed(tmp_path):
    (tmp_path / "README.md").write_text(
        "Paper: [10.1145/3613904.3641909](https://doi.org/10.1145/3613904.3641909).\n")
    assert P.find_doi(tmp_path) == "10.1145/3613904.3641909"   # trailing ) and . stripped


def test_manifest_doi_wins_over_readme(tmp_path):
    (tmp_path / "README.md").write_text("10.9999/readme.one\n")
    got = P.find_doi(tmp_path, {"paper": {"doi": "10.1145/manifest.wins"}})
    assert got == "10.1145/manifest.wins"


def test_malformed_manifest_doi_is_ignored(tmp_path):
    (tmp_path / "README.md").write_text("10.9999/readme.one\n")
    # not DOI-shaped -> fall through to the README rather than trust it
    assert P.find_doi(tmp_path, {"paper": {"doi": "not a doi; rm -rf /"}}) == "10.9999/readme.one"


def test_no_doi_anywhere(tmp_path):
    (tmp_path / "README.md").write_text("no identifiers here\n")
    assert P.find_doi(tmp_path) is None


def test_pin_doi_is_the_last_resort(tmp_path):
    assert P.find_doi(tmp_path, {}, pin_value="10.5281/zenodo.123") == "10.5281/zenodo.123"


# --- PDF discovery -----------------------------------------------------------

def test_pdf_preference_prefers_paper_like_shallow_file(tmp_path):
    (tmp_path / "figures").mkdir()
    (tmp_path / "figures" / "fig1.pdf").write_bytes(b"%PDF-1.4 fig")
    (tmp_path / "appendix.pdf").write_bytes(b"%PDF-1.4 appendix that is quite large" * 10)
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.4 the manuscript")
    assert P.find_pdf(tmp_path).name == "paper.pdf"


def test_manifest_pdf_wins(tmp_path):
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.4 a")
    (tmp_path / "chosen.pdf").write_bytes(b"%PDF-1.4 b")
    assert P.find_pdf(tmp_path, {"paper": {"pdf": "chosen.pdf"}}).name == "chosen.pdf"


def test_no_pdf_returns_none(tmp_path):
    assert P.find_pdf(tmp_path) is None


def test_oversized_pdf_is_refused_not_parsed(tmp_path, monkeypatch):
    big = tmp_path / "paper.pdf"
    big.write_bytes(b"%PDF-1.4" + b"x" * 4096)
    monkeypatch.setattr(P, "_MAX_PDF_BYTES", 100)
    text, warns = P.pdf_text(big)
    assert text == "" and any("over the" in w and "cap" in w for w in warns)


# --- excerpt selection -------------------------------------------------------

def test_excerpt_keeps_the_statistics_not_just_the_intro():
    # A real paper's opening pages hold no numbers; naive truncation would send
    # the intro and drop every comparable value.
    intro = "Introduction and related work. " * 400          # ~12k chars, no stats
    stats = ("The ART found a significant main effect (F(1,16) = 11.12, p = 0.004, "
             "eta_p^2 = 0.41, M = 3.2, SD = 1.1). ")
    text = "TITLE: x\nABSTRACT: y\n" + intro + stats * 5 + intro
    p = P.Paper(source="repo-pdf", ref="paper.pdf", text=text)
    ex = p.excerpt(budget=4000)
    assert len(ex) <= 4200
    assert "F(1,16) = 11.12" in ex, "the results passage was dropped"
    assert ex.startswith("TITLE: x")                          # opening kept


def test_excerpt_is_a_noop_when_short():
    p = P.Paper(source="repo-pdf", ref="p.pdf", text="short paper")
    assert p.excerpt(budget=8000) == "short paper"


# --- DOI acquisition (stubbed API) -------------------------------------------

def _openalex_payload(oa_url=None, abstract=True):
    inv = {"Gaze": [0], "interaction": [1], "works": [2]} if abstract else None
    return {"title": "A Paper", "open_access": {"oa_url": oa_url},
            "abstract_inverted_index": inv}


def test_doi_text_falls_back_to_abstract_when_oa_pdf_is_forbidden(tmp_path, monkeypatch):
    # the motivating real case: ACM advertises an OA PDF that then 403s a script
    monkeypatch.setattr(P, "_openalex",
                        lambda doi: _openalex_payload("https://dl.acm.org/doi/pdf/10.1145/x"))
    monkeypatch.setattr(P, "assert_safe_url", lambda u: "dl.acm.org")
    monkeypatch.setattr(P, "download", lambda *a, **k: (False, "download failed: 403 Forbidden"))
    p = P.doi_text("10.1145/3613904.3641909", tmp_path)
    assert p.source == "doi-abstract"
    assert "ABSTRACT ONLY" in p.coverage
    assert "Gaze interaction works" in p.text
    assert any("did not return a PDF" in w for w in p.warnings)


def test_doi_text_refuses_an_internal_oa_url(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "_openalex", lambda doi: _openalex_payload("http://169.254.169.254/x.pdf"))
    monkeypatch.setattr(P, "download", lambda *a, **k: pytest.fail("must not download"))
    p = P.doi_text("10.1145/x", tmp_path)
    assert p.source == "doi-abstract"
    assert any("refused the open-access link" in w for w in p.warnings)


def test_doi_text_rejects_malformed_doi(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "_openalex", lambda doi: pytest.fail("must not query the API"))
    p = P.doi_text("not-a-doi", tmp_path)
    assert p.text == "" and any("malformed DOI" in w for w in p.warnings)


def test_doi_text_survives_api_failure(tmp_path, monkeypatch):
    def boom(doi):
        raise OSError("network down")
    monkeypatch.setattr(P, "_openalex", boom)
    p = P.doi_text("10.1145/x", tmp_path)
    assert p.text == "" and "no paper text" in p.coverage


def test_locate_returns_none_without_pdf_or_doi(tmp_path):
    (tmp_path / "README.md").write_text("nothing here\n")
    assert P.locate(tmp_path, tmp_path) is None


# --- the advisory role -------------------------------------------------------

class _Client:
    confidence_threshold = 0.6

    def __init__(self, payload):
        self.payload = payload
        self.prompt = ""

    def generate_json(self, prompt, system=None, schema=None):
        self.prompt = prompt
        return self.payload


def test_compare_results_normalizes_unknown_verdicts():
    from reprobe.llm import roles
    c = _Client({"claims": [
        {"claim": "F test", "verdict": "definitely-matches"},     # not in the vocabulary
        {"claim": "p value", "verdict": "match", "produced_value": "0.04"},
        {"no_claim_key": 1},                                      # dropped
    ], "confidence": 0.9})
    out = roles.compare_results(c, paper="p", produced="p = 0.04", coverage="full text")
    assert [x["verdict"] for x in out["claims"]] == ["unclear", "match"]
    assert out["is_advisory"] is True and out["meets_threshold"] is True


def test_compare_results_fences_both_untrusted_inputs():
    from reprobe.llm import prompts, roles
    c = _Client({"claims": [{"claim": "x", "verdict": "match"}]})
    roles.compare_results(c, paper="PAPER-TEXT", produced="RUN-OUTPUT", coverage="full text")
    assert c.prompt.count(prompts.UNTRUSTED_OPEN) == 2      # paper AND produced output
    assert "PAPER-TEXT" in c.prompt and "RUN-OUTPUT" in c.prompt


def test_compare_results_returns_none_on_junk():
    from reprobe.llm import roles
    assert roles.compare_results(_Client({"nope": 1}), paper="p", produced="q",
                                 coverage="c") is None
    assert roles.compare_results(_Client({"claims": []}), paper="p", produced="q",
                                 coverage="c") is None


def test_a_deposits_figures_are_never_mistaken_for_the_paper(tmp_path):
    """Merging a data deposit into the tree put its PDFs in reach of the paper
    finder, and a figure from OSF was reported as "full text of ... (committed in
    the repository)" — wrong twice: not the paper, and not committed."""
    from reprobe.paper import find_pdf

    (tmp_path / "dataset" / "HeatMap").mkdir(parents=True)
    fig = tmp_path / "dataset" / "HeatMap" / "gaze_entropy_annotated_OSF_35.pdf"
    fig.write_bytes(b"%PDF-1.4 figure")

    merged = {"dataset/HeatMap/gaze_entropy_annotated_OSF_35.pdf"}
    assert find_pdf(tmp_path, exclude=merged) is None
    assert find_pdf(tmp_path) == fig            # without the exclusion it is picked

    # the submission's own PDF still wins over anything the deposit brought
    own = tmp_path / "paper.pdf"
    own.write_bytes(b"%PDF-1.4 manuscript")
    assert find_pdf(tmp_path, exclude=merged) == own


def test_declared_paper_pdf_wins_even_inside_a_deposit_path(tmp_path):
    """An author who explicitly points at a PDF has made a statement; the
    exclusion is a heuristic and must not override it."""
    from reprobe.paper import find_pdf

    (tmp_path / "dataset").mkdir()
    p = tmp_path / "dataset" / "manuscript.pdf"
    p.write_bytes(b"%PDF-1.4")
    assert find_pdf(tmp_path, {"paper": {"pdf": "dataset/manuscript.pdf"}},
                    exclude={"dataset/manuscript.pdf"}) == p


# --------------------------------------------------------------------------- #
# DOI discovery: badges are images, a CFF names the software unless told otherwise
# --------------------------------------------------------------------------- #

def test_a_zenodo_badge_image_is_not_part_of_its_doi(tmp_path):
    (tmp_path / "README.md").write_text(
        "[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.1234567.svg)]"
        "(https://doi.org/10.5281/zenodo.1234567)\n", encoding="utf-8")
    assert P.find_doi(tmp_path) == "10.5281/zenodo.1234567"


def test_a_shields_badge_colour_is_not_part_of_its_doi(tmp_path):
    (tmp_path / "README.md").write_text(
        "![paper](https://img.shields.io/badge/DOI-10.1145/3613904.3641909-blue.svg)\n",
        encoding="utf-8")
    assert P.find_doi(tmp_path) == "10.1145/3613904.3641909"


def test_the_paper_doi_beats_the_code_deposit_badge_above_it(tmp_path):
    """A README's first DOI is usually the Zenodo badge for the CODE, which is the
    wrong thing to compare results against."""
    (tmp_path / "README.md").write_text(
        "[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.99.svg)](https://doi.org/10.5281/zenodo.99)\n"
        "Paper: https://doi.org/10.1145/3613904.3641909\n", encoding="utf-8")
    assert P.find_doi(tmp_path) == "10.1145/3613904.3641909"


def test_a_closing_paren_belongs_to_the_doi_only_when_it_closes_one(tmp_path):
    (tmp_path / "README.md").write_text(
        "See the study (doi 10.1016/S0140-6736(20)30183-5).\n", encoding="utf-8")
    assert P.find_doi(tmp_path) == "10.1016/S0140-6736(20)30183-5"


def test_cff_preferred_citation_is_the_paper_not_the_software(tmp_path):
    (tmp_path / "CITATION.cff").write_text(
        "cff-version: 1.2.0\ntitle: tool\ndoi: 10.5281/zenodo.99\n"
        "preferred-citation:\n  type: article\n  title: the paper\n  doi: 10.1145/3613904.3641909\n",
        encoding="utf-8")
    assert P.find_doi(tmp_path) == "10.1145/3613904.3641909"


def test_cff_identifiers_of_type_doi_count(tmp_path):
    (tmp_path / "CITATION.cff").write_text(
        "cff-version: 1.2.0\ntitle: tool\nidentifiers:\n"
        "  - type: url\n    value: https://example.org\n"
        "  - type: doi\n    value: 10.1145/3613904.3641909\n", encoding="utf-8")
    assert P.find_doi(tmp_path) == "10.1145/3613904.3641909"


def test_a_broken_cff_falls_back_to_a_plain_scan(tmp_path):
    (tmp_path / "CITATION.cff").write_text("doi: 10.1145/3613904.3641909\n  bad: [unclosed\n",
                                           encoding="utf-8")
    assert P.find_doi(tmp_path) == "10.1145/3613904.3641909"


@pytest.mark.parametrize("spelling", ["doi:10.1145/3613904.3641909", "DOI: 10.1145/3613904.3641909",
                                      "https://www.doi.org/10.1145/3613904.3641909"])
def test_a_manifest_doi_may_use_any_common_spelling(tmp_path, spelling):
    assert P.find_doi(tmp_path, {"paper": {"doi": spelling}}) == "10.1145/3613904.3641909"


# --------------------------------------------------------------------------- #
# A manifest-declared PDF must stay inside the repository
# --------------------------------------------------------------------------- #

def test_a_manifest_cannot_point_the_paper_finder_at_a_host_pdf(tmp_path):
    outside = tmp_path / "private.pdf"
    outside.write_bytes(b"%PDF-1.4 secret")
    repo = tmp_path / "repo"
    repo.mkdir()
    for declared in (str(outside), "../private.pdf", "sub/../../private.pdf"):
        assert P.find_pdf(repo, {"paper": {"pdf": declared}}) is None, declared


def test_a_committed_symlink_cannot_smuggle_a_host_pdf_in(tmp_path):
    outside = tmp_path / "private.pdf"
    outside.write_bytes(b"%PDF-1.4 secret")
    repo = tmp_path / "repo"
    repo.mkdir()
    try:
        os.symlink(outside, repo / "paper.pdf")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not creatable on this platform/user")
    assert P.find_pdf(repo, {"paper": {"pdf": "paper.pdf"}}) is None
    assert P.find_pdf(repo) is None                       # nor by heuristic search


def test_an_in_repo_declared_pdf_is_still_honoured(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "p.pdf").write_bytes(b"%PDF-1.4")
    assert P.find_pdf(tmp_path, {"paper": {"pdf": "docs/p.pdf"}}) == tmp_path / "docs" / "p.pdf"


# --------------------------------------------------------------------------- #
# PDF parsing runs in a time-boxed child process
# --------------------------------------------------------------------------- #

def _pdf_with_text(text: str) -> bytes:
    """A minimal one-page PDF with correct xref offsets."""
    esc = text.replace("\\", "\\\\").replace("(", r"\(").replace(")", r"\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({esc}) Tj ET".encode()
    objs = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
            b"/Resources<</Font<</F1 5 0 R>>>>>>",
            b"<</Length " + str(len(stream)).encode() + b">>\nstream\n" + stream + b"\nendstream",
            b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += (f"trailer\n<</Size {len(objs) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


pypdf = pytest.importorskip("pypdf")


def test_a_real_pdf_is_read_through_the_child_process(tmp_path):
    f = tmp_path / "paper.pdf"
    f.write_bytes(_pdf_with_text("F(1,16) = 11.12, p = .004"))
    text, warnings = P.pdf_text(f)
    assert "11.12" in text and "p = .004" in text
    assert not warnings


def test_a_pdf_that_hangs_the_parser_costs_a_warning_not_the_run(tmp_path, monkeypatch):
    import subprocess

    f = tmp_path / "paper.pdf"
    f.write_bytes(_pdf_with_text("x"))

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(a[0], k.get("timeout", 0))
    monkeypatch.setattr(P.subprocess, "run", hang)
    text, warnings = P.pdf_text(f)
    assert text == "" and any("was stopped" in w and "NOT read" in w for w in warnings)


def test_a_corrupt_pdf_is_a_warning_not_an_exception(tmp_path):
    f = tmp_path / "paper.pdf"
    f.write_bytes(b"%PDF-1.4\n" + os.urandom(2000))
    text, warnings = P.pdf_text(f)
    assert text == "" and any("NOT read" in w for w in warnings)


def test_pdf_parsing_is_isolated_from_the_calling_environment(tmp_path, monkeypatch):
    """`python -I` ignores PYTHON* variables and the cwd, so a hostile
    sitecustomize / pypdf shadow in the working directory is never imported."""
    (tmp_path / "pypdf.py").write_text("raise SystemExit('shadowed pypdf was imported')\n",
                                       encoding="utf-8")
    f = tmp_path / "paper.pdf"
    f.write_bytes(_pdf_with_text("isolated 3.14"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    text, _ = P.pdf_text(f)
    assert "isolated 3.14" in text


def test_a_match_that_quotes_no_number_is_not_evidence():
    from reprobe.llm import roles
    c = _Client({"claims": [{"claim": "p value", "verdict": "match"},
                            {"claim": "effect", "verdict": "mismatch", "produced_value": "large"}],
                 "confidence": 0.9})
    out = roles.compare_results(c, paper="p", produced="p = 0.04", coverage="full text")
    assert [x["verdict"] for x in out["claims"]] == ["unclear", "unclear"]
    assert all(x["why"].startswith("[harness]") for x in out["claims"])
