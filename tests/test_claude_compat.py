"""Request-shaping shared by MindRoom's Claude provider adapters."""

from __future__ import annotations

import pytest
from anthropic.lib.streaming import MessageStopEvent
from anthropic.types import Message as AnthropicMessage

from mindroom.providers.anthropic_claude import MindRoomAnthropicClaude


@pytest.mark.parametrize("model_id", ["claude-sonnet-5-5", "claude-opus-5-5", "claude-sonnet-5"])
def test_default_sampling_models_lose_sampling_controls_everywhere(model_id: str) -> None:
    """Agno 3 moves sampling controls into extra_body; the strip must follow them there."""
    model = MindRoomAnthropicClaude(id=model_id, api_key="test", temperature=0.3, top_p=0.9, top_k=5)

    request_params = model.get_request_params()

    assert not {"temperature", "top_p", "top_k"} & set(request_params)
    assert "extra_body" not in request_params


def test_other_claude_models_keep_sampling_controls_in_extra_body() -> None:
    """Models that still accept sampling controls keep agno's extra_body routing intact."""
    model = MindRoomAnthropicClaude(id="claude-haiku-4-5", api_key="test", temperature=0.3)

    request_params = model.get_request_params()

    assert request_params["extra_body"] == {"temperature": 0.3}


def _message(*, stop_reason: str, content: list[dict[str, object]]) -> AnthropicMessage:
    return AnthropicMessage.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
    )


@pytest.mark.parametrize("stream", [False, True])
def test_parsed_responses_keep_the_terminal_stop_reason(stream: bool) -> None:
    """The Agno parser keeps the stop reason of a response and of a stream's last event, so a cut-off shows."""
    model = MindRoomAnthropicClaude(id="claude-sonnet-5", api_key="test")
    message = _message(stop_reason="max_tokens", content=[{"type": "text", "text": "Chapter one"}])

    parsed = (
        model._parse_provider_response_delta(MessageStopEvent(type="message_stop", message=message))
        if stream
        else model._parse_provider_response(message)
    )

    assert parsed.provider_data is not None
    assert parsed.provider_data["stop_reason"] == "max_tokens"


def test_a_tool_call_cut_off_at_the_output_limit_is_kept() -> None:
    """Agno drops tool calls unless the response stopped for them; the cut-off call is kept to be answered."""
    model = MindRoomAnthropicClaude(id="claude-sonnet-5", api_key="test")
    message = _message(
        stop_reason="max_tokens",
        content=[{"type": "tool_use", "id": "toolu_cut", "name": "write_file", "input": {"path": "big.py"}}],
    )

    parsed = model._parse_provider_response(message)

    assert [call["id"] for call in parsed.tool_calls] == ["toolu_cut"]
