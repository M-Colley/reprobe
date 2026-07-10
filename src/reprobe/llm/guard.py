"""Validates every LLM response against a passive schema and rejects anything
that looks like it is trying to smuggle executable instructions into a field
the harness might act on. Defense-in-depth: even though the client cannot
execute, we never let an LLM string reach a shell.
"""

from __future__ import annotations

import re
from typing import Any

_SUSPICIOUS = re.compile(
    r"(?:\$\(|`|;\s*rm\s|--privileged|/var/run/docker\.sock|curl\s|wget\s|nc\s|bash\s+-c|os\.system|subprocess)",
    re.I,
)

# Display-only fields: advisory text shown to a human, NEVER executed by the
# harness. They may legitimately contain commands/markdown backticks. The only
# field the harness could ever act on is a run-order "path", which stays strict.
_FREEFORM_FIELDS = {"notes", "summary", "likely_cause", "why", "suggested_fixes", "uncertain"}


def is_clean(obj: Any, *, _field: str | None = None) -> bool:
    if isinstance(obj, dict):
        return all(is_clean(v, _field=k) for k, v in obj.items())
    if isinstance(obj, list):
        return all(is_clean(v, _field=_field) for v in obj)
    if isinstance(obj, str):
        if _field in _FREEFORM_FIELDS:
            return True
        return _SUSPICIOUS.search(obj) is None
    return True


def sanitize(obj: dict[str, Any]) -> dict[str, Any] | None:
    """Return obj if clean, else None (caller treats None as 'no LLM advice')."""
    return obj if is_clean(obj) else None
