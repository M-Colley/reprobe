"""Ollama client — the ONLY way the harness talks to the local LLM.

It returns DATA ONLY. There is no tool/function-calling surface wired to it, so
it physically cannot execute anything. Fully offline after the model is pulled.
If Ollama is unreachable or ``--no-llm`` is set, every method degrades to None
and the harness stays fully functional and deterministic.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import requests

_CONNECT_TIMEOUT_S = 5


def _normalize_tag(name: str) -> str:
    # Ollama lists untagged pulls as "<name>:latest"; compare tag-tolerantly.
    return name if ":" in name else f"{name}:latest"


def _model_in_tags(model: str, tags: Any) -> bool:
    """True iff `model` appears in an Ollama /api/tags payload."""
    if not isinstance(tags, dict) or not isinstance(tags.get("models"), list):
        return False
    names = {_normalize_tag(m.get("name", "")) for m in tags["models"] if isinstance(m, dict)}
    return _normalize_tag(model) in names


class OllamaClient:
    def __init__(self, endpoint: str, model: str, *, timeout_s: int = 60,
                 keep_alive: str = "10m", confidence_threshold: float = 0.6):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.keep_alive = keep_alive
        self.confidence_threshold = confidence_threshold

    def available(self) -> bool:
        """Endpoint reachability only; use model_available()/status() to also
        check that the pinned model is pulled."""
        try:
            r = requests.get(f"{self.endpoint}/api/tags", timeout=_CONNECT_TIMEOUT_S)
            return r.status_code == 200
        except Exception:
            return False

    def model_available(self) -> bool:
        """True iff the pinned model is actually pulled on the Ollama server."""
        return self.status().get("model_pulled") is True

    def status(self) -> dict[str, Any]:
        """Health snapshot for `reprobe doctor` and report provenance. Never
        raises. Keys: ok, reachable, model, model_pulled (None if unreachable),
        detail (human-readable; includes the fix command when the model is
        missing)."""
        st: dict[str, Any] = {"ok": False, "reachable": False, "model": self.model,
                              "model_pulled": None, "detail": ""}
        try:
            r = requests.get(f"{self.endpoint}/api/tags", timeout=_CONNECT_TIMEOUT_S)
        except Exception:
            st["detail"] = f"Ollama not reachable at {self.endpoint}"
            return st
        if r.status_code != 200:
            st["detail"] = f"Ollama at {self.endpoint} answered HTTP {r.status_code}"
            return st
        st["reachable"] = True
        try:
            tags = r.json()
        except Exception:
            tags = {}
        st["model_pulled"] = _model_in_tags(self.model, tags)
        if st["model_pulled"]:
            st["ok"] = True
            st["detail"] = f"{self.model} pulled and ready"
        else:
            st["detail"] = f"reachable but model NOT pulled — run: ollama pull {self.model}"
        return st

    def generate_json(self, prompt: str, *, system: str | None = None) -> Optional[dict[str, Any]]:
        """Single-shot, temperature 0, JSON-formatted. Returns a dict or None.
        Never raises into the pipeline."""
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0},
        }
        if system:
            payload["system"] = system
        try:
            # (connect, read) timeout: with stream=False Ollama sends nothing
            # until generation finishes, so the read timeout effectively caps
            # generation time — but it is NOT a strict total wall-clock bound.
            r = requests.post(f"{self.endpoint}/api/generate", json=payload,
                              timeout=(_CONNECT_TIMEOUT_S, self.timeout_s))
            if r.status_code != 200:
                return None
            text = r.json().get("response", "")
            return json.loads(text)
        except Exception:
            return None


def from_config(pins_llm: dict[str, Any]) -> Optional[OllamaClient]:
    if not pins_llm or not pins_llm.get("enabled", False):
        return None
    if pins_llm.get("provider") != "ollama":
        return None
    return OllamaClient(
        endpoint=pins_llm.get("endpoint", "http://127.0.0.1:11434"),
        model=pins_llm.get("model", "gemma4:e4b"),
        timeout_s=int(pins_llm.get("timeout_s", 60)),
        keep_alive=pins_llm.get("keep_alive", "10m"),
        confidence_threshold=float(pins_llm.get("confidence_threshold", 0.6)),
    )
