"""Runner for R Markdown documents (.Rmd) via rmarkdown::render."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Optional

from ..models import Capabilities, RawRunOutput, RunResult
from .base import _R_ENV, BaseRunner, RunContext, _q

_NO_LATEX = "[reprobe] PDF output requested but the image has no LaTeX; rendering as HTML"

# A PDF format (pdf_document, bookdown::pdf_document2, papaja::apa6_pdf, ...)
# needs a LaTeX toolchain the base image does not carry, and the render then
# fails AFTER every chunk ran — a typesetting failure read as the analysis
# failing. With no LaTeX on PATH the document is rendered as HTML instead: the
# code (the part a reproduction checks) runs the same, and the substitution is
# recorded. No single quotes: the expression sits inside single quotes in bash.
_RENDER = ("a<-commandArgs(TRUE); f<-rmarkdown::all_output_formats(a[[1]]); fmt<-NULL; "
           "if (length(f) && grepl(\"pdf\", f[[1]], ignore.case=TRUE) && "
           "!any(nzchar(Sys.which(c(\"pdflatex\", \"xelatex\", \"lualatex\"))))) "
           f"{{ message(\"{_NO_LATEX}\"); fmt<-\"html_document\" }}; "
           "rmarkdown::render(a[[1]], output_format=fmt, output_dir=a[[2]])")


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
        return ["bash", "-c",
                f"mkdir -p /work/.reprobe_Rlib; {_R_ENV}"
                f"Rscript -e '{_RENDER}' {_q(t)} {_q(f'/work/{outdir}')}"]

    def interpret(self, raw: Optional[RawRunOutput], ctx: RunContext) -> RunResult:
        res = super().interpret(raw, ctx)
        if raw is not None and raw.log_path and _said(raw.log_path, _NO_LATEX):
            res.diagnostics["rendered_as"] = "html_document (no LaTeX in the image)"
            res.not_verified.append("the PDF output: the document asks for PDF, and with no LaTeX "
                                    "in the image it was rendered as HTML — the code ran, the "
                                    "typesetting was not checked")
        return res


def _said(log_path: str, line: str, head: int = 256 * 1024) -> bool:
    """Whether the marker appears in the first part of the log (it is printed
    before rendering starts)."""
    try:
        with open(Path(log_path), "rb") as fh:
            return line.encode() in fh.read(head)
    except OSError:
        return False
