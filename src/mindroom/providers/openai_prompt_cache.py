"""Preserve the shared agent prefix at the OpenAI Responses wire boundary."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any

from mindroom.system_prompt import SESSION_CONTEXT_BOUNDARY

if TYPE_CHECKING:
    from agno.models.openai import OpenAIResponses


def supports_openai_cache_breakpoints(model: OpenAIResponses) -> bool:
    """Enable explicit breakpoints only for supported models on the public API."""
    version_match = re.match(r"gpt-(\d+)(?:\.(\d+))?(?:-|$)", model.id)
    if version_match is None or (int(version_match[1]), int(version_match[2] or 0)) < (5, 6):
        return False
    clients = [client for client in (model.client, model.async_client) if client is not None]
    if clients:
        base_urls = [client.base_url for client in clients]
    else:
        configured_url = (model.client_params or {}).get("base_url", model.base_url)
        base_urls = [configured_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"]
    return all(str(url).rstrip("/") == "https://api.openai.com/v1" for url in base_urls)


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
