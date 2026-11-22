"""Defects the ACM-badged benchmark (benchmarks/acm-badged.csv) found in reprobe."""

import json
import zipfile

from reprobe.detect import detect
from reprobe.fetch import base

NB = json.dumps({"nbformat": 4, "nbformat_minor": 5,
                 "metadata": {"kernelspec": {"name": "python3", "language": "python"}},
                 "cells": [{"cell_type": "code", "metadata": {}, "execution_count": None,
                            "outputs": [], "source": ["print(1)"]}]})


def _tree(root, files):
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")


def _zip(path, members):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, text in members.items():
            z.writestr(name, text)


def test_a_deposit_that_is_one_archive_one_folder_down_is_extracted(tmp_path):
    """Zenodo's GitHub integration names the file `Owner/repo-tag.zip`."""
    _zip(tmp_path / "Owner" / "repo-v1.zip", {"repo-v1/main.py": "print(1)\n"})
    warnings: list[str] = []
    base.maybe_unzip(tmp_path, warnings)
    assert (tmp_path / "repo-v1" / "main.py").is_file() and not warnings


def test_one_release_in_two_formats_is_extracted_once(tmp_path):
    import tarfile
    _zip(tmp_path / "Tool-0.1.zip", {"Tool-0.1/main.py": "print(1)\n"})
    with tarfile.open(tmp_path / "Tool-0.1.tar.gz", "w:gz"):
        pass
    warnings: list[str] = []
    base.maybe_unzip(tmp_path, warnings)
    assert (tmp_path / "Tool-0.1" / "main.py").is_file()
    assert warnings and "Tool-0.1.zip was extracted" in warnings[0]


def test_two_different_archives_are_still_left_alone(tmp_path):
    _zip(tmp_path / "a" / "x.zip", {"x/main.py": ""})
    _zip(tmp_path / "b" / "y.zip", {"y/main.py": ""})
    base.maybe_unzip(tmp_path, [])
    assert not (tmp_path / "x").exists() and not (tmp_path / "y").exists()


def test_macos_zip_metadata_is_never_a_step(tmp_path):
    """zenodo.23205571 ran 26 `__MACOSX/.../._x.ipynb` resource forks as notebooks."""
    _tree(tmp_path, {"a.ipynb": NB, "__MACOSX/code/._a.ipynb": "\x00\x05\x16\x07",
                     "code/._b.ipynb": "\x00\x05\x16\x07"})
    res, _ = detect(tmp_path)
    assert [s.target for s in res.steps] == ["a.ipynb"]


def test_notebooks_under_docs_are_documentation_when_there_is_more(tmp_path):
    _tree(tmp_path, {"analysis.ipynb": NB, "code/docs/backend/api.ipynb": NB, "docs/tour.ipynb": NB})
    res, _ = detect(tmp_path)
    assert [s.target for s in res.steps] == ["analysis.ipynb"]
    assert any("under docs/" in n and "code/docs/backend/api.ipynb" in n for n in res.notes)
    res, _ = detect(_only_docs(tmp_path))
    assert [s.target for s in res.steps] == ["docs/tour.ipynb"]


def _only_docs(tmp_path):
    d = tmp_path / "only"
    _tree(d, {"docs/tour.ipynb": NB})
    return d


def test_nothing_run_is_not_explained_by_other_scripts(tmp_path):
    """14 artifacts were told their scripts were skipped because "other scripts
    were found to run the analysis" — when nothing ran at all."""
    _tree(tmp_path, {"tool/maian.py": "print(1)\n", "tool/blockchain.py": "print(1)\n"})
    res, _ = detect(tmp_path)
    assert not res.steps
    note = next(n for n in res.notes if "NOT run" in n)
    assert "no entry point could be identified" in note and "other scripts" not in note


def test_a_script_importing_its_own_package_from_an_ancestor_gets_that_folder(tmp_path):
    _tree(tmp_path, {"dcp-main/dcp/__init__.py": "", "dcp-main/dcp/utils.py": "",
                     "dcp-main/dcp/core/pipeline.py": "import os\nfrom dcp.utils import x\n",
                     "dcp-main/main.py": "import numpy\n"})
    from reprobe.detect.signatures import import_root
    root = tmp_path.resolve()
    assert import_root(root / "dcp-main/dcp/core/pipeline.py", root) == "dcp-main"
    # its own folder is already on sys.path; stdlib and PyPI names are not the artifact's
    assert import_root(root / "dcp-main/main.py", root) is None


def test_an_import_its_own_folder_satisfies_needs_no_path(tmp_path):
    _tree(tmp_path, {"s/run.py": "import helper\n", "s/helper.py": "", "helper.py": ""})
    from reprobe.detect.signatures import import_root
    assert import_root((tmp_path / "s/run.py").resolve(), tmp_path.resolve()) is None


def test_package_modules_are_named_when_nothing_runs(tmp_path):
    """JStap (ACSAC 2019): 26 Python files, all in packages, reported as "1 document"."""
    _tree(tmp_path, {"classification/__init__.py": "", "classification/learner.py": "print(1)\n",
                     "README.md": "x"})
    res, _ = detect(tmp_path)
    assert "python" in res.artifact_types and not res.steps
    assert any("modules of a package" in n and "classification/learner.py" in n for n in res.notes)


def test_keras_alone_brings_its_tensorflow_backend(tmp_path):
    from reprobe.detect.signatures import scan_py_packages
    (tmp_path / "a.py").write_text("import keras\nimport numpy\n", encoding="utf-8")
    assert "tensorflow" in scan_py_packages(tmp_path, [tmp_path / "a.py"])
    (tmp_path / "b.py").write_text("import keras\nimport torch\n", encoding="utf-8")
    assert "tensorflow" not in scan_py_packages(tmp_path, [tmp_path / "b.py"])
