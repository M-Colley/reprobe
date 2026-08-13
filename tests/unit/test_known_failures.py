"""Failures whose console output has exactly one possible cause.

A chair reading `fail` learns nothing. These two signatures came out of real
artifacts in benchmarks/artifacts.csv, and both name a defect the author cannot
see from their own machine — the script works for them precisely because of the
thing that is missing here.

Pure: no network, no Docker, no LLM.
"""

from __future__ import annotations

import pytest

from reprobe.orchestrator import known_failure_diagnosis as diagnose

RSTUDIO_LOG = """$ docker run --rm ... Rscript 'Evaluation_CHI25_ROADS.R'

Error: RStudio not running
Execution halted
"""

ARGPARSE_LOG = """$ docker run --rm ... python 'analyze_auditory_cues.py'

usage: analyze_auditory_cues.py [-h] [--outdir OUTDIR] [--recursive]
                                [--no-plots] [--no-pdf] [--no-single]
                                inputs [inputs ...]
analyze_auditory_cues.py: error: the following arguments are required: inputs
"""


def test_an_rstudio_only_script_is_named_as_such():
    """roads-chi25-data: `setwd(dirname(getActiveDocumentContext()$path))` on line
    2. The package installs like any other — it is the IDE that is absent."""
    d = diagnose(RSTUDIO_LOG, 1)
    assert d is not None
    assert "rstudioapi" in d["likely_cause"]
    assert "IDE" in d["likely_cause"]
    assert d["source"].startswith("harness (deterministic")
    assert any("here::here" in f or "working directory" in f for f in d["suggested_fixes"])


def test_the_rstudio_diagnosis_does_not_blame_a_missing_package():
    """The tempting wrong fix is "add rstudioapi to the image". It is already
    there; installing it again changes nothing, and saying so would send an
    author to fix the one thing that is not broken."""
    d = diagnose(RSTUDIO_LOG, 1)
    cause = d["likely_cause"].lower()
    assert "installed fine" in cause or "package itself installed" in cause
    assert "install rstudioapi" not in cause


def test_missing_cli_arguments_are_named_with_the_argument():
    """bvi-auditory-hav: argparse exits 2 with its usage block when the script
    needs positional arguments the deposit never documents."""
    d = diagnose(ARGPARSE_LOG, 2)
    assert d is not None
    assert "inputs" in d["likely_cause"]
    assert "argparse" in d["likely_cause"]
    assert any("args:" in f for f in d["suggested_fixes"])


def test_a_usage_block_without_exit_2_is_not_claimed():
    """A script that merely PRINTS usage text (a --help path, a docstring echoed
    on error) has not necessarily failed for want of arguments."""
    assert diagnose(ARGPARSE_LOG, 1) is None


def test_exit_2_without_a_usage_block_is_not_claimed():
    assert diagnose("Traceback (most recent call last):\nValueError: nope\n", 2) is None


def test_an_ordinary_failure_falls_through_to_the_advisory_model():
    """The whole value of this list is that it is short. Anything not recognised
    must reach the LLM path rather than be given a confident wrong answer."""
    assert diagnose("ModuleNotFoundError: No module named 'shap'\n", 1) is None
    assert diagnose("Error in library(foo) : there is no package called 'foo'\n", 1) is None


def test_nothing_is_claimed_for_an_empty_log():
    assert diagnose("", 2) is None
    assert diagnose("", None) is None


@pytest.mark.parametrize("exit_code", [2, None, 0])
def test_rstudio_is_recognised_whatever_the_exit_code(exit_code):
    # Rscript's exit code for a halted script is not something to depend on.
    assert diagnose(RSTUDIO_LOG, exit_code) is not None
