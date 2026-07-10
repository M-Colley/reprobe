"""Runner for R scripts (.R)."""

from __future__ import annotations

from .base import BaseRunner, RunContext, _q
from ..models import Capabilities


class RScriptRunner(BaseRunner):
    id = "r"
    display_name = "R script"
    handles_types = frozenset({"r"})
    image_key = "r"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            can_verify=["script executes without error", "packages load"],
            cannot_verify=["numerical results match the paper"],
        )

    def build_command(self, ctx: RunContext) -> list[str]:
        t = ctx.step.target
        extra = " ".join(_q(a) for a in ctx.step.argv)
        # R_LIBS_USER points at packages installed during the gated-egress phase.
        return ["bash", "-c",
                f"mkdir -p /work/.reprobe_Rlib; export R_LIBS_USER=/work/.reprobe_Rlib; Rscript {_q(t)} {extra}".rstrip()]
