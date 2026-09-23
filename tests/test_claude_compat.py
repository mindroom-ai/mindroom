"""Request-shaping shared by MindRoom's Claude provider adapters."""

from __future__ import annotations

from mindroom.anthropic_claude import MindRoomAnthropicClaude


def test_default_sampling_models_lose_sampling_controls_everywhere() -> None:
    """Agno 3 moves sampling controls into extra_body; the strip must follow them there."""
    model = MindRoomAnthropicClaude(id="claude-sonnet-5", api_key="test", temperature=0.3, top_p=0.9, top_k=5)

    request_params = model.get_request_params()

    assert not {"temperature", "top_p", "top_k"} & set(request_params)
    assert "extra_body" not in request_params


def test_other_claude_models_keep_sampling_controls_in_extra_body() -> None:
    """Models that still accept sampling controls keep agno's extra_body routing intact."""
    model = MindRoomAnthropicClaude(id="claude-haiku-4-5", api_key="test", temperature=0.3)

    request_params = model.get_request_params()

    assert request_params["extra_body"] == {"temperature": 0.3}
