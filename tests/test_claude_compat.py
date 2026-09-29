"""Request-shaping shared by MindRoom's Claude provider adapters."""

from __future__ import annotations

import pytest

from mindroom.anthropic_claude import MindRoomAnthropicClaude


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
