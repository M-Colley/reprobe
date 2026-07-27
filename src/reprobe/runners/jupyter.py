"""Runner for Jupyter notebooks (Python or R kernels).

Executes headlessly with papermill when available, falling back to
``jupyter nbconvert --execute``. The executed notebook is written back into the
working copy so it is collected as a produced artifact (and so figures/outputs
embedded in the notebook are preserved for the reviewer).
"""

from __future__ import annotations

from pathlib import PurePosixPath

from .base import BaseRunner, RunContext, _q
from ..models import Capabilities


class JupyterRunner(BaseRunner):
    id = "jupyter"
    display_name = "Jupyter notebook"
    handles_types = frozenset({"jupyter"})
    image_key = "python"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            can_verify=["notebook executes top-to-bottom without error", "all cells run"],
            cannot_verify=["results match the paper", "figures are scientifically correct"],
        )

    def build_command(self, ctx: RunContext) -> list[str]:
        t = ctx.step.target
        stem = PurePosixPath(t).stem
        nbdir = PurePosixPath(t).parent          # "." for root-level notebooks
        out = f"{nbdir}/{stem}.executed.ipynb"
        # --cwd makes papermill run the kernel in the notebook's own directory,
        # matching nbconvert's default — the verdict must not flip on which tool
        # the image happens to contain. HOME/caches: see python_script.py.
        #
        # --log-output is what makes a timeout diagnosable. papermill defaults to
        # --no-log-output, so a notebook that hangs for the whole budget emits a
        # single line ("Executing notebook with kernel: python3") and the log tail
        # cannot say which cell stalled. With it, papermill logs an
        # "Executing Cell N---" boundary per cell and streams cell output, so the
        # tail names the culprit. Logging only; it cannot change the exit code.
        cmd = (
            "export HOME=/work XDG_CACHE_HOME=/work/.reprobe_cache MPLCONFIGDIR=/tmp; "
            "export PYTHONPATH=/work/.reprobe_deps:$PYTHONPATH; "
            f"if command -v papermill >/dev/null 2>&1; then "
            f"papermill --no-progress-bar --log-output --cwd {_q(nbdir)} {_q(t)} {_q(out)}; "
            f"else jupyter nbconvert --to notebook --execute --output {_q(stem + '.executed')} {_q(t)}; fi"
        )
        return ["bash", "-c", cmd]
