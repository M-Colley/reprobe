from reprobe.llm.guard import is_clean, sanitize


def test_rejects_shell_injection_in_structured_fields():
    assert not is_clean({"steps": [{"path": "x.py; rm -rf /"}]})
    assert not is_clean({"steps": [{"path": "$(curl evil)"}]})
    assert sanitize({"path": "`id`"}) is None


def test_allows_clean_paths_and_freeform_notes():
    assert is_clean({"steps": [{"path": "notebooks/01.ipynb", "why": "first"}], "confidence": 0.8})
    # free-text fields may legitimately mention commands
    assert is_clean({"summary": "the script runs python main.py to reproduce"})
    assert sanitize({"likely_cause": "missing package; pip install pandas"}) is not None


def test_allows_backticks_in_advisory_fix_fields():
    # Regression: small models wrap fixes in markdown backticks. suggested_fixes is
    # display-only (never executed), so backticks must NOT be rejected — only an
    # executable "path" field stays strict.
    assert is_clean({"suggested_fixes": ["Run `install.packages('colleyRstats')`", "`pip install numpy`"]})
    assert sanitize({"likely_cause": "missing pkg",
                     "suggested_fixes": ['`remotes::install_github("M-Colley/colleyRstats")`']}) is not None
    # but a path that tries command substitution is still rejected
    assert sanitize({"steps": [{"path": "`id`.py"}]}) is None
