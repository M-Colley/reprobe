"""pins.yaml is the single source of truth, but a few files must repeat a pin
because they cannot read YAML at runtime (docker-compose's `${VAR:-default}`).
Those copies drifted together once — both still named an Ollama too old for the
pinned model — so the repetition is checked here instead of trusted."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PINS = yaml.safe_load((ROOT / "config" / "pins.yaml").read_text(encoding="utf-8"))
COMPOSE_TEXT = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")


def test_compose_ollama_image_default_matches_pins():
    defaults = set(re.findall(r"\$\{REPROBE_OLLAMA_IMAGE:-([^}]+)\}", COMPOSE_TEXT))
    assert defaults == {PINS["llm"]["ollama_image"]}


def test_compose_model_default_matches_pins():
    defaults = set(re.findall(r"\$\{REPROBE_LLM_MODEL:-([^}]+)\}", COMPOSE_TEXT))
    assert defaults == {PINS["llm"]["model"]}


def test_base_image_tags_follow_year_and_revision():
    # build-images.sh tags whatever these say; a tag that disagrees with
    # year.revision means a republish bumped one and not the other.
    want = f"{PINS['year']}.{PINS['revision']}"
    for key in ("python", "r", "controller"):
        ref = PINS["base_images"][key]
        assert ref.rsplit(":", 1)[1] == want, f"base_images.{key} = {ref}, expected tag {want}"


def _version(ref: str) -> tuple[int, ...]:
    tag = ref.split("@", 1)[0].rsplit(":", 1)[1]
    return tuple(int(p) for p in tag.split("."))


def test_ollama_runtime_can_serve_a_gemma4_model():
    # Ollama < 0.20 refuses to pull gemma4 tags ("requires a newer version of
    # Ollama"), which leaves the LLM permanently enabled-but-unavailable.
    if PINS["llm"]["model"].startswith("gemma4"):
        assert _version(PINS["llm"]["ollama_image"]) >= (0, 20, 0)
