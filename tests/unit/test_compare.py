"""Regenerated-vs-committed output comparison (reprobe.compare). Offline, tmp_path only."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import reprobe.compare
from reprobe.compare import compare_outputs, summarize


def _pair(tmp_path, rel, committed, regenerated):
    """Write committed/regenerated copies of rel under src/ and run/; bytes or str."""
    src, run = tmp_path / "src", tmp_path / "run"
    for root, content in ((src, committed), (run, regenerated)):
        if content is None:
            continue
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            p.write_bytes(content.encode("utf-8"))
        else:
            p.write_bytes(content)
    src.mkdir(exist_ok=True)
    run.mkdir(exist_ok=True)
    return src, run


def _one(tmp_path, rel, committed, regenerated, **kw):
    src, run = _pair(tmp_path, rel, committed, regenerated)
    [res] = compare_outputs(src, run, [rel], **kw)
    json.dumps(res, allow_nan=False)  # report-ready: plain, strict JSON
    return res


# --------------------------------------------------------------------------- #
#  Tabular
# --------------------------------------------------------------------------- #

CSV = "group,n,mean,p\nA,10,1.25,0.031\nB,12,2.5,0.4\n"


def test_identical_csv(tmp_path):
    res = _one(tmp_path, "results/table.csv", CSV, CSV)
    assert res["status"] == "identical" and res["kind"] == "tabular"


def test_csv_float_noise_is_equivalent(tmp_path):
    noisy = "group,n,mean,p\nA,10,1.250000000001,0.031\nB,12,2.5,0.400000000000001\n"
    res = _one(tmp_path, "t.csv", CSV, noisy)
    assert res["status"] == "equivalent"
    assert 0 < res["max_abs_diff"] < 1e-11
    assert res["n_cells_differing"] == 0 and res["n_cells_compared"] == 8


def test_csv_formatting_only_is_equivalent_with_exact_values(tmp_path):
    reformatted = 'group,n,mean,p\r\n"A",10,1.250,0.0310\r\n"B",12,2.50,0.40\r\n'
    res = _one(tmp_path, "t.csv", CSV, reformatted)
    assert res["status"] == "equivalent"
    assert res["max_abs_diff"] == 0 and "formatting" in res["detail"]


def test_csv_real_difference_reports_magnitude_and_cells(tmp_path):
    changed = "group,n,mean,p\nA,10,1.75,0.031\nB,12,2.5,0.4\n"
    res = _one(tmp_path, "t.csv", CSV, changed)
    assert res["status"] == "differs"
    assert res["n_cells_differing"] == 1
    assert res["max_abs_diff"] == pytest.approx(0.5)
    assert res["max_rel_diff"] == pytest.approx(0.4)
    assert res["columns_differing"] == ["mean"]
    assert res["examples"][0] == {"location": "row 1, column 'mean'", "committed": "1.25", "regenerated": "1.75"}


def test_integer_columns_are_exact(tmp_path):
    # rel. diff 8e-7 is inside rtol=1e-6, but a count that moved by one is not noise.
    res = _one(tmp_path, "t.csv", "n\n1234567\n", "n\n1234568\n")
    assert res["status"] == "differs" and res["n_cells_differing"] == 1


def test_csv_changed_shape_and_columns(tmp_path):
    more_rows = CSV + "C,8,3.0,0.9\n"
    res = _one(tmp_path, "rows.csv", CSV, more_rows)
    assert res["status"] == "differs"
    assert res["shape_committed"] == [2, 4] and res["shape_regenerated"] == [3, 4]
    assert "row count" in res["detail"]

    renamed = CSV.replace("mean", "avg")
    res = _one(tmp_path, "cols.csv", CSV, renamed)
    assert res["status"] == "differs"
    assert res["columns_added"] == ["avg"] and res["columns_removed"] == ["mean"]
    assert "shared columns agree" in res["detail"]


def test_csv_column_order_is_not_ignored(tmp_path):
    swapped = "group,mean,n,p\nA,1.25,10,0.031\nB,2.5,12,0.4\n"
    res = _one(tmp_path, "t.csv", CSV, swapped)
    assert res["status"] == "differs" and res["column_order_changed"] is True


def test_csv_missing_values(tmp_path):
    # R writes NA, pandas writes an empty field: both mean missing.
    res = _one(tmp_path, "na.csv", "x,y\n1.5,NA\n2.5,3\n", "x,y\n1.5,\n2.5,3\n")
    assert res["status"] == "equivalent"
    # ...but missing versus a number is a difference.
    res = _one(tmp_path, "na2.csv", "x,y\n1.5,NA\n2.5,3\n", "x,y\n1.5,7\n2.5,3\n")
    assert res["status"] == "differs" and res["n_cells_differing"] == 1
    assert res["examples"][0]["committed"] == "<missing>"


def test_mixed_text_and_number_column(tmp_path):
    # A p-value column with "<0.001" is read as text; numeric cells still get the tolerance.
    com = "term,p\na,<0.001\nb,0.0231\n"
    assert _one(tmp_path, "p.csv", com, "term,p\na,<0.001\nb,0.02310000000001\n")["status"] == "equivalent"
    res = _one(tmp_path, "p2.csv", com, "term,p\na,0.002\nb,0.0231\n")
    assert res["status"] == "differs" and res["n_cells_differing"] == 1


def test_tsv(tmp_path):
    com = "a\tb\n1.0\tx\n2.0\ty\n"
    res = _one(tmp_path, "t.tsv", com, "a\tb\n1.0000000000001\tx\n2.0\ty\n")
    assert res["kind"] == "tabular" and res["status"] == "equivalent"
    res = _one(tmp_path, "u.tsv", com, "a\tb\n1.0\tx\n2.0\tz\n")
    assert res["status"] == "differs" and res["columns_differing"] == ["b"]


def test_headerless_numeric_csv(tmp_path):
    # numpy.savetxt output: noise in row 1 must not look like renamed columns.
    com = "1.000000000000000000e+00,2.000000000000000000e+00\n3.0,4.0\n"
    reg = "1.000000000000000100e+00,2.000000000000000000e+00\n3.0,4.0\n"
    res = _one(tmp_path, "m.csv", com, reg)
    assert res["status"] == "equivalent" and res["header_row"] is False


def test_row_labels_are_compared(tmp_path):
    # R write.table layout: the header has one field fewer, pandas makes the
    # labels an index. They must still be compared.
    com = "estimate,se\n(Intercept),1.5,0.1\nx,2.5,0.2\n"
    res = _one(tmp_path, "coef.csv", com, com.replace("x,", "z,"))
    assert res["status"] == "differs"


def test_unparseable_csv_is_not_compared(tmp_path):
    res = _one(tmp_path, "bad.csv", "a,b\n1,2\n", "a,b\n1,2\n3,4,5,6\n")
    assert res["status"] == "not-compared" and res["reason"] == "unparseable"
    assert "ParserError" in res["detail"] and "regenerated" in res["detail"]


def test_empty_regenerated_csv_differs(tmp_path):
    assert _one(tmp_path, "e.csv", CSV, "")["status"] == "differs"


def test_parquet(tmp_path):
    pytest.importorskip("pyarrow")
    pd = pytest.importorskip("pandas")
    src, run = _pair(tmp_path, "x", None, None)
    pd.DataFrame({"a": [1.0, 2.0], "b": ["x", "y"]}).to_parquet(src / "t.parquet")
    pd.DataFrame({"a": [1.0 + 1e-12, 2.0], "b": ["x", "y"]}).to_parquet(run / "t.parquet")
    [res] = compare_outputs(src, run, ["t.parquet"])
    assert res["kind"] == "tabular" and res["status"] == "equivalent"


# --------------------------------------------------------------------------- #
#  JSON
# --------------------------------------------------------------------------- #

JSON_COM = {"model": "lm", "results": [{"beta": 0.5, "n": 100}, {"beta": -1.25, "n": 100}], "ok": True}


def test_json_numeric_noise_and_key_order(tmp_path):
    reg = {"ok": True, "results": [{"n": 100, "beta": 0.5 + 1e-13}, {"beta": -1.25, "n": 100}], "model": "lm"}
    res = _one(tmp_path, "r.json", json.dumps(JSON_COM), json.dumps(reg, indent=2))
    assert res["kind"] == "json" and res["status"] == "equivalent"
    assert res["max_abs_diff"] < 1e-12


def test_json_real_difference_reports_path(tmp_path):
    reg = json.loads(json.dumps(JSON_COM))
    reg["results"][1]["beta"] = -1.5
    res = _one(tmp_path, "r.json", json.dumps(JSON_COM), json.dumps(reg))
    assert res["status"] == "differs"
    assert res["differing_paths"] == ["$.results[1].beta"]
    assert res["max_abs_diff"] == pytest.approx(0.25)
    assert "$.results[1].beta" in res["detail"]


def test_json_structure_and_type_changes(tmp_path):
    reg = {"model": "lm", "results": [{"beta": 0.5, "n": 100}], "ok": 1, "extra key": None}
    res = _one(tmp_path, "r.json", json.dumps(JSON_COM), json.dumps(reg))
    assert res["status"] == "differs"
    assert set(res["differing_paths"]) == {"$.ok", "$.results", '$["extra key"]'}


def test_unparseable_json(tmp_path):
    res = _one(tmp_path, "r.json", json.dumps(JSON_COM), '{"model": ')
    assert res["status"] == "not-compared" and "JSONDecodeError" in res["detail"]


# --------------------------------------------------------------------------- #
#  Text
# --------------------------------------------------------------------------- #

def test_text_line_endings_only(tmp_path):
    res = _one(tmp_path, "out/log.txt", "line one\nline two  \n\n", "line one\r\nline two\r\n")
    assert res["kind"] == "text" and res["status"] == "equivalent"
    assert res["normalization"] == "line-endings"


STATS = """Coefficients:
            Estimate Std. Error t value Pr(>|t|)
(Intercept)  0.0000  0.1234567890   1.23    0.221
x            2.5012345678  0.0412345678  60.66   <2e-16
n = 150
"""


def test_text_stats_table_far_decimals_is_equivalent(tmp_path):
    # A printed zero that gains a minus sign re-aligns the column; the 10th decimal moves.
    reg = STATS.replace("(Intercept)  0.0000", "(Intercept) -0.0000").replace("2.5012345678", "2.5012345679")
    res = _one(tmp_path, "summary.txt", STATS, reg)
    assert res["status"] == "equivalent"
    assert res["normalization"] == "whitespace+numeric-tolerance"
    assert res["n_numbers_differing"] == 0 and res["max_abs_diff"] < 1e-9


def test_text_number_beyond_tolerance_differs_with_line(tmp_path):
    res = _one(tmp_path, "summary.txt", STATS, STATS.replace("60.66", "61.02"))
    assert res["status"] == "differs"
    assert res["n_numbers_differing"] == 1 and res["first_differing_line"] == 4
    assert res["max_abs_diff"] == pytest.approx(0.36)


def test_text_count_is_exact(tmp_path):
    res = _one(tmp_path, "s.txt", "n = 1234567\n", "n = 1234568\n")
    assert res["status"] == "differs"


def test_text_differing_words(tmp_path):
    res = _one(tmp_path, "notes.md", "# Result\nThe effect is significant.\n",
               "# Result\nThe effect is not significant.\n")
    assert res["status"] == "differs"
    assert res["n_lines_differing"] == 1 and res["first_differing_line"] == 2
    assert res["examples"][0]["regenerated"] == "The effect is not significant."


def test_html_embedded_image_is_masked_and_flagged(tmp_path):
    com = '<p>beta = 0.5</p><img src="data:image/png;base64,iVBORw0KGgoAAAA1">\n'
    reg = '<p>beta = 0.5</p><img src="data:image/png;base64,iVBORw0KGgoBBBB2">\n'
    res = _one(tmp_path, "report.html", com, reg)
    # The text matches to the last digit, but "equivalent" would vouch for figures
    # nobody compared — regenerated plots almost always differ byte-wise.
    assert res["status"] == "not-compared" and res["reason"] == "embedded-data"
    assert "cannot be compared" in res["detail"]


def test_text_not_utf8_is_not_compared(tmp_path):
    res = _one(tmp_path, "l.txt", b"caf\xe9\n", b"caf\xe8\n")
    assert res["status"] == "not-compared" and res["reason"] == "unparseable"


# --------------------------------------------------------------------------- #
#  Binary, limits and path safety
# --------------------------------------------------------------------------- #

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(64))


def test_differing_png_is_not_compared_never_differs(tmp_path):
    res = _one(tmp_path, "fig/plot.png", PNG, PNG[:-1] + b"\x00")
    assert res["kind"] == "binary" and res["status"] == "not-compared"
    assert res["reason"] == "no-comparator" and ".png" in res["detail"]
    assert _one(tmp_path, "fig/same.png", PNG, PNG)["status"] == "identical"


def test_oversized_file_is_not_compared(tmp_path):
    res = _one(tmp_path, "big.csv", CSV, CSV + "C,1,1,1\n", max_bytes=10)
    assert res["status"] == "not-compared" and res["reason"] == "too-large"


def test_missing_side_is_not_compared(tmp_path):
    res = _one(tmp_path, "gone.csv", CSV, None)
    assert res["status"] == "not-compared" and res["reason"] == "missing"
    assert "regenerated" in res["detail"]


def test_symlink_is_not_followed(tmp_path):
    src, run = _pair(tmp_path, "t.csv", CSV, None)
    outside = tmp_path / "outside.csv"
    outside.write_text(CSV)
    try:
        (run / "t.csv").symlink_to(outside)
        (run / "d").symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not creatable on this platform/user")
    (src / "d").mkdir()
    (src / "d" / "outside.csv").write_text(CSV)
    results = {r["path"]: r for r in compare_outputs(src, run, ["t.csv", "d/outside.csv"])}
    assert results["t.csv"]["reason"] == "symlink"
    assert results["d/outside.csv"]["reason"] == "symlink"   # a link in a parent directory counts too
    assert all(r["status"] == "not-compared" for r in results.values())


@pytest.mark.parametrize("bad", ["../x.csv", "a/../../x.csv", "/etc/passwd", "C:/x.csv", "a\\..\\x.csv", ""])
def test_path_traversal_is_refused(tmp_path, bad):
    src, run = _pair(tmp_path, "x.csv", CSV, CSV)
    (tmp_path / "x.csv").write_text(CSV)  # a real file one level up, which must not be reached
    [res] = compare_outputs(src, run, [bad])
    assert res["status"] == "not-compared" and res["reason"] == "unsafe-path"


def test_one_bad_file_does_not_break_the_batch_and_results_are_sorted(tmp_path):
    src, run = _pair(tmp_path, "b.csv", "a,b\n1,2\n", "a,b\n1,2\n3,4,5,6\n")
    _pair(tmp_path, "a.txt", "x\n", "x\r\n")
    _pair(tmp_path, "c.json", "[1]", "[1.0000000000001]")
    results = compare_outputs(src, run, ["c.json", "../evil", "b.csv", "a.txt", "a.txt"])
    assert [r["path"] for r in results] == ["../evil", "a.txt", "b.csv", "c.json"]
    assert [r["status"] for r in results] == ["not-compared", "equivalent", "not-compared", "equivalent"]


def test_file_cap_prefers_comparable_kinds(tmp_path):
    src, run = _pair(tmp_path, "a.png", PNG, PNG)
    for name in ("b.csv", "c.txt"):
        _pair(tmp_path, name, "1\n", "1\n")
    results = {r["path"]: r for r in compare_outputs(src, run, ["a.png", "b.csv", "c.txt"], max_files=2)}
    assert results["b.csv"]["status"] == results["c.txt"]["status"] == "identical"
    assert results["a.png"]["status"] == "not-compared" and results["a.png"]["reason"] == "over-file-cap"


# --------------------------------------------------------------------------- #
#  Summary and import cost
# --------------------------------------------------------------------------- #

def test_summarize_counts_and_headline():
    results = [{"status": "identical"}, {"status": "identical"}, {"status": "equivalent"},
               {"status": "differs"}, {"status": "not-compared"}]
    s = summarize(results)
    assert s["total"] == 5
    assert s["counts"] == {"identical": 2, "equivalent": 1, "differs": 1, "not-compared": 1}
    assert "2 identical" in s["headline"] and "1 differs" in s["headline"]
    assert "not a verdict" in s["headline"]
    empty = summarize([])
    assert empty["total"] == 0 and "nothing to compare" in empty["headline"]


def test_import_does_not_load_pandas():
    src = str(Path(reprobe.compare.__file__).resolve().parents[1])
    code = (f"import sys; sys.path.insert(0, {src!r}); import reprobe.compare; "
            "print('pandas' in sys.modules or 'numpy' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_a_count_and_a_float_spelling_of_a_different_count_differ(tmp_path):
    """1234567 vs 1234568.0 is a relative difference of 8e-7: inside rtol=1e-6, yet
    it is a difference of one in a count. Whole numbers on both sides are exact,
    however a writer spelled them."""
    for name, a, b in (("c.json", "[1234567]", "[1234568.0]"),
                       ("c.csv", "n\n1234567\n", "n\n1234568.0\n"),
                       ("c.txt", "n = 1234567\n", "n = 1234568.0\n")):
        assert _one(tmp_path, name, a, b)["status"] == "differs", name


def test_float_noise_on_a_whole_number_result_is_still_equivalent(tmp_path):
    # R prints 3.0 as 3; the next run's 3.0000000000000004 is rounding, not a change
    assert _one(tmp_path, "m.csv", "mean\n3\n", "mean\n3.0000000000000004\n")["status"] == "equivalent"
    assert _one(tmp_path, "m.json", "[3]", "[3.0000000000000004]")["status"] == "equivalent"


def test_tiny_values_are_not_absorbed_by_the_absolute_tolerance(tmp_path):
    """p = 1e-10 and p = 9e-10 differ ninefold. With atol=1e-9 both were "zero"."""
    assert _one(tmp_path, "p.csv", "p\n1e-10\n", "p\n9e-10\n")["status"] == "differs"
    assert _one(tmp_path, "z.csv", "r\n0\n", "r\n1e-16\n")["status"] == "equivalent"   # noise around zero
