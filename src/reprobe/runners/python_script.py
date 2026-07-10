"""Runner for plain Python scripts."""

from __future__ import annotations

from .base import BaseRunner, RunContext, _q
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
        # HOME/caches must land somewhere writable under the read-only rootfs
        # (mirrors the install phase). Exported in the command, not via
        # ContainerSpec.env, so _redact doesn't mangle the logged argv.
        # PYTHONPATH lets a gated-egress install phase drop deps into /work/.reprobe_deps
        return ["bash", "-c",
                f"export HOME=/work XDG_CACHE_HOME=/work/.reprobe_cache MPLCONFIGDIR=/tmp; "
                f"export PYTHONPATH=/work/.reprobe_deps:$PYTHONPATH; python {_q(target)} {extra}".rstrip()]
