"""Claude native compaction: the request edit, the replay projection, and the checkpoint recorded on each response.

Claude compacts a long conversation server-side when a request carries the ``compact_20260112`` edit, and
returns a ``compaction`` block whose encrypted content replaces the earlier prefix on later requests. MindRoom
stores that checkpoint on the assistant message under ``mindroom_native_compaction`` without changing the
canonical conversation, so another model or the portable compactor can still read the original messages.
The shared selection and route rules live in :mod:`mindroom.native_compaction`.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from mindroom.model_defaults import ANTHROPIC_BASE_URL_DEFAULT, CLAUDE_NATIVE_COMPACTION_MODEL_PREFIXES
from mindroom.native_compaction import (
    checkpoint_items,
    native_replay_messages,
    native_replay_route_matches,
    record_native_checkpoint,
    record_native_request_prefix,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import NativeCompactionReplay
    from mindroom.providers.anthropic.settings import ClaudeSettings

COMPACTION_BETA = "compact-2026-01-12"
_COMPACTION_EDIT_TYPE = "compact_20260112"
# Claude requires at least this many input tokens before it compacts.
MIN_COMPACTION_THRESHOLD = 50_000
_NATIVE_COMPACTION_PROVIDERS = frozenset({"Anthropic", "VertexAI"})
_COMPACTION_INSTRUCTIONS = (
    "Summarize the conversation for continuation inside <summary></summary> tags. "
    "Preserve user requests, decisions, constraints, completed work, and next steps. "
    "Do not repeat system instructions. Do not call any tools; respond with text only."
)


def _compact_edits(context_management: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    edits = context_management.get("edits", []) if isinstance(context_management, dict) else []
    return [edit for edit in edits if isinstance(edit, dict) and edit.get("type") == _COMPACTION_EDIT_TYPE]


def authored_native_compaction_supported(*, provider: str, settings: ClaudeSettings) -> bool:
    """Return whether the model's own ``context_management`` carries a compaction edit MindRoom can replay."""
    return provider in _NATIVE_COMPACTION_PROVIDERS and bool(_compact_edits(settings.effective_context_management()))


def native_compaction_supported(*, provider: str, model_id: str, settings: ClaudeSettings, endpoint: str) -> bool:
    """Return whether MindRoom may add its own compaction trigger on this route.

    An authored ``context_management`` keeps its own policy, and the direct API supports it only on its
    public endpoint.
    """
    request_params = settings.request_params or {}
    return (
        provider in _NATIVE_COMPACTION_PROVIDERS
        and model_id.startswith(CLAUDE_NATIVE_COMPACTION_MODEL_PREFIXES)
        and settings.context_management is None
        and "context_management" not in request_params
        and "context_management" not in (request_params.get("extra_body") or {})
        and (provider == "VertexAI" or endpoint == ANTHROPIC_BASE_URL_DEFAULT)
    )


def compaction_context_management(threshold: int) -> dict[str, Any]:
    """Return the ``context_management`` edit that asks Claude to compact once the input reaches ``threshold``."""
    return {
        "edits": [
            {
                "type": _COMPACTION_EDIT_TYPE,
                "trigger": {"type": "input_tokens", "value": threshold},
                "pause_after_compaction": False,
                "instructions": _COMPACTION_INSTRUCTIONS,
            },
        ],
    }


def uses_compaction_edit(context_management: Mapping[str, Any] | None) -> bool:
    """Return whether the effective context management asks Claude to compact, so the request needs the beta.

    An edit that pauses after compaction is refused, because MindRoom requires automatic continuation.
    """
    edits = _compact_edits(context_management)
    if any(edit.get("pause_after_compaction") for edit in edits):
        msg = "pause_after_compaction=True is unsupported; MindRoom requires automatic continuation."
        raise ValueError(msg)
    return bool(edits)


def _is_unsigned_thinking(block: dict[str, Any]) -> bool:
    """Return whether a stored thinking block was never signed, as when a stream stops mid-block.

    The API rejects an unsigned thinking block on replay. Empty thinking text is normal and stays; only the
    missing signature makes the block invalid.
    """
    return block.get("type") == "thinking" and not block.get("signature")


def request_uses_native_checkpoint(messages: Sequence[Message], route: str | None) -> bool:
    """Return whether the projected request replays a stored checkpoint of this route."""
    return route is not None and any(checkpoint_items(message, route) for message in messages)


def native_replay_messages_for_route(
    messages: Sequence[Message],
    *,
    route: str | None,
    manual_thinking: bool,
) -> list[Message]:
    """Project the conversation for one request: the latest checkpoint plus its tail, with replayable thinking.

    Thinking after a removed checkpoint is signed against a different prefix, so its whole later chain is
    dropped on routes that do not replay that checkpoint; a response produced after fallback starts a new
    valid chain. Legacy manual thinking requires unchanged thinking inside a tool turn and predates prefix
    binding, so it keeps every block. Unsigned thinking never replays. The input messages are not mutated.
    """
    prepared = native_replay_messages(messages, route)
    thinking_route = route if request_uses_native_checkpoint(prepared, route) else None
    result: list[Message] = []
    stale_thinking = False
    for message in prepared:
        data = message.provider_data or {}
        if not data and not stale_thinking:
            result.append(message)
            continue
        items = checkpoint_items(message, route)
        block_lists = {
            key: data[key] for key in ("content_blocks", "server_tool_blocks") if isinstance(data.get(key), list)
        }
        if items:
            stale_thinking = False
        elif any(
            block.get("type") == "compaction" and block.get("content")
            for blocks in block_lists.values()
            for block in blocks
        ):
            stale_thinking = True
        elif (matches := native_replay_route_matches(message, thinking_route)) is not None:
            stale_thinking = not matches
        dropped_types = {"compaction"}
        drop_thinking = stale_thinking and not manual_thinking
        if drop_thinking:
            dropped_types.update({"thinking", "redacted_thinking", "redacted_reasoning_content"})
        next_data = dict(data)
        for key, blocks in block_lists.items():
            next_data[key] = [
                block for block in blocks if block.get("type") not in dropped_types and not _is_unsigned_thinking(block)
            ]
        if items:
            next_data["content_blocks"] = [item for item in items if not _is_unsigned_thinking(item)]
        if drop_thinking:
            next_data.pop("signature", None)
            result.append(
                replace(message, provider_data=next_data, reasoning_content=None, redacted_reasoning_content=None),
            )
        else:
            result.append(replace(message, provider_data=next_data))
    return result


def record_native_checkpoint_on(
    message: Message,
    replay: NativeCompactionReplay | None,
    *,
    checkpoint_prefix: bool,
) -> None:
    """Record ``mindroom_native_compaction`` on one completed assistant message.

    Every response records its route, None included, because approval rebuilds and signed-thinking replay
    need the provenance even when nothing compacted. A checkpoint in the output means the provider compacted
    on this request, so the items from that checkpoint on are stored for replay; ``checkpoint_prefix`` marks
    output produced from a request that itself replayed a checkpoint.
    """
    # The API serializes a no-op compaction with a null summary, which the stored blocks omit; restore the
    # explicit marker so the shared selector sees the no-op rather than an opaque payload.
    items = [
        {**block, "content": block.get("content")} if block.get("type") == "compaction" else block
        for block in (message.provider_data or {}).get("content_blocks", [])
    ]
    record_native_checkpoint(message, items, replay)
    record_native_request_prefix(message, checkpoint_prefix=checkpoint_prefix)


__all__ = [
    "COMPACTION_BETA",
    "MIN_COMPACTION_THRESHOLD",
    "authored_native_compaction_supported",
    "compaction_context_management",
    "native_compaction_supported",
    "native_replay_messages_for_route",
    "record_native_checkpoint_on",
    "request_uses_native_checkpoint",
    "uses_compaction_edit",
]
