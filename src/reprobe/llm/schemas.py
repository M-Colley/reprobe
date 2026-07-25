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

# Claim-by-claim comparison of the paper against what the run actually printed.
# "verdict" is deliberately three-valued: "unclear" is the honest answer whenever
# the produced output does not report the number at all, and is what the model
# should reach for instead of guessing.
RESULTS_CHECK = {
    "type": "object",
    "required": ["claims"],
    "properties": {
        "claims": {"type": "array", "items": {
            "type": "object",
            "required": ["claim", "verdict"],
            "properties": {
                "claim": {"type": "string"},
                "paper_value": {"type": "string"},
                "produced_value": {"type": "string"},
                "verdict": {"enum": ["match", "mismatch", "unclear", "not-reported"]},
                "why": {"type": "string"},
            }}},
        "overall": {"type": "string"},
        "confidence": {"type": "number"},
        "is_advisory": {"type": "boolean"},
    },
}
