"""Secondary data sources — the "code here, data there" artifact.

A submission is routinely split in two: the code sits in a git repo and the data
sits in a repository deposit (OSF, Zenodo, Dryad, ...) that the README links to
in prose. Fetching either half alone produces a report about nothing — the code
half dies on ``FileNotFoundError`` at the first ``read_csv``, and the data half
has nothing to run. Neither outcome is a statement about the artifact.

Every source here goes through the SAME fetcher registry as a primary
submission, so OSF / Zenodo / Dryad / figshare / Dataverse / git / local paths
work unchanged. A bare http(s) URL — an OSF ``?zip=`` bundle link, a lab web
server — falls back to a hardened download plus archive extraction, reusing the
SSRF guard, byte caps and zip-bomb guards in ``base.py``.

Two honesty rules are enforced in this module:

* a data source NEVER overwrites a file the code source already provided.
  Collisions are skipped and reported — a silent overwrite would mean the code
  reviewed is not the code submitted.
* a data source NEVER strengthens the Available badge. It is author-controlled
  bytes fetched at review time, and it is recorded with its own pin (for OSF
  project storage, that pin is honestly ``none``).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from urllib.parse import unquote, urlparse

from ..models import FetchResult, Pin
from .base import (FetchError, assert_safe_url, download, maybe_unzip,
                   new_checksum_stats, record_download, safe_join)


def parse_ref(spec: str) -> tuple[str, str]:
    """Split ``URL::subdir`` into ``(url, subdir)``; ``subdir`` may be empty.

    The default (no ``::``) merges the deposit at the root of the artifact tree,
    which is where an author's own working copy had it when the README said
    "download the logs". ``::`` places it somewhere else when the code expects a
    specific directory."""
    head, sep, tail = spec.rpartition("::")
    # "::" also occurs inside an IPv6 literal (http://[::1]/x). Such hosts are
    # refused by the SSRF guard anyway — but don't mangle one into a subdir.
    if sep and tail and "]" not in tail and "://" not in tail:
        return head.strip(), tail.strip().strip("/\\")
    return spec.strip(), ""


def _filename_for(url: str) -> str:
    """A on-disk name for a URL that need not name a file."""
    p = urlparse(url)
    name = Path(unquote(p.path)).name
    if name and "." in name:
        return name
    # OSF/figshare bundle links end in "/?zip=": the payload is an archive even
    # though the URL names no file, and maybe_unzip() dispatches on the suffix.
    blob = f"{p.path} {p.query}".lower()
    return "data.zip" if "zip" in blob else "data.bin"


class DirectUrlFetcher:
    """Plain http(s) download for a DATA source only.

    Deliberately kept out of the primary registry: a bare URL as a *submission*
    should still fail with the "no fetcher matched" list, because a submission
    needs a pin and a bare URL has none. A data link pasted out of a README is
    the opposite case — there is no platform API to ask, and refusing it just
    means the code cannot run at all."""

    name = "direct-url"

    def can_handle(self, ref: str) -> bool:
        return ref.lower().startswith(("http://", "https://"))

    def fetch(self, ref: str, dest: Path) -> FetchResult:
        assert_safe_url(ref)                     # http(s) + public host; raises FetchError
        dest.mkdir(parents=True, exist_ok=True)
        warnings: list[str] = []
        stats = new_checksum_stats()

        archive = safe_join(dest, _filename_for(ref))
        before = set(dest.iterdir())
        ok, note = download(ref, archive, restrict_public=True)
        record_download(stats, ok, note, had_checksum=False)
        if not ok:
            raise FetchError(f"download failed: {note}")

        maybe_unzip(dest, warnings)
        extracted = set(dest.iterdir()) - before - {archive}
        if extracted and archive.exists():
            # The tree is the payload; keeping the bundle too doubles the disk a
            # multi-GB deposit costs.
            archive.unlink()
        elif not extracted and archive.suffix == ".zip":
            warnings.append(f"{archive.name} did not extract — treating it as an opaque file")

        return FetchResult(
            input=ref, resolved_type="direct-url", src_dir=str(dest),
            pin=Pin(kind="none", value=ref),
            fetch_layer="http-download",
            checksum_verified=False,
            warnings=warnings + [
                "direct URL: no platform checksum or version to verify against, so these "
                "bytes are whatever the server returned today"],
            metadata={"checksums": stats, "extracted": bool(extracted)},
        )


def fetch_data_source(ref: str, dest: str | Path) -> FetchResult:
    """Fetch one data deposit into ``dest``.

    Platform fetchers are tried first — they carry pins and per-file checksums —
    then a bare http(s) URL falls back to ``DirectUrlFetcher``."""
    from .registry import fetch as fetch_primary, select

    dest = Path(dest)
    if select(ref) is not None:
        return fetch_primary(ref, dest)
    direct = DirectUrlFetcher()
    if direct.can_handle(ref):
        return direct.fetch(ref, dest)
    # Let the registry produce its full "supported sources" message.
    return fetch_primary(ref, dest)


# Data repositories whose links in a README mean "the data lives over there".
_DEPOSIT_HOSTS = ("osf.io", "zenodo.org", "datadryad.org", "figshare.com",
                  "dataverse.harvard.edu", "dataverse.nl", "researchdata")
# Strict enough that only a well-formed URL is ever echoed into a report.
_URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")
_DOC_GLOBS = ("README*", "readme*", "*.md", "*.txt", "*.rst")


def _clean_url(url: str) -> str:
    """Trim the prose a URL was embedded in.

    READMEs write links as ``[OSF](https://…/?zip=)`` and end sentences with
    them, so a greedy match keeps the markdown. A trailing bracket that closes
    nothing inside the URL belongs to the text — but a balanced one (…/Foo_(bar))
    is part of it."""
    url = url.rstrip(".,;:!?'\"")
    while url and url[-1] in ")]>":
        opener = {")": "(", "]": "[", ">": "<"}[url[-1]]
        if url.count(opener) >= url.count(url[-1]):
            break                        # balanced: the bracket is part of the URL
        url = url[:-1]
    return url.rstrip(".,;:!'\"")


def referenced_deposits(src_root: str | Path, limit: int = 5) -> list[str]:
    """Data-repository URLs the artifact's own documentation points at.

    An artifact that says "download the logs from OSF" has declared its data in
    prose — true for a human, invisible to a harness. Surfacing the link turns a
    mystified ``FileNotFoundError`` into a one-line instruction for the chair.
    Read-only: nothing here is fetched, and only well-formed URLs are echoed."""
    root = Path(src_root)
    found: list[str] = []
    for pattern in _DOC_GLOBS:
        for doc in sorted(root.glob(pattern))[:20]:
            if not doc.is_file() or doc.stat().st_size > 1_000_000:
                continue
            text = doc.read_text(encoding="utf-8", errors="replace")
            for raw in _URL_RE.findall(text):
                url = _clean_url(raw)
                if any(h in url.lower() for h in _DEPOSIT_HOSTS) and url not in found:
                    found.append(url)
                    if len(found) >= limit:
                        return found
    return found


def merge_into(src_root: str | Path, dest_root: str | Path,
               into: str = "") -> tuple[list[str], list[str]]:
    """Copy the tree at ``src_root`` under ``dest_root``/``into``.

    Returns ``(copied_paths, collisions)``, both relative to ``dest_root``. The
    paths matter beyond counting: the rest of the pipeline has to tell deposit
    files from the submission's own, or a deposit of figures supplies "the
    paper" and its PDFs get described as committed in a repository they were
    never in.

    An existing file is NEVER replaced: the code source defines the artifact, and
    a data deposit that silently overwrote a script would mean the reviewed code
    is not the submitted code. Collisions are returned so the report can name
    them."""
    src_root = Path(src_root)
    target_root = safe_join(Path(dest_root), into) if into else Path(dest_root)
    target_root.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    collisions: list[str] = []
    for src in sorted(p for p in src_root.rglob("*") if p.is_file()):
        rel = src.relative_to(src_root).as_posix()
        # safe_join re-validates every component, so a deposit cannot escape the
        # tree through a crafted member name that survived extraction.
        dst = safe_join(target_root, rel)
        out_rel = dst.relative_to(Path(dest_root)).as_posix()
        if dst.exists():
            collisions.append(out_rel)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(out_rel)
    return copied, collisions
