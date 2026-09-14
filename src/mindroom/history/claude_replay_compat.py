"""Claude reasoning-signature cleanup after portable history rewriting.

This compatibility rule applies only to completed portable turns. Native
checkpoint replay owns its distinct reasoning policy in the provider adapter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agno.models.message import Message


_QUEUED_MESSAGE_NOTICE_MARKER_KEY = "mindroom_queued_message_notice"


def strip_stale_anthropic_replay_fields(messages: list[Message]) -> int:
    """Strip stale Anthropic thinking replay fields from completed turns."""
    last_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        provider_data = messages[i].provider_data
        is_queued_notice = isinstance(provider_data, dict) and provider_data.get(_QUEUED_MESSAGE_NOTICE_MARKER_KEY) in (
            True,
            "persisted",
        )
        if messages[i].role == "user" and not is_queued_notice:
            last_user_idx = i
            break
    if last_user_idx < 0:
        return 0
    modified = 0
    for msg in messages[:last_user_idx]:
        if msg.role != "assistant":
            continue
        pd = msg.provider_data
        if not isinstance(pd, dict):
            continue
        has_replay_fields = "signature" in pd
        content_blocks = pd.get("content_blocks")
        if isinstance(content_blocks, list):
            retained_blocks = [
                block
                for block in content_blocks
                if not (
                    isinstance(block, dict)
                    and block.get("type") in {"thinking", "redacted_thinking", "redacted_reasoning_content"}
                )
            ]
            if len(retained_blocks) != len(content_blocks):
                pd["content_blocks"] = retained_blocks
                has_replay_fields = True
        if not has_replay_fields:
            continue
        msg.reasoning_content = None
        msg.redacted_reasoning_content = None
        pd.pop("signature", None)
        modified += 1
    return modified
