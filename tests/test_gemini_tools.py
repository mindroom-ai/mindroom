"""Tests for the MindRoom Gemini media toolkit."""

from types import SimpleNamespace
from unittest.mock import Mock

import agno.tools.models.gemini as agno_gemini
from google.genai import types

from mindroom.custom_tools.gemini_media import MindRoomGeminiTools
from mindroom.tool_system.agno_toolkits import ToolCallActor
from tests.agent_tool_helpers import toolkit_offers


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

    result = toolkit.generate_image(ToolCallActor(session_state={}), "A geometric Matrix room")

    assert result.images is not None
    assert len(result.images) == 1
    assert result.images[0].content == b"image-bytes"
    kwargs = generate_content.call_args.kwargs
    assert kwargs["model"] == "gemini-3.1-flash-image"
    assert kwargs["contents"] == "A geometric Matrix room"
    assert kwargs["config"].response_modalities == ["IMAGE"]


def test_generate_image_offers_the_model_its_prompt(monkeypatch) -> None:  # noqa: ANN001
    """The schema asks for the prompt and the docstring describes it, though `Agent` is imported for type checking only."""
    monkeypatch.setattr(agno_gemini, "Client", lambda **_kwargs: SimpleNamespace())
    toolkit = MindRoomGeminiTools(api_key="test-key", enable_generate_video=False)

    function = toolkit_offers(toolkit, "generate_image")

    assert function.description == "Generate an image through the Gemini content-generation API."
    assert function.parameters == {
        "type": "object",
        "properties": {"prompt": {"type": "string"}},
        "required": ["prompt"],
    }
    assert function.injected_params == {"agent": "actor"}
