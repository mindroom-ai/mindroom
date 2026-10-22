"""Keep the shared agent instructions a cacheable prefix of an OpenAI Responses request."""

from __future__ import annotations

from typing import Any

from mindroom.system_prompt import SESSION_CONTEXT_BOUNDARY

__all__ = ["formatted_input_with_shared_system_prefix"]


def formatted_input_with_shared_system_prefix(
    formatted_input: list[Any],
    *,
    explicit_breakpoint: bool,
) -> list[Any]:
    """Split MindRoom's initial system text without rewriting history or custom blocks."""
    if not formatted_input:
        return formatted_input
    first_message = formatted_input[0]
    if not isinstance(first_message, dict) or first_message.get("role") not in {"system", "developer"}:
        return formatted_input
    content = first_message.get("content")
    if not isinstance(content, str):
        return formatted_input
    shared_text, boundary, session_text = content.partition(SESSION_CONTEXT_BOUNDARY)
    if not boundary or not shared_text.strip():
        return formatted_input

    shared_block: dict[str, Any] = {"type": "input_text", "text": shared_text}
    if explicit_breakpoint:
        shared_block["prompt_cache_breakpoint"] = {"mode": "explicit"}
    return [
        {**first_message, "content": [shared_block]},
        {**first_message, "content": boundary + session_text},
        *formatted_input[1:],
    ]
