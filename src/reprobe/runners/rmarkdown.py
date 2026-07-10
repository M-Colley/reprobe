"""Runner for R Markdown documents (.Rmd) via rmarkdown::render."""

from __future__ import annotations

from pathlib import PurePosixPath

from .base import BaseRunner, RunContext
from ..models import Capabilities


class RMarkdownRunner(BaseRunner):
    id = "rmarkdown"
    display_name = "R Markdown"
    handles_types = frozenset({"rmarkdown"})
    image_key = "r"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            can_verify=["document renders without error", "embedded code runs"],
            cannot_verify=["results match the paper"],
        )

    def build_command(self, ctx: RunContext) -> list[str]:
        t = ctx.step.target
        outdir = PurePosixPath(t).parent
        q = lambda s: str(s).replace("'", "")
        r = f"rmarkdown::render('{q(t)}', output_dir='/work/{q(outdir)}')"
        return ["bash", "-c",
                f"mkdir -p /work/.reprobe_Rlib; export R_LIBS_USER=/work/.reprobe_Rlib; Rscript -e \"{r}\""]
