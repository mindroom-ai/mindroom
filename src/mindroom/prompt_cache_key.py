"""Stable prompt-cache routing keys derived from execution identity."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

__all__ = ["derive_session_prompt_cache_key", "inherit_session_prompt_cache_key"]

_PROMPT_CACHE_KEY_PREFIX = "mindroom"


@runtime_checkable
class _SessionPromptCacheModel(Protocol):
    """Model carrying a provider-side prompt-cache routing key."""

    prompt_cache_key: str | None


def inherit_session_prompt_cache_key(*, source_model: object, target_model: object) -> None:
    """Copy a live model's session cache identity onto a dedicated model."""
    if not isinstance(source_model, _SessionPromptCacheModel):
        return
    if not isinstance(target_model, _SessionPromptCacheModel):
        return
    target_model.prompt_cache_key = source_model.prompt_cache_key


def derive_session_prompt_cache_key(identity: ToolExecutionIdentity) -> str | None:
    """Derive a stable prompt-cache routing key for one active execution."""
    if identity.session_id is None:
        return None
    source = ":".join(
        (
            identity.channel,
            identity.agent_name,
            identity.requester_id or "",
            identity.room_id or "",
            identity.resolved_thread_id or identity.thread_id or "",
            identity.session_id,
        ),
    )
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]
    return f"{_PROMPT_CACHE_KEY_PREFIX}-{digest}"
