"""Render one workspace todo template with full Jinja in a short-lived, resource-limited child process.

Workspace templates are worker-writable, and the sandbox stops escapes but not allocation or looping,
so they never render in the primary. The child reads one JSON request on stdin and writes one JSON
result on stdout; memory, CPU, and output size are capped there, and the parent bounds wall time.
"""

from __future__ import annotations

import contextlib
import json
import math
import resource
import subprocess
import sys
from threading import BoundedSemaphore
from typing import TYPE_CHECKING, Any

from jinja2 import StrictUndefined, TemplateSyntaxError, UndefinedError
from jinja2.sandbox import SandboxedEnvironment, SecurityError

if TYPE_CHECKING:
    from collections.abc import Mapping

_MEMORY_LIMIT_BYTES = 256 * 1024 * 1024
_MAX_ERROR_CHARS = 500
# Bound how many render children one primary runs at once.
_render_slots = BoundedSemaphore(4)


def render_workspace_template(
    template_text: str,
    params: Mapping[str, Any],
    *,
    max_chars: int,
    timeout_seconds: float,
) -> str:
    """Return the rendered text, cut to ``max_chars + 1`` characters so the caller can refuse an oversized render.

    Raises ValueError with a template-facing message when rendering fails or exceeds its limits.
    """
    request = json.dumps(
        {"template": template_text, "params": dict(params), "max_chars": max_chars},
        default=str,
    ).encode("utf-8")
    if not _render_slots.acquire(blocking=False):
        msg = "todo template renderer is busy; try again shortly"
        raise ValueError(msg)
    try:
        completed = subprocess.run(
            # Run this file isolated by path: no MindRoom import, no inherited environment or user site-packages.
            [sys.executable, "-I", __file__, str(max(1, math.ceil(timeout_seconds)))],
            input=request,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={},
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        msg = "rendering exceeded its time limit"
        raise ValueError(msg) from None
    finally:
        _render_slots.release()
    if completed.returncode != 0:
        msg = "rendering exceeded its time or memory limit"
        raise ValueError(msg)
    result = json.loads(completed.stdout)
    if "error" in result:
        raise ValueError(result["error"])
    return result["rendered"]


def _render(request: Mapping[str, Any]) -> dict[str, str]:
    environment = SandboxedEnvironment(autoescape=False, undefined=StrictUndefined)
    max_chars = request["max_chars"]
    rendered: list[str] = []
    size = 0
    try:
        for chunk in environment.from_string(request["template"]).generate(**request["params"]):
            rendered.append(chunk)
            size += len(chunk)
            if size > max_chars:
                break
    except UndefinedError as exc:
        return {"error": f"undefined variable: {exc}"[:_MAX_ERROR_CHARS]}
    except TemplateSyntaxError as exc:
        return {"error": f"syntax error: {exc}"[:_MAX_ERROR_CHARS]}
    except SecurityError as exc:
        return {"error": f"unsafe template expression: {exc}"[:_MAX_ERROR_CHARS]}
    except MemoryError:
        return {"error": "rendering exceeded its memory limit"}
    except Exception as exc:
        return {"error": f"render error: {type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]}
    return {"rendered": "".join(rendered)[: max_chars + 1]}


def _main() -> None:
    cpu_seconds = int(sys.argv[1])
    request = json.load(sys.stdin)
    # Limits apply after startup imports, so they bound only the render.
    # macOS does not support lowering RLIMIT_AS; CPU and wall-time limits still apply there.
    with contextlib.suppress(ValueError, OSError):
        resource.setrlimit(resource.RLIMIT_AS, (_MEMORY_LIMIT_BYTES, _MEMORY_LIMIT_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    json.dump(_render(request), sys.stdout)


if __name__ == "__main__":
    _main()
