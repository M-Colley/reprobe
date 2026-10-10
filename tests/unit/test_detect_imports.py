"""Import and library() discovery on the shapes real repositories have.

What this decides is what the install phase is ASKED to fetch from the public
index while the network is open, so the failure modes that matter are the ones
that install the wrong thing: a name taken from a docstring, an optional import
that changes which branch runs, or the deposit's own module mistaken for a PyPI
package that anyone could have registered. Offline, tmp_path trees only."""

from __future__ import annotations

import json

import pytest

from reprobe.detect import signatures as S


def _py(tmp_path, files: dict[str, str]):
    for rel, text in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    return S.scan_py_packages(tmp_path, sorted(p for p in tmp_path.rglob("*") if p.is_file()))


def _nb(tmp_path, cells, name="n.ipynb", kernel="python3"):
    nb = {"cells": [{"cell_type": "code", "source": c} for c in cells],
          "metadata": {"kernelspec": {"name": kernel, "language": "python"}}}
    (tmp_path / name).write_text(json.dumps(nb), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Python: what the code actually imports
# --------------------------------------------------------------------------- #

def test_every_name_on_a_multi_import_line_is_found(tmp_path):
    # `import os, yaml, requests` once yielded nothing: the regex took the first name.
    assert _py(tmp_path, {"a.py": "import os, yaml, requests\n"}) == ["pyyaml", "requests"]


def test_an_import_inside_a_docstring_or_string_is_not_an_import(tmp_path):
    src = '"""\nimport mylabtool\nfrom ghostpkg import x\n"""\ns = "import fakepkg"\nimport shap\n'
    assert _py(tmp_path, {"a.py": src}) == ["shap"]


def test_an_optional_import_is_not_installed(tmp_path):
    """`try: import ujson / except ImportError:` means the author wrote a fallback.
    Installing it would change which branch the analysis takes."""
    src = ("try:\n    import ujson as json\nexcept ImportError:\n    import json\n"
           "try:\n    import orjson\nexcept (ImportError, ValueError):\n    orjson = None\n"
           "try:\n    import hard_dep\nexcept KeyError:\n    pass\n")
    assert _py(tmp_path, {"a.py": src}) == ["hard-dep"]      # only the KeyError guard is not an import guard


def test_type_checking_only_imports_are_not_needed_at_run_time(tmp_path):
    src = ("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import pandas_stubs_only\n"
           "import pandas\n")
    assert _py(tmp_path, {"a.py": src}) == ["pandas"]


def test_relative_imports_and_stdlib_are_never_distributions(tmp_path):
    src = "from . import sibling\nfrom .sub import thing\nimport os, sys, json, collections\n"
    assert _py(tmp_path, {"pkg/a.py": src}) == []


def test_a_function_level_import_counts(tmp_path):
    assert _py(tmp_path, {"a.py": "def f():\n    import shap\n    return shap\n"}) == ["shap"]


@pytest.mark.parametrize("files", [
    {"utils.py": "x = 1\n", "main.py": "import utils\n"},                         # sibling module
    {"src/mypkg/core.py": "x = 1\n", "main.py": "import mypkg\n"},                 # src layout, no __init__
    {"lab/stats/x.py": "x = 1\n", "main.py": "import stats\nimport lab\n"},        # nested namespace dirs
    {"analysis/helpers/io.py": "x = 1\n", "analysis/run.py": "import helpers\n"},  # script's own dir
])
def test_the_deposits_own_modules_are_never_pip_installed(tmp_path, files):
    """`import utils` in a repo shipping utils.py once asked pip for a package
    called "utils". Any unregistered name is one an attacker can register, and the
    install phase has network."""
    assert _py(tmp_path, files) == []


def test_import_names_map_to_the_real_distribution(tmp_path):
    src = "import cv2, sklearn, skimage, yaml, PIL, Bio, umap, tables, bs4, dateutil\n"
    assert _py(tmp_path, {"a.py": src}) == sorted([
        "opencv-python", "scikit-learn", "scikit-image", "pyyaml", "pillow",
        "biopython", "umap-learn", "tables", "beautifulsoup4", "python-dateutil"])


def test_names_that_are_not_pypi_distributions_are_not_asked_for():
    # pipreqs carries `tables -> pytables`; PyPI's distribution is `tables`. A name
    # that is not a real distribution is one anyone can register.
    assert S.dist_for_import("tables") == "tables"
    assert S.dist_for_import("requests") == "requests"     # pipreqs spells it "Requests"
    assert S.dist_for_import("umap") == "umap-learn"
    # our decisions win over the vendored table, which is only a fallback
    assert S.dist_for_import("yaml") == "pyyaml"
    # unmapped names fall through to themselves
    assert S.dist_for_import("shap") == "shap"


def test_the_vendored_mapping_is_attributed_and_parses():
    from reprobe.detect import _pipreqs_mapping as M
    assert "Apache License" in M.__doc__ and "bndr/pipreqs" in M.__doc__
    table = S._pipreqs_mapping()
    assert len(table) > 500 and table.get("zmq") == "pyzmq"


def test_distribution_names_are_validated_before_reaching_a_shell(tmp_path):
    # a hostile module name cannot even be an identifier, so it never reaches _PY_DIST_RE
    assert _py(tmp_path, {"a.py": "import shap\nimport os; import x__y\n"}) == ["shap", "x-y"]


def test_submodule_extras_still_resolve(tmp_path):
    src = "from ray import tune\nimport ray.train\nfrom dask import dataframe\n"
    assert _py(tmp_path, {"a.py": src}) == ["dask[dataframe]", "ray[train]", "ray[tune]"]


def test_a_python2_file_falls_back_to_line_scanning(tmp_path):
    src = "import shap\nprint 'hello'\nimport pandas, numpy\n"
    assert _py(tmp_path, {"old.py": src}) == ["numpy", "pandas", "shap"]


def test_binary_garbage_and_pathological_nesting_never_crash_detection(tmp_path):
    (tmp_path / "bad.py").write_bytes(b"\x00\xff\xfe import evil\n")
    (tmp_path / "deep.py").write_text("x = " + "(" * 5000 + "1" + ")" * 5000 + "\nimport pandas\n",
                                      encoding="utf-8")
    S.scan_py_packages(tmp_path, sorted(tmp_path.glob("*.py")))        # must not raise


# --------------------------------------------------------------------------- #
# Notebooks: magics are not Python, a bad cell costs only that cell
# --------------------------------------------------------------------------- #

def test_notebook_magics_and_shell_cells_do_not_break_or_pollute_the_scan(tmp_path):
    _nb(tmp_path, [
        "%matplotlib inline\nimport shap\n!pip install notapackage\n",
        "%%bash\nimport shell_only_not_python\npip install other\n",
        "%%time\nimport lightgbm\n",
        "import pandas\nx = [\n",                           # syntax error: only this cell is degraded
    ])
    found = S.scan_py_packages(tmp_path, [tmp_path / "n.ipynb"])
    assert {"shap", "lightgbm", "pandas"} <= set(found)
    assert not {"notapackage", "shell-only-not-python", "other"} & set(found)


def test_an_r_kernel_notebook_is_not_scanned_for_python(tmp_path):
    _nb(tmp_path, ["library(dplyr)"], kernel="ir")
    nb = json.loads((tmp_path / "n.ipynb").read_text())
    nb["metadata"]["kernelspec"]["language"] = "R"
    (tmp_path / "n.ipynb").write_text(json.dumps(nb), encoding="utf-8")
    assert S.scan_py_packages(tmp_path, [tmp_path / "n.ipynb"]) == []


# --------------------------------------------------------------------------- #
# R: library() calls are code; comments, strings and prose are not
# --------------------------------------------------------------------------- #

def _r(tmp_path, name, text):
    (tmp_path / name).write_text(text, encoding="utf-8")
    return S.scan_r_packages([tmp_path / name])


def test_commented_out_library_calls_are_not_dependencies(tmp_path):
    assert _r(tmp_path, "a.R", "library(dplyr)\n# library(oldpkg)\n  # require(alsoold)\n") == ["dplyr"]


def test_a_namespace_inside_a_string_is_not_a_dependency(tmp_path):
    assert _r(tmp_path, "a.R", 'msg <- "see foo.bar::baz"\nlibrary(tidyr)\n') == ["tidyr"]


def test_a_hash_inside_a_string_does_not_hide_the_rest_of_the_line(tmp_path):
    assert _r(tmp_path, "a.R", 'u <- "http://x/#frag"; library(tidyr)\nlibrary(stringr)\n') == ["stringr", "tidyr"]


def test_pacman_p_load_names_every_package(tmp_path):
    src = 'pacman::p_load(dplyr, ggplot2, "lme4", install = FALSE, update = FALSE)\n'
    assert _r(tmp_path, "a.R", src) == ["dplyr", "ggplot2", "lme4", "pacman"]


def test_character_only_takes_the_name_from_a_variable(tmp_path):
    src = 'pkg <- "dplyr"\nlibrary(pkg, character.only = TRUE)\nlibrary("lme4", character.only = TRUE)\n'
    # a bare `pkg` is a variable; a quoted name is still a package
    assert _r(tmp_path, "a.R", src) == ["lme4"]


def test_only_the_first_argument_of_library_is_a_package(tmp_path):
    assert _r(tmp_path, "a.R", "library(dplyr, quietly = TRUE, warn.conflicts = FALSE)\n") == ["dplyr"]


def test_rmarkdown_prose_is_not_code_but_its_chunks_are(tmp_path):
    src = ("# Methods\n\nFirst call library(prose) as in the text.\n\n"
           "```{r setup}\nlibrary(knitr)\nlibrary(emmeans)\n```\n\nInline `r lubridate::today()`.\n")
    assert _r(tmp_path, "a.Rmd", src) == ["emmeans", "knitr", "lubridate"]


def test_quarto_documents_are_scanned_too(tmp_path):
    assert _r(tmp_path, "a.qmd", "```{r}\nlibrary(brms)\n```\n") == ["brms"]


def test_unterminated_strings_cost_one_linear_pass(tmp_path):
    import time
    src = ('"' * 1) + ('x "' * 20000) + "\nlibrary(dplyr)\n"
    t0 = time.monotonic()
    _r(tmp_path, "a.R", src)
    assert time.monotonic() - t0 < 2.0
