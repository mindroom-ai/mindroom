"""Tests for the MindRoom Fal media adapter."""

from types import SimpleNamespace
from unittest.mock import Mock

import fal_client
import pytest
from agno.agent import Agent

from mindroom.custom_tools.fal import MindRoomFalTools
from mindroom.model_defaults import FAL_VIDEO
from mindroom.tools.fal import fal_tools


@pytest.mark.parametrize(
    ("response", "expected_urls", "expected_content"),
    [
        pytest.param(
            {
                "images": [
                    {
                        "url": "https://cdn.example.test/first.webp",
                        "content_type": "image/webp",
                        "file_name": "first.webp",
                        "file_size": 123,
                        "width": 1024,
                        "height": 1024,
                    },
                    {
                        "url": "https://cdn.example.test/second.webp",
                        "content_type": "image/webp",
                        "file_name": "second.webp",
                        "file_size": 456,
                        "width": 1024,
                        "height": 1024,
                    },
                ],
            },
            [
                "https://cdn.example.test/first.webp",
                "https://cdn.example.test/second.webp",
            ],
            (
                "Generated 2 image(s) successfully: "
                "https://cdn.example.test/first.webp, https://cdn.example.test/second.webp"
            ),
            id="plural",
        ),
        pytest.param(
            {
                "image": {
                    "url": "https://cdn.example.test/image.webp",
                    "content_type": "image/webp",
                    "file_name": "image.webp",
                    "file_size": 123,
                    "width": 1024,
                    "height": 1024,
                },
            },
            ["https://cdn.example.test/image.webp"],
            "Image generated successfully at https://cdn.example.test/image.webp",
            id="singular",
        ),
    ],
)
def test_generate_media_uses_configured_key_and_returns_images(
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, object],
    expected_urls: list[str],
    expected_content: str,
) -> None:
    """Configurable image models should retain singular and plural outputs."""
    subscribe = Mock(return_value=response)
    sync_client = Mock(return_value=SimpleNamespace(subscribe=subscribe))
    monkeypatch.setattr(fal_client, "SyncClient", sync_client)
    toolkit = MindRoomFalTools(api_key="configured-key", model="fal-ai/flux-2")

    result = toolkit.generate_media(Mock(spec=Agent), prompt="A geometric Matrix room")

    sync_client.assert_called_once_with(key="configured-key")
    subscribe.assert_called_once_with(
        "fal-ai/flux-2",
        arguments={"prompt": "A geometric Matrix room"},
        with_logs=True,
        on_queue_update=toolkit.on_queue_update,
    )
    assert result.content == expected_content
    assert result.images is not None
    assert [image.url for image in result.images] == expected_urls


@pytest.mark.parametrize(
    ("response", "expected_urls", "expected_content"),
    [
        pytest.param(
            {
                "videos": [
                    {
                        "url": "https://cdn.example.test/first.mp4",
                        "content_type": "video/mp4",
                        "file_name": "first.mp4",
                        "file_size": 123,
                    },
                    {
                        "url": "https://cdn.example.test/second.mp4",
                        "content_type": "video/mp4",
                        "file_name": "second.mp4",
                        "file_size": 456,
                    },
                ],
            },
            [
                "https://cdn.example.test/first.mp4",
                "https://cdn.example.test/second.mp4",
            ],
            (
                "Generated 2 video(s) successfully: "
                "https://cdn.example.test/first.mp4, https://cdn.example.test/second.mp4"
            ),
            id="plural",
        ),
        pytest.param(
            {
                "video": {
                    "url": "https://cdn.example.test/video.mp4",
                    "content_type": "video/mp4",
                    "file_name": "video.mp4",
                    "file_size": 123,
                },
            },
            ["https://cdn.example.test/video.mp4"],
            "Video generated successfully at https://cdn.example.test/video.mp4",
            id="singular",
        ),
    ],
)
def test_generate_media_uses_default_model_and_returns_videos(
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, object],
    expected_urls: list[str],
    expected_content: str,
) -> None:
    """The default Hunyuan model should retain singular and plural video outputs."""
    subscribe = Mock(return_value=response)
    sync_client = Mock(return_value=SimpleNamespace(subscribe=subscribe))
    monkeypatch.setattr(fal_client, "SyncClient", sync_client)
    toolkit = MindRoomFalTools(api_key="configured-key", model=FAL_VIDEO)

    result = toolkit.generate_media(Mock(spec=Agent), prompt="A camera circles a forest")

    sync_client.assert_called_once_with(key="configured-key")
    subscribe.assert_called_once_with(
        FAL_VIDEO,
        arguments={"prompt": "A camera circles a forest"},
        with_logs=True,
        on_queue_update=toolkit.on_queue_update,
    )
    assert result.content == expected_content
    assert result.videos is not None
    assert [video.url for video in result.videos] == expected_urls


def test_image_to_image_registration_exposes_edit_arguments() -> None:
    """Registered schema should expose model inputs and hide Agno's injected agent."""
    toolkit = MindRoomFalTools(
        api_key="configured-key",
        enable_generate_media=False,
        enable_image_to_image=True,
    )

    function = toolkit.functions["image_to_image"]
    function.process_entrypoint()

    assert set(function.parameters["properties"]) == {"prompt", "image_url"}
    assert function.parameters["required"] == ["prompt"]


def test_image_to_image_uses_flux_2_edit_and_returns_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Image edits should honor credentials and Flux 2's request shape."""
    edited_image_url = "https://cdn.example.test/edited.webp"
    subscribe = Mock(
        return_value={
            "images": [
                {
                    "url": edited_image_url,
                    "content_type": "image/webp",
                    "file_name": "edited.webp",
                    "file_size": 123,
                    "width": 1024,
                    "height": 1024,
                },
            ],
            "seed": 42,
            "has_nsfw_concepts": [False],
            "prompt": "Add a blue sky",
        },
    )
    sync_client = Mock(return_value=SimpleNamespace(subscribe=subscribe))
    monkeypatch.setattr(fal_client, "SyncClient", sync_client)

    toolkit_class = fal_tools()
    assert toolkit_class is MindRoomFalTools
    toolkit = toolkit_class(
        api_key="configured-key",
        enable_generate_media=False,
        enable_image_to_image=True,
    )

    result = toolkit.image_to_image(
        Mock(spec=Agent),
        prompt="Add a blue sky",
        image_url="https://cdn.example.test/source.png",
    )

    sync_client.assert_called_once_with(key="configured-key")
    subscribe.assert_called_once_with(
        "fal-ai/flux-2/edit",
        arguments={
            "image_urls": ["https://cdn.example.test/source.png"],
            "prompt": "Add a blue sky",
        },
        with_logs=True,
        on_queue_update=toolkit.on_queue_update,
    )
    assert result.content == f"Image generated successfully at {edited_image_url}"
    assert result.images is not None
    assert len(result.images) == 1
    assert result.images[0].url == edited_image_url


def test_image_to_image_rejects_missing_image_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing input image should fail before constructing a provider client."""
    sync_client = Mock()
    monkeypatch.setattr(fal_client, "SyncClient", sync_client)
    toolkit = MindRoomFalTools(api_key="configured-key", enable_generate_media=False)

    result = toolkit.image_to_image(Mock(spec=Agent), prompt="Add a blue sky")

    sync_client.assert_not_called()
    assert result.content == "Error: image_url is required for image editing."
    assert result.images is None


def test_image_to_image_returns_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider failures should remain a readable tool result."""
    subscribe = Mock(side_effect=RuntimeError("provider unavailable"))
    monkeypatch.setattr(
        fal_client,
        "SyncClient",
        Mock(return_value=SimpleNamespace(subscribe=subscribe)),
    )
    toolkit = MindRoomFalTools(api_key="configured-key", enable_generate_media=False)

    result = toolkit.image_to_image(
        Mock(spec=Agent),
        prompt="Add a blue sky",
        image_url="https://cdn.example.test/source.png",
    )

    assert result.content == "Error: provider unavailable"
    assert result.images is None
