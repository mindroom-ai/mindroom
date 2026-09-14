"""Non-destructive provider checkpoint selection shared by replay and history budgeting.

The checkpoint lives on the assistant message which produced it. Canonical messages
remain available for another model and for the portable text compactor.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agno.models.message import Message
    from agno.models.response import ModelResponse

_NATIVE_CHECKPOINT_KEY = "mindroom_native_compaction"


@dataclass(frozen=True)
class _NativeCompactionSettings:
    """One request route and its provider-owned automatic trigger."""

    route: str
    threshold: int


class NativeCompactionModel:
    """Capability seam implemented by MindRoom's native provider adapters."""

    native_compaction: _NativeCompactionSettings | None = None
    id: str
    provider: str

    def native_compaction_supported(self) -> bool:
        """Return whether this concrete route can use automatic compaction."""
        raise NotImplementedError

    def native_compaction_endpoint(self) -> str:
        """Return the endpoint identity used to bind opaque replay state."""
        raise NotImplementedError

    def configure_native_compaction(self, *, threshold: int | None, history_generation: str = "") -> None:
        """Enable replay for this route and portable-history generation."""
        self.native_compaction = None
        if threshold is None or not self.native_compaction_supported():
            return
        identity = [self.provider, self.id, self.native_compaction_endpoint(), history_generation]
        route = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        self.native_compaction = _NativeCompactionSettings(route=route, threshold=threshold)


def record_native_checkpoint(
    response: ModelResponse,
    items: Sequence[dict[str, Any]],
    settings: _NativeCompactionSettings | None,
) -> None:
    """Retain only the latest usable checkpoint and the native output after it."""
    if settings is None:
        return
    last = next(
        (index for index in range(len(items) - 1, -1, -1) if _is_checkpoint(items[index])),
        None,
    )
    if last is None:
        return
    response.provider_data = {
        **(response.provider_data or {}),
        _NATIVE_CHECKPOINT_KEY: {"route": settings.route, "items": list(items[last:])},
    }


def _is_checkpoint(item: dict[str, Any]) -> bool:
    if item.get("type") != "compaction":
        return False
    # Claude's explicit null summary is a no-op, even when other fields exist.
    if "content" in item:
        return isinstance(item["content"], str) and bool(item["content"].strip())
    return isinstance(item.get("encrypted_content"), str) and bool(item["encrypted_content"])


def checkpoint_items(message: Message, route: str | None) -> list[dict[str, Any]]:
    """Read compatible, structurally valid checkpoint data from persisted input."""
    if route is None or not message.provider_data or message.role != "assistant":
        return []
    checkpoint = message.provider_data.get(_NATIVE_CHECKPOINT_KEY)
    if not isinstance(checkpoint, dict) or checkpoint.get("route") != route:
        return []
    items = checkpoint.get("items")
    if not isinstance(items, list) or not items or not all(isinstance(item, dict) for item in items):
        return []
    return items if _is_checkpoint(items[0]) else []


def native_replay_messages(messages: Sequence[Message], route: str | None) -> list[Message]:
    """Project the latest checkpoint plus tail, retaining current system messages."""
    last = next(
        (index for index in range(len(messages) - 1, -1, -1) if checkpoint_items(messages[index], route)),
        None,
    )
    if last is None:
        return list(messages)
    return [
        message for index, message in enumerate(messages) if index >= last or message.role in {"system", "developer"}
    ]


def checkpoint_estimated_tokens(items: Sequence[dict[str, Any]]) -> int:
    """Conservatively size the replay payload, including opaque checkpoint bytes.

    Ciphertext size is a guard estimate, not provider token usage. Never use the
    response's billed input count: it can include the pre-compaction transcript.
    """
    return (len(json.dumps(items, ensure_ascii=False)) + 3) // 4
