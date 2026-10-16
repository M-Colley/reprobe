"""Local path 'fetcher' — used for fixtures, offline testing, and pre-downloaded
artifacts. Records the directory as-is with no archival pin.

Operator-only: the registry never offers it for an author-declared reference
(see ``registry.check_untrusted_ref``). The tree it copies is still untrusted —
a pre-downloaded artifact can carry symlinks into the chair's home directory."""

from __future__ import annotations

import shutil
from pathlib import Path

from ..models import FetchResult, Pin
from .base import FetchError


def _enclosing_child(root: Path, dest: Path) -> Path | None:
    """The top-level entry of ``root`` that contains ``dest``, if any.

    ``reprobe run .`` with the default ``--workroot work`` puts the copy INSIDE
    the tree being copied: copytree then walks into its own output, and every
    earlier run's work directory becomes part of the artifact. That entry is
    the workroot (or this submission's directory in it) — never the author's."""
    try:
        rel = dest.resolve().relative_to(root)
    except ValueError:
        return None
    return root / rel.parts[0] if rel.parts else None


def _unsafe_links(root: Path, skipped: list[str], exclude: Path | None = None):
    """``copytree`` ignore hook: drop every symlink that leaves ``root``, dangles,
    or names a directory.

    The copy follows file links (``symlinks=False``), so a committed link to
    ~/.aws would otherwise land the chair's own files in a tree that is shown to
    an LLM and mounted into the sandbox. A directory link is dropped even inside
    the tree: copytree does not detect cycles, and ``a -> ..`` would loop it."""
    def ignore(dirpath: str, names: list[str]) -> set[str]:
        out: set[str] = set()
        for name in names:
            p = Path(dirpath) / name
            if exclude is not None and p == exclude:
                out.add(name)
                continue
            if not p.is_symlink():
                continue
            try:
                target = p.resolve(strict=True)
                safe = target.is_file() and target.is_relative_to(root)
            except (OSError, RuntimeError):      # dangling, or a link loop
                safe = False
            if not safe:
                out.add(name)
                skipped.append(p.relative_to(root).as_posix())
        return out
    return ignore


class LocalFetcher:
    name = "local"

    def can_handle(self, ref: str) -> bool:
        try:
            return Path(ref).exists()
        except OSError:
            return False

    def fetch(self, ref: str, dest: Path) -> FetchResult:
        src = Path(ref).resolve()
        if not src.exists():
            raise FetchError(f"path does not exist: {ref}")
        dest.mkdir(parents=True, exist_ok=True)
        skipped: list[str] = []
        workroot = _enclosing_child(src, dest) if src.is_dir() else None
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True,
                            ignore=_unsafe_links(src, skipped, exclude=workroot))
        else:
            shutil.copy2(src, dest / src.name)
        warnings = ["local path: no archival pin (Available badge requires an archival deposit)"]
        if workroot is not None:
            warnings.append(
                f"the work directory is inside the artifact; `{workroot.relative_to(src).as_posix()}/` "
                "was left out of the copy (pass --workroot outside the artifact to avoid this)")
        if skipped:
            warnings.append(
                f"skipped {len(skipped)} symlink(s) that leave the tree, dangle or name a directory "
                f"({', '.join(skipped[:5])}{', …' if len(skipped) > 5 else ''})")
        return FetchResult(
            input=ref, resolved_type="local", src_dir=str(dest),
            pin=Pin(kind="none", value=str(src)),
            fetch_layer="local-copy",
            warnings=warnings,
            metadata={"skipped_symlinks": skipped} if skipped else {},
        )
