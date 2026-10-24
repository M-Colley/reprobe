"""Orchestrates detection: manifest first, deterministic signatures always, LLM
advisory only when the heuristic is ambiguous (and never as an override)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..models import DetectResult
from . import manifest as manifest_mod
from . import signatures


def detect(
    src_dir: str | Path,
    *,
    use_llm: bool = False,
    llm_client: Any | None = None,
) -> tuple[DetectResult, dict[str, Any]]:
    """Returns (DetectResult, manifest_meta). manifest_meta carries environment
    hints + expected_outputs + author badge claims (possibly empty)."""
    src_dir = Path(src_dir)
    heuristic = signatures.scan(src_dir)
    loaded = manifest_mod.load(src_dir)

    if loaded is not None:
        m_result, meta = loaded
        if m_result.steps:                       # full autoui manifest: it wins
            # carry over manifest-declared expected outputs onto steps lacking them
            exp = meta.get("expected_outputs", [])
            for s in m_result.steps:
                if not s.expected_outputs and exp:
                    s.expected_outputs = list(exp)
                    s.outputs_inherited = len(m_result.steps) > 1
            # the deterministic repo scan still informs environment planning
            # (e.g. Dockerfile -> needs-repo2docker), so keep its flags — and
            # its non-code inventory, which a manifest doesn't declare
            m_result.flags = sorted(set(m_result.flags) | set(heuristic.flags))
            m_result.inventory = heuristic.inventory
            m_result.artifact_types = sorted(set(m_result.artifact_types) | set(heuristic.inventory))
            # the deterministic scan also discovers required R packages and Python
            # imports, which a manifest doesn't enumerate — carry them for the env
            # planner. Dropping either means the planner installs nothing it
            # detected, and the whole discovery path is dead in the real pipeline
            # while still passing its own unit tests.
            m_result.r_packages = heuristic.r_packages
            m_result.py_packages = heuristic.py_packages
            # ...and the license / dependency-manifest files on disk, which feed
            # the FAIR score. A manifest never declares these, so without this a
            # manifest-carrying repo would score WORSE than an unmanaged one.
            m_result.license_file = heuristic.license_file
            m_result.dep_manifest = heuristic.dep_manifest
            # which image runs a notebook depends on its kernel, which the
            # manifest does not state; read it from the notebook itself
            root = src_dir.resolve()
            pkg_root = signatures.own_package_root(root)
            for s in m_result.steps:
                f = (src_dir / s.target).resolve()
                if not (f.is_relative_to(root) and f.is_file()):
                    continue
                if s.kind == "jupyter" and "language" not in s.args:
                    s.args["language"] = signatures.notebook_language(f)
                elif s.kind == "r" and "cwd" not in s.args and (wd := signatures.r_working_dir(f, root)):
                    s.args["cwd"] = wd
                if pkg_root is not None and "pythonpath" not in s.args and (
                        s.kind == "python" or s.args.get("language") == "python"):
                    s.args["pythonpath"] = pkg_root
            return m_result, meta
        # codecheck or partial manifest: use heuristic steps + lifted expected outputs
        exp = meta.get("expected_outputs", [])
        for s in heuristic.steps:
            if not s.expected_outputs and exp:
                s.expected_outputs = list(exp)
                s.outputs_inherited = len(heuristic.steps) > 1
        heuristic.manifest_path = m_result.manifest_path
        heuristic.notes = m_result.notes + heuristic.notes
        return heuristic, meta

    # No manifest: optionally ask the LLM for an alternative ordering (advisory).
    meta: dict[str, Any] = {"environment": {}, "expected_outputs": [], "badges_claimed": [], "data": [], "paper": {}}
    if use_llm and llm_client is not None and signatures.is_ambiguous(heuristic):
        try:
            from ..llm import roles
            suggestion = roles.detect_run_order(llm_client, src_dir, heuristic)
            if suggestion is not None:
                heuristic.llm_confidence = suggestion.get("confidence")
                heuristic.notes.append(
                    "LLM proposed an alternative run order (advisory; not applied): "
                    + ", ".join(s.get("path", "?") for s in suggestion.get("steps", []))
                )
        except Exception:
            pass

    return heuristic, meta
