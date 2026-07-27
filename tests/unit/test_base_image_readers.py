"""The base image must be able to open every format the detector advertises.

reprobe's detector inventories `.parquet`, `.xlsx`, `.h5` and friends as
"dataset" files. If the base image ships a pandas that cannot read them, real
artifacts die with `ImportError: Unable to find a usable engine` and get scored
as broken *code* — the harness blaming the artifact for its own gap. That is the
worst failure mode this project has, so the coupling is asserted here rather
than left to whoever next edits either file.
"""

from pathlib import Path

import yaml

from reprobe.detect.signatures import _NONCODE_TYPES

REPO = Path(__file__).resolve().parents[2]
ENV_YAML = REPO / "images" / "base-py" / "env.yaml"

# Every dataset extension -> the conda package that makes it readable from
# Python, or None when the stdlib/pandas core already handles it (or when the
# format belongs to the R image). Adding an extension to _NONCODE_TYPES without
# adding it here fails the first test below, forcing an explicit decision.
_READER_FOR: dict[str, str | None] = {
    "csv": None,          # pandas core
    "tsv": None,          # pandas core
    "jsonl": None,        # pandas core
    "ndjson": None,       # pandas core
    "sqlite": None,       # stdlib sqlite3
    "sqlite3": None,      # stdlib sqlite3
    "dta": None,          # pandas core (Stata); pyreadstat is only a faster path
    "rds": None,          # R image
    "rdata": None,        # R image
    "parquet": "pyarrow",
    "feather": "pyarrow",
    "arrow": "pyarrow",
    "xlsx": "openpyxl",
    "xls": "xlrd",
    "h5": "pytables",
    "hdf5": "pytables",
    "nc": "netcdf4",
    "sav": "pyreadstat",
}


def _declared_packages() -> set[str]:
    deps = yaml.safe_load(ENV_YAML.read_text(encoding="utf-8"))["dependencies"]
    names = set()
    for d in deps:
        if isinstance(d, str):
            names.add(d.split("=")[0].split(">")[0].split("<")[0].strip().lower())
    return names


def test_every_dataset_extension_has_a_declared_reader():
    """A new dataset extension must come with a decision about how to read it."""
    undecided = sorted(set(_NONCODE_TYPES["dataset"]) - set(_READER_FOR))
    assert not undecided, (
        f"detect/signatures.py advertises {undecided} as dataset formats but "
        f"tests/unit/test_base_image_readers.py:_READER_FOR does not say which package "
        f"reads them. Add the mapping (and the package to images/base-py/env.yaml)."
    )


def test_base_image_ships_every_required_reader():
    declared = _declared_packages()
    missing = sorted(
        {pkg for ext, pkg in _READER_FOR.items()
         if pkg and ext in _NONCODE_TYPES["dataset"] and pkg.lower() not in declared}
    )
    assert not missing, (
        f"images/base-py/env.yaml is missing {missing}. The detector calls the "
        f"matching formats 'dataset', so an artifact reading one fails with an "
        f"ImportError that gets reported as an artifact failure, not a harness gap."
    )


def test_pyarrow_specifically_is_present():
    """Regression: parquet is the common case and its absence cost two of five
    steps on a real submission (PDRA_XAI_OS, 2026-07)."""
    assert "pyarrow" in _declared_packages()
