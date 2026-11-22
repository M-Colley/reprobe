"""Choosing the Python an artifact's pinned requirements install on."""

import pytest

from reprobe.envbuild import pyversion
from reprobe.envbuild.pyversion import _supports, choose_python, declared_python, exact_pins

_ML = "manylinux_2_17_x86_64.manylinux2014_x86_64"
#: What PyPI lists for these releases (abridged, real file-name shapes).
FAKE = {
    ("pandas", "1.4.3"): ((f"pandas-1.4.3-cp38-cp38-{_ML}.whl", f"pandas-1.4.3-cp39-cp39-{_ML}.whl",
                          f"pandas-1.4.3-cp310-cp310-{_ML}.whl", "pandas-1.4.3.tar.gz"), ">=3.8",
                         "2022-06-23T18:20:00.123456Z"),
    ("scikit-learn", "1.1.2"): ((f"scikit_learn-1.1.2-cp310-cp310-{_ML}.whl",
                                 f"scikit_learn-1.1.2-cp38-cp38-{_ML}.whl"), ">=3.8",
                                "2022-08-05T21:40:00Z"),
    ("seaborn", "0.11.2"): (("seaborn-0.11.2-py3-none-any.whl",), ">=3.6"),
    ("pydoe", "0.3.8"): (("pyDOE-0.3.8.zip",), None),
    ("cryptography", "42.0.0"): ((f"cryptography-42.0.0-cp39-abi3-{_ML}.whl",), ">=3.7"),
    ("numpy", "2.1.0"): ((f"numpy-2.1.0-cp313-cp313-{_ML}.whl",), ">=3.10"),
    ("tensorflow", "1.13.1"): (("tensorflow-1.13.1-cp36-cp36m-manylinux1_x86_64.whl",
                                "tensorflow-1.13.1-cp37-cp37m-manylinux1_x86_64.whl"), None),
}


@pytest.fixture
def pypi(monkeypatch):
    monkeypatch.setattr(pyversion, "release_files", lambda n, v: FAKE.get((n, v)))


def test_exact_pins_only():
    assert exact_pins(["pandas==1.4.3", "numpy>=1.20", "scipy==1.*", "Scikit_Learn==1.1.2 # c",
                       "pywin32==306; sys_platform == 'win32'", "not a requirement"]) == [
        ("pandas", "1.4.3"), ("scikit-learn", "1.1.2")]


def test_wheel_tags_decide_support():
    files, rp, _ = FAKE[("pandas", "1.4.3")]
    assert [m for m in ("3.13", "3.10", "3.8") if _supports(files, rp, m)] == ["3.10", "3.8"]
    assert _supports(*FAKE[("cryptography", "42.0.0")], "3.13")         # abi3 from 3.9 up
    assert _supports(*FAKE[("pydoe", "0.3.8")], "3.13")                 # sdist only: pip builds it
    assert not _supports(*FAKE[("numpy", "2.1.0")], "3.9")              # requires-python


def test_old_pins_get_the_newest_python_they_ship_wheels_for(pypi):
    c = choose_python([("pandas", "1.4.3"), ("scikit-learn", "1.1.2"), ("seaborn", "0.11.2"),
                       ("pydoe", "0.3.8")])
    assert c.version == "3.10" and not c.unsupported and c.cutoff == "2022-08-06T00:00:00Z"
    assert "pandas==1.4.3" in c.reason and "scikit-learn==1.1.2" in c.reason


def test_pins_the_base_python_takes_change_nothing(pypi):
    assert choose_python([("seaborn", "0.11.2"), ("numpy", "2.1.0")]) is None
    assert choose_python([("unknown-to-pypi", "1.0")]) is None


def test_a_declared_python_is_honoured(tmp_path, pypi):
    (tmp_path / ".python-version").write_text("3.9.18\n", encoding="utf-8")
    c = choose_python([("seaborn", "0.11.2")], declared_python(tmp_path))
    assert c.version == "3.9" and "declares" in c.reason


def test_pins_no_python_satisfies_get_the_best_fit_and_are_named(pypi):
    c = choose_python([("pandas", "1.4.3"), ("scikit-learn", "1.1.2"), ("numpy", "2.1.0")])
    assert c.version == "3.10" and c.unsupported == ["numpy==2.1.0"]
    # a tie keeps the base image's Python
    assert choose_python([("pandas", "1.4.3"), ("numpy", "2.1.0")]) is None


def _plan(tmp_path, files, **kw):
    from reprobe.envbuild.base import _install_plan
    for rel, text in files.items():
        (tmp_path / rel).write_text(text, encoding="utf-8")
    return _install_plan({}, tmp_path, False, executes_code=True, py_needed=True,
                         python_versions=pyversion.DEFAULT_CANDIDATES, **kw)


def test_the_requirements_go_into_a_python_env_of_the_chosen_version(tmp_path, pypi):
    ip = _plan(tmp_path, {"requirements.txt": "pandas==1.4.3\nscikit-learn==1.1.2\n", "a.py": ""},
               notebooks=True)
    assert ip.python_version == "3.10" and ip.conda_prefix == "/work/.reprobe_env"
    create = ip.commands[0]
    assert "micromamba create" in create and "python=3.10 pip papermill ipykernel nbconvert" in create
    assert ip.origins[0] == "harness-detected"           # a failed env build is the harness's
    pip = next(c for c in ip.commands if "-r /work/.reprobe_req/requirements.txt" in c)
    assert "/work/.reprobe_env/bin/python" in pip and "--target" not in pip
    # open requirements resolve as of the day after the newest pinned upload
    assert "--uploaded-prior-to=2022-08-06T00:00:00Z" in pip
    assert any("Python 3.10 was used instead of the base image's 3.13" in w for w in ip.warnings)


def test_requirements_the_base_python_takes_install_as_before(tmp_path, pypi):
    ip = _plan(tmp_path, {"requirements.txt": "seaborn==0.11.2\n", "a.py": ""})
    assert ip.python_version is None and not any("micromamba create" in c for c in ip.commands)


def test_an_environment_yml_keeps_its_own_python(tmp_path, pypi):
    ip = _plan(tmp_path, {"environment.yml": "name: x\ndependencies:\n  - python=3.11\n",
                          "requirements.txt": "pandas==1.4.3\n", "a.py": ""})
    assert ip.python_version is None
    assert sum("micromamba create" in c for c in ip.commands) == 1


def test_pyproject_pins_and_requires_python_are_read(tmp_path, pypi):
    ip = _plan(tmp_path, {"pyproject.toml": '[project]\nname = "x"\nrequires-python = ">=3.8,<3.10"\n'
                                            'dependencies = ["seaborn==0.11.2"]\n', "a.py": ""})
    assert ip.python_version == "3.9"


def test_tensorflow_1_pins_get_python_3_7(pypi):
    """Benchmark row 15: tensorflow==1.13.1 ships wheels up to cp37m only."""
    c = choose_python([("tensorflow", "1.13.1"), ("seaborn", "0.11.2")])
    assert c.version == "3.7" and not c.unsupported
