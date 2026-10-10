"""Fetcher selection. Order matters: specific platforms before the generic git
matcher; local path last as a catch-all. A bare DOI / doi.org URL that no
platform claims directly is resolved (via the doi.org handle API) and
re-dispatched.

configure() rebuilds the list with chair-supplied host lists from the
``fetch:`` section of config/pins.yaml (extra_git_hosts / dataverse_hosts), so
adding an institutional GitLab or Dataverse install never means editing src/.

``untrusted=True`` is for references the AUTHOR chose — a manifest's
``data_sources`` entry, or wherever a DOI's registrant points it. The chair's own
``--data`` / submission ref may name a local path; an author's must never: the
local fetcher copies whatever host path it is given, ``..`` and ``~/.ssh``
included, into a tree that is then shown to an LLM and mounted into a sandbox."""

from __future__ import annotations

from pathlib import Path

from ..models import FetchResult
from .anonymous_github import AnonymousGithubFetcher
from .base import Fetcher, FetchError, RefusedSource, assert_safe_url
from .dataverse import DataverseFetcher
from .doi import normalize_doi, resolve_doi
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


def check_untrusted_ref(ref: str) -> str:
    """Admit an author-chosen reference, or raise RefusedSource.

    Admitted: an http(s) URL whose host resolves only to public addresses, a
    DOI, or a SWHID (both resolve through fixed public APIs, never a host the
    author names). Refused: local paths, ``file:``/``ssh://``/``git://`` and
    scp-style ``git@host:path`` refs (the latter two would clone with the
    chair's SSH keys), and anything that reaches an internal address."""
    ref = str(ref or "").strip()
    if not ref:
        raise RefusedSource("refusing an empty author-declared source")
    low = ref.lower()
    if not low.startswith(("http://", "https://")):
        if normalize_doi(ref) or low.startswith("swh:1:"):
            return ref
        raise RefusedSource(
            f"refusing author-declared source {ref!r}: only http(s) URLs, DOIs and SWHIDs are "
            "fetched from a manifest (local paths, ssh/scp-style git and other schemes are "
            "operator-only, via --data)")
    try:
        assert_safe_url(ref)
    except FetchError as e:
        raise RefusedSource(f"refusing author-declared source: {e}")
    return ref


def select(ref: str, *, untrusted: bool = False) -> Fetcher | None:
    for f in _FETCHERS:
        if untrusted and isinstance(f, LocalFetcher):
            continue
        try:
            if f.can_handle(ref):
                return f
        except Exception:
            continue
    return None


def fetch(ref: str, dest: str | Path, *, allow_lfs: bool = False,
          untrusted: bool = False) -> FetchResult:
    dest = Path(dest)
    if untrusted:
        ref = check_untrusted_ref(ref)
    fetcher = select(ref, untrusted=untrusted)
    use_ref = ref
    doi = normalize_doi(ref) if fetcher is None else None
    if doi:
        # Ask doi.org where the DOI points, then re-dispatch on that URL. The
        # target is whatever the DOI's registrant registered — author-controlled
        # for a self-minted DOI — so it gets the untrusted rules whoever passed
        # the DOI in: never a local path, never an internal host.
        resolved = resolve_doi(doi)
        if resolved:
            try:
                check_untrusted_ref(resolved)
            except RefusedSource as e:
                raise RefusedSource(f"DOI {doi} points at {resolved!r}, which is not fetched: {e}")
            fetcher = select(resolved, untrusted=True)
            if fetcher is not None:
                use_ref, untrusted = resolved, True
    if fetcher is None:
        raise FetchError(
            f"no fetcher matched '{ref}'. Supported: git hosts, Zenodo, figshare, "
            f"Dryad, OSF, Dataverse, Software Heritage, anonymous.4open.science, "
            f"local paths, and resolvable DOIs. Institutional git hosts / Dataverse "
            f"installs can be added in config/pins.yaml (fetch.extra_git_hosts / "
            f"fetch.dataverse_hosts)."
        )
    # allow_lfs / untrusted are git-only switches; other fetchers keep the
    # 2-arg contract (their hosts are fixed, or SSRF-guarded inside — Dataverse).
    if isinstance(fetcher, GitHostFetcher):
        return fetcher.fetch(use_ref, dest, allow_lfs=allow_lfs, untrusted=untrusted)
    return fetcher.fetch(use_ref, dest)
