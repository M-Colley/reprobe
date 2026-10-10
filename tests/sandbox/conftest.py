"""Gate for the live-daemon sandbox suite (every test here is marked ``docker``).

These tests start real containers and check from inside them that the envelope
holds. With no daemon they SKIP, which is right on a laptop. With
REPROBE_REQUIRE_DOCKER=1 (CI sets it) they FAIL instead, because a sandbox job
that skips itself shows green for checks that never ran.

The gate is a fixture, not an import-time check. The default suite
(``-m 'not docker'``) still collects this directory, and it should not call
``docker info`` just to deselect everything.
"""

from __future__ import annotations

import os
from typing import NoReturn

import pytest

from reprobe import docker_exec
from reprobe.config import load_config

REQUIRE_ENV = "REPROBE_REQUIRE_DOCKER"


def _docker_required() -> bool:
    return os.environ.get(REQUIRE_ENV, "").strip() == "1"


def _unavailable(reason: str) -> NoReturn:
    if _docker_required():
        pytest.fail(f"{reason}. {REQUIRE_ENV}=1 is set, so this run must exercise a real Docker "
                    "daemon; refusing to skip the sandbox suite.", pytrace=False)
    pytest.skip(f"{reason} (set {REQUIRE_ENV}=1 to make this a failure instead of a skip)")


@pytest.fixture(scope="session", autouse=True)
def sandbox_image() -> str:
    """The small trusted image every probe runs in, pulled once per session.

    pins.yaml's ``fetch.fallback_python_image`` is a plain python:*-slim. It has
    no uid 57439 and nothing preinstalled, so the probes depend on nothing a
    reprobe base image adds."""
    if not docker_exec.docker_available():
        _unavailable("no reachable Docker daemon (`docker info` failed, or docker is not on PATH)")
    image = (load_config().fetch_cfg or {}).get("fallback_python_image")
    if not image:
        pytest.fail("config/pins.yaml has no fetch.fallback_python_image for the sandbox probes to run in",
                    pytrace=False)
    if not docker_exec.image_present(image) and not docker_exec.pull_image(image):
        _unavailable(f"could not pull the probe image {image}")
    return image
