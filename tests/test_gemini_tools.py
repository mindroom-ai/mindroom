"""Tests for the MindRoom Gemini media toolkit."""

from types import SimpleNamespace
from unittest.mock import Mock

import agno.tools.models.gemini as agno_gemini
from agno.agent import Agent
from google.genai import types

from mindroom.custom_tools.gemini_media import MindRoomGeminiTools


def test_generate_image_uses_gemini_content_generation(monkeypatch) -> None:  # noqa: ANN001
    """Nano Banana 2 should use generate_content while preserving generate_image."""
    response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model",
                    parts=[types.Part(inline_data=types.Blob(data=b"image-bytes", mime_type="image/png"))],
                ),
            ),
        ],
    )
    generate_content = Mock(return_value=response)
    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    monkeypatch.setattr(agno_gemini, "Client", lambda **_kwargs: client)
    toolkit = MindRoomGeminiTools(
        api_key="test-key",
        image_generation_model="gemini-3.1-flash-image",
        enable_generate_video=False,
    )

    result = toolkit.generate_image(Mock(spec=Agent), "A geometric Matrix room")

    assert result.images is not None
    assert len(result.images) == 1
    assert result.images[0].content == b"image-bytes"
    kwargs = generate_content.call_args.kwargs
    assert kwargs["model"] == "gemini-3.1-flash-image"
    assert kwargs["contents"] == "A geometric Matrix room"
    assert kwargs["config"].response_modalities == ["IMAGE"]
