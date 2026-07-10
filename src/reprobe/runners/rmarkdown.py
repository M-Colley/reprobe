"""Runner for R Markdown documents (.Rmd) via rmarkdown::render."""

from __future__ import annotations

from pathlib import PurePosixPath

from .base import BaseRunner, RunContext, _q
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
        # The author-controlled path reaches R via commandArgs(TRUE), never by
        # interpolation into R source or a double-quoted (shell-active) string.
        render = "a<-commandArgs(TRUE); rmarkdown::render(a[[1]], output_dir=a[[2]])"
        return ["bash", "-c",
                f"mkdir -p /work/.reprobe_Rlib; export R_LIBS_USER=/work/.reprobe_Rlib; "
                f"Rscript -e '{render}' {_q(t)} {_q(f'/work/{outdir}')}"]
