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
    """One replay route; a null trigger leaves the authored provider policy intact."""

    route: str
    threshold: int | None


class NativeCompactionModel:
    """Capability seam implemented by MindRoom's native provider adapters."""

    native_compaction: _NativeCompactionSettings | None = None
    id: str
    provider: str

    def configure_portable_replay(self) -> None:
        """Let adapters keep local history budgets authoritative over stored state."""

    def estimate_portable_replay_tokens(self, messages: list[Message]) -> int | None:  # noqa: ARG002
        """Return a provider-aware estimate, or use the shared history estimator."""
        return None

    def native_compaction_supported(self) -> bool:
        """Return whether this concrete route can use automatic compaction."""
        raise NotImplementedError

    def native_compaction_endpoint(self) -> str:
        """Return the endpoint identity used to bind opaque replay state."""
        raise NotImplementedError

    def authored_native_compaction_supported(self) -> bool:
        """Return whether this adapter can replay its caller-authored native policy."""
        return False

    def configure_native_compaction(
        self,
        *,
        threshold: int | None,
        history_generation: str = "",
        allow_authored: bool = False,
    ) -> None:
        """Enable replay for this route and portable-history generation."""
        self.native_compaction = None
        if threshold is None or not self.native_compaction_supported():
            if not allow_authored or not self.authored_native_compaction_supported():
                return
            threshold = None
        endpoint = self.native_compaction_endpoint()
        if not endpoint:
            return
        identity = [self.provider, self.id, endpoint, history_generation]
        route = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        self.native_compaction = _NativeCompactionSettings(route=route, threshold=threshold)


def common_native_endpoint(identities: Sequence[str]) -> str:
    """Return one compatible client route, or disable ambiguous native replay."""
    routes = set(identities)
    return next(iter(routes)) if len(routes) == 1 else ""


def record_native_checkpoint(
    response: ModelResponse,
    items: Sequence[dict[str, Any]],
    settings: _NativeCompactionSettings | None,
) -> None:
    """Record effective replay settings and, when present, the latest checkpoint.

    Every completed response records its route, including explicit native-off
    state. Approval rebuilds and signed-thinking replay need this even when the
    provider did not compact on this request.
    """
    state: dict[str, Any] | None = None
    if settings is not None:
        state = {"route": settings.route, "threshold": settings.threshold}
        last = next(
            (index for index in range(len(items) - 1, -1, -1) if _is_checkpoint(items[index])),
            None,
        )
        state["checkpoint_prefix"] = last is not None
        if last is not None:
            state["items"] = list(items[last:])
    response.provider_data = {
        **(response.provider_data or {}),
        _NATIVE_CHECKPOINT_KEY: state,
    }


def record_native_request_prefix(response: ModelResponse, *, checkpoint_prefix: bool) -> None:
    """Mark completed output that inherited a checkpoint from its actual request."""
    state = (response.provider_data or {}).get(_NATIVE_CHECKPOINT_KEY)
    if checkpoint_prefix and isinstance(state, dict):
        state["checkpoint_prefix"] = True


def recorded_native_settings(message: Message) -> _NativeCompactionSettings | None:
    """Read the effective native policy from one completed assistant response."""
    state = (message.provider_data or {}).get(_NATIVE_CHECKPOINT_KEY)
    if message.role != "assistant" or not isinstance(state, dict):
        return None
    route, threshold = state.get("route"), state.get("threshold")
    if not isinstance(route, str) or not route or "threshold" not in state:
        return None
    if threshold is not None and (type(threshold) is not int or threshold <= 0):
        return None
    return _NativeCompactionSettings(route=route, threshold=threshold)


def native_replay_route_matches(message: Message, route: str | None) -> bool | None:
    """Compare effective prefix provenance; None means legacy history without it."""
    data = message.provider_data or {}
    if _NATIVE_CHECKPOINT_KEY not in data:
        return None
    state = data[_NATIVE_CHECKPOINT_KEY]
    if state is None:
        return route is None
    if not isinstance(state, dict) or not isinstance(state.get("route"), str):
        return False
    # Missing provenance cannot prove that legacy thinking used canonical input.
    prefix_route = state["route"] if state.get("checkpoint_prefix", True) else None
    return prefix_route == route


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
