"""Gate for the end-to-end suite (every test here is marked ``e2e``).

These run the whole pipeline — fetch, detect, plan, the networked install phase,
the offline run, the report — inside the published base images. With no daemon,
or without the images, they SKIP; with REPROBE_REQUIRE_DOCKER=1 (the CI job sets
it) they FAIL, so a misconfigured job cannot show green for checks that never ran.
"""

from __future__ import annotations

import os

import pytest

from reprobe import docker_exec
from reprobe.config import load_config


def _unavailable(reason: str):
    if os.environ.get("REPROBE_REQUIRE_DOCKER", "").strip() == "1":
        pytest.fail(f"{reason}; REPROBE_REQUIRE_DOCKER=1 is set, refusing to skip", pytrace=False)
    pytest.skip(reason)


@pytest.fixture(scope="session", autouse=True)
def base_images():
    if not docker_exec.docker_available():
        _unavailable("no reachable Docker daemon")
    cfg = load_config()
    images = {k: cfg.base_image(k) for k in ("python", "r")}
    for key, image in images.items():
        if not image or not docker_exec.image_present(image):
            _unavailable(f"base image for {key} ({image}) is not present — run `reprobe pull`")
    return images
