"""Dataverse fetcher (Harvard Dataverse and any Dataverse install).

Resolves a dataset by persistent id (a ``…?persistentId=doi:…`` link, optionally
``&version=N.M``), lists that version's files through the native API, and
downloads each one, verifying the checksum Dataverse recorded for it. Pin = the
dataset DOI, plus the concrete version fetched (metadata ``version``).

The install's HOST comes from the submitted URL, so it is untrusted twice over:
every request is SSRF-guarded (public host, per-hop redirect check, DNS pinned),
and the DOI is only recorded as an archival pin once doi.org confirms it points
at this host — otherwise ``dataverse.<anything>`` could serve its own bytes and
"verify" them against its own API under someone else's DOI.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..models import FetchResult, Pin
from .base import (
    FetchError,
    assert_safe_url,
    checksum_verdict,
    download,
    get_json,
    hash_algorithm,
    maybe_unzip,
    new_checksum_stats,
    record_download,
    safe_join,
)
from .doi import resolve_doi

_DOI = re.compile(r"(doi:10\.\d{4,9}/[^\s&#?]+|hdl:[^\s&#?]+)", re.I)
_PID_OK = re.compile(r"^(?:doi:10\.\d{4,9}/[-._;()/:A-Za-z0-9]+|hdl:[-._/A-Za-z0-9]+)$", re.I)
_VERSION_OK = re.compile(r"^\d{1,6}(?:\.\d{1,6})?$")


def _hostname(ref: str) -> str:
    """The URL's real host — ``.hostname`` drops userinfo and port, so
    ``http://dataverse@169.254.169.254/`` is 169.254.169.254, not "dataverse"."""
    try:
        p = urlparse(ref)
        return (p.hostname or "").lower() if p.scheme in ("http", "https") else ""
    except ValueError:
        return ""


def _same_host(a: str, b: str) -> bool:
    return a.lower().removeprefix("www.") == b.lower().removeprefix("www.")


def _version_label(data: dict) -> str:
    major, minor = data.get("versionNumber"), data.get("versionMinorNumber")
    if major is None:
        return str(data.get("versionState") or "unknown")
    return f"{major}.{minor if minor is not None else 0}"


class DataverseFetcher:
    name = "dataverse"

    def __init__(self, extra_hosts: tuple[str, ...] = ()):
        # chair-supplied installs whose hostname lacks "dataverse"
        # (config/pins.yaml fetch.dataverse_hosts), e.g. darus.uni-stuttgart.de
        self.extra_hosts = tuple(h.strip().lower() for h in extra_hosts if h and h.strip())

    def can_handle(self, ref: str) -> bool:
        host = _hostname(ref)
        if not host:
            return False
        # A configured install matches exactly (or a subdomain of it) — never as
        # a substring, which "darus.uni-stuttgart.de.evil.example" would satisfy.
        known = "dataverse" in host or any(host == h or host.endswith("." + h)
                                           for h in self.extra_hosts)
        r = ref.lower()
        return known and ("persistentid=" in r or "/dataset" in r)

    def fetch(self, ref: str, dest: Path) -> FetchResult:
        p = urlparse(ref)
        host = _hostname(ref)
        if not host:
            raise FetchError("could not parse a Dataverse http(s) URL")
        netloc = f"[{host}]" if ":" in host else host
        if p.port:
            netloc += f":{p.port}"
        base = f"{p.scheme}://{netloc}"
        assert_safe_url(base)                       # public host only; raises FetchError

        query = parse_qs(p.query)
        pid = (query.get("persistentId") or [""])[0].strip()
        if not pid:
            d = _DOI.search(ref)
            pid = d.group(1) if d else ""
        if not _PID_OK.match(pid):
            raise FetchError("could not parse Dataverse host + persistentId (doi:… or hdl:…)")

        warnings: list[str] = []
        requested = (query.get("version") or [""])[0].strip()
        if requested and not _VERSION_OK.match(requested):
            # DRAFT needs an API token, and anything else is not a version number.
            warnings.append(f"ignored version={requested!r} (not a published version number); "
                            "fetched the latest published version instead")
            requested = ""
        try:
            meta = get_json(f"{base}/api/datasets/:persistentId/versions/"
                            f"{requested or ':latest-published'}",
                            params={"persistentId": pid, "excludeMetadataBlocks": "true"},
                            timeout=60, restrict_public=True)
        except Exception as e:
            raise FetchError(f"Dataverse API error: {e}")
        data = (meta or {}).get("data") or {}
        version = _version_label(data)
        if not requested:
            warnings.append(f"reference did not pin a Dataverse version; fetched the latest "
                            f"published one ({version}) — cite …&version={version} for a stable pin")

        dest.mkdir(parents=True, exist_ok=True)
        files = data.get("files") or []
        stats = new_checksum_stats()
        for entry in files:
            df = entry.get("dataFile") or {}
            fid = str(df.get("id") or "")
            # An ingested tabular file is served as a derived .tab by default; its
            # recorded checksum is of the ORIGINAL upload, so fetch that.
            tabular = bool(df.get("originalFileFormat"))
            name = ((df.get("originalFileName") if tabular else None)
                    or entry.get("label") or df.get("filename") or f"file-{fid}")
            folder = entry.get("directoryLabel")
            rel = f"{folder}/{name}" if folder else str(name)
            if not fid.isdigit():
                warnings.append(f"no file id for {rel}")
                continue
            if entry.get("restricted"):
                record_download(stats, False, "restricted", True)
                warnings.append(f"{rel}: restricted file — not downloadable without access")
                continue
            ck = df.get("checksum") or {}
            algo = hash_algorithm(ck.get("type"))
            digest = (algo, str(ck.get("value") or "")) if algo and ck.get("value") else None
            if ck.get("value") and not algo:
                warnings.append(f"{rel}: unsupported checksum type {ck.get('type')!r}")
            ok, note = download(f"{base}/api/access/datafile/{fid}", safe_join(dest, rel),
                                expected_digest=digest,
                                params={"format": "original"} if tabular else None,
                                timeout=600, restrict_public=True)
            record_download(stats, ok, note, bool(digest))
            if not ok:
                warnings.append(f"{rel}: {note}")
        if not files:
            warnings.append(f"Dataverse version {version} lists no files")
        verified = checksum_verdict(stats, warnings) if files else False
        maybe_unzip(dest, warnings)

        return FetchResult(
            input=ref, resolved_type="dataverse", src_dir=str(dest),
            pin=self._pin(pid, host, warnings),
            fetch_layer="dataverse-api", checksum_verified=verified, warnings=warnings,
            metadata={"host": base, "persistent_id": pid, "version": version,
                      "version_requested": requested or None, "checksums": stats},
        )

    @staticmethod
    def _pin(pid: str, host: str, warnings: list[str]) -> Pin:
        """version_doi only when doi.org says the DOI belongs to THIS install."""
        if not pid.lower().startswith("doi:"):
            return Pin(kind="none", value=pid)       # a Handle is not a DOI
        doi = pid[4:]
        target = resolve_doi(doi)
        target_host = _hostname(target or "")
        if target_host and _same_host(target_host, host):
            return Pin(kind="version_doi", value=doi)
        warnings.append(f"DOI {doi} does not resolve to {host} (doi.org: {target or 'no target'}), "
                        "so it is not recorded as an archival pin for these bytes")
        return Pin(kind="none", value=doi)
