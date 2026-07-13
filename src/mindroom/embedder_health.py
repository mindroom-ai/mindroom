"""Process-wide embedder health: classification, probe, and last-known failure.

The OpenAI-compatible embedder records a failure here before raising and
records healthy on every validated response, so recovery is self-clearing the
moment a real embedding request succeeds. Probes cover the paths passive
recording cannot see: startup, config reload, and subprocess knowledge
refreshes that never touch the main-process embedder.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING

from mindroom.background_tasks import create_background_task
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from agno.knowledge.embedder.base import Embedder

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_EMBEDDER_AUTH_FAILED_DETAIL = "embedder authentication failed (HTTP 401)"
_EMBEDDER_PERMISSION_DENIED_DETAIL = "embedder permission denied (HTTP 403)"
EMBEDDER_UNREACHABLE_DETAIL = "embedder endpoint unreachable"
EMBEDDER_EMPTY_VECTOR_DETAIL = "embedder returned an empty vector"
_PROBE_TEXT = "mindroom embedder health check"

_failure_lock = Lock()
_current_failure: str | None = None
# Bumped when a reload changes the active embedder or its use, so a slow
# probe holding a pre-reload config snapshot cannot overwrite newer health.
_health_generation = 0


@dataclass(frozen=True)
class EmbedderHealthRecorder:
    """Generation-bound writer for embedding request outcomes."""

    generation: int

    def record(self, error: str | None) -> bool:
        """Record an outcome only while this recorder is current."""
        return _record_embedder_health_for_generation(self.generation, error)

    def is_current(self) -> bool:
        """Return whether this recorder still belongs to the active config."""
        return self.generation == _health_generation_snapshot()


class EmbedderRequestError(RuntimeError):
    """Embedder failure carrying only the classified, credential-safe detail.

    The embedder boundary raises this instead of the provider exception so
    upstream loggers — including agno's catch-log-and-return-empty paths —
    can never render a raw response body that may echo the rejected key.
    """


def get_embedder_failure() -> str | None:
    """Return the last recorded embedder failure, or None when healthy."""
    with _failure_lock:
        return _current_failure


def _health_generation_snapshot() -> int:
    with _failure_lock:
        return _health_generation


def capture_embedder_health_recorder() -> EmbedderHealthRecorder:
    """Capture a writer that cannot mutate health after a config reload."""
    return EmbedderHealthRecorder(_health_generation_snapshot())


def _record_embedder_health_for_generation(generation: int, error: str | None) -> bool:
    """Record a probe outcome unless the config generation moved on."""
    global _current_failure
    with _failure_lock:
        if _health_generation != generation:
            return False
        _current_failure = error
        return True


def _reset_embedder_health_generation() -> None:
    """Clear recorded health and invalidate every in-flight probe."""
    global _current_failure, _health_generation
    with _failure_lock:
        _health_generation += 1
        _current_failure = None


def handle_embedder_credential_change() -> None:
    """Invalidate health writers after the effective credential may have changed."""
    _reset_embedder_health_generation()


def is_embedder_auth_failure_detail(detail: str | None) -> bool:
    """Return whether a recorded failure detail describes a credential rejection."""
    return detail in {_EMBEDDER_AUTH_FAILED_DETAIL, _EMBEDDER_PERMISSION_DENIED_DETAIL}


# Fully-fixed classified forms only: the type-name fallback is excluded because
# identifier-shaped text extracted from operator free text could be a secret.
_CLASSIFIED_DETAIL_PATTERN = re.compile(
    r"embedder authentication failed \(HTTP 401\)"
    r"|embedder permission denied \(HTTP 403\)"
    r"|embedder request failed \(HTTP \d{3}\)"
    r"|embedder endpoint unreachable"
    r"|embedder returned an empty vector"
    r"|embedder returned \d+ embeddings for \d+ inputs",
)


def extract_classified_embedder_detail(text: str | None) -> str | None:
    """Extract the classified embedder detail from persisted free text, if any.

    Persisted knowledge ``last_error`` strings are operator-grade free text
    (git output, reader errors, refresh summaries); only the fixed vocabulary
    this module emits may cross into model-facing prompts and tool output.
    """
    if text is None:
        return None
    match = _CLASSIFIED_DETAIL_PATTERN.search(text)
    return match.group(0) if match else None


def _is_embedder_provider_error(exc: BaseException) -> bool:
    """Return whether an exception came from the embedding provider SDK."""
    # Deferred so slim entry points never pay the openai SDK import; when a
    # provider call raised, the SDK is already loaded.
    from openai import OpenAIError  # noqa: PLC0415

    return isinstance(exc, OpenAIError)


def classified_embedder_error(exc: BaseException) -> str | None:
    """Return a safe detail only when the exception is known to be embedder-related."""
    if isinstance(exc, EmbedderRequestError) or _is_embedder_provider_error(exc):
        return describe_embedder_error(exc)
    return None


def describe_embedder_error(exc: BaseException) -> str:
    """Return a compact failure description safe for logs, metadata, and tool text.

    Every branch returns fixed text (plus at most an HTTP status or exception
    type name), never `str(exc)` of a provider exception: arbitrary exception
    text can carry response bodies, hosts, or the rejected key itself.
    """
    if isinstance(exc, EmbedderRequestError):
        return str(exc)

    from openai import APIConnectionError, APIStatusError, AuthenticationError, PermissionDeniedError  # noqa: PLC0415

    if isinstance(exc, AuthenticationError):
        return _EMBEDDER_AUTH_FAILED_DETAIL
    if isinstance(exc, PermissionDeniedError):
        return _EMBEDDER_PERMISSION_DENIED_DETAIL
    if isinstance(exc, APIStatusError):
        return f"embedder request failed (HTTP {exc.status_code})"
    if isinstance(exc, APIConnectionError):
        return EMBEDDER_UNREACHABLE_DETAIL
    return f"embedder request failed ({type(exc).__name__})"


def embedder_in_use(config: Config) -> bool:
    """Return whether the active config can send keyed embedder requests."""
    return config.memory.embedder.provider == "openai" and semantic_embedder_configured(config)


def semantic_embedder_configured(config: Config) -> bool:
    """Return whether any memory backend or knowledge base needs the shared embedder."""
    if any(base.mode == "semantic" for base in config.knowledge_bases.values()):
        return True
    if _memory_backend_uses_embedder(config.memory.backend, config.memory.search.mode):
        return True
    for agent_name in config.agents:
        entity = config.resolve_entity(agent_name)
        if _memory_backend_uses_embedder(entity.memory_backend, entity.memory_search.mode):
            return True
    return False


def _memory_backend_uses_embedder(backend: str, search_mode: str) -> bool:
    return backend == "mem0" or (backend == "file" and search_mode == "semantic")


_PROBE_TIMEOUT_SECONDS = 10.0


def _bound_probe_client_timeout(embedder: Embedder) -> None:
    """Cap the probe's SDK client so a stalled endpoint fails fast.

    The installed OpenAI SDK defaults allow a 600-second read timeout plus
    retries, which would hang `mindroom doctor` and pin probe threads for
    minutes; normal runtime embedding traffic keeps the SDK defaults.
    """
    from mindroom.openai_embedder import MindRoomOpenAIEmbedder  # noqa: PLC0415

    if isinstance(embedder, MindRoomOpenAIEmbedder):
        embedder.client_params = {
            **(embedder.client_params or {}),
            "timeout": _PROBE_TIMEOUT_SECONDS,
            "max_retries": 0,
        }


def probe_embedder(
    config: Config,
    runtime_paths: RuntimePaths,
    health_recorder: EmbedderHealthRecorder | None = None,
) -> str | None:
    """Run one strict embedding round-trip; return None when healthy."""
    # Deferred to break the import cycle with the embedding factory and keep
    # provider SDKs out of module import time.
    from mindroom.embedding_factory import create_configured_embedder  # noqa: PLC0415

    try:
        if health_recorder is None:
            embedder = create_configured_embedder(config, runtime_paths)
        else:
            embedder = create_configured_embedder(config, runtime_paths, health_recorder=health_recorder)
        _bound_probe_client_timeout(embedder)
        vector = embedder.get_embedding(_PROBE_TEXT)
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
    _reset_embedder_health_generation()
    if not embedder_in_use(new_config):
        return
    create_background_task(
        check_embedder_health(new_config, runtime_paths, reason="config_reload"),
        name="embedder_reload_health_check",
    )
