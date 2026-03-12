"""Tests for the avatar generation utility script."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai import types

from mindroom import avatar_generation as generate_avatars


def test_load_config_uses_config_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # noqa: ANN001
    """The script should read the active MindRoom config path, not scripts/config.yaml."""
    config_path = tmp_path / "custom-config.yaml"
    config_path.write_text("agents:\n  general:\n    role: helper\n", encoding="utf-8")

    monkeypatch.setattr(generate_avatars, "CONFIG_PATH", config_path)

    assert generate_avatars.load_config() == {"agents": {"general": {"role": "helper"}}}


def test_get_avatar_path_uses_workspace_avatars_dir(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # noqa: ANN001
    """Generated avatars should land in the workspace avatars directory."""
    monkeypatch.setattr(generate_avatars, "avatars_dir", lambda: tmp_path / "avatars")

    avatar_path = generate_avatars.get_avatar_path("agents", "general")

    assert avatar_path == tmp_path / "avatars" / "agents" / "general.png"
    assert avatar_path.parent.is_dir()


def test_extract_image_bytes_returns_first_inline_image() -> None:
    """Gemini inline image parts should be converted back to raw bytes."""
    response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model",
                    parts=[types.Part(inline_data=types.Blob(data=b"png-bytes", mime_type="image/png"))],
                ),
            ),
        ],
    )

    assert generate_avatars.extract_image_bytes(response) == b"png-bytes"


@pytest.mark.asyncio
async def test_generate_prompt_uses_gemini_prompt_model() -> None:
    """Prompt generation should call the Gemini text model and compose the base style."""
    generate_content = AsyncMock(return_value=SimpleNamespace(text="teal and copper, visor eyes"))
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))

    prompt = await generate_avatars.generate_prompt(
        client,
        entity_type="agents",
        entity_name="research",
        role="Finds information",
    )

    assert prompt == f"{generate_avatars.CHARACTER_STYLE}, teal and copper, visor eyes"
    kwargs = generate_content.await_args.kwargs
    assert kwargs["model"] == generate_avatars.PROMPT_MODEL
    assert kwargs["contents"] == "Agent name: research\nRole: Finds information\nType: agents"
    assert kwargs["config"].system_instruction == generate_avatars.AGENT_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_generate_avatar_writes_generated_image(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # noqa: ANN001
    """The script should save Gemini-generated image bytes to the expected avatar file."""
    avatar_path = tmp_path / "generated.png"
    image_response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model",
                    parts=[types.Part(inline_data=types.Blob(data=b"avatar-bytes", mime_type="image/png"))],
                ),
            ),
        ],
    )
    generate_content = AsyncMock(return_value=image_response)
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))

    monkeypatch.setattr(generate_avatars, "get_avatar_path", lambda *_args: avatar_path)
    monkeypatch.setattr(generate_avatars, "generate_prompt", AsyncMock(return_value="avatar prompt"))

    await generate_avatars.generate_avatar(
        client,
        entity_type="agents",
        entity_name="general",
        entity_data={"role": "Helpful assistant"},
    )

    assert avatar_path.read_bytes() == b"avatar-bytes"
    kwargs = generate_content.await_args.kwargs
    assert kwargs["model"] == generate_avatars.IMAGE_MODEL
    assert kwargs["contents"] == "avatar prompt"
    assert kwargs["config"].response_modalities == ["IMAGE"]
