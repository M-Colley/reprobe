"""Runner for Jupyter notebooks (Python or R kernels).

Executes headlessly with papermill when available, falling back to
``jupyter nbconvert --execute``. The executed notebook is written back into the
working copy so it is collected as a produced artifact (and so figures/outputs
embedded in the notebook are preserved for the reviewer).
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Optional

from ..models import Capabilities, RawRunOutput, RunResult, RunStep
from .base import _R_ENV, BaseRunner, RunContext, _q, env_path_prefix

#: Printed, with this exit status, when the image has no notebook executor at all:
#: the harness chose an image that cannot run notebooks — not the artifact's fault.
_NO_EXECUTOR = "[reprobe] no notebook executor (papermill or jupyter) in this image"
_NO_EXECUTOR_EXIT = 97

# The kernel each supported notebook language runs under, in its base image:
# ipykernel's `python3` in base-py (or in the artifact's own conda env, which
# gets ipykernel installed), IRkernel's `ir` in base-r.
_KERNELS = {"python": ("python", "python3"), "r": ("r", "ir")}


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

    def image_key_for(self, step: RunStep) -> str:
        return _KERNELS.get(_language(step), _KERNELS["python"])[0]

    def unsupported(self, step: RunStep) -> Optional[str]:
        lang = _language(step)
        if lang in _KERNELS:
            return None
        return (f"the notebook's kernel language is `{lang}`, and no reprobe base image has a "
                "kernel for it (Python and R are supported) — not run, so no statement about it")

    def build_command(self, ctx: RunContext) -> list[str]:
        t = ctx.step.target
        lang = _language(ctx.step)
        kernel = _KERNELS.get(lang, _KERNELS["python"])[1]
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
        #
        # The kernel is named, not taken from the notebook: its kernelspec name is
        # whatever the author's machine called it ("conda-env-study-py", "myenv"),
        # and papermill's "No such kernel" for that would be read as the
        # artifact failing. The LANGUAGE is the notebook's own.
        cmd = (
            "export HOME=/work XDG_CACHE_HOME=/work/.reprobe_cache MPLCONFIGDIR=/tmp; "
            "export PYTHONPATH=/work/.reprobe_deps${PYTHONPATH:+:$PYTHONPATH} PYTHONUNBUFFERED=1; "
            + (_R_ENV if lang == "r" else "")
            + f"{env_path_prefix(ctx)}"
            f"if command -v papermill >/dev/null 2>&1; then "
            f"papermill --no-progress-bar --log-output -k {kernel} --cwd {_q(nbdir)} {_q(t)} {_q(out)}; "
            f"elif command -v jupyter >/dev/null 2>&1; then "
            f"jupyter nbconvert --to notebook --execute --ExecutePreprocessor.kernel_name={kernel} "
            f"--output {_q(stem + '.executed')} {_q(t)}; "
            f"else echo '{_NO_EXECUTOR}' >&2; exit {_NO_EXECUTOR_EXIT}; fi"
        )
        return ["bash", "-c", cmd]

    def interpret(self, raw: Optional[RawRunOutput], ctx: RunContext) -> RunResult:
        res = super().interpret(raw, ctx)
        if raw is not None and raw.exit_code == _NO_EXECUTOR_EXIT                 and _NO_EXECUTOR in str(res.diagnostics.get("log_tail", "")):
            res.status = "error"
            res.diagnostics["harness_error"] = (
                f"the `{self.image_key_for(ctx.step)}` base image has no notebook executor, so "
                "this notebook could not be run — a gap in the harness's image, not the artifact")
        return res


def _language(step: RunStep) -> str:
    return str((step.args or {}).get("language") or "python").lower()
