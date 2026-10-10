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

A source the AUTHOR declared (manifest ``data_sources``) is fetched with
``untrusted=True``: never a local path, never ssh/scp git, never an internal
host — see ``registry.check_untrusted_ref``. Only the operator's ``--data`` may
name a directory on the host.

Two honesty rules are enforced in this module:

* a data source NEVER overwrites a file the code source already provided.
  Collisions are skipped and reported — a silent overwrite would mean the code
  reviewed is not the code submitted.
* a data source NEVER strengthens the Available badge. It is author-controlled
  bytes fetched at review time, and it is recorded with its own pin (for OSF
  project storage, that pin is honestly ``none``).
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from ..models import FetchResult, Pin
from .base import (
    FetchError,
    assert_safe_url,
    download,
    get,
    maybe_unzip,
    new_checksum_stats,
    probe_status,
    record_download,
    safe_join,
)


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


def fetch_data_source(ref: str, dest: str | Path, *, untrusted: bool = False) -> FetchResult:
    """Fetch one data deposit into ``dest``.

    Platform fetchers are tried first — they carry pins and per-file checksums —
    then a bare http(s) URL falls back to ``DirectUrlFetcher``.

    ``untrusted=True`` for a source the author declared (manifest
    ``data_sources``): a local path, an ssh/scp-style or non-http(s) reference,
    or an internal host raises ``RefusedSource`` (a FetchError) before anything
    is fetched. Leave it False only for the operator's own ``--data``."""
    from .registry import check_untrusted_ref, select
    from .registry import fetch as fetch_primary

    dest = Path(dest)
    if untrusted:
        ref = check_untrusted_ref(ref)
    if select(ref, untrusted=untrusted) is not None:
        return fetch_primary(ref, dest, untrusted=untrusted)
    direct = DirectUrlFetcher()
    if direct.can_handle(ref):
        return direct.fetch(ref, dest)
    # Let the registry resolve a DOI, or produce its "supported sources" message.
    return fetch_primary(ref, dest, untrusted=untrusted)


_ZENODO_ID_RE = re.compile(r"(?:zenodo\.org/records?/|10\.5281/zenodo\.)(\d+)", re.I)
_OSF_GUID_RE = re.compile(r"(?:osf\.io/|10\.17605/osf\.io/)([a-z0-9]{4,8})", re.I)


def probe_data_source(url: str) -> dict[str, Any]:
    """Ask a deposit what state it is in. Metadata only — nothing is downloaded.

    This runs when the CODE half of a composite artifact could not be fetched. The
    run stops there, but the deposit's state is the availability finding that
    survives it: "the data is deposited and its embargo lifts on 2026-09-21" and
    "there is nothing at that DOI" are opposite conclusions about the same paper,
    and a report that mentions neither hands the chair nothing. Downloading would
    be wasted work — there is no code left to run it against.

    Never raises. It is the last thing standing on a path that already failed."""
    rec: dict[str, Any] = {"input": url, "status": "unknown", "detail": ""}
    try:
        if (m := _ZENODO_ID_RE.search(url)):
            return {**rec, **_probe_zenodo(m.group(1))}
        if (m := _OSF_GUID_RE.search(url)):
            return {**rec, **_probe_osf(m.group(1))}
        # Every redirect hop is SSRF-checked: a plain GET would follow a public
        # URL into the chair's network and echo an internal service's status.
        code = probe_status(url, timeout=20)
        return {**rec, "status": "available" if code < 400 else "not-found",
                "detail": f"unauthenticated GET returned HTTP {code}"}
    except Exception as e:                       # noqa: BLE001 - diagnosis only
        return {**rec, "detail": f"could not be checked: {type(e).__name__}: {e}"[:200]}


def _probe_zenodo(rec_id: str) -> dict[str, Any]:
    r = get(f"https://zenodo.org/api/records/{rec_id}", timeout=30)
    if r.status_code == 404:
        return {"status": "not-found", "detail": f"Zenodo has no record {rec_id}"}
    meta = (r.json() or {}).get("metadata") or {}
    access = str(meta.get("access_right") or "").lower()
    title = str(meta.get("title") or "")[:120]
    n = len((r.json() or {}).get("files") or [])
    if access == "embargoed":
        until = meta.get("embargo_date") or "an unstated date"
        return {"status": "embargoed",
                "detail": f"Zenodo record {rec_id} ({title!r}) exists but is embargoed until "
                          f"{until}; the API lists no downloadable files before then"}
    if access in ("restricted", "closed"):
        return {"status": "restricted",
                "detail": f"Zenodo record {rec_id} ({title!r}) is {access} — access is by "
                          "request to the depositor, not open"}
    return {"status": "available",
            "detail": f"Zenodo record {rec_id} ({title!r}) is {access or 'open'}, {n} file(s)"}


def _probe_osf(guid: str) -> dict[str, Any]:
    r = get(f"https://api.osf.io/v2/nodes/{guid}/", timeout=30)
    if r.status_code in (404, 410):
        return {"status": "not-found", "detail": f"OSF has no public node {guid}"}
    if r.status_code == 403:
        return {"status": "restricted", "detail": f"OSF node {guid} is private"}
    attrs = ((r.json() or {}).get("data") or {}).get("attributes") or {}
    title = str(attrs.get("title") or "")[:120]
    if attrs.get("public"):
        return {"status": "available", "detail": f"OSF node {guid} ({title!r}) is public"}
    return {"status": "restricted", "detail": f"OSF node {guid} ({title!r}) is not public"}


# Data repositories whose links in a README mean "the data lives over there".
_DEPOSIT_HOSTS = ("osf.io", "zenodo.org", "datadryad.org", "figshare.com",
                  "dataverse.harvard.edu", "dataverse.nl", "researchdata")
# Strict enough that only a well-formed URL is ever echoed into a report.
_URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")
_DOC_GLOBS = ("README*", "readme*", "*.md", "*.txt", "*.rst")


_MAX_DOC_CHARS = 1_000_000
_MAX_DOC_URLS = 2000
_MAX_URL_CHARS = 2048


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
            # A README is author-controlled: bound what is read and what is cleaned.
            # (_clean_url re-counts brackets per trailing bracket, so a 200 KB line of
            # `http://x)))))…` took half a minute.)
            with doc.open("r", encoding="utf-8", errors="replace") as fh:
                text = fh.read(_MAX_DOC_CHARS)
            for raw in _URL_RE.findall(text)[:_MAX_DOC_URLS]:
                if len(raw) > _MAX_URL_CHARS:
                    continue
                url = _clean_url(raw)
                if any(h in url.lower() for h in _DEPOSIT_HOSTS) and url not in found:
                    found.append(url)
                    if len(found) >= limit:
                        return found
    return found


def merge_into(src_root: str | Path, dest_root: str | Path,
               into: str = "", *, skipped: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Copy the tree at ``src_root`` under ``dest_root``/``into``.

    Returns ``(copied_paths, collisions)``, both relative to ``dest_root``. The
    paths matter beyond counting: the rest of the pipeline has to tell deposit
    files from the submission's own, or a deposit of figures supplies "the
    paper" and its PDFs get described as committed in a repository they were
    never in.

    An existing file is NEVER replaced: the code source defines the artifact, and
    a data deposit that silently overwrote a script would mean the reviewed code
    is not the submitted code. Collisions are returned so the report can name
    them — a path the artifact already occupies with a symlink, or one that would
    lead out of the tree through one, counts as a collision too.

    Symlinks in the deposit are never followed and never copied: a link to
    ~/.aws would otherwise copy the chair's own files into the artifact. They are
    appended (relative to ``src_root``) to ``skipped`` when the caller passes a
    list, so the report can say what was left out."""
    src_root = Path(src_root)
    target_root = safe_join(Path(dest_root), into) if into else Path(dest_root)
    target_root.mkdir(parents=True, exist_ok=True)

    files: list[tuple[str, Path]] = []
    links: list[str] = []
    # os.walk(followlinks=False) never descends through a directory link.
    for dirpath, dirnames, filenames in os.walk(src_root):
        here = Path(dirpath)
        for name in list(dirnames):
            if (here / name).is_symlink():
                dirnames.remove(name)
                links.append((here / name).relative_to(src_root).as_posix())
        for name in filenames:
            src = here / name
            rel = src.relative_to(src_root).as_posix()
            if src.is_symlink():
                links.append(rel)
            elif src.is_file():                 # not a fifo/device a tar could carry
                files.append((rel, src))

    copied: list[str] = []
    collisions: list[str] = []
    for rel, src in sorted(files):
        try:
            # safe_join re-validates every component, and refuses a path that
            # resolves out of the tree through a symlink the artifact committed.
            dst = safe_join(target_root, rel)
        except FetchError:
            collisions.append((target_root.relative_to(Path(dest_root)) / rel).as_posix())
            continue
        out_rel = dst.relative_to(Path(dest_root)).as_posix()
        if os.path.lexists(dst):                # lexists: a dangling link is still "there"
            collisions.append(out_rel)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst, follow_symlinks=False)
        copied.append(out_rel)
    if skipped is not None:
        skipped.extend(sorted(links))
    return copied, collisions
