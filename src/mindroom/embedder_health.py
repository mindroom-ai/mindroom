"""Process-wide embedder health: classification, probe, and last-known failure.

The OpenAI-compatible embedder records a failure here before re-raising and
records healthy on any non-empty vector, so recovery is self-clearing the
moment a real embedding request succeeds. Probes cover the paths passive
recording cannot see: startup, config reload, and subprocess knowledge
refreshes that never touch the main-process embedder.
"""

from __future__ import annotations

import asyncio
from threading import Lock
from typing import TYPE_CHECKING

from mindroom.background_tasks import create_background_task
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_EMBEDDER_AUTH_FAILED_DETAIL = "embedder authentication failed (HTTP 401)"
_EMBEDDER_PERMISSION_DENIED_DETAIL = "embedder permission denied (HTTP 403)"
EMBEDDER_UNREACHABLE_DETAIL = "embedder endpoint unreachable"
_EMBEDDER_EMPTY_VECTOR_DETAIL = "embedder returned an empty vector"
_PROBE_TEXT = "mindroom embedder health check"

_failure_lock = Lock()
_current_failure: str | None = None


def record_embedder_health(error: str | None) -> None:
    """Record the outcome of the most recent embedding request or probe."""
    global _current_failure
    with _failure_lock:
        _current_failure = error


def get_embedder_failure() -> str | None:
    """Return the last recorded embedder failure, or None when healthy."""
    with _failure_lock:
        return _current_failure


def is_embedder_auth_failure_detail(detail: str | None) -> bool:
    """Return whether a recorded failure detail describes a credential rejection."""
    return detail in {_EMBEDDER_AUTH_FAILED_DETAIL, _EMBEDDER_PERMISSION_DENIED_DETAIL}


def is_embedder_provider_error(exc: BaseException) -> bool:
    """Return whether an exception came from the embedding provider SDK."""
    # Deferred so slim entry points never pay the openai SDK import; when a
    # provider call raised, the SDK is already loaded.
    from openai import OpenAIError  # noqa: PLC0415

    return isinstance(exc, OpenAIError)


def describe_embedder_error(exc: BaseException) -> str:
    """Return a compact failure description safe for logs, metadata, and tool text.

    HTTP and transport failures map to fixed messages so raw response bodies,
    hosts, and keys never leak; anything else is credential-redacted free text.
    """
    from openai import APIConnectionError, APIStatusError, AuthenticationError, PermissionDeniedError  # noqa: PLC0415

    # Deferred: importing the knowledge package here would recurse back into
    # this module through knowledge/__init__ -> refresh_scheduler.
    from mindroom.knowledge.redaction import redact_credentials_in_text  # noqa: PLC0415

    if isinstance(exc, AuthenticationError):
        return _EMBEDDER_AUTH_FAILED_DETAIL
    if isinstance(exc, PermissionDeniedError):
        return _EMBEDDER_PERMISSION_DENIED_DETAIL
    if isinstance(exc, APIStatusError):
        return f"embedder request failed (HTTP {exc.status_code})"
    if isinstance(exc, APIConnectionError):
        return EMBEDDER_UNREACHABLE_DETAIL
    return redact_credentials_in_text(f"{type(exc).__name__}: {exc}")


def embedder_in_use(config: Config) -> bool:
    """Return whether the active config can send keyed embedder requests."""
    if config.memory.embedder.provider != "openai":
        return False
    if any(base.mode == "semantic" for base in config.knowledge_bases.values()):
        return True
    if _memory_backend_uses_embedder(config.memory.backend, config.memory.search.mode):
        return True
    return any(
        _memory_backend_uses_embedder(
            config.resolve_entity(agent_name).memory_backend,
            config.resolve_entity(agent_name).memory_search.mode,
        )
        for agent_name in config.agents
    )


def _memory_backend_uses_embedder(backend: str, search_mode: str) -> bool:
    return backend == "mem0" or (backend == "file" and search_mode == "semantic")


def probe_embedder(config: Config, runtime_paths: RuntimePaths) -> str | None:
    """Run one strict embedding round-trip; return None when healthy."""
    # Deferred to break the import cycle with the embedding factory and keep
    # provider SDKs out of module import time.
    from mindroom.embedding_factory import create_configured_embedder  # noqa: PLC0415

    try:
        embedder = create_configured_embedder(config, runtime_paths)
        vector = embedder.get_embedding(_PROBE_TEXT)
    except Exception as exc:
        return describe_embedder_error(exc)
    if not vector:
        return _EMBEDDER_EMPTY_VECTOR_DETAIL
    return None


async def check_embedder_health(config: Config, runtime_paths: RuntimePaths, *, reason: str) -> None:
    """Probe the configured embedder off the event loop and record the outcome.

    No-ops when the config cannot send keyed embedder requests. Never raises,
    so fire-and-forget callers cannot break startup or refresh handling.
    """
    if not embedder_in_use(config):
        return
    error = await asyncio.to_thread(probe_embedder, config, runtime_paths)
    record_embedder_health(error)
    if error is not None:
        logger.error("embedder_health_check_failed", reason=reason, error=error)


def handle_embedder_config_reload(current_config: Config, new_config: Config, runtime_paths: RuntimePaths) -> None:
    """Reset recorded health and re-probe when a reload changed the embedder."""
    if current_config.memory.embedder == new_config.memory.embedder:
        return
    record_embedder_health(None)
    create_background_task(
        check_embedder_health(new_config, runtime_paths, reason="config_reload"),
        name="embedder_reload_health_check",
    )
