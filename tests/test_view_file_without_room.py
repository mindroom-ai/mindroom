"""Turns without a Matrix room, headless and through the OpenAI-compatible API, offer view_file of `attachments`."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig, RouterConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.headless import run_turn
from mindroom.memory import MemoryPromptParts
from mindroom.tool_system.worker_routing import agent_workspace_root_path
from tests.identity_helpers import persist_entity_accounts
from tests.minimal_agent_fixtures import ScriptedProvider, offered_tools
from tests.test_media_delivery import image_bytes
from tests.test_openai_compat import _openai_client

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.llm.provider import ModelRequest

_ROOM_FUNCTIONS = {"get_attachment", "list_attachments", "register_attachment"}


def _headless_config(*tools: object) -> dict[str, Any]:
    return {
        "administrators": ["@alice:localhost"],
        "models": {"default": {"provider": "openai", "id": "scripted"}},
        "memory": "none",
        "agents": {"coder": {"display_name": "Coder", "tools": list(tools)}},
    }


def _tool_result(request: ModelRequest) -> str:
    return str([message for message in request.messages if message.role == "tool"][-1].content)


def _images(request: ModelRequest) -> list[tuple[str | None, bytes | None]]:
    return [(image.mime_type, image.content) for message in request.messages for image in message.images or ()]


@pytest.mark.asyncio
async def test_a_headless_turn_views_an_image_in_its_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """view_file reads the workdir the file tools work in, and the next request carries the image."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "board.png").write_bytes(image_bytes())
    provider = ScriptedProvider(steps=[[("view_file", {"path": "board.png"})], "done"])
    provider.install(monkeypatch)

    await run_turn(
        _headless_config("file", "attachments"),
        agent="coder",
        prompt="look",
        storage=tmp_path / "storage",
        workdir=workdir,
    )

    offered = set(offered_tools(provider.requests[0]))
    assert "view_file" in offered
    assert not offered & _ROOM_FUNCTIONS
    system = [str(message.content) for message in provider.requests[0].messages if message.role == "system"]
    assert any("`view_file`: the workdir; other path tools: agent workspace" in text for text in system)
    assert json.loads(_tool_result(provider.requests[1]))["view_status"] == "ready"
    assert _images(provider.requests[1]) == [("image/png", image_bytes())]


def test_an_openai_compatible_turn_views_an_image_in_the_agent_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completion's agent gets view_file without its room-only siblings, and sees the image it views."""
    config = Config(
        agents={"general": AgentConfig(display_name="General", tools=["file", "attachments"], rooms=[])},
        models={"default": ModelConfig(provider="ollama", id="test-model")},
        router=RouterConfig(model="default"),
        defaults=DefaultsConfig(tools=[]),
    )
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true", "MINDROOM_EXECUTION_MODE": "off"},
    )
    persist_entity_accounts(config, runtime_paths)
    workspace = agent_workspace_root_path(runtime_paths.storage_root, "general")
    workspace.mkdir(parents=True)
    (workspace / "board.png").write_bytes(image_bytes())
    provider = ScriptedProvider(steps=[[("view_file", {"path": "board.png"})], "done"])
    provider.install(monkeypatch)

    with (
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        _openai_client(config, runtime_paths) as client,
    ):
        response = client.post(
            "/v1/chat/completions",
            json={"model": "general", "messages": [{"role": "user", "content": "look"}]},
        )

    assert response.status_code == 200, response.text
    offered = set(offered_tools(provider.requests[0]))
    assert "view_file" in offered
    assert not offered & _ROOM_FUNCTIONS
    assert _images(provider.requests[1]) == [("image/png", image_bytes())]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tools", "file_name", "content", "message"),
    [
        (
            ["file", "attachments"],
            "board.png",
            image_bytes(),
            "board.png is an image (image/png), not text. View it with view_file.",
        ),
        (
            ["file", "attachments"],
            "logo.svg",
            "<svg>\N{LATIN SMALL LETTER E WITH ACUTE}</svg>".encode("latin-1"),
            "'utf-8' codec can't decode byte 0xe9 in position 5: invalid continuation byte",
        ),
        (["file"], "board.png", image_bytes(), "board.png is an image (image/png), not text."),
        (
            ["file", {"attachments": {"exclude_tools": ["view_file"]}}],
            "board.png",
            image_bytes(),
            "board.png is an image (image/png), not text.",
        ),
    ],
    ids=["offered", "not-viewable", "no-attachments", "view-file-excluded"],
)
async def test_read_file_on_an_image_says_so_and_names_view_file_when_offered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: list[object],
    file_name: str,
    content: bytes,
    message: str,
) -> None:
    """read_file reports an image view_file shows as one, naming view_file when the turn offers it, else the decode error."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / file_name).write_bytes(content)
    provider = ScriptedProvider(steps=[[("read_file", {"file_name": file_name})], "done"])
    provider.install(monkeypatch)

    await run_turn(
        _headless_config(*tools),
        agent="coder",
        prompt="look",
        storage=tmp_path / "storage",
        workdir=workdir,
    )

    assert _tool_result(provider.requests[1]) == f"Error reading file: {message}"


@pytest.mark.asyncio
async def test_read_file_names_view_file_only_for_a_relative_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared directories and saved outputs, which view_file cannot reach, go by absolute path and get no hint."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "board.png").write_bytes(image_bytes())
    file_name = str(workdir / "board.png")
    provider = ScriptedProvider(steps=[[("read_file", {"file_name": file_name})], "done"])
    provider.install(monkeypatch)

    await run_turn(
        _headless_config("file", "attachments"),
        agent="coder",
        prompt="look",
        storage=tmp_path / "storage",
        workdir=workdir,
    )

    assert _tool_result(provider.requests[1]) == f"Error reading file: {file_name} is an image (image/png), not text."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry", "saved", "offered"),
    [
        ({"attachments": {"include_tools": ["list_attachments", "view_file"]}}, {}, {"view_file"}),
        ({"attachments": {"include_tools": ["list_attachments"]}}, {}, set()),
        ({"attachments": {"exclude_tools": ["view_file"]}}, {}, set()),
        ("attachments", {"include_tools": ["list_attachments"]}, set()),
        ("attachments", {"exclude_tools": ["view_file"]}, set()),
        ("matrix_message", {}, {"view_file"}),
    ],
    ids=["included", "not-included", "excluded", "saved-not-included", "saved-excluded", "implied"],
)
async def test_a_turn_without_a_room_offers_view_file_within_the_toolkit_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: object,
    saved: dict[str, object],
    offered: set[str],
) -> None:
    """Of `attachments`, a turn without a room offers view_file alone, and only where its authored or saved filters keep it."""
    storage = tmp_path / "storage"
    if saved:
        runtime_paths = resolve_runtime_paths(config_path=storage / "config.yaml", storage_path=storage)
        get_runtime_credentials_manager(runtime_paths).save_credentials("attachments", saved)
    provider = ScriptedProvider(steps=["done"])
    provider.install(monkeypatch)

    await run_turn(_headless_config(entry), agent="coder", prompt="look", storage=storage)

    assert set(offered_tools(provider.requests[0])) & {"view_file", *_ROOM_FUNCTIONS} == offered
