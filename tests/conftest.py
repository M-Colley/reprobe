import sys
from pathlib import Path

# Allow running the test suite from a source checkout without `pip install`.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_pypi(request, monkeypatch):
    """Planning asks PyPI which Pythons pinned releases support; tests stay
    offline (an unknown release constrains nothing). Tests that exercise the
    choice install their own fake; end-to-end tests ask the real PyPI."""
    if request.node.get_closest_marker("e2e"):
        return
    from reprobe.envbuild import pyversion
    monkeypatch.setattr(pyversion, "release_files", lambda name, version: None)
