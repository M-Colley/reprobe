"""Runner for Jupyter notebooks (Python or R kernels).

Executes headlessly with papermill when available, falling back to
``jupyter nbconvert --execute``. The executed notebook is written back into the
working copy so it is collected as a produced artifact (and so figures/outputs
embedded in the notebook are preserved for the reviewer).
"""

from __future__ import annotations

from pathlib import PurePosixPath

from .base import BaseRunner, RunContext
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
        out = f"{PurePosixPath(t).parent}/{stem}.executed.ipynb"
        q = lambda s: "'" + str(s).replace("'", "'\\''") + "'"
        cmd = (
            "export PYTHONPATH=/work/.reprobe_deps:$PYTHONPATH; "
            f"if command -v papermill >/dev/null 2>&1; then "
            f"papermill --no-progress-bar {q(t)} {q(out)}; "
            f"else jupyter nbconvert --to notebook --execute --output {q(stem + '.executed')} {q(t)}; fi"
        )
        return ["bash", "-c", cmd]
