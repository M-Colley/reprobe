"""JSON Schemas the LLM roles must conform to. Each is sent to Ollama as the
structured-output ``format`` (so the model is constrained to it while it decodes)
AND validated with ``jsonschema`` on the way back (so a model or runtime that
ignores the constraint is caught, not trusted). Permissive on free-text fields,
strict on structure: a ``suggested_fixes`` that comes back as one string once
rendered one character per line.

``confidence`` is optional everywhere — small models omit it, and a missing
confidence is coerced to 0.0, below any threshold."""

RUN_ORDER = {
    "type": "object",
    "required": ["steps"],
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
    "required": ["likely_cause", "suggested_fixes"],
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
CLAIM = {
    "type": "object",
    "required": ["claim", "verdict"],
    "properties": {
        "claim": {"type": "string"},
        "paper_value": {"type": "string"},
        "produced_value": {"type": "string"},
        "verdict": {"enum": ["match", "mismatch", "unclear", "not-reported"]},
        "why": {"type": "string"},
    },
}

RESULTS_CHECK = {
    "type": "object",
    "required": ["claims"],
    "properties": {
        "claims": {"type": "array", "items": CLAIM},
        "overall": {"type": "string"},
        "confidence": {"type": "number"},
        "is_advisory": {"type": "boolean"},
    },
}
