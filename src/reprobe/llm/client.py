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


class OllamaClient:
    def __init__(self, endpoint: str, model: str, *, timeout_s: int = 60, keep_alive: str = "10m"):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.keep_alive = keep_alive

    def available(self) -> bool:
        try:
            r = requests.get(f"{self.endpoint}/api/tags", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

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
            r = requests.post(f"{self.endpoint}/api/generate", json=payload, timeout=self.timeout_s)
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
    )
