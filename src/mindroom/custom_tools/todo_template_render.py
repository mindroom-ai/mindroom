"""Render one workspace todo template with full Jinja in a short-lived, resource-limited child process.

Workspace templates are worker-writable, and the sandbox stops escapes but not allocation or looping,
so they never render in the primary. The child reads one JSON request on stdin and writes one JSON
result on stdout; memory, CPU, and output size are capped there, and the parent bounds wall time.
"""

from __future__ import annotations

import json
import math
import resource
import subprocess
import sys
from threading import BoundedSemaphore
from typing import TYPE_CHECKING, Any

from jinja2 import StrictUndefined, TemplateSyntaxError, UndefinedError, nodes
from jinja2.sandbox import SandboxedEnvironment, SecurityError

if TYPE_CHECKING:
    from collections.abc import Mapping

_MEMORY_LIMIT_BYTES = 128 * 1024 * 1024
_MAX_ERROR_CHARS = 500
# Children share the primary's CPU and memory quota, so one renders at a time.
_render_slots = BoundedSemaphore(1)


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


class _SubstitutionOnlyError(Exception):
    """Raised when a template needs more than substitution where memory cannot be capped."""


def _render_text(request: Mapping[str, Any], *, memory_limited: bool) -> str:
    environment = SandboxedEnvironment(autoescape=False, undefined=StrictUndefined)
    max_chars = request["max_chars"]
    parsed = environment.parse(request["template"])
    # Plain substitution cannot allocate, so it is safe where memory cannot be capped.
    substitution = (nodes.Output, nodes.TemplateData, nodes.Name)
    if not memory_limited and not all(isinstance(node, substitution) for node in parsed.find_all(nodes.Node)):
        msg = "this platform cannot cap template memory, so templates may only substitute `{{ NAME }}`"
        raise _SubstitutionOnlyError(msg)
    rendered: list[str] = []
    size = 0
    for chunk in environment.from_string(parsed).generate(**request["params"]):
        rendered.append(chunk)
        size += len(chunk)
        if size > max_chars:
            break
    return "".join(rendered)[: max_chars + 1]


def render_trusted_template(template_text: str, params: Mapping[str, Any], *, max_chars: int) -> str:
    """Render a template MindRoom ships in this process, with the same output cap and error messages."""
    result = _render({"template": template_text, "params": params, "max_chars": max_chars}, memory_limited=True)
    if "error" in result:
        raise ValueError(result["error"])
    return result["rendered"]


def _render(request: Mapping[str, Any], *, memory_limited: bool) -> dict[str, str]:
    try:
        return {"rendered": _render_text(request, memory_limited=memory_limited)}
    except UndefinedError as exc:
        message = f"undefined variable: {exc}"
    except TemplateSyntaxError as exc:
        message = f"syntax error: {exc}"
    except SecurityError as exc:
        message = f"unsafe template expression: {exc}"
    except _SubstitutionOnlyError as exc:
        message = str(exc)
    except MemoryError:
        message = "rendering exceeded its memory limit"
    except Exception as exc:
        message = f"render error: {type(exc).__name__}: {exc}"
    return {"error": message[:_MAX_ERROR_CHARS]}


def _main() -> None:
    cpu_seconds = int(sys.argv[1])
    request = json.load(sys.stdin)
    # Limits apply after startup imports, so they bound only the render.
    # Only Linux enforces RLIMIT_AS; elsewhere the call may succeed without capping anything.
    memory_limited = sys.platform == "linux"
    if memory_limited:
        resource.setrlimit(resource.RLIMIT_AS, (_MEMORY_LIMIT_BYTES, _MEMORY_LIMIT_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    json.dump(_render(request, memory_limited=memory_limited), sys.stdout)


if __name__ == "__main__":
    _main()
