"""Claude turns stored before each turn kept its response blocks once, in request shape, under ``claude_blocks``.

Rows that kept the response blocks as the API returned them read as their blocks in request shape. Older rows that
kept no blocks rebuild from their message fields, with their signed reasoning and server-tool blocks ahead of the text.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from mindroom.native_compaction import NATIVE_CHECKPOINT_KEY
from mindroom.providers.anthropic.repairs import replay_safe_blocks
from mindroom.providers.claude_wire_blocks import CLAUDE_BLOCKS_KEY, RebuiltBlock, as_dict

if TYPE_CHECKING:
    from mindroom.llm.messages import Message

__all__ = ["current_claude_turn", "legacy_rebuilt_blocks"]

_RESPONSE_BLOCKS_KEY = "content_blocks"
_SERVER_TOOL_BLOCKS_KEY = "server_tool_blocks"
_SIGNATURE_KEY = "signature"


# LEGACY_COMPAT: Claude turns stored with their response blocks as the API returned them.
# Legacy format: an assistant message whose ``provider_data`` holds a non-empty ``content_blocks`` list and no
#   ``claude_blocks``: each response block's ``model_dump`` without None fields, in order, so a text block keeps its
#   citations, a tool-search result its response-only fields, and a search use that got no result stays. Beside it,
#   ``server_tool_blocks`` repeats the blocks other than text, thinking, and tool use, ``signature`` holds the last
#   thinking block's signature, and a native checkpoint's ``items`` are the blocks from its compaction block on.
# Last legacy release: v2026.10.651, which wrote this shape as every release since v2026.9.44 (Agno 3.0.9) did;
#   replacement: the next release stores the blocks once, in request shape, under ``claude_blocks``.
# Handling: the turn reads with its blocks under ``claude_blocks`` and its checkpoint items, both in request shape
#   (``replay_safe_blocks``), as every request repaired them, so its requests keep their bytes; ``server_tool_blocks``
#   and ``signature`` are no longer read, since the blocks hold both.
# Coverage: tests/providers/test_anthropic_family_parity.py::test_owned_adapter_reproduces_golden (each replay golden's
#   history holds turns stored this way),
#   tests/providers/test_anthropic_family_parity.py::test_a_turn_stored_now_replays_like_the_same_turn_stored_earlier,
#   tests/providers/test_anthropic_family_replay.py::test_a_turn_stored_with_its_response_blocks_replays_as_a_new_turn_does,
#   tests/providers/test_anthropic_family_replay.py::test_a_checkpoint_stored_with_its_response_blocks_replays_as_a_new_one_does,
#   tests/providers/test_anthropic_family_replay.py::test_canonical_fallback_removes_the_thinking_bound_to_the_checkpoint
#   (its "Ready" turn),
#   tests/providers/test_anthropic_family_replay.py::test_a_turn_stored_with_its_response_blocks_reads_in_request_shape_outside_a_request,
#   tests/providers/test_anthropic_family_replay.py::test_cited_answers_replay_as_plain_text
def current_claude_turn(message: Message) -> Message:
    """Return a stored Claude turn with its blocks under ``claude_blocks``; any other message is returned as it is."""
    data = message.provider_data
    blocks = data.get(_RESPONSE_BLOCKS_KEY) if data else None
    if data is None or CLAUDE_BLOCKS_KEY in data or not isinstance(blocks, list) or not blocks:
        return message
    current = {**data, CLAUDE_BLOCKS_KEY: replay_safe_blocks(blocks)}
    checkpoint = as_dict(data.get(NATIVE_CHECKPOINT_KEY))
    items = checkpoint.get("items") if checkpoint is not None else None
    if checkpoint is not None and isinstance(items, list) and all(isinstance(item, dict) for item in items):
        current[NATIVE_CHECKPOINT_KEY] = {**checkpoint, "items": replay_safe_blocks(items)}
    return replace(message, provider_data=current)


# LEGACY_COMPAT: Claude turns stored without their response blocks.
# Legacy format: an assistant message whose ``provider_data`` holds neither ``claude_blocks`` nor a non-empty
#   ``content_blocks`` list, with ``signature`` beside its joined ``reasoning_content``, its joined
#   ``redacted_reasoning_content``, and ``server_tool_blocks`` (each response block other than text, thinking, and
#   tool use, as the SDK's ``model_dump``).
# Last legacy release: v2026.9.43 stored them through Agno 3.0.5; v2026.9.44 (Agno 3.0.9) also stored every response
#   block in order under ``content_blocks``.
# Handling: the turn rebuilds from its fields: a thinking block from its reasoning while a signature is stored, a
#   redacted thinking block, then each stored server-tool block that is a dict, in request shape, ahead of its text
#   and tool uses, as Agno's formatter rebuilt them. Native compaction leaves these turns as they are: they predate it
#   (v2026.9.129), so no checkpoint or stale thinking comes before them.
# Coverage: tests/providers/test_anthropic_family_replay.py::test_a_turn_stored_without_content_blocks_replays_its_server_tool_blocks,
#   tests/providers/test_anthropic_family_replay.py::test_a_turn_stored_without_content_blocks_replays_its_tool_search_in_request_shape,
#   tests/providers/test_anthropic_family_replay.py::test_reasoning_without_an_anthropic_signature_never_becomes_a_thinking_block,
#   tests/test_mid_turn_compaction.py::test_request_sizing_counts_deferred_tool_definitions_once_tool_search_loads_them[claude_legacy]
def legacy_rebuilt_blocks(message: Message) -> list[dict[str, Any]]:
    """Return the blocks a turn stored without its response blocks replays ahead of its text and tool uses."""
    data = message.provider_data or {}
    blocks: list[dict[str, Any]] = []
    signature = data.get(_SIGNATURE_KEY)
    # Reasoning without a signature (another provider's, or a partially stored response) cannot be replayed.
    if message.reasoning_content is not None and isinstance(signature, str) and signature:
        blocks.append(RebuiltBlock(signature=signature, thinking=message.reasoning_content, type="thinking"))
    if message.redacted_reasoning_content is not None:
        blocks.append(RebuiltBlock(data=message.redacted_reasoning_content, type="redacted_thinking"))
    server_blocks = data.get(_SERVER_TOOL_BLOCKS_KEY)
    if isinstance(server_blocks, list):
        blocks.extend(replay_safe_blocks([block for block in server_blocks if isinstance(block, dict)]))
    return blocks
