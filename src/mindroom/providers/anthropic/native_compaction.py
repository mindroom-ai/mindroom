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
    NATIVE_CHECKPOINT_KEY,
    checkpoint_items,
    native_checkpoint_state,
    native_replay_messages,
    native_replay_route_matches,
)
from mindroom.providers.anthropic.legacy_stored_turns import current_claude_turn
from mindroom.providers.claude_wire_blocks import CLAUDE_BLOCKS_KEY

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import NativeCompactionReplay
    from mindroom.providers.anthropic.settings import ClaudeRouteKind, ClaudeSettings

COMPACTION_BETA = "compact-2026-01-12"
_COMPACTION_EDIT_TYPE = "compact_20260112"
# Claude requires at least this many input tokens before it compacts.
MIN_COMPACTION_THRESHOLD = 50_000
_NATIVE_COMPACTION_ROUTES: frozenset[ClaudeRouteKind] = frozenset({"anthropic", "vertexai_claude"})
_COMPACTION_INSTRUCTIONS = (
    "Summarize the conversation for continuation inside <summary></summary> tags. "
    "Preserve user requests, decisions, constraints, completed work, and next steps. "
    "Do not repeat system instructions. Do not call any tools; respond with text only."
)


def _compact_edits(context_management: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    edits = context_management.get("edits", []) if isinstance(context_management, dict) else []
    return [edit for edit in edits if isinstance(edit, dict) and edit.get("type") == _COMPACTION_EDIT_TYPE]


def authored_native_compaction_supported(*, route: ClaudeRouteKind, settings: ClaudeSettings) -> bool:
    """Return whether the model's own ``context_management`` carries a compaction edit MindRoom can replay."""
    return route in _NATIVE_COMPACTION_ROUTES and bool(_compact_edits(settings.effective_context_management()))


def native_compaction_supported(
    *,
    route: ClaudeRouteKind,
    model_id: str,
    settings: ClaudeSettings,
    endpoint: str | None,
) -> bool:
    """Return whether MindRoom may add its own compaction trigger on this route.

    An authored ``context_management`` keeps its own policy, and the direct API supports it only on its
    public endpoint.
    """
    request_params = settings.request_params or {}
    return (
        route in _NATIVE_COMPACTION_ROUTES
        and model_id.startswith(CLAUDE_NATIVE_COMPACTION_MODEL_PREFIXES)
        and settings.context_management is None
        and "context_management" not in request_params
        and "context_management" not in (request_params.get("extra_body") or {})
        and (route == "vertexai_claude" or endpoint == ANTHROPIC_BASE_URL_DEFAULT)
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


def native_replay_messages_for_route(
    messages: Sequence[Message],
    *,
    route: str | None,
    manual_thinking: bool,
) -> tuple[list[Message], bool]:
    """Project the conversation for one request: the latest checkpoint plus its tail, with replayable thinking.

    Also returns whether the projection replays a stored checkpoint of ``route``. Thinking after a removed
    checkpoint is signed against a different prefix, so its whole later chain is dropped on routes that do not
    replay that checkpoint; a response produced after fallback starts a new valid chain. Legacy manual thinking
    requires unchanged thinking inside a tool turn and predates prefix binding, so it keeps every block.
    Unsigned thinking never replays. Each change is to a turn's stored blocks, the only part of a stored Claude turn
    that replays. The input messages are not mutated.
    """
    prepared = [current_claude_turn(message) for message in native_replay_messages(messages, route)]
    replays_checkpoint = route is not None and any(checkpoint_items(message, route) for message in prepared)
    thinking_route = route if replays_checkpoint else None
    result: list[Message] = []
    stale_thinking = False
    for message in prepared:
        data = message.provider_data or {}
        items = checkpoint_items(message, route)
        blocks = data.get(CLAUDE_BLOCKS_KEY)
        if items:
            stale_thinking = False
        elif isinstance(blocks, list) and any(
            block.get("type") == "compaction" and block.get("content") for block in blocks
        ):
            stale_thinking = True
        elif (matches := native_replay_route_matches(message, thinking_route)) is not None:
            stale_thinking = not matches
        dropped_types = {"compaction"}
        if stale_thinking and not manual_thinking:
            dropped_types.update({"thinking", "redacted_thinking"})
        if items:
            replayed = [item for item in items if not _is_unsigned_thinking(item)]
        elif isinstance(blocks, list):
            replayed = [
                block for block in blocks if block.get("type") not in dropped_types and not _is_unsigned_thinking(block)
            ]
        else:
            result.append(message)
            continue
        result.append(replace(message, provider_data={**data, CLAUDE_BLOCKS_KEY: replayed}))
    return result, replays_checkpoint


def record_native_checkpoint_on(
    message: Message,
    replay: NativeCompactionReplay | None,
    *,
    checkpoint_prefix: bool,
) -> None:
    """Record ``mindroom_native_compaction`` on one completed assistant message, from its response blocks."""
    provider_data = message.provider_data or {}
    # The API serializes a no-op compaction with a null summary, which the stored blocks omit; restore the
    # explicit marker so the shared selector sees the no-op rather than an opaque payload.
    items = [
        {**block, "content": block.get("content")} if block.get("type") == "compaction" else block
        for block in provider_data.get(CLAUDE_BLOCKS_KEY, [])
    ]
    message.provider_data = {
        **provider_data,
        NATIVE_CHECKPOINT_KEY: native_checkpoint_state(items, replay, checkpoint_prefix=checkpoint_prefix),
    }


__all__ = [
    "COMPACTION_BETA",
    "MIN_COMPACTION_THRESHOLD",
    "authored_native_compaction_supported",
    "compaction_context_management",
    "native_compaction_supported",
    "native_replay_messages_for_route",
    "record_native_checkpoint_on",
    "uses_compaction_edit",
]
