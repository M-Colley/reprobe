"""Fetcher routing + id/DOI parsing. Pure (no network): select() and can_handle
only inspect the string."""

import pytest

from reprobe.fetch.registry import select, _DOI_LIKE
from reprobe.fetch.figshare import _ID as FIG_ID
from reprobe.fetch.dryad import _DOI as DRYAD_DOI
from reprobe.fetch.software_heritage import _SWHID


def _name(ref):
    f = select(ref)
    return f.name if f else None


@pytest.mark.parametrize("ref,expected", [
    ("https://figshare.com/articles/dataset/foo/12345678", "figshare"),
    ("10.6084/m9.figshare.12345678", "figshare"),
    ("https://datadryad.org/stash/dataset/doi:10.5061/dryad.abc123", "dryad"),
    ("10.5061/dryad.abc123", "dryad"),
    ("https://osf.io/ab12c/", "osf"),
    ("10.17605/OSF.IO/AB12C", "osf"),
    ("https://dataverse.harvard.edu/dataset.xhtml?persistentId=doi:10.7910/DVN/ABCDEF", "dataverse"),
    ("swh:1:dir:0000000000000000000000000000000000000000", "software_heritage"),
    ("https://archive.softwareheritage.org/swh:1:dir:0000000000000000000000000000000000000000", "software_heritage"),
    ("https://zenodo.org/records/123456", "zenodo"),
    ("https://github.com/foo/bar", "git"),
    ("https://anonymous.4open.science/r/foo-1234", "anonymous_github"),
])
def test_routing(ref, expected):
    assert _name(ref) == expected


def test_unmatched_is_none():
    assert _name("not-a-real-reference-xyz") is None


def test_id_and_doi_parsing():
    m = FIG_ID.search("10.6084/m9.figshare.99887766")
    assert (m.group(1) or m.group(2)) == "99887766"
    assert DRYAD_DOI.search("doi:10.5061/dryad.q2bvt").group(1) == "10.5061/dryad.q2bvt"
    assert _SWHID.search("swh:1:rev:" + "a" * 40).group(1).startswith("swh:1:rev:")


def test_doi_like_matches():
    assert _DOI_LIKE.search("10.5281/zenodo.1")
    assert _DOI_LIKE.search("https://doi.org/10.6084/m9.figshare.1")
    assert not _DOI_LIKE.search("https://example.com/notadoi")
