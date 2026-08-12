"""Locating the paper an artifact reproduces, and the advisory comparison role.

Pure: no network, no Docker, no LLM. Network paths are exercised with stubbed
session/API responses so the SSRF and cap behaviour is asserted, not assumed.
"""


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

    def generate_json(self, prompt, system=None):
        self.prompt = prompt
        return self.payload


def test_compare_results_normalizes_unknown_verdicts():
    from reprobe.llm import roles
    c = _Client({"claims": [
        {"claim": "F test", "verdict": "definitely-matches"},     # not in the vocabulary
        {"claim": "p value", "verdict": "match"},
        {"no_claim_key": 1},                                      # dropped
    ], "confidence": 0.9})
    out = roles.compare_results(c, paper="p", produced="q", coverage="full text")
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
