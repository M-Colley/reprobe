"""Fetcher selection. Order matters: specific platforms before the generic git
matcher; local path last as a catch-all. A bare DOI / doi.org URL that no
platform claims directly is resolved (followed) and re-dispatched."""

from __future__ import annotations

import re
from pathlib import Path

import requests

from ..models import FetchResult
from .anonymous_github import AnonymousGithubFetcher
from .base import Fetcher, FetchError
from .dataverse import DataverseFetcher
from .dryad import DryadFetcher
from .figshare import FigshareFetcher
from .git_host import GitHostFetcher
from .local import LocalFetcher
from .osf import OSFFetcher
from .software_heritage import SoftwareHeritageFetcher
from .zenodo import ZenodoFetcher

# Specific platforms first; git + local are the broad catch-alls.
_FETCHERS: list[Fetcher] = [
    AnonymousGithubFetcher(),
    ZenodoFetcher(),
    FigshareFetcher(),
    DryadFetcher(),
    OSFFetcher(),
    DataverseFetcher(),
    SoftwareHeritageFetcher(),
    GitHostFetcher(),
    LocalFetcher(),
]

_DOI_LIKE = re.compile(r"^(https?://)?(dx\.)?doi\.org/10\.|^10\.\d{4,}/", re.I)


def select(ref: str) -> Fetcher | None:
    for f in _FETCHERS:
        try:
            if f.can_handle(ref):
                return f
        except Exception:
            continue
    return None


def _resolve_doi(ref: str) -> str | None:
    url = ref if ref.lower().startswith("http") else f"https://doi.org/{ref}"
    try:
        r = requests.get(url, allow_redirects=True, timeout=30)
        return r.url
    except Exception:
        return None


def fetch(ref: str, dest: str | Path) -> FetchResult:
    dest = Path(dest)
    fetcher = select(ref)
    use_ref = ref
    if fetcher is None and _DOI_LIKE.search(ref):
        # follow the DOI to its landing page, then re-dispatch on the real URL
        resolved = _resolve_doi(ref)
        if resolved:
            fetcher = select(resolved)
            if fetcher is not None:
                use_ref = resolved
    if fetcher is None:
        raise FetchError(
            f"no fetcher matched '{ref}'. Supported: git hosts, Zenodo, figshare, "
            f"Dryad, OSF, Dataverse, Software Heritage, anonymous.4open.science, "
            f"local paths, and resolvable DOIs."
        )
    return fetcher.fetch(use_ref, dest)
