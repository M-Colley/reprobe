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

import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .fetch.base import _MAX_DOWNLOAD_BYTES, FetchError, assert_safe_url, download, get
from .fetch.doi import DOI_STRICT as _DOI_STRICT
from .fetch.doi import normalize_doi

# A DOI is author-controlled input that we interpolate into an API URL, so keep
# it to the registered shape: 10.<registrant>/<suffix>, no spaces or delimiters.
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)")
# A badge IMAGE is not part of the DOI it displays:
#   https://zenodo.org/badge/DOI/10.5281/zenodo.1234567.svg
#   https://img.shields.io/badge/DOI-10.1145/3613904.3641909-blue.svg
_IMAGE_SUFFIX = re.compile(r"\.(?:svg|png|jpe?g|gif|webp)$", re.I)
_SHIELDS_COLOUR = re.compile(r"-[A-Za-z]+$")
# Data-repository prefixes (Zenodo, figshare, Dryad, OSF, Harvard Dataverse). A
# README's first DOI is usually the code deposit's badge, not the paper.
_DEPOSIT_PREFIXES = ("10.5281/", "10.6084/", "10.5061/", "10.17605/", "10.7910/")

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
_PDF_TIMEOUT_S = 120                  # wall clock for parsing one PDF, in a child process
_PDF_MEMORY_BYTES = 2 * 1024**3       # address-space cap for that child (POSIX only)


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
def _trim_doi(doi: str, before: str) -> str:
    """Strip what the prose / markup a DOI was matched in glued onto it.

    The DOI pattern admits ``(`` and ``)``, so in ``![x](…/10.5281/z.1.svg)`` the
    match ends ``.svg)`` — the image suffix only becomes visible once the closing
    bracket is gone, so the trims repeat until nothing more comes off."""
    shields = "shields.io/badge/" in before.lower()
    prev = None
    while doi != prev:
        prev = doi
        doi = _IMAGE_SUFFIX.sub("", doi)
        if shields and doi != prev:
            doi = _SHIELDS_COLOUR.sub("", doi)
        # Trailing punctuation belongs to the sentence; a trailing ')' belongs to
        # the DOI only when it closes a '(' inside it (10.1016/S0140-6736(20)30183-5).
        if doi and doi[-1] in ").,;:" and not (doi[-1] == ")" and doi.count("(") >= doi.count(")")):
            doi = doi[:-1]
    return doi


def _text_dois(text: str) -> list[str]:
    out: list[str] = []
    for m in _DOI_RE.finditer(text):
        doi = _trim_doi(m.group(1), text[max(0, m.start() - 40):m.start()])
        if _DOI_STRICT.match(doi) and doi not in out:
            out.append(doi)
    return out


def _cff_dois(text: str) -> list[str]:
    """DOIs a CITATION.cff declares, the paper's first.

    ``preferred-citation`` is how CFF says "cite this paper instead"; the
    top-level ``doi`` usually names the software deposit itself — the wrong thing
    to compare results against. ``identifiers:`` entries of type doi count too.
    Unparseable YAML falls back to a plain DOI scan."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return _text_dois(text)
    if not isinstance(data, dict):
        return _text_dois(text)
    raw: list[Any] = []
    for block in (data.get("preferred-citation"), data):
        if not isinstance(block, dict):
            continue
        raw.append(block.get("doi"))
        idents = block.get("identifiers")
        for ident in idents if isinstance(idents, list) else []:
            kind = ident.get("type") if isinstance(ident, dict) else None
            if isinstance(kind, str) and kind.lower() == "doi":
                raw.append(ident.get("value"))
    out: list[str] = []
    for value in raw:
        # strings only: a YAML alias tree is cheap to load and exponential to str()
        doi = normalize_doi(value) if isinstance(value, str) and value else None
        if doi and doi not in out:
            out.append(doi)
    return out


def find_doi(src: Path, manifest_meta: dict[str, Any] | None = None,
             pin_value: str = "") -> Optional[str]:
    """The paper's DOI from (in order) the manifest, CITATION.cff, README, or the
    archival pin. Returns None when nothing DOI-shaped is present.

    Within CITATION.cff and the READMEs a paper DOI beats a data-repository one:
    the Zenodo badge at the top of a README names the code deposit, and the pin
    is already the last resort for that."""
    declared = ((manifest_meta or {}).get("paper") or {}).get("doi")
    if declared and (doi := normalize_doi(str(declared))):
        return doi
    found: list[str] = []
    for name in ("CITATION.cff", "README.md", "README.rst", "README.txt", "README"):
        f = src / name
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")[:200_000]
        except OSError:
            continue
        dois = _cff_dois(text) if name == "CITATION.cff" else _text_dois(text)
        found += [d for d in dois if d not in found]
    for doi in found:
        if not doi.startswith(_DEPOSIT_PREFIXES):
            return doi
    if found:
        return found[0]
    if pin_value and _DOI_STRICT.match(pin_value.strip()):
        return pin_value.strip()
    return None


def _inside(src: Path, rel: str) -> Optional[Path]:
    """``src/rel`` when it stays inside the repository, else None.

    The manifest is author-written: an absolute path, a ``../`` or a committed
    symlink would otherwise hand any PDF on the host to the LLM prompt."""
    try:
        root = src.resolve()
        cand = src / rel
        if cand.is_symlink():
            return None
        real = cand.resolve()
        if not real.is_relative_to(root):
            return None
        return src / real.relative_to(root)
    except (OSError, ValueError, RuntimeError):
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
        cand = _inside(src, str(declared))
        if cand is not None and cand.is_file() and cand.suffix.lower() == ".pdf":
            return cand                  # an explicit declaration always wins
    skip = exclude or set()
    root = src.resolve()
    pdfs = [p for p in src.rglob("*.pdf")
            if p.is_file() and not p.is_symlink() and ".git" not in p.parts
            and p.resolve().is_relative_to(root)
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
# Runs in a child interpreter: pypdf on a hostile PDF has a long record of
# infinite loops and memory blow-ups (a dozen advisories in 2026 alone), and this
# is the trusted host. argv: pdf, pypdf's sys.path entry, max pages, max chars,
# address-space cap. Output: one JSON object on stdout (ASCII, json's default).
_EXTRACT = r"""
import json, sys
path, pypdf_home = sys.argv[1], sys.argv[2]
max_pages, max_chars, mem = int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
try:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    resource.setrlimit(resource.RLIMIT_FSIZE, (50_000_000, 50_000_000))   # its stderr file
except Exception:
    pass
sys.path.append(pypdf_home)
from pypdf import PdfReader
reader = PdfReader(path)
n = len(reader.pages)
out, total = [], 0
for i in range(min(n, max_pages)):
    try:
        chunk = reader.pages[i].extract_text() or ""
    except Exception:
        continue
    out.append(chunk)
    total += len(chunk)
    if total >= max_chars:
        break
json.dump({"pages": n, "text": "\n".join(out)[:max_chars]}, sys.stdout)
"""


def pdf_text(path: Path) -> tuple[str, list[str]]:
    """Extract text from an untrusted PDF under strict caps. Returns (text,
    warnings); text is "" when extraction is impossible, never an exception.

    Parsing happens in a separate ``python -I`` process under a wall-clock limit
    (and an address-space cap where the OS has one), so a PDF that sends pypdf
    into a loop or eats memory costs one warning, not the chair's run."""
    warnings: list[str] = []
    try:
        size = path.stat().st_size
    except OSError as e:
        return "", [f"could not stat PDF: {e}"]
    if size > _MAX_PDF_BYTES:
        return "", [f"PDF is {size} bytes, over the {_MAX_PDF_BYTES}-byte cap; not parsed"]
    try:
        import pypdf
    except ImportError:
        return "", ["a paper PDF was found but text extraction needs pypdf "
                    "(pip install 'reprobe[paper]'); the PDF was NOT read"]
    if not sys.executable:
        return "", ["no Python interpreter to isolate PDF parsing in; the PDF was NOT read"]
    # -I ignores PYTHON* env and user site-packages and keeps the cwd off
    # sys.path, so pass where THIS process found pypdf (it may be a --user install).
    home = str(Path(pypdf.__file__).resolve().parent.parent)
    argv = [sys.executable, "-I", "-c", _EXTRACT, str(path.resolve()), home,
            str(_MAX_PDF_PAGES), str(_MAX_TEXT_CHARS), str(_PDF_MEMORY_BYTES)]
    try:
        # stdout is ONE json object whose text the child itself caps; stderr (pypdf's
        # warnings, a traceback) goes to a temp file and only its tail is read, so a
        # hostile PDF cannot make the host buffer an unbounded pipe
        with tempfile.TemporaryFile() as err:
            proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=err, timeout=_PDF_TIMEOUT_S,
                                  stdin=subprocess.DEVNULL, cwd=tempfile.gettempdir())
            err.seek(0, 2)
            err.seek(max(0, err.tell() - 4096))
            proc.stderr = err.read()
    except subprocess.TimeoutExpired:
        return "", [f"parsing the PDF took over {_PDF_TIMEOUT_S}s and was stopped (malformed or "
                    "hostile PDF?); it was NOT read"]
    except OSError as e:
        return "", [f"could not start PDF parsing ({type(e).__name__}); the PDF was NOT read"]
    if proc.returncode != 0:
        last = (proc.stderr.decode("utf-8", "replace").strip().splitlines() or ["unknown error"])[-1]
        kind = last.split(":", 1)[0].rsplit(".", 1)[-1].strip()[:60] or "error"
        return "", [f"could not parse the PDF ({kind}); it was NOT read"]
    try:
        out = json.loads(proc.stdout.decode("utf-8", "replace"))
        pages, text = int(out.get("pages") or 0), str(out.get("text") or "")[:_MAX_TEXT_CHARS]
    except (ValueError, AttributeError, TypeError):
        return "", ["PDF parsing returned no usable result; it was NOT read"]
    if pages > _MAX_PDF_PAGES:
        warnings.append(f"PDF has {pages} pages; only the first {_MAX_PDF_PAGES} were read")
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
