# reprobe-base-r — pinned R runtime for author analyses (Rscript, rmarkdown).
ARG MICROMAMBA_BASE=mambaorg/micromamba:1.5.10-noble
FROM ${MICROMAMBA_BASE}

ARG MAMBA_DOCKERFILE_ACTIVATE=1
COPY --chown=$MAMBA_USER:$MAMBA_USER env.yaml conda-lock.yml* /tmp/
RUN if [ -f /tmp/conda-lock.yml ]; then \
      micromamba install -y -n base -f /tmp/conda-lock.yml ; \
    else \
      micromamba install -y -n base -f /tmp/env.yaml ; \
    fi && micromamba clean -ay

# R-kernel notebooks run here under IRkernel (`papermill -k ir`). Register the
# kernelspec in the environment and fail the BUILD if it is missing: an image
# without it would fail every R notebook with "No such kernel", read as the
# artifact's fault.
RUN Rscript -e 'IRkernel::installspec(user = FALSE, prefix = Sys.getenv("CONDA_PREFIX"))'     && jupyter kernelspec list | grep -qw ir

USER $MAMBA_USER
WORKDIR /work
