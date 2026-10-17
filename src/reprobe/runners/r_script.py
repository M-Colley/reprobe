"""Runner for R scripts (.R)."""

from __future__ import annotations

from ..models import Capabilities
from .base import _R_ENV, BaseRunner, RunContext, _q

#: Answers getActiveDocumentContext / getSourceEditorContext / documentPath with
#: the script's own path, then runs the script with source(). commandArgs is
#: masked so the script sees exactly what `Rscript script.R ...` gives it
#: (source() would leave the script's path in front of its arguments). No
#: single quotes anywhere: the expression sits inside single quotes in bash.
RSTUDIO_SHIM_NOTE = ("[reprobe] harness-assisted retry: rstudioapi document-context calls are "
                     "answered with the path of this script; nothing else is changed")
_RSTUDIO_SHIM = (
    "a <- commandArgs(TRUE); f <- normalizePath(a[[1]]); "
    "ctx <- function(...) list(id = \"#reprobe\", path = f, "
    "contents = readLines(f, warn = FALSE), selection = list()); "
    "ns <- asNamespace(\"rstudioapi\"); "
    "for (fn in c(\"getActiveDocumentContext\", \"getSourceEditorContext\")) "
    "if (exists(fn, envir = ns, inherits = FALSE)) assignInNamespace(fn, ctx, \"rstudioapi\"); "
    "if (exists(\"documentPath\", envir = ns, inherits = FALSE)) "
    "assignInNamespace(\"documentPath\", function(...) f, \"rstudioapi\"); "
    "commandArgs <- function(trailingOnly = FALSE) { x <- base::commandArgs(trailingOnly); "
    "if (trailingOnly) x[-1] else x }; "
    f"message(\"{RSTUDIO_SHIM_NOTE}\"); "
    "source(a[[1]], echo = FALSE)")


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
                f"mkdir -p /work/.reprobe_Rlib; {_R_ENV}Rscript {_q(t)} {extra}".rstrip()]

    def assisted_rstudio_command(self, ctx: RunContext) -> list[str]:
        """The same run, with rstudioapi's document-context calls answered as the
        IDE would answer them: the path of this script. Nothing else changes and
        the author's file is not edited. Used only for the supplementary retry
        after the RStudio diagnosis; its result never enters the verdict."""
        t = ctx.step.target
        extra = " ".join(_q(a) for a in ctx.step.argv)
        return ["bash", "-c",
                f"mkdir -p /work/.reprobe_Rlib; {_R_ENV}"
                f"Rscript -e '{_RSTUDIO_SHIM}' {_q(t)} {extra}".rstrip()]
