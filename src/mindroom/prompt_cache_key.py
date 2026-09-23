"""Stable prompt-cache routing keys derived from execution identity."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

__all__ = ["derive_agent_prompt_cache_key", "derive_session_routing_key"]

_PROMPT_CACHE_KEY_PREFIX = "mindroom"


def _hashed_key(parts: tuple[str | None, ...]) -> str:
    """Hash structured scope fields without exposing identities or ambiguous separators."""
    source = json.dumps(parts, separators=(",", ":"))
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]
    return f"{_PROMPT_CACHE_KEY_PREFIX}-{digest}"


def derive_agent_prompt_cache_key(identity: ToolExecutionIdentity, *, storage_root: Path) -> str:
    """Group one agent's threads within the same storage-root and requester scope."""
    return _hashed_key(
        (
            str(storage_root),
            identity.tenant_id,
            identity.account_id,
            identity.channel,
            identity.agent_name,
            identity.requester_id,
        ),
    )


def derive_session_routing_key(identity: ToolExecutionIdentity, *, storage_root: Path) -> str | None:
    """Keep Codex conversation headers distinct from the shared cache group."""
    if identity.session_id is None:
        return None
    return _hashed_key(
        (
            derive_agent_prompt_cache_key(identity, storage_root=storage_root),
            identity.room_id,
            identity.resolved_thread_id or identity.thread_id,
            identity.session_id,
        ),
    )
