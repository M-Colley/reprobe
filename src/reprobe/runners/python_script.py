"""Runner for plain Python scripts."""

from __future__ import annotations

from .base import BaseRunner, RunContext
from ..models import Capabilities


class PythonScriptRunner(BaseRunner):
    id = "python"
    display_name = "Python script"
    handles_types = frozenset({"python"})
    image_key = "python"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            can_verify=["script executes without raising", "dependencies import"],
            cannot_verify=["numerical results match the paper", "statistical validity"],
        )

    def build_command(self, ctx: RunContext) -> list[str]:
        target = ctx.step.target
        extra = " ".join(_q(a) for a in ctx.step.argv)
        # PYTHONPATH lets a gated-egress install phase drop deps into /work/.reprobe_deps
        return ["bash", "-c",
                f"export PYTHONPATH=/work/.reprobe_deps:$PYTHONPATH; python {_q(target)} {extra}".rstrip()]


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
