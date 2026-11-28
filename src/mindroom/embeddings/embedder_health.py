"""Probe the configured semantic embedder at startup, after reloads, and after subprocess refreshes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mindroom.background_tasks import create_background_task
from mindroom.config.resolution import memory_backend, memory_search
from mindroom.embeddings.errors import EMBEDDER_EMPTY_VECTOR_DETAIL, describe_embedder_error
from mindroom.embeddings.health_state import (
    EmbedderHealthRecorder,
    capture_embedder_health_recorder,
    get_embedder_failure,
    reset_embedder_health_generation,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.embeddings.base import Embedder

logger = get_logger(__name__)

_PROBE_TEXT = "mindroom embedder health check"


def handle_embedder_credential_change(config: Config, runtime_paths: RuntimePaths) -> None:
    """Invalidate old writers and probe the replacement credential."""
    health_recorder = reset_embedder_health_generation()
    create_background_task(
        check_embedder_health(
            config,
            runtime_paths,
            reason="credential_change",
            health_recorder=health_recorder,
        ),
        name="embedder_credential_change_health_check",
    )


def embedder_in_use(config: Config) -> bool:
    """Return whether the active config can send embedder requests whose credentials can be rejected."""
    return config.memory.embedder.provider in {"openai", "vertex_ai"} and semantic_embedder_configured(config)


def semantic_embedder_configured(config: Config) -> bool:
    """Return whether any memory backend or knowledge base needs the shared embedder."""
    if any(base.mode == "semantic" for base in config.knowledge_bases.values()):
        return True
    if _memory_backend_uses_embedder(config.memory.backend, config.memory.search.mode):
        return True
    for agent_name in config.agents:
        if _memory_backend_uses_embedder(memory_backend(config, agent_name), memory_search(config, agent_name).mode):
            return True
    return False


def _memory_backend_uses_embedder(backend: str, search_mode: str) -> bool:
    return backend == "mem0" or (backend == "file" and search_mode == "semantic")


_PROBE_TIMEOUT_SECONDS = 10.0


def _bound_probe_client_timeout(embedder: Embedder, provider: str) -> None:
    """Cap the probe's SDK client so a stalled endpoint fails fast.

    The installed OpenAI SDK defaults allow a 600-second read timeout plus
    retries, and the Vertex AI embedder a 60-second one plus retries, which
    would hang `mindroom doctor` and pin probe threads for minutes; runtime
    searches and memory keep those defaults, and only a knowledge
    refresh, which retries itself, turns the OpenAI embedder's retries off.
    google-genai loads only for a Vertex AI embedder.
    """
    from mindroom.embeddings.openai_embedder import OpenAIEmbedder  # noqa: PLC0415

    if isinstance(embedder, OpenAIEmbedder):
        embedder.timeout = _PROBE_TIMEOUT_SECONDS
        embedder.max_retries = 0
    elif provider == "vertex_ai":
        from mindroom.embeddings.vertex_ai_embedder import VertexAIEmbedder  # noqa: PLC0415

        if isinstance(embedder, VertexAIEmbedder):
            embedder.timeout = _PROBE_TIMEOUT_SECONDS
            embedder.single_attempt = True


def probe_embedder(
    config: Config,
    runtime_paths: RuntimePaths,
    health_recorder: EmbedderHealthRecorder | None = None,
) -> str | None:
    """Run one strict embedding round-trip; return None when healthy."""
    # Load embedder construction and its provider integrations only when probing.
    from mindroom.embeddings.factory import create_configured_embedder  # noqa: PLC0415

    try:
        embedder = create_configured_embedder(config, runtime_paths, health_recorder=health_recorder)
        _bound_probe_client_timeout(embedder, config.memory.embedder.provider)
        vector = embedder.embed([_PROBE_TEXT], purpose="query")[0]
    except Exception as exc:
        return describe_embedder_error(exc)
    if not vector:
        return EMBEDDER_EMPTY_VECTOR_DETAIL
    return None


async def check_embedder_health(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    reason: str,
    health_recorder: EmbedderHealthRecorder | None = None,
) -> None:
    """Probe the configured embedder off the event loop and record the outcome.

    No-ops when the config cannot send keyed embedder requests. Never raises,
    so fire-and-forget callers cannot break startup or refresh handling.
    """
    if not embedder_in_use(config):
        return
    health_recorder = health_recorder or capture_embedder_health_recorder()
    error = await asyncio.to_thread(probe_embedder, config, runtime_paths, health_recorder)
    if not health_recorder.record(error):
        logger.info("embedder_health_probe_discarded_stale", reason=reason)
        return
    if error is not None:
        logger.error("embedder_health_check_failed", reason=reason, error=error)


def handle_embedder_config_reload(current_config: Config, new_config: Config, runtime_paths: RuntimePaths) -> None:
    """Reset recorded health and re-probe when a reload changed embedder use.

    A reload that only enables or disables the last semantic consumer changes
    what the recorded health describes even when the embedder block itself is
    identical, so both signals reset the generation.
    """
    if current_config.memory.embedder == new_config.memory.embedder and embedder_in_use(
        current_config,
    ) == embedder_in_use(new_config):
        return
    reset_embedder_health_generation()
    if not embedder_in_use(new_config):
        return
    create_background_task(
        check_embedder_health(new_config, runtime_paths, reason="config_reload"),
        name="embedder_reload_health_check",
    )


__all__ = [
    "EmbedderHealthRecorder",
    "capture_embedder_health_recorder",
    "check_embedder_health",
    "embedder_in_use",
    "get_embedder_failure",
    "handle_embedder_config_reload",
    "handle_embedder_credential_change",
    "probe_embedder",
    "semantic_embedder_configured",
]
