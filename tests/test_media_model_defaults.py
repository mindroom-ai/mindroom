"""Runtime media defaults must match the models advertised in tool configuration."""

from pathlib import Path

import pytest

import mindroom.tools  # noqa: F401  # register the built-in factories
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system.metadata import _AUTHORED_OVERRIDE_INHERIT, get_tool_by_name
from mindroom.tool_system.registry_state import TOOL_METADATA


@pytest.mark.parametrize(
    ("tool_name", "model_fields"),
    [
        (
            "openai",
            {
                "transcription_model": "transcription_model",
                "text_to_speech_model": "tts_model",
                "image_model": "image_model",
            },
        ),
        ("gemini", {"image_generation_model": "image_model", "video_generation_model": "video_model"}),
        (
            "groq",
            {
                "transcription_model": "transcription_model",
                "translation_model": "translation_model",
                "tts_model": "tts_model",
                "tts_voice": "tts_voice",
            },
        ),
        ("cartesia", {"model_id": "model_id"}),
        ("eleven_labs", {"model_id": "model_id"}),
        ("fal", {"model": "model"}),
        ("replicate", {"model": "model"}),
    ],
)
def test_media_model_defaults_reach_real_tool_instances(
    tmp_path: Path,
    tool_name: str,
    model_fields: dict[str, str],
) -> None:
    """A bare configured toolkit must use current models instead of older SDK defaults."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    tool = get_tool_by_name(
        tool_name,
        runtime_paths,
        credential_overrides={"api_key": "test-key"},
        disable_sandbox_proxy=True,
        worker_target=None,
    )

    expected = {
        field.name: field.default
        for field in TOOL_METADATA[tool_name].config_fields or ()
        if field.name in model_fields
    }
    assert set(expected) == set(model_fields)
    assert {name: vars(tool)[attribute] for name, attribute in model_fields.items()} == expected


@pytest.mark.parametrize("authored_model", [None, "agent-custom-model"])
def test_media_model_defaults_preserve_configured_override_precedence(
    tmp_path: Path,
    authored_model: str | None,
) -> None:
    """Saved model choices beat defaults, and authored agent overrides beat saved choices."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    tool = get_tool_by_name(
        "fal",
        runtime_paths,
        credential_overrides={"api_key": "test-key", "model": "saved-custom-model"},
        tool_config_overrides={"model": authored_model} if authored_model else None,
        disable_sandbox_proxy=True,
        worker_target=None,
    )

    assert vars(tool)["model"] == (authored_model or "saved-custom-model")


@pytest.mark.parametrize("saved_model", [None, "saved-custom-model"])
def test_media_model_inherit_preserves_saved_or_current_default(
    tmp_path: Path,
    saved_model: str | None,
) -> None:
    """The inherit sentinel retains the selected default instead of reaching the SDK."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    credentials: dict[str, object] = {"api_key": "test-key"}
    if saved_model is not None:
        credentials["model"] = saved_model
    tool = get_tool_by_name(
        "fal",
        runtime_paths,
        credential_overrides=credentials,
        tool_config_overrides={"model": _AUTHORED_OVERRIDE_INHERIT},
        disable_sandbox_proxy=True,
        worker_target=None,
    )

    default_model = next(field.default for field in TOOL_METADATA["fal"].config_fields or () if field.name == "model")
    assert vars(tool)["model"] == (saved_model or default_model)
