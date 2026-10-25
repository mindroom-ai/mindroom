"""Claude reasoning-signature cleanup after portable history rewriting.

This compatibility rule applies only to completed portable turns. Native
checkpoint replay owns its distinct reasoning policy in the provider adapter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.history.run_visibility import is_queued_notice_message

if TYPE_CHECKING:
    from mindroom.llm.messages import Message


def strip_stale_anthropic_replay_fields(messages: list[Message]) -> int:
    """Strip stale Anthropic thinking replay fields from completed turns."""
    user_indexes = [i for i, msg in enumerate(messages) if msg.role == "user" and not is_queued_notice_message(msg)]
    if not user_indexes:
        return 0
    modified = 0
    for msg in messages[: user_indexes[-1]]:
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
