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
    """The most common failure across the benchmark (7 of them). The message
    proves the package was not ATTACHED; it does not prove it was installed, so
    the diagnosis must not claim that (when tidyr failed to install, it would
    have told the author the package was present)."""
    d = diagnose('Error in gather(x) : could not find function "gather"\n', 1)
    assert d is not None
    assert "tidyr" in d["likely_cause"] and "was not attached" in d["likely_cause"]
    assert any("library(tidyr)" in f for f in d["suggested_fixes"])
    assert "install phase put it there" not in d["likely_cause"]


def test_an_unknown_function_gets_no_deterministic_diagnosis():
    """A name the curated map does not know may be the author's own undefined
    function: calling it "a package that is installed but not attached" would be
    a guess stated as fact. It falls through to the (labelled) advisory."""
    assert diagnose('could not find function "some_private_helper"\n', 1) is None


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


# --------------------------------------------------------------------------- #
# R's one "cannot open file" message has several producers: never guess which
# --------------------------------------------------------------------------- #
from reprobe.orchestrator import known_failure_diagnosis as _kfd  # noqa: E402

READ_FAIL = """Error in file(file, "rt") : cannot open the connection
Calls: read.csv -> read.table -> file
In addition: Warning message:
In file(file, "rt") :
  cannot open file 'data/survey.csv': No such file or directory
Execution halted"""

WRITE_FAIL = """Error in file(file, ifelse(append, "a", "w")) :
  cannot open the connection
Calls: write.csv -> eval.parent -> eval -> eval -> <Anonymous> -> file
In addition: Warning message:
In file(file, ifelse(append, "a", "w")) :
  cannot open file 'results/tables/out.csv': No such file or directory"""


def test_a_missing_input_is_not_called_a_missing_output_directory():
    """The old pattern told an author whose data file was absent to
    `dir.create("data/survey.csv")` — labelled "not an LLM guess"."""
    d = _kfd(READ_FAIL, 1)
    assert "reads `data/survey.csv`" in d["likely_cause"]
    assert "dir.create" not in " ".join(d["suggested_fixes"])
    assert any("data_sources" in f for f in d["suggested_fixes"])


def test_a_write_into_a_missing_directory_names_the_directory():
    w = WRITE_FAIL.replace('ifelse(append, "a", "w")', '"w"')
    d = _kfd(w, 1)
    assert "writes into `results/tables`" in d["likely_cause"]
    assert 'dir.create("results/tables"' in d["suggested_fixes"][0]


def test_without_the_mode_line_nothing_is_stated_as_fact():
    d = _kfd("cannot open file 'x.csv': No such file or directory\nExecution halted", 1)
    assert d is None


def test_a_missing_step_script_is_named_as_such():
    d = _kfd("Fatal error: cannot open file 'analysis/run_all.R': No such file or directory", 2)
    assert "does not exist in the deposit" in d["likely_cause"]


def test_setwd_to_the_authors_machine_is_its_own_finding():
    d = _kfd('Error in setwd("C:/Users/me/proj") : cannot change working directory', 1)
    assert "setwd()" in d["likely_cause"] and "dir.create" not in " ".join(d["suggested_fixes"])


def test_the_rstudio_advice_depends_on_where_the_script_lives():
    root = _kfd("Error: RStudio not running", 1, "run_all.R")
    sub = _kfd("Error: RStudio not running", 1, "analysis/run_all.R")
    assert "drop the two rstudioapi lines" in root["suggested_fixes"][0]
    assert 'setwd(here::here("analysis"))' in sub["suggested_fixes"][0]
    assert "drop the two rstudioapi lines" not in " ".join(sub["suggested_fixes"])
