"""What the artifact's own dependency files may ask the install phase to do.

The install phase has network egress. A dependency file is the one place an
author can steer it, and each of these asks for something that either runs the
author's code with that network (an editable install of the repo, a ``git+``
build) or points it at a server the author chose (a custom index, a conda
channel, a GitHub package in an ``renv.lock``). None of it is needed to install
the packages a paper's analysis uses, so none of it is applied: the file is
reduced to the lines that name a package on an index reprobe trusts, and every
line dropped is reported — the report never implies an environment was built
from a file it did not build from.

Pure functions over text: no I/O beyond reading the files they are handed, no
network, nothing executed. The plan embeds the SANITISED result in the install
command (base64), so what pip or micromamba reads is exactly what was reviewed
here, not whatever sits in ``/work`` when the phase starts.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement

#: Content larger than this is not embedded in a command line (the kernel caps one
#: argument at 128 KiB; base64 adds a third).
MAX_EMBED_BYTES = 64 * 1024

#: Conda channels whose packages the harness's own base is built from, or that
#: are curated public channels. A bare channel name on anaconda.org is a user
#: account: anyone can publish there, and conda runs post-link scripts.
DEFAULT_CONDA_CHANNELS = ("conda-forge", "bioconda", "pytorch", "nvidia", "defaults")

_MAX_INCLUDE_DEPTH = 5
# pip's own list of archive extensions (pip._internal.utils.filetypes), plus .egg.
# A requirement that ends in one is read as a LOCAL FILE when it exists in pip's
# working directory, and installing a committed sdist runs its build code.
_ARCHIVE_SUFFIXES = (".whl", ".zip", ".tar.gz", ".tgz", ".tar", ".tar.bz2", ".tbz", ".tbz2",
                     ".tar.xz", ".txz", ".tlz", ".tar.lz", ".tar.lzma", ".egg")


def _looks_like_archive(token: str) -> bool:
    t = token.strip().lower()
    return t.endswith(_ARCHIVE_SUFFIXES) or t.startswith((".", "/", "~"))
# Only a well-formed hash token is recognised; anything else after a requirement
# is a pip option and the line is dropped. (A token ending in a backslash once
# survived and made pip glue the NEXT requirement onto it.)
_HASH_OPT = re.compile(r"\s--hash[= ](?:md5|sha1|sha224|sha256|sha384|sha512):[0-9A-Fa-f]+")


def embed_file_command(content: str, dest: str) -> str:
    """A shell command that writes ``content`` to ``dest`` inside the container.

    Base64 keeps arbitrary text (quotes, newlines, `$`) out of the shell."""
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    d = shlex.quote(dest)
    return f"mkdir -p {shlex.quote(str(Path(dest).parent.as_posix()))} && printf %s '{b64}' | base64 -d > {d}"


# --------------------------------------------------------------------------- #
# pip requirements
# --------------------------------------------------------------------------- #
def _logical_lines(text: str) -> list[str]:
    """requirements.txt lines with comments removed and `\\` continuations joined."""
    out: list[str] = []
    buf = ""
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        # pip never continues a COMMENT line, whatever it ends with: a trailing
        # backslash on one would otherwise swallow (and hide) the next requirement.
        if line.endswith("\\") and not line.lstrip().startswith("#"):
            buf += line[:-1] + " "
            continue
        line = buf + line
        buf = ""
        # pip: a comment starts at '#' at line start or preceded by whitespace
        line = re.sub(r"(^|\s)#.*$", "", line).strip()
        if line:
            out.append(line)
    if buf.strip():
        out.append(buf.strip())
    return out


def _inside(root: Path, p: Path) -> bool:
    try:
        return not p.is_symlink() and p.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):     # ValueError: an embedded NUL in the path
        return False


def inside_tree(root: Path, p: Path) -> bool:
    """True when ``p`` is a real (non-link) path inside ``root``. The planner reads
    several author-named files host-side; a manifest path of ``../notes.txt`` or a
    committed symlink would otherwise read the chair's own files into the report."""
    return _inside(root, p)


def filter_requirements(path: Path, root: Path) -> tuple[list[str], list[tuple[str, str]]]:
    """``(kept_lines, dropped)`` for a requirements file inside ``root``.

    Kept: plain PEP 508 requirements (name, extras, version specifiers, markers),
    with their ``--hash`` pins. ``-r`` includes are inlined when they stay inside
    the repository.

    Dropped, each with its reason: editable installs and local paths (they run
    the repository's own ``setup.py``/build backend with network access), VCS and
    direct-URL requirements (author-chosen servers), constraint files (they limit
    versions but name nothing to install, and cannot be inlined), every ``pip``
    option that moves the index or changes how builds run (``--index-url``,
    ``--find-links``, ``--trusted-host``, ``--global-option``, ``--no-binary``, …),
    and anything that is not a valid requirement."""
    return _filter_file(path, root, 0, set())


def filter_requirement_lines(lines: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """The same rules for a list of requirement strings with no file around them
    (the ``pip:`` section of a conda environment): includes are never followed."""
    return _filter_lines(lines, None, None, 0, set())


def _filter_file(path: Path, root: Path, depth: int,
                 seen: set[Path]) -> tuple[list[str], list[tuple[str, str]]]:
    try:
        real = path.resolve()
        if real in seen:
            return [], []
        seen.add(real)
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], []
    return _filter_lines(_logical_lines(text), path.parent, root, depth, seen)


def _filter_lines(lines: list[str], base: Path | None, root: Path | None, depth: int,
                  seen: set[Path]) -> tuple[list[str], list[tuple[str, str]]]:
    kept: list[str] = []
    dropped: list[tuple[str, str]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("-"):
            m = re.match(r"(-{1,2}[A-Za-z0-9-]*)(?:[ =]+(.*))?$", line)   # `--opt arg` and `--opt=arg`
            opt, arg = (m.group(1), (m.group(2) or "").strip()) if m else (line, "")
            if opt in ("-r", "--requirement"):
                inc = base / arg if (base is not None and arg) else None
                if inc is not None and root is not None and depth < _MAX_INCLUDE_DEPTH \
                        and _inside(root, inc):
                    k, d = _filter_file(inc, root, depth + 1, seen)
                    kept += k
                    dropped += d
                else:
                    dropped.append((line, "include outside the repository or too deeply nested"))
            elif opt in ("-c", "--constraint"):
                dropped.append((line, "constraint file not applied: it limits versions but names "
                                      "nothing to install, so the versions pip resolves may differ"))
            elif opt in ("-e", "--editable"):
                dropped.append((line, "editable install: builds the repository itself, running its "
                                      "setup.py / build backend with network access"))
            elif opt in ("-i", "--index-url", "--extra-index-url", "-f", "--find-links",
                         "--trusted-host", "--no-index"):
                dropped.append((line, "moves the package index to a server the artifact chose"))
            else:
                dropped.append((line, "pip option not applied (it changes how packages are "
                                      "found or built)"))
            continue

        hashes = _HASH_OPT.findall(line)
        spec = _HASH_OPT.sub("", line).strip()
        if " --" in spec:
            dropped.append((line, "per-requirement pip option not applied (build/install arguments)"))
            continue
        # BEFORE parsing: `pkg-1.0.tar.gz` is a perfectly valid requirement NAME, but
        # pip reads a requirement that ends in an archive suffix as a local file when
        # one exists in its working directory (/work) — and installing a committed
        # sdist runs its build code while the network is open.
        head = re.split(r"[\s;\[<>=!~@(,]", spec, maxsplit=1)[0]
        if _looks_like_archive(head) or _looks_like_archive(spec):
            dropped.append((line, "local path or archive: builds code from the repository itself"))
            continue
        try:
            req = Requirement(spec)
        except InvalidRequirement:
            low = spec.lower()
            if low.startswith(("git+", "hg+", "svn+", "bzr+")) or "://" in low:
                reason = "VCS or direct URL: fetched from a server the artifact chose"
            else:
                reason = "not a valid requirement"
            dropped.append((line, reason))
            continue
        if req.url:
            dropped.append((line, "direct URL reference: fetched from a server the artifact chose"))
            continue
        if _looks_like_archive(req.name):        # `evil-1.0.tar.gz[x] ; marker` parses to this name
            dropped.append((line, "local path or archive: builds code from the repository itself"))
            continue
        kept.append(spec + "".join(hashes))
    return kept, dropped


# --------------------------------------------------------------------------- #
# conda environment.yml
# --------------------------------------------------------------------------- #
def _bad_conda_spec(spec: str, channels: set[str]) -> str | None:
    s = spec.strip()
    if not s:
        return "empty"
    low = s.lower()
    if "[" in s or "]" in s:
        # conda's bracket match-spec syntax (`numpy[channel=evil-user]`, `url=`, …)
        # can name a channel or a URL inside a spec that looks like a plain name
        return "bracket match-spec: can name a channel or URL"
    if "://" in low or low.startswith(("file:", "/", ".", "~")) or low.endswith((".conda", ".tar.bz2")):
        return "URL, path or package archive: not resolved from an allowed channel"
    if "::" in s:
        chan = s.split("::", 1)[0].strip().lower()
        if chan not in channels:
            return f"names channel {chan!r}, which is not allowed"
    return None


def sanitize_conda_env(spec: Any, allowed_channels: tuple[str, ...] | list[str] = DEFAULT_CONDA_CHANNELS,
                       ) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """``(clean_spec, dropped)`` for a parsed ``environment.yml``.

    Channels outside the allow-list are dropped (a user channel on anaconda.org is
    an arbitrary author's packages, and conda runs their post-link scripts);
    package specs that name a URL, path or non-allowed channel are dropped; the
    ``pip:`` section goes through :func:`filter_requirements`' rules. Keys other
    than ``name``/``channels``/``dependencies`` (``variables``, ``prefix``) are
    not applied by ``micromamba create`` and are dropped."""
    dropped: list[tuple[str, str]] = []
    allowed = {c.lower() for c in allowed_channels}
    if not isinstance(spec, dict):
        return {"channels": ["conda-forge"], "dependencies": []}, [("(file)", "not a mapping")]

    # Only plain strings are read. A YAML alias tree is shared, not copied, so it
    # costs nothing to load — and a `str()` of it expands exponentially, which a
    # 350-byte file used to turn into minutes and gigabytes on the host.
    channels: list[str] = []
    raw_channels = spec.get("channels")
    for c in raw_channels if isinstance(raw_channels, list) else []:
        if not isinstance(c, str):
            dropped.append(("(non-string channel)", "not an allowed channel"))
        elif c.strip().lower() in allowed:
            channels.append(c.strip())
        else:
            dropped.append((f"channel {c.strip()[:60]}", "not an allowed channel"))
    if not channels:
        channels = ["conda-forge"]

    deps: list[Any] = []
    raw_deps = spec.get("dependencies")
    for d in raw_deps if isinstance(raw_deps, list) else []:
        if isinstance(d, str):
            why = _bad_conda_spec(d, allowed)
            if why:
                dropped.append((d, why))
            else:
                deps.append(d)
        elif isinstance(d, dict) and "pip" in d:
            raw_pip = d.get("pip")
            raw_pip = raw_pip if isinstance(raw_pip, list) else []
            pip_lines, dr = filter_requirement_lines([x for x in raw_pip if isinstance(x, str)])
            dropped += dr
            if any(not isinstance(x, str) for x in raw_pip):
                dropped.append(("(non-string pip entry)", "not a valid requirement"))
            if pip_lines:
                deps.append({"pip": pip_lines})
        else:
            dropped.append(("(unsupported entry)", "unsupported dependency entry"))
    # `name:` is not carried over: the environment is created with -p, so it names
    # nothing the build needs, and a value like `../../x` is only ever a risk.
    return {"channels": channels, "dependencies": deps}, dropped


# --------------------------------------------------------------------------- #
# renv.lock
# --------------------------------------------------------------------------- #
#: Lockfile sources reprobe restores: a CRAN-like repository, and Bioconductor
#: (a fixed public repository renv resolves from the version the lock records).
_RENV_OK_SOURCES = frozenset({"repository", "bioconductor"})
#: Names renv gives CRAN-like repositories. A package whose `Repository` field names
#: anything else points at a repository the lockfile defined itself.
_RENV_OK_REPOSITORIES = frozenset({"cran", "rspm", "ppm", "p3m"})


class _DuplicateKey(ValueError):
    pass


def _no_duplicates(pairs):
    seen: dict = {}
    for k, v in pairs:
        if k in seen:
            raise _DuplicateKey(k)          # Python would keep the LAST; another parser the first
        seen[k] = v
    return seen


def classify_renv_lock(text: str) -> tuple[list[str] | None, list[tuple[str, str]]]:
    """``(excluded_packages, notes)`` for an ``renv.lock``; ``excluded_packages`` is
    ``None`` when the lockfile is REFUSED (unparseable, or a duplicate key that two
    JSON parsers read differently) and nothing may be restored from it.

    A package whose ``Source`` is GitHub / GitLab / Bitbucket / git / a URL /
    Local is fetched from, and built from, wherever the lockfile says — code the
    artifact chose, built with network. Those are excluded from the restore and
    listed; ``Repository`` and ``Bioconductor`` sources are restored (and CRAN
    ones from reprobe's pinned snapshot, not the lockfile's mirror)."""
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicates)
        packages = (data or {}).get("Packages") or {}
    except (ValueError, AttributeError, RecursionError) as e:
        why = ("has a duplicate key, which two JSON parsers read differently" if isinstance(e, _DuplicateKey)
               else "could not be parsed as JSON")
        return None, [("(renv.lock)", f"{why}; restoring it as written is refused — nothing was "
                                      "restored from it")]
    if not isinstance(packages, dict):
        return [], []
    excluded: list[str] = []
    notes: list[tuple[str, str]] = []
    for name, info in sorted(packages.items()):
        info = info if isinstance(info, dict) else {}
        # Exclusion is by key, and a key that is not an R package name cannot be
        # passed to `exclude=` at all; a key that disagrees with the record's own
        # `Package` excludes one name while renv installs another. Either way a
        # record would slip past the exclusion and be fetched from wherever it
        # says, with network — so the lockfile is refused, not partly trusted.
        pkg = info.get("Package", name)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9.]*", str(name)) or pkg != name:
            why = ("is not a valid R package name" if pkg == name
                   else f"names a different package ({str(pkg)[:40]!r})")
            return None, [(str(name)[:60], f"the record {why}; restoring this lockfile is refused "
                                           "— nothing was restored from it")]
        source = str(info.get("Source") or "").lower() if isinstance(info.get("Source"), str) else ""
        repo = info.get("Repository")
        if source == "repository" and isinstance(repo, str) and repo.strip().lower() not in _RENV_OK_REPOSITORIES:
            source = f"repository {repo.strip()[:30]!r}"
        if source not in _RENV_OK_SOURCES:
            excluded.append(str(name))
            notes.append((str(name), f"source {source or 'unknown'!r} is fetched from where the "
                                     "lockfile says, not from a CRAN-like repository"))
    return excluded, notes
