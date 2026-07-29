"""Locate the paper whose claims an artifact is supposed to reproduce.

Two sources, in priority order:

  1. a PDF committed in the repository (best — full text, no network), or
  2. a DOI (manifest, CITATION.cff, README, or the fetch pin), resolved through
     OpenAlex to an open-access full text when one is really downloadable, and to
     the abstract otherwise.

Everything here handles UNTRUSTED author-controlled bytes on the TRUSTED host,
outside every sandbox, so each path is bounded: only fixed metadata APIs are
queried (never an author-supplied URL), a candidate OA link must pass the SSRF
guard and actually be a PDF, and every read is byte/page/char capped.

The result feeds an ADVISORY LLM comparison only. Nothing here can grant a badge
— ``coverage`` records how much of the paper was actually available so the report
can never imply more was checked than was read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .fetch.base import _MAX_DOWNLOAD_BYTES, FetchError, assert_safe_url, download, get

# A DOI is author-controlled input that we interpolate into an API URL, so keep
# it to the registered shape: 10.<registrant>/<suffix>, no spaces or delimiters.
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)")
_DOI_STRICT = re.compile(r"^10\.\d{4,9}/[-._;()/:A-Za-z0-9]+$")

# Markers of a reported statistic. Used to find the passages of a paper that
# actually contain comparable numbers (see Paper.excerpt).
_STATS_RE = re.compile(
    r"(?:[MSD]{1,2}\s*=\s*-?\d|\bSD\b|\bM\s*=|"
    r"\b[Ftrz]\s*\(\s*\d|\bχ2|\bchi|\bp\s*[<=>]\s*[.\d]|"
    r"\bη|\beta[_ ]?p|\bd\s*=\s*-?\d|\bCI\b|\bN\s*=\s*\d|\bn\s*=\s*\d)",
    re.I)

_MAX_PDF_BYTES = 64 * 1024 * 1024     # a paper is ~1-10 MB; refuse anything absurd
_MAX_PDF_PAGES = 60
_MAX_TEXT_CHARS = 60_000              # what we are willing to hold / fence into a prompt
_API_TIMEOUT = 30


@dataclass
class Paper:
    """Text of the paper (or as much as could legitimately be obtained)."""

    source: str                       # "repo-pdf" | "doi-fulltext" | "doi-abstract"
    ref: str                          # path in repo, or the DOI
    text: str = ""
    title: str = ""
    coverage: str = ""                # human-readable honesty note
    warnings: list[str] = field(default_factory=list)

    @property
    def is_full_text(self) -> bool:
        return self.source in ("repo-pdf", "doi-fulltext")

    def excerpt(self, budget: int = 8000) -> str:
        """The parts of the paper worth comparing, within a small local model's
        context. Naively truncating a paper yields its title, abstract and
        related work — precisely the pages with no results in them. So keep the
        opening (abstract/claims) and then the passages densest in reported
        statistics."""
        text = self.text
        if len(text) <= budget:
            return text
        head = text[:1500]
        rest = text[1500:]
        window = 700
        scored: list[tuple[int, int]] = []
        for start in range(0, len(rest), window):
            chunk = rest[start:start + window]
            hits = len(_STATS_RE.findall(chunk))
            if hits:
                scored.append((hits, start))
        scored.sort(key=lambda x: (-x[0], x[1]))
        picked: list[int] = []
        room = budget - len(head)
        for _, start in scored:
            if room <= 0:
                break
            picked.append(start)
            room -= window
        if not picked:
            return text[:budget]
        parts = [rest[s:s + window] for s in sorted(picked)]
        return head + "\n[…]\n" + "\n[…]\n".join(parts)


# --------------------------------------------------------------------------- #
# DOI discovery
# --------------------------------------------------------------------------- #
def find_doi(src: Path, manifest_meta: dict[str, Any] | None = None,
             pin_value: str = "") -> Optional[str]:
    """The paper's DOI from (in order) the manifest, CITATION.cff, README, or the
    archival pin. Returns None when nothing DOI-shaped is present."""
    declared = ((manifest_meta or {}).get("paper") or {}).get("doi")
    if declared and _DOI_STRICT.match(str(declared).strip()):
        return str(declared).strip()
    for name in ("CITATION.cff", "README.md", "README.rst", "README.txt", "README"):
        f = src / name
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")[:200_000]
        except OSError:
            continue
        m = _DOI_RE.search(text)
        if m:
            return m.group(1).rstrip(").,;]}>\"'")
    if pin_value and _DOI_STRICT.match(pin_value.strip()):
        return pin_value.strip()
    return None


def find_pdf(src: Path, manifest_meta: dict[str, Any] | None = None, *,
             exclude: Optional[set[str]] = None) -> Optional[Path]:
    """The most paper-like PDF in the repo, or None.

    A manifest ``paper.pdf`` wins. Otherwise prefer a shallow file whose name
    looks like a paper rather than a figure/appendix, then the largest — a
    manuscript is normally the biggest PDF a repo carries.

    ``exclude`` holds repo-relative paths that came from a merged data deposit.
    A deposit of figures is full of PDFs, and calling one of them "the paper,
    committed in the repository" is wrong twice over — it is not the paper, and
    it was not committed. The paper is a property of the submission itself."""
    declared = ((manifest_meta or {}).get("paper") or {}).get("pdf")
    if declared:
        cand = src / str(declared)
        if cand.is_file() and cand.suffix.lower() == ".pdf":
            return cand                  # an explicit declaration always wins
    skip = exclude or set()
    pdfs = [p for p in src.rglob("*.pdf")
            if p.is_file() and not p.is_symlink() and ".git" not in p.parts
            and p.relative_to(src).as_posix() not in skip][:200]
    if not pdfs:
        return None
    hint = re.compile(r"paper|manuscript|preprint|camera|accepted|main|article", re.I)

    def rank(p: Path) -> tuple:
        depth = len(p.relative_to(src).parts)
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        return (0 if hint.search(p.stem) else 1, depth, -size)

    return sorted(pdfs, key=rank)[0]


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
def pdf_text(path: Path) -> tuple[str, list[str]]:
    """Extract text from an untrusted PDF under strict caps. Returns (text,
    warnings); text is "" when extraction is impossible, never an exception."""
    warnings: list[str] = []
    try:
        size = path.stat().st_size
    except OSError as e:
        return "", [f"could not stat PDF: {e}"]
    if size > _MAX_PDF_BYTES:
        return "", [f"PDF is {size} bytes, over the {_MAX_PDF_BYTES}-byte cap; not parsed"]
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", ["a paper PDF was found but text extraction needs pypdf "
                    "(pip install 'reprobe[paper]'); the PDF was NOT read"]
    try:
        reader = PdfReader(str(path))
        pages = reader.pages[:_MAX_PDF_PAGES]
        if len(reader.pages) > _MAX_PDF_PAGES:
            warnings.append(f"PDF has {len(reader.pages)} pages; only the first "
                            f"{_MAX_PDF_PAGES} were read")
        out: list[str] = []
        total = 0
        for page in pages:
            try:
                chunk = page.extract_text() or ""
            except Exception:                      # a single malformed page must not abort
                continue
            out.append(chunk)
            total += len(chunk)
            if total >= _MAX_TEXT_CHARS:
                break
        text = "\n".join(out)[:_MAX_TEXT_CHARS]
    except Exception as e:                          # untrusted input: never propagate
        return "", [f"could not parse the PDF ({type(e).__name__}); it was NOT read"]
    if not text.strip():
        warnings.append("the PDF yielded no extractable text (scanned image?); it was NOT read")
    return text, warnings


def _openalex(doi: str) -> dict[str, Any]:
    r = get(f"https://api.openalex.org/works/doi:{doi}", timeout=_API_TIMEOUT)
    r.raise_for_status()
    return r.json() or {}


def _abstract_from_inverted_index(inv: dict[str, list[int]] | None) -> str:
    """OpenAlex stores abstracts as {word: [positions]}; rebuild the prose."""
    if not isinstance(inv, dict):
        return ""
    pos: dict[int, str] = {}
    for word, idxs in inv.items():
        if not isinstance(idxs, list):
            continue
        for i in idxs:
            if isinstance(i, int):
                pos[i] = str(word)
    return " ".join(pos[i] for i in sorted(pos))[:_MAX_TEXT_CHARS]


def doi_text(doi: str, workdir: Path) -> Paper:
    """Best legitimately-obtainable text for a DOI.

    Only the fixed OpenAlex API is queried with the (validated) DOI. An OA link
    it reports is author-influenced, so it must pass the SSRF guard, download
    under the byte cap, and actually begin with %PDF before we parse it. Many
    venues (ACM among them) advertise an OA PDF that then refuses automated
    requests — in that case we fall back to the abstract and SAY SO."""
    paper = Paper(source="doi-abstract", ref=doi)
    if not _DOI_STRICT.match(doi):
        paper.warnings.append(f"ignoring malformed DOI {doi!r}")
        paper.coverage = "no paper text available"
        return paper
    try:
        work = _openalex(doi)
    except Exception as e:
        paper.warnings.append(f"could not query OpenAlex for {doi} ({type(e).__name__})")
        paper.coverage = "no paper text available"
        return paper

    paper.title = str(work.get("title") or work.get("display_name") or "")
    oa_url = ((work.get("open_access") or {}).get("oa_url") or "")

    if oa_url:
        try:
            assert_safe_url(oa_url)               # http(s) + public host only
            dest = workdir / "paper.pdf"
            ok, note = download(oa_url, dest, timeout=60,
                                max_bytes=min(_MAX_PDF_BYTES, _MAX_DOWNLOAD_BYTES),
                                restrict_public=True)
            if ok and dest.is_file() and dest.open("rb").read(5).startswith(b"%PDF"):
                text, warns = pdf_text(dest)
                paper.warnings.extend(warns)
                if text.strip():
                    paper.source, paper.text = "doi-fulltext", text
                    paper.coverage = f"full text of the open-access PDF for {doi}"
                    return paper
            else:
                paper.warnings.append(
                    f"the open-access link for {doi} did not return a PDF "
                    f"({note if not ok else 'not PDF content'}); using the abstract instead")
        except FetchError as e:
            paper.warnings.append(f"refused the open-access link for {doi}: {e}")

    abstract = _abstract_from_inverted_index(work.get("abstract_inverted_index"))
    if abstract:
        paper.text = (f"TITLE: {paper.title}\n\nABSTRACT:\n{abstract}"
                      if paper.title else abstract)
        paper.coverage = (f"ABSTRACT ONLY for {doi} — the full text was not openly "
                          "retrievable, so only headline claims could be compared")
    else:
        paper.coverage = f"no readable text for {doi} (metadata only)"
        paper.warnings.append(f"no abstract available for {doi}")
    return paper


def locate(src_dir: str | Path, workdir: str | Path, *,
           manifest_meta: dict[str, Any] | None = None,
           pin_value: str = "",
           exclude: Optional[set[str]] = None) -> Optional[Paper]:
    """Find the paper for this artifact: a committed PDF first (full text, no
    network), else a DOI. Returns None when the artifact references no paper.

    ``exclude`` names files merged in from a data deposit — they are not part of
    the submission and must never be mistaken for its paper."""
    src, work = Path(src_dir), Path(workdir)
    pdf = find_pdf(src, manifest_meta, exclude=exclude)
    if pdf is not None:
        rel = str(pdf.relative_to(src)).replace("\\", "/")
        text, warns = pdf_text(pdf)
        if text.strip():
            return Paper(source="repo-pdf", ref=rel, text=text,
                         coverage=f"full text of {rel} (committed in the repository)",
                         warnings=warns)
        # a PDF that could not be read must not silently fall through unexplained
        doi = find_doi(src, manifest_meta, pin_value)
        if doi:
            paper = doi_text(doi, work)
            paper.warnings = warns + paper.warnings
            return paper
        return Paper(source="repo-pdf", ref=rel, coverage="no paper text available",
                     warnings=warns)
    doi = find_doi(src, manifest_meta, pin_value)
    return doi_text(doi, work) if doi else None
