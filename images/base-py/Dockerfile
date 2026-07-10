# reprobe-base-py — pinned Python runtime for author analyses.
# Non-root by default; the harness mounts a throwaway /work copy and runs with
# --network none. Build via images/build-images.sh (which pins the digest).
ARG MICROMAMBA_BASE=mambaorg/micromamba:1.5.10-noble
FROM ${MICROMAMBA_BASE}

ARG MAMBA_DOCKERFILE_ACTIVATE=1
# Prefer the solved lock for reproducibility; fall back to env.yaml if absent.
COPY --chown=$MAMBA_USER:$MAMBA_USER env.yaml conda-lock.yml* /tmp/
RUN if [ -f /tmp/conda-lock.yml ]; then \
      micromamba install -y -n base -f /tmp/conda-lock.yml ; \
    else \
      micromamba install -y -n base -f /tmp/env.yaml ; \
    fi && micromamba clean -ay

USER $MAMBA_USER
WORKDIR /work
