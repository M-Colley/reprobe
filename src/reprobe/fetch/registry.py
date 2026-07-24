"""Fetcher selection. Order matters: specific platforms before the generic git
matcher; local path last as a catch-all. A bare DOI / doi.org URL that no
platform claims directly is resolved (followed) and re-dispatched.

configure() rebuilds the list with chair-supplied host lists from the
``fetch:`` section of config/pins.yaml (extra_git_hosts / dataverse_hosts), so
adding an institutional GitLab or Dataverse install never means editing src/."""

from __future__ import annotations

import re
from pathlib import Path

from ..models import FetchResult
from .anonymous_github import AnonymousGithubFetcher
from .base import Fetcher, FetchError, get
from .dataverse import DataverseFetcher
from .dryad import DryadFetcher
from .figshare import FigshareFetcher
from .git_host import GitHostFetcher
from .local import LocalFetcher
from .osf import OSFFetcher
from .software_heritage import SoftwareHeritageFetcher
from .zenodo import ZenodoFetcher


def _build(fetch_cfg: dict | None = None) -> list[Fetcher]:
    cfg = fetch_cfg or {}
    # Specific platforms first; git + local are the broad catch-alls.
    return [
        AnonymousGithubFetcher(),
        ZenodoFetcher(),
        FigshareFetcher(),
        DryadFetcher(),
        OSFFetcher(),
        DataverseFetcher(extra_hosts=tuple(cfg.get("dataverse_hosts") or ())),
        SoftwareHeritageFetcher(),
        GitHostFetcher(extra_hosts=tuple(cfg.get("extra_git_hosts") or ())),
        LocalFetcher(),
    ]


_FETCHERS: list[Fetcher] = _build()


def configure(fetch_cfg: dict | None) -> None:
    """Apply the config/pins.yaml ``fetch:`` section (call before fetch())."""
    global _FETCHERS
    _FETCHERS = _build(fetch_cfg)


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
        r = get(url, allow_redirects=True, timeout=30)
        return r.url
    except Exception:
        return None


def fetch(ref: str, dest: str | Path, *, allow_lfs: bool = False) -> FetchResult:
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
            f"local paths, and resolvable DOIs. Institutional git hosts / Dataverse "
            f"installs can be added in config/pins.yaml (fetch.extra_git_hosts / "
            f"fetch.dataverse_hosts)."
        )
    # allow_lfs is a git-only, opt-in switch (default off keeps skip-smudge); only
    # the git fetcher accepts it — other fetchers keep the 2-arg contract.
    if isinstance(fetcher, GitHostFetcher):
        return fetcher.fetch(use_ref, dest, allow_lfs=allow_lfs)
    return fetcher.fetch(use_ref, dest)
