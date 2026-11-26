"""Tests for the native Fal media toolkit."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import fal_client
import pytest

import mindroom.tools  # noqa: F401
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.credentials.manager import CredentialsManager
from mindroom.custom_tools.fal import FalTools
from mindroom.model_defaults import FAL_VIDEO
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.tool_function import ToolValue
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
from mindroom.tools.fal import fal_tools
from tests.agent_tool_helpers import toolkit_offers

if TYPE_CHECKING:
    from pathlib import Path


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
    toolkit = FalTools(api_key="configured-key", model="fal-ai/flux-2")

    result = toolkit.generate_media(prompt="A geometric Matrix room")

    assert isinstance(result, ToolValue)
    sync_client.assert_called_once_with(key="configured-key")
    subscribe.assert_called_once_with(
        "fal-ai/flux-2",
        arguments={"prompt": "A geometric Matrix room"},
        with_logs=True,
        on_queue_update=toolkit.on_queue_update,
    )
    assert result.content == expected_content
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
    """The default video model should retain singular and plural video outputs."""
    subscribe = Mock(return_value=response)
    sync_client = Mock(return_value=SimpleNamespace(subscribe=subscribe))
    monkeypatch.setattr(fal_client, "SyncClient", sync_client)
    toolkit = FalTools(api_key="configured-key", model=FAL_VIDEO)

    result = toolkit.generate_media(prompt="A camera circles a forest")

    sync_client.assert_called_once_with(key="configured-key")
    subscribe.assert_called_once_with(
        FAL_VIDEO,
        arguments={"prompt": "A camera circles a forest"},
        with_logs=True,
        on_queue_update=toolkit.on_queue_update,
    )
    assert result.content == expected_content
    assert [video.url for video in result.videos] == expected_urls


def test_image_to_image_registration_exposes_edit_arguments() -> None:
    """Registered schema should expose only model inputs."""
    toolkit = FalTools(
        api_key="configured-key",
        enable_generate_media=False,
        enable_image_to_image=True,
    )

    function = toolkit_offers(toolkit, "image_to_image")

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
    assert toolkit_class is FalTools
    toolkit = toolkit_class(
        api_key="configured-key",
        enable_generate_media=False,
        enable_image_to_image=True,
    )

    result = toolkit.image_to_image(
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
    assert len(result.images) == 1
    assert result.images[0].url == edited_image_url


def test_image_to_image_rejects_missing_image_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing input image should fail before constructing a provider client."""
    sync_client = Mock()
    monkeypatch.setattr(fal_client, "SyncClient", sync_client)
    toolkit = FalTools(api_key="configured-key", enable_generate_media=False)

    result = toolkit.image_to_image(prompt="Add a blue sky")

    sync_client.assert_not_called()
    assert result.content == "Error: image_url is required for image editing."
    assert not result.images


def test_image_to_image_returns_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider failures should remain a readable tool result."""
    subscribe = Mock(side_effect=RuntimeError("provider unavailable"))
    monkeypatch.setattr(
        fal_client,
        "SyncClient",
        Mock(return_value=SimpleNamespace(subscribe=subscribe)),
    )
    toolkit = FalTools(api_key="configured-key", enable_generate_media=False)

    result = toolkit.image_to_image(
        prompt="Add a blue sky",
        image_url="https://cdn.example.test/source.png",
    )

    assert result.content == "Error: provider unavailable"
    assert not result.images


@pytest.mark.parametrize("api_key", [None, "", "   "])
def test_fal_missing_or_blank_api_key_fails_closed_without_sdk_fallback(
    monkeypatch: pytest.MonkeyPatch,
    api_key: str | None,
) -> None:
    """Missing or blank Fal keys must fail closed before constructing SyncClient even when FAL_KEY is set."""
    monkeypatch.setenv("FAL_KEY", "ambient-fal-key")
    sync_client = Mock(side_effect=AssertionError("SyncClient must not be constructed without a resolved key"))
    monkeypatch.setattr(fal_client, "SyncClient", sync_client)
    toolkit = FalTools(api_key=api_key, enable_image_to_image=True)

    media_result = toolkit.generate_media("A sunset")
    edit_result = toolkit.image_to_image("Add clouds", image_url="https://cdn.example.test/source.png")

    sync_client.assert_not_called()
    assert media_result.content == "Error: FAL_API_KEY not set."
    assert edit_result.content == "Error: FAL_API_KEY not set."


def test_fal_env_fallback_priority_and_scoped_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAL_KEY resolves when FAL_API_KEY is unset, FAL_API_KEY wins over FAL_KEY, and scoped-denied fails closed."""
    monkeypatch.setenv("FAL_KEY", "ambient-fal-key")
    sync_client = Mock(side_effect=AssertionError("SyncClient must not be constructed for denied scoped agents"))
    monkeypatch.setattr(fal_client, "SyncClient", sync_client)

    (tmp_path / "config.yaml").write_text("agents: {}\n")
    tools = builtin_tool_registry()
    credentials = CredentialsManager(tmp_path / "credentials")

    fal_key_only_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_NO_AUTO_INSTALL_TOOLS": "1", "FAL_KEY": "fal-standard-key"},
    )
    unscoped_fal_key = get_tool_by_name(
        "fal",
        fal_key_only_paths,
        tools=tools,
        credentials_manager=credentials,
        runs_in_worker=False,
        worker_target=None,
    )
    assert isinstance(unscoped_fal_key, FalTools)
    assert unscoped_fal_key.api_key == "fal-standard-key"

    both_keys_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={
            "MINDROOM_NO_AUTO_INSTALL_TOOLS": "1",
            "FAL_API_KEY": "fal-api-key",
            "FAL_KEY": "fal-standard-key",
        },
    )
    unscoped_both = get_tool_by_name(
        "fal",
        both_keys_paths,
        tools=tools,
        credentials_manager=credentials,
        runs_in_worker=False,
        worker_target=None,
    )
    assert isinstance(unscoped_both, FalTools)
    assert unscoped_both.api_key == "fal-api-key"

    credentials.save_credentials("fal", {"api_key": "shared-stored-key"})
    scoped_runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_NO_AUTO_INSTALL_TOOLS": "1"},
    )
    scoped_target = resolve_worker_target(
        "user",
        "alpha",
        ToolExecutionIdentity("matrix", "alpha", "@alice:example.test", None, None, None, None, tenant_id="t"),
    )
    scoped_denied = get_tool_by_name(
        "fal",
        scoped_runtime_paths,
        tools=tools,
        credentials_manager=credentials,
        runs_in_worker=False,
        worker_target=scoped_target,
        allowed_shared_services=frozenset(),
    )
    assert isinstance(scoped_denied, FalTools)
    assert scoped_denied.api_key is None
    denied_result = scoped_denied.generate_media("A sunset")
    assert denied_result.content == "Error: FAL_API_KEY not set."
    sync_client.assert_not_called()

    scoped_granted = get_tool_by_name(
        "fal",
        scoped_runtime_paths,
        tools=tools,
        credentials_manager=credentials,
        runs_in_worker=False,
        worker_target=scoped_target,
        allowed_shared_services=frozenset({"fal"}),
    )
    assert isinstance(scoped_granted, FalTools)
    assert scoped_granted.api_key == "shared-stored-key"
