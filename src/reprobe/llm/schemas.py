"""Passive JSON shapes the three roles must conform to. Used by guard + light
structural checks. Kept permissive on free-text fields, strict on structure."""

RUN_ORDER = {
    "type": "object",
    "required": ["steps", "confidence"],
    "properties": {
        "steps": {"type": "array", "items": {"type": "object",
                  "required": ["path"], "properties": {"path": {"type": "string"},
                  "why": {"type": "string"}}}},
        "confidence": {"type": "number"},
        "uncertain": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
}

DIAGNOSIS = {
    "type": "object",
    "required": ["likely_cause", "suggested_fixes", "confidence"],
    "properties": {
        "likely_cause": {"type": "string"},
        "suggested_fixes": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
        "is_advisory": {"type": "boolean"},
    },
}

SUMMARY = {
    "type": "object",
    "required": ["summary"],
    "properties": {"summary": {"type": "string"}},
}
