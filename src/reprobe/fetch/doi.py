"""DOI parsing and resolution, shared by the fetcher registry, the Dataverse pin
check and the paper locator.

A DOI arrives in many spellings (``doi:10.…``, ``DOI: 10.…``,
``https://www.doi.org/10.…``, ``http://dx.doi.org/10.…``, percent-encoded) and is
then interpolated into API URLs on the TRUSTED host, so it is reduced to the bare
``10.<registrant>/<suffix>`` form and held to the registered shape before use.

Not idutils: its DOI pattern accepts any suffix (``.+`` — whitespace, ``?`` and
``#`` included) and misses ``www.doi.org``, so the strict check below would still
be needed, and it would bring isbnlib along for one regex."""

from __future__ import annotations

import re
from urllib.parse import quote, unquote

from .base import get

# Crossref's recommended pattern (covers ~99.9% of registered DOIs). Strict on
# purpose: anything else is not interpolated into a URL.
DOI_STRICT = re.compile(r"^10\.\d{4,9}/[-._;()/:A-Za-z0-9]+$")
_PREFIX = re.compile(
    r"^(?:doi\s*:\s*|info:doi/|(?:https?://)?(?:dx\.|www\.)?doi\.org/(?:doi:)?)", re.I)


def normalize_doi(ref: str) -> str | None:
    """``10.x/y`` for any common spelling of a DOI, else None. Never matches a
    URL on some other host, so a deposit URL is not mistaken for a DOI."""
    s = unquote(str(ref or "").strip())
    s = _PREFIX.sub("", s, count=1).strip()
    return s if DOI_STRICT.match(s) else None


def resolve_doi(doi: str, *, timeout: int = 30) -> str | None:
    """The URL a DOI's registrant registered, read from the doi.org handle API.

    The API answers with the target in JSON, so nothing at the target is fetched
    to learn it: following the redirect chain instead would fetch the registrant's
    landing page — author-controlled for a self-minted DOI — from the trusted
    host. The caller decides whether the target may be fetched at all."""
    doi = normalize_doi(doi) or ""
    if not doi:
        return None
    try:
        r = get(f"https://doi.org/api/handles/{quote(doi, safe='/')}",
                params={"type": "URL"}, timeout=timeout, allow_redirects=False)
        if r.status_code != 200:
            return None
        body = r.json() or {}
    except Exception:                            # noqa: BLE001 - unresolvable is an answer
        return None
    if body.get("responseCode") != 1:
        return None
    for value in body.get("values") or []:
        if not isinstance(value, dict) or value.get("type") != "URL":
            continue
        url = (value.get("data") or {}).get("value")
        if isinstance(url, str) and url.lower().startswith(("http://", "https://")):
            return url.strip()
    return None
