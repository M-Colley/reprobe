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


# --- classes the benchmark surfaced repeatedly --------------------------------

def test_an_unattached_r_package_names_the_library_line_to_add():
    """The most common failure across the benchmark (7 of them). The package is
    INSTALLED — the install phase put it there — so this is not a missing
    dependency, and saying "install X" would send an author to fix the one thing
    that is not broken."""
    d = diagnose('Error in gather(x) : could not find function "gather"\n', 1)
    assert d is not None
    assert "tidyr" in d["likely_cause"]
    assert any("library(tidyr)" in f for f in d["suggested_fixes"])
    assert "not a missing dependency" in d["likely_cause"]


def test_an_unknown_function_is_named_without_inventing_a_package():
    """The map is curated. A function not in it still gets the class named — but
    a guessed package would be a wrong instruction stated as fact."""
    d = diagnose('could not find function "some_private_helper"\n', 1)
    assert d is not None
    assert "some_private_helper" in d["likely_cause"]
    assert "library(" in " ".join(d["suggested_fixes"])
    for pkg in ("tidyr", "report", "bayestestR"):
        assert f"library({pkg})" not in " ".join(d["suggested_fixes"])


def test_a_runtime_download_is_reported_as_a_reproducibility_finding():
    """roads-chi25-data sources its function library from a moving branch at run
    time. The finding is not the failed request — it is that the deposit does not
    contain what its results depend on."""
    log = ("devtools::source_url(\"https://raw.githubusercontent.com/M-Colley/rCode/main/r.R\")\n"
           "Could not resolve host: raw.githubusercontent.com\n")
    d = diagnose(log, 1)
    assert d is not None
    assert "not self-contained" in d["likely_cause"]
    assert "raw.githubusercontent.com" in d["likely_cause"]
    assert any("vendor" in f for f in d["suggested_fixes"])


def test_a_missing_output_directory_explains_why_git_lost_it():
    d = diagnose("Error in `ggsave()`:\n! Cannot find directory 'plots'.\n", 1)
    assert d is not None
    assert "plots" in d["likely_cause"]
    assert "does not track empty directories" in d["likely_cause"]
    assert any("dir.create" in f for f in d["suggested_fixes"])


ONATTACH_CONFLICTED_LOG = """Error: package or namespace load failed for 'easystats':
 .onAttach failed in attachNamespace() for 'easystats', details:
  call: eval(mc$quietly, parent.frame(i))
  error: object 'quietly' not found
Execution halted
"""


def test_a_failed_attach_hook_is_not_reported_as_a_missing_package():
    """Seen 3 times across the benchmark, and with no deterministic answer the
    advisory model filled in with "update the easystats package" — confidently
    wrong. The package is installed and R ran its hook; installing or updating it
    changes nothing."""
    d = diagnose(ONATTACH_CONFLICTED_LOG, 1)
    assert d is not None
    cause = d["likely_cause"]
    assert "easystats" in cause
    assert "neither a missing nor an out-of-date package" in cause
    assert "conflicted" in cause
    fixes = " ".join(d["suggested_fixes"])
    assert "set_conflicts = FALSE" in fixes
    assert "install" not in fixes.lower().replace("installing", "")   # never "install X"


def test_the_conflicted_case_explains_the_ordering_that_fixes_it():
    d = diagnose(ONATTACH_CONFLICTED_LOG, 1)
    assert any("BEFORE" in f for f in d["suggested_fixes"])
    assert "do not compose" in " ".join(d["suggested_fixes"])


def test_an_attach_hook_failure_without_the_conflicted_fingerprint_stays_narrow():
    """Attach hooks fail for many reasons. Without the `conflicted` fingerprint,
    name the class and point at the evidence — do not assert a cause."""
    log = ("Error: package or namespace load failed for 'somePkg':\n"
           " .onAttach failed in attachNamespace() for 'somePkg', details:\n"
           "  call: stop('no license found')\n  error: no license found\n")
    d = diagnose(log, 1)
    assert d is not None
    assert "somePkg" in d["likely_cause"]
    assert "not a missing package" in d["likely_cause"]
    assert "conflicted" not in d["likely_cause"]
    assert "set_conflicts" not in " ".join(d["suggested_fixes"])
