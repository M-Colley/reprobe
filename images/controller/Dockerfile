# reprobe-controller — the orchestrator + docker CLI, for CI (Docker-out-of-Docker).
# Mount the host docker socket at runtime; this image NEVER runs author code
# itself — it spawns sibling sandboxed containers via the host daemon.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY src /app/src
COPY config /app/config
RUN pip install --no-cache-dir -e .

ENV REPROBE_CONFIG_DIR=/app/config
ENTRYPOINT ["reprobe"]
CMD ["--help"]
