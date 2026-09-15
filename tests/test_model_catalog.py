"""Public model metadata and bounded Matrix raster publication."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest
from PIL import Image

from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.model_catalog import ModelCatalog
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_catalog_allowlist_and_live_metadata_revision(tmp_path: Path) -> None:
    """Credentials and provider endpoints never enter discovery or revision data."""
    catalog = ModelCatalog(client=AsyncMock(spec=nio.AsyncClient), runtime_paths=test_runtime_paths(tmp_path))
    config = Config(
        models={
            "fast": ModelConfig(
                provider="openai",
                id="test-model",
                display_name="Quick helper",
                api_key="secret",
                host="https://private.invalid",
                extra_kwargs={"secret": "hidden"},
            ),
        },
    )
    entries, revision = await catalog.snapshot(config)
    assert entries == [{"key": "fast", "display_name": "Quick helper", "provider": "openai", "id": "test-model"}]
    assert len(revision) == 64
    config.models["fast"].api_key = "changed"
    assert (await catalog.snapshot(config))[1] == revision
    config.models["fast"].display_name = None
    renamed, next_revision = await catalog.snapshot(config)
    assert renamed[0]["display_name"] == "fast"
    assert next_revision != revision
    assert "secret" not in json.dumps(entries)


@pytest.mark.asyncio
async def test_icons_are_content_cached_and_matrix_only(tmp_path: Path) -> None:
    """Concurrent matching bytes upload once; new bytes publish a new Matrix URI."""
    paths = test_runtime_paths(tmp_path)
    paths.config_path.parent.mkdir(parents=True, exist_ok=True)
    path = paths.config_path.parent / "icon.png"
    Image.new("RGB", (2, 2), "red").save(path)
    client = AsyncMock(spec=nio.AsyncClient)
    client.upload.side_effect = [
        (nio.UploadResponse("mxc://example.org/first"), None),
        (nio.UploadResponse("mxc://example.org/next"), None),
    ]
    catalog = ModelCatalog(client=client, runtime_paths=paths)
    config = Config(models={"fast": ModelConfig(provider="openai", id="test-model", icon="icon.png")})
    first, same = await asyncio.gather(catalog.snapshot(config), catalog.snapshot(config))
    assert first == same
    assert first[0][0]["icon_url"] == "mxc://example.org/first"
    assert client.upload.await_count == 1
    Image.new("RGB", (2, 2), "blue").save(path)
    changed, revision = await catalog.snapshot(config)
    assert changed[0]["icon_url"] == "mxc://example.org/next"
    assert revision != first[1]


@pytest.mark.parametrize("kind", ["missing", "malformed", "svg", "oversize", "directory", "external", "bad_mxc"])
@pytest.mark.asyncio
async def test_bad_icons_fall_back_without_upload(tmp_path: Path, kind: str) -> None:
    """Unusable files and external URLs cannot leak through the public catalog."""
    paths = test_runtime_paths(tmp_path)
    paths.config_path.parent.mkdir(parents=True, exist_ok=True)
    path = paths.config_path.parent / "icon.png"
    if kind == "directory":
        path.mkdir()
    elif kind != "missing":
        path.write_bytes(b"x" * (1024 * 1024 + 1) if kind == "oversize" else b"<svg/>")
    model = ModelConfig(provider="openai", id="test-model", icon="icon.png")
    if kind == "external":
        model.icon = "https://example.org/logo.png"
    elif kind == "bad_mxc":
        model.icon = "mxc://bad host/image"
    client = AsyncMock(spec=nio.AsyncClient)
    entries, _ = await ModelCatalog(client=client, runtime_paths=paths).snapshot(Config(models={"fast": model}))
    assert "icon_url" not in entries[0]
    client.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_mxc_passthrough_and_stable_order(tmp_path: Path) -> None:
    """Authored Matrix media needs no upload and ordering is deterministic."""
    catalog = ModelCatalog(client=AsyncMock(spec=nio.AsyncClient), runtime_paths=test_runtime_paths(tmp_path))
    model = ModelConfig(provider="openai", id="test-model", icon="mxc://example.org/image")
    entries, revision = await catalog.snapshot(Config(models={"z": model, "a": model}))
    assert [entry["key"] for entry in entries] == ["a", "z"]
    assert entries[0]["icon_url"] == "mxc://example.org/image"
    assert (await catalog.snapshot(Config(models={"a": model, "z": model})))[1] == revision


@pytest.mark.asyncio
async def test_icon_upload_uses_real_nio_data_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The actual nio upload adapter must consume the exact validated image bytes."""
    paths = test_runtime_paths(tmp_path)
    paths.config_path.parent.mkdir(parents=True, exist_ok=True)
    path = paths.config_path.parent / "icon.png"
    Image.new("RGB", (2, 2)).save(path)
    client = nio.AsyncClient("https://example.org", "@router:example.org")
    client.access_token = "test-token"  # noqa: S105 - Test-only Matrix login.
    uploaded = []

    async def network(*_args: object, **kwargs: object) -> nio.UploadResponse:
        provider = kwargs["data_provider"]
        stream = await provider(0, 0)
        uploaded.append(b"".join([chunk async for chunk in stream]))
        return nio.UploadResponse("mxc://example.org/real-provider")

    monkeypatch.setattr(client, "_send", network)
    config = Config(models={"fast": ModelConfig(provider="openai", id="test-model", icon="icon.png")})
    entries, _ = await ModelCatalog(client=client, runtime_paths=paths).snapshot(config)
    assert uploaded == [path.read_bytes()]
    assert entries[0]["icon_url"] == "mxc://example.org/real-provider"


@pytest.mark.asyncio
async def test_bad_png_checksum_falls_back(tmp_path: Path) -> None:
    """Recognizable PNG headers do not let corrupt raster data break discovery."""
    paths = test_runtime_paths(tmp_path)
    paths.config_path.parent.mkdir(parents=True, exist_ok=True)
    path = paths.config_path.parent / "icon.png"
    Image.new("RGB", (2, 2)).save(path)
    data = bytearray(path.read_bytes())
    data[-13] ^= 255
    path.write_bytes(data)
    config = Config(models={"fast": ModelConfig(provider="openai", id="test-model", icon="icon.png")})
    entries, _ = await ModelCatalog(client=AsyncMock(spec=nio.AsyncClient), runtime_paths=paths).snapshot(config)
    assert "icon_url" not in entries[0]


@pytest.mark.parametrize(
    ("kind", "published"),
    [
        ("parent", False),
        ("outside_symlink", False),
        ("normalized", True),
        ("inside_symlink", True),
        ("symlink_loop", False),
    ],
)
@pytest.mark.asyncio
async def test_local_icon_resolved_target_stays_inside_config_directory(
    tmp_path: Path,
    kind: str,
    *,
    published: bool,
) -> None:
    """Authored escapes fall back, while normalized paths and contained symlinks publish."""
    paths = test_runtime_paths(tmp_path / "config")
    directory = paths.config_path.parent
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "icons").mkdir()
    Image.new("RGB", (2, 2)).save(directory / "logo.png")
    Image.new("RGB", (2, 2)).save(directory.parent / "outside.png")
    icon = "icons/../logo.png"
    if kind == "parent":
        icon = "../outside.png"
    elif kind in {"outside_symlink", "inside_symlink", "symlink_loop"}:
        icon = "link.png"
        target = {"outside_symlink": "../outside.png", "inside_symlink": "logo.png", "symlink_loop": "link.png"}[kind]
        (directory / icon).symlink_to(target)
    client = AsyncMock(spec=nio.AsyncClient)
    client.upload.return_value = (nio.UploadResponse("mxc://example.org/contained"), None)
    config = Config(models={"fast": ModelConfig(provider="openai", id="test-model", icon=icon)})
    entries, _ = await ModelCatalog(client=client, runtime_paths=paths).snapshot(config)
    assert (entries[0].get("icon_url") == "mxc://example.org/contained") is published
    assert client.upload.await_count == int(published)
