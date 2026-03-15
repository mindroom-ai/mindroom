"""Tests for the avatar generation module."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from google.genai import types

import mindroom.constants as constants_mod
from mindroom import avatar_generation as generate_avatars

if TYPE_CHECKING:
    from pathlib import Path


def _workspace_avatar_path(
    tmp_path: Path,
    entity_type: str,
    entity_name: str,
    runtime_paths: constants_mod.RuntimePaths,
) -> Path:
    del runtime_paths
    return tmp_path / "avatars" / entity_type / f"{entity_name}.png"


def _runtime_paths(tmp_path: Path, *, config_path: Path | None = None) -> constants_mod.RuntimePaths:
    """Build explicit runtime paths for avatar-generation tests."""
    return constants_mod.resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
    )


def _config_with_runtime_paths(
    raw_config: dict[str, object],
    tmp_path: Path,
) -> generate_avatars.Config:
    runtime_paths = _runtime_paths(tmp_path)
    config = generate_avatars.Config.model_validate(raw_config, context={"runtime_paths": runtime_paths})
    config._runtime_paths = runtime_paths
    return config


@pytest.fixture
def workspace_avatar_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Patch avatar path helpers to use a temporary workspace."""
    avatars_path = tmp_path / "avatars"
    monkeypatch.setattr(
        generate_avatars,
        "workspace_avatar_path",
        lambda entity_type, entity_name, runtime_paths: _workspace_avatar_path(
            tmp_path,
            entity_type,
            entity_name,
            runtime_paths,
        ),
    )
    monkeypatch.setattr(
        generate_avatars,
        "resolve_avatar_path",
        lambda entity_type, entity_name, runtime_paths: _workspace_avatar_path(
            tmp_path,
            entity_type,
            entity_name,
            runtime_paths,
        ),
    )
    return avatars_path


def test_get_avatar_path_uses_workspace_avatars_dir(workspace_avatar_dir: Path) -> None:
    """Generated avatars should land in the workspace avatars directory."""
    avatar_path = generate_avatars.get_avatar_path("agents", "general", _runtime_paths(workspace_avatar_dir.parent))

    assert avatar_path == workspace_avatar_dir / "agents" / "general.png"
    assert avatar_path.parent.is_dir()


def test_get_console_returns_shared_console_instance() -> None:
    """Avatar generation should reuse one Rich console instance across prints and progress."""
    assert generate_avatars.get_console() is generate_avatars.get_console()


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


def test_has_missing_managed_avatars_detects_complete_avatar_set(
    workspace_avatar_dir: Path,
) -> None:
    """Existing managed workspace avatars should not be reported as missing."""
    raw_config = {
        "models": {"default": {"provider": "anthropic", "id": "claude-sonnet-4-6"}},
        "router": {"model": "default"},
        "agents": {
            "general": {
                "display_name": "General",
                "model": "default",
            },
        },
        "matrix_space": {"enabled": False},
    }
    config = _config_with_runtime_paths(raw_config, workspace_avatar_dir.parent)
    runtime_paths = _runtime_paths(workspace_avatar_dir.parent)
    for entity_type, entity_name in (("agents", "general"), ("agents", "router")):
        avatar_path = workspace_avatar_dir / entity_type / f"{entity_name}.png"
        avatar_path.parent.mkdir(parents=True, exist_ok=True)
        avatar_path.write_bytes(b"avatar")

    assert not generate_avatars.has_missing_managed_avatars(config, runtime_paths)


def test_has_missing_managed_avatars_ignores_direct_room_ids(
    workspace_avatar_dir: Path,
) -> None:
    """External room IDs should not be treated as managed avatar targets."""
    raw_config = {
        "models": {"default": {"provider": "anthropic", "id": "claude-sonnet-4-6"}},
        "router": {"model": "default"},
        "agents": {
            "general": {
                "display_name": "General",
                "model": "default",
                "rooms": ["!external:localhost"],
            },
        },
        "matrix_space": {"enabled": False},
    }
    config = _config_with_runtime_paths(raw_config, workspace_avatar_dir.parent)
    runtime_paths = _runtime_paths(workspace_avatar_dir.parent)
    for entity_type, entity_name in (("agents", "general"), ("agents", "router")):
        avatar_path = workspace_avatar_dir / entity_type / f"{entity_name}.png"
        avatar_path.parent.mkdir(parents=True, exist_ok=True)
        avatar_path.write_bytes(b"avatar")

    assert not generate_avatars.has_missing_managed_avatars(config, runtime_paths)


def test_has_missing_managed_avatars_ignores_full_room_aliases(
    workspace_avatar_dir: Path,
) -> None:
    """External room aliases should not be treated as managed avatar targets."""
    raw_config = {
        "models": {"default": {"provider": "anthropic", "id": "claude-sonnet-4-6"}},
        "router": {"model": "default"},
        "agents": {
            "general": {
                "display_name": "General",
                "model": "default",
                "rooms": ["#external:localhost"],
            },
        },
        "matrix_space": {"enabled": False},
    }
    config = _config_with_runtime_paths(raw_config, workspace_avatar_dir.parent)
    runtime_paths = _runtime_paths(workspace_avatar_dir.parent)
    for entity_type, entity_name in (("agents", "general"), ("agents", "router")):
        avatar_path = workspace_avatar_dir / entity_type / f"{entity_name}.png"
        avatar_path.parent.mkdir(parents=True, exist_ok=True)
        avatar_path.write_bytes(b"avatar")

    assert not generate_avatars.has_missing_managed_avatars(config, runtime_paths)


def test_has_missing_managed_avatars_treats_bundled_avatars_as_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Bundled runtime avatars should count as present for generation checks."""
    raw_config = {
        "models": {"default": {"provider": "anthropic", "id": "claude-sonnet-4-6"}},
        "router": {"model": "default"},
        "agents": {
            "general": {
                "display_name": "General",
                "model": "default",
                "rooms": ["lobby"],
            },
        },
        "matrix_space": {"enabled": False},
    }
    config = _config_with_runtime_paths(raw_config, tmp_path)
    runtime_paths = _runtime_paths(tmp_path)
    bundled_root = tmp_path / "bundled"

    def _resolve_avatar_path(
        entity_type: str,
        entity_name: str,
        runtime_paths: constants_mod.RuntimePaths,
    ) -> Path:
        del runtime_paths
        return bundled_root / entity_type / f"{entity_name}.png"

    monkeypatch.setattr(generate_avatars, "resolve_avatar_path", _resolve_avatar_path)

    for entity_type, entity_name in (("agents", "general"), ("agents", "router"), ("rooms", "lobby")):
        avatar_path = bundled_root / entity_type / f"{entity_name}.png"
        avatar_path.parent.mkdir(parents=True, exist_ok=True)
        avatar_path.write_bytes(b"avatar")

    assert not generate_avatars.has_missing_managed_avatars(config, runtime_paths)


@pytest.mark.asyncio
async def test_run_avatar_generation_skips_google_key_when_all_managed_avatars_exist(
    monkeypatch: pytest.MonkeyPatch,
    workspace_avatar_dir: Path,
) -> None:
    """Existing managed avatars should skip generation even without Google credentials."""
    raw_config = {
        "models": {"default": {"provider": "anthropic", "id": "claude-sonnet-4-6"}},
        "router": {"model": "default"},
        "agents": {
            "general": {
                "display_name": "General",
                "model": "default",
            },
        },
        "matrix_space": {"enabled": False},
    }
    for entity_type, entity_name in (("agents", "general"), ("agents", "router")):
        avatar_path = workspace_avatar_dir / entity_type / f"{entity_name}.png"
        avatar_path.parent.mkdir(parents=True, exist_ok=True)
        avatar_path.write_bytes(b"avatar")

    monkeypatch.setattr(
        generate_avatars,
        "load_validated_config",
        lambda *_args, **_kwargs: _config_with_runtime_paths(raw_config, workspace_avatar_dir.parent),
    )
    monkeypatch.setattr(generate_avatars.genai, "Client", lambda **_kwargs: pytest.fail("generation should be skipped"))
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY_FILE", raising=False)

    await generate_avatars.run_avatar_generation(_runtime_paths(workspace_avatar_dir.parent))


@pytest.mark.asyncio
async def test_run_avatar_generation_skips_google_key_when_all_managed_avatars_are_bundled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Bundled runtime avatars should skip generation without workspace overrides."""
    raw_config = {
        "models": {"default": {"provider": "anthropic", "id": "claude-sonnet-4-6"}},
        "router": {"model": "default"},
        "agents": {
            "general": {
                "display_name": "General",
                "model": "default",
                "rooms": ["lobby"],
            },
        },
        "matrix_space": {"enabled": False},
    }
    bundled_root = tmp_path / "bundled"
    workspace_root = tmp_path / "workspace"

    def _workspace_path(
        entity_type: str,
        entity_name: str,
        runtime_paths: constants_mod.RuntimePaths,
    ) -> Path:
        del runtime_paths
        return workspace_root / entity_type / f"{entity_name}.png"

    def _resolve_avatar_path(
        entity_type: str,
        entity_name: str,
        runtime_paths: constants_mod.RuntimePaths,
    ) -> Path:
        del runtime_paths
        return bundled_root / entity_type / f"{entity_name}.png"

    monkeypatch.setattr(
        generate_avatars,
        "load_validated_config",
        lambda *_args, **_kwargs: _config_with_runtime_paths(raw_config, tmp_path),
    )
    monkeypatch.setattr(generate_avatars, "workspace_avatar_path", _workspace_path)
    monkeypatch.setattr(generate_avatars, "resolve_avatar_path", _resolve_avatar_path)
    monkeypatch.setattr(generate_avatars.genai, "Client", lambda **_kwargs: pytest.fail("generation should be skipped"))
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY_FILE", raising=False)

    for entity_type, entity_name in (("agents", "general"), ("agents", "router"), ("rooms", "lobby")):
        avatar_path = bundled_root / entity_type / f"{entity_name}.png"
        avatar_path.parent.mkdir(parents=True, exist_ok=True)
        avatar_path.write_bytes(b"avatar")

    await generate_avatars.run_avatar_generation(_runtime_paths(tmp_path))

    assert not (workspace_root / "agents" / "general.png").exists()
    assert not (workspace_root / "rooms" / "lobby.png").exists()


@pytest.mark.asyncio
async def test_run_avatar_generation_raises_when_missing_avatars_still_fail_generation(
    monkeypatch: pytest.MonkeyPatch,
    workspace_avatar_dir: Path,
) -> None:
    """Startup avatar generation should fail when required assets remain missing."""
    raw_config = {
        "models": {"default": {"provider": "anthropic", "id": "claude-sonnet-4-6"}},
        "router": {"model": "default"},
        "agents": {
            "general": {
                "display_name": "General",
                "model": "default",
            },
        },
        "matrix_space": {"enabled": False},
    }
    router_avatar = workspace_avatar_dir / "agents" / "router.png"
    router_avatar.parent.mkdir(parents=True, exist_ok=True)
    router_avatar.write_bytes(b"avatar")

    monkeypatch.setattr(
        generate_avatars,
        "load_validated_config",
        lambda *_args, **_kwargs: _config_with_runtime_paths(raw_config, workspace_avatar_dir.parent),
    )
    monkeypatch.setattr(
        generate_avatars.genai,
        "Client",
        lambda **_kwargs: SimpleNamespace(aio=SimpleNamespace(aclose=AsyncMock())),
    )
    monkeypatch.setattr(generate_avatars, "generate_prompt", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-key")

    with pytest.raises(generate_avatars.AvatarGenerationError, match="Avatar generation failed"):
        await generate_avatars.run_avatar_generation(_runtime_paths(workspace_avatar_dir.parent))

    assert not (workspace_avatar_dir / "agents" / "general.png").exists()


@pytest.mark.asyncio
async def test_run_avatar_generation_accepts_null_optional_sections(
    tmp_path: Path,
    workspace_avatar_dir: Path,
) -> None:
    """Avatar generation should accept legacy configs normalized by Config.from_yaml()."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: anthropic\n    id: claude-sonnet-4-6\n"
        "agents:\n  a:\n    display_name: A\n    model: default\n"
        "router:\n  model: default\n"
        "teams: null\n"
        "matrix_space: null\n",
    )
    for entity_type, entity_name in (
        ("agents", "a"),
        ("agents", "router"),
        ("spaces", generate_avatars.ROOT_SPACE_AVATAR_NAME),
    ):
        avatar_path = workspace_avatar_dir / entity_type / f"{entity_name}.png"
        avatar_path.parent.mkdir(parents=True, exist_ok=True)
        avatar_path.write_bytes(b"avatar")
    await generate_avatars.run_avatar_generation(_runtime_paths(tmp_path, config_path=config_path))


@pytest.mark.asyncio
async def test_generate_prompt_uses_gemini_prompt_model() -> None:
    """Prompt generation should call the Gemini text model and compose the base style."""
    generate_content = AsyncMock(return_value=SimpleNamespace(text="teal and copper, visor eyes"))
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))

    prompt = await generate_avatars.generate_prompt(
        client,
        generate_avatars.AvatarTarget(
            entity_type="agents",
            entity_name="research",
            role="Finds information",
        ),
    )

    assert prompt == f"{generate_avatars.CHARACTER_STYLE}, teal and copper, visor eyes"
    kwargs = generate_content.await_args.kwargs
    assert kwargs["model"] == generate_avatars.PROMPT_MODEL
    assert kwargs["contents"] == "Agent name: research\nRole: Finds information\nType: agents"
    assert kwargs["config"].system_instruction == generate_avatars.AGENT_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_generate_prompt_uses_room_style_for_spaces() -> None:
    """Space avatars should use the same icon-style prompt path as rooms."""
    generate_content = AsyncMock(return_value=SimpleNamespace(text="deep blue, doorway outline"))
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))

    prompt = await generate_avatars.generate_prompt(
        client,
        generate_avatars.AvatarTarget(
            entity_type="spaces",
            entity_name="root_space",
            role="Workspace space that organizes rooms",
        ),
    )

    assert prompt == f"{generate_avatars.ROOM_STYLE}, deep blue, doorway outline"
    kwargs = generate_content.await_args.kwargs
    assert kwargs["config"].system_instruction == generate_avatars.ROOM_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_generate_avatar_writes_generated_image(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # noqa: ANN001
    """The avatar generation module should save Gemini-generated image bytes to the expected avatar file."""
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

    monkeypatch.setattr(generate_avatars, "get_avatar_path", lambda *_args, **_kwargs: avatar_path)
    monkeypatch.setattr(generate_avatars, "generate_prompt", AsyncMock(return_value="avatar prompt"))

    await generate_avatars.generate_avatar(
        client,
        generate_avatars.AvatarTarget(
            entity_type="agents",
            entity_name="general",
            role="Helpful assistant",
        ),
        _runtime_paths(tmp_path),
    )

    assert avatar_path.read_bytes() == b"avatar-bytes"
    kwargs = generate_content.await_args.kwargs
    assert kwargs["model"] == generate_avatars.IMAGE_MODEL
    assert kwargs["contents"] == "avatar prompt"
    assert kwargs["config"].response_modalities == ["IMAGE"]


@pytest.mark.asyncio
async def test_run_avatar_generation_includes_team_rooms_and_root_space(
    monkeypatch: pytest.MonkeyPatch,
    workspace_avatar_dir: Path,
) -> None:
    """Generation should cover team-only rooms and the managed root space."""
    raw_config = {
        "models": {"default": {"provider": "anthropic", "id": "claude-sonnet-4-6"}},
        "router": {"model": "default"},
        "agents": {
            "general": {
                "display_name": "General",
                "model": "default",
                "rooms": ["lobby"],
            },
        },
        "teams": {
            "ops_team": {
                "display_name": "Ops Team",
                "role": "Coordinates operations",
                "agents": ["general"],
                "rooms": ["war_room"],
                "model": "default",
            },
        },
        "matrix_space": {"enabled": True, "name": "Workspace"},
    }

    async def _generate_avatar(
        _client: object,
        target: generate_avatars.AvatarTarget,
        runtime_paths: constants_mod.RuntimePaths,
    ) -> None:
        del runtime_paths
        avatar_path = workspace_avatar_dir / target.entity_type / f"{target.entity_name}.png"
        avatar_path.parent.mkdir(parents=True, exist_ok=True)
        avatar_path.write_bytes(b"generated")

    generated = AsyncMock(side_effect=_generate_avatar)
    client = SimpleNamespace(aio=SimpleNamespace(aclose=AsyncMock()))

    def _make_client(*, api_key: str) -> object:
        assert api_key == "test-google-key"
        return client

    monkeypatch.setattr(
        generate_avatars,
        "load_validated_config",
        lambda *_args, **_kwargs: _config_with_runtime_paths(raw_config, workspace_avatar_dir.parent),
    )
    monkeypatch.setattr(generate_avatars.genai, "Client", _make_client)
    monkeypatch.setattr(generate_avatars, "generate_avatar", generated)
    workspace_avatar_dir.mkdir(parents=True, exist_ok=True)
    api_key_file = workspace_avatar_dir / "google-key.txt"
    api_key_file.write_text("test-google-key", encoding="utf-8")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY_FILE", str(api_key_file))

    await generate_avatars.run_avatar_generation(_runtime_paths(workspace_avatar_dir.parent))

    generated_entities = {(call.args[1].entity_type, call.args[1].entity_name) for call in generated.await_args_list}
    assert ("rooms", "lobby") in generated_entities
    assert ("rooms", "war_room") in generated_entities
    assert ("spaces", generate_avatars.ROOT_SPACE_AVATAR_NAME) in generated_entities
    client.aio.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_room_avatars_in_matrix_includes_team_rooms_and_root_space(
    monkeypatch: pytest.MonkeyPatch,
    workspace_avatar_dir: Path,
) -> None:
    """Matrix avatar sync should cover team-only rooms and the managed root space."""
    raw_config = {
        "models": {"default": {"provider": "anthropic", "id": "claude-sonnet-4-6"}},
        "router": {"model": "default"},
        "agents": {
            "general": {
                "display_name": "General",
                "model": "default",
            },
        },
        "teams": {
            "ops_team": {
                "display_name": "Ops Team",
                "role": "Coordinates operations",
                "agents": ["general"],
                "rooms": ["war_room"],
                "model": "default",
            },
        },
        "matrix_space": {"enabled": True, "name": "Workspace"},
    }
    room_avatar_path = workspace_avatar_dir / "rooms" / "war_room.png"
    room_avatar_path.parent.mkdir(parents=True)
    room_avatar_path.write_bytes(b"room-bytes")
    space_avatar_path = workspace_avatar_dir / "spaces" / "root_space.png"
    space_avatar_path.parent.mkdir(parents=True)
    space_avatar_path.write_bytes(b"space-bytes")

    router_account = SimpleNamespace(username="router")
    router_account.password = b"pw".decode()

    def _get_account(key: str) -> object | None:
        return router_account if key == "agent_router" else None

    state = SimpleNamespace(
        space_room_id="!space:localhost",
        get_account=_get_account,
    )
    client = SimpleNamespace(close=AsyncMock())
    set_room_avatar_from_file = AsyncMock(return_value=True)

    def _get_room_id(room_name: str, runtime_paths: constants_mod.RuntimePaths) -> str | None:
        del runtime_paths
        return "!war:localhost" if room_name == "war_room" else None

    monkeypatch.setattr(
        generate_avatars,
        "load_validated_config",
        lambda *_args, **_kwargs: _config_with_runtime_paths(raw_config, workspace_avatar_dir.parent),
    )
    monkeypatch.setattr(generate_avatars.MatrixState, "load", staticmethod(lambda **_kwargs: state))
    monkeypatch.setattr(generate_avatars, "login_agent_user", AsyncMock(return_value=client))
    monkeypatch.setattr(generate_avatars, "set_room_avatar_from_file", set_room_avatar_from_file)
    monkeypatch.setattr(generate_avatars, "get_room_id", _get_room_id)
    monkeypatch.setattr(
        generate_avatars.constants,
        "runtime_matrix_homeserver",
        lambda *_args, **_kwargs: "http://localhost:8008",
    )

    await generate_avatars.set_room_avatars_in_matrix(_runtime_paths(workspace_avatar_dir.parent))

    synced_targets = {(call.args[1], call.args[2].name) for call in set_room_avatar_from_file.await_args_list}
    assert ("!war:localhost", "war_room.png") in synced_targets
    assert ("!space:localhost", "root_space.png") in synced_targets
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_room_avatars_in_matrix_raises_when_room_avatar_updates_fail(
    monkeypatch: pytest.MonkeyPatch,
    workspace_avatar_dir: Path,
) -> None:
    """Matrix avatar sync should fail the command when a room avatar update is rejected."""
    raw_config = {
        "models": {"default": {"provider": "anthropic", "id": "claude-sonnet-4-6"}},
        "router": {"model": "default"},
        "agents": {
            "general": {
                "display_name": "General",
                "model": "default",
                "rooms": ["war_room"],
            },
        },
        "matrix_space": {"enabled": False},
    }
    room_avatar_path = workspace_avatar_dir / "rooms" / "war_room.png"
    room_avatar_path.parent.mkdir(parents=True)
    room_avatar_path.write_bytes(b"room-bytes")

    router_account = SimpleNamespace(username="router")
    router_account.password = b"pw".decode()

    def _get_account(key: str) -> object | None:
        return router_account if key == "agent_router" else None

    state = SimpleNamespace(
        space_room_id=None,
        get_account=_get_account,
    )
    client = SimpleNamespace(close=AsyncMock())

    monkeypatch.setattr(
        generate_avatars,
        "load_validated_config",
        lambda *_args, **_kwargs: _config_with_runtime_paths(raw_config, workspace_avatar_dir.parent),
    )
    monkeypatch.setattr(generate_avatars.MatrixState, "load", staticmethod(lambda **_kwargs: state))
    monkeypatch.setattr(generate_avatars, "login_agent_user", AsyncMock(return_value=client))
    monkeypatch.setattr(generate_avatars, "set_room_avatar_from_file", AsyncMock(return_value=False))
    monkeypatch.setattr(
        generate_avatars,
        "get_room_id",
        lambda room_name, _runtime_paths: "!war:localhost" if room_name == "war_room" else None,
    )
    monkeypatch.setattr(
        generate_avatars.constants,
        "runtime_matrix_homeserver",
        lambda *_args, **_kwargs: "http://localhost:8008",
    )

    with pytest.raises(
        generate_avatars.AvatarSyncError,
        match=r"Failed to set avatars for: room 'war_room'",
    ):
        await generate_avatars.set_room_avatars_in_matrix(_runtime_paths(workspace_avatar_dir.parent))

    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_room_avatars_in_matrix_skips_stale_root_space_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
    workspace_avatar_dir: Path,
) -> None:
    """Matrix avatar sync must not mutate a stale root Space when the feature is disabled."""
    raw_config = {
        "models": {"default": {"provider": "anthropic", "id": "claude-sonnet-4-6"}},
        "router": {"model": "default"},
        "agents": {
            "general": {
                "display_name": "General",
                "model": "default",
            },
        },
        "matrix_space": {"enabled": False, "name": "Workspace"},
    }
    space_avatar_path = workspace_avatar_dir / "spaces" / "root_space.png"
    space_avatar_path.parent.mkdir(parents=True)
    space_avatar_path.write_bytes(b"space-bytes")

    router_account = SimpleNamespace(username="router")
    router_account.password = b"pw".decode()

    def _get_account(key: str) -> object | None:
        return router_account if key == "agent_router" else None

    state = SimpleNamespace(
        space_room_id="!space:localhost",
        get_account=_get_account,
    )
    client = SimpleNamespace(close=AsyncMock())
    set_room_avatar_from_file = AsyncMock(return_value=True)

    monkeypatch.setattr(
        generate_avatars,
        "load_validated_config",
        lambda *_args, **_kwargs: _config_with_runtime_paths(raw_config, workspace_avatar_dir.parent),
    )
    monkeypatch.setattr(generate_avatars.MatrixState, "load", staticmethod(lambda **_kwargs: state))
    monkeypatch.setattr(generate_avatars, "login_agent_user", AsyncMock(return_value=client))
    monkeypatch.setattr(generate_avatars, "set_room_avatar_from_file", set_room_avatar_from_file)
    monkeypatch.setattr(
        generate_avatars.constants,
        "runtime_matrix_homeserver",
        lambda *_args, **_kwargs: "http://localhost:8008",
    )

    await generate_avatars.set_room_avatars_in_matrix(_runtime_paths(workspace_avatar_dir.parent))

    synced_targets = {(call.args[1], call.args[2].name) for call in set_room_avatar_from_file.await_args_list}
    assert ("!space:localhost", "root_space.png") not in synced_targets
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_room_avatars_in_matrix_requires_initialized_router_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Standalone avatar sync should fail fast until MindRoom has initialized the router account."""
    state = SimpleNamespace(get_account=lambda _key: None)
    monkeypatch.setattr(generate_avatars.MatrixState, "load", staticmethod(lambda **_kwargs: state))

    with pytest.raises(
        generate_avatars.AvatarSyncError,
        match="No router account found in Matrix state",
    ):
        await generate_avatars.set_room_avatars_in_matrix(_runtime_paths(tmp_path))


@pytest.mark.asyncio
async def test_set_room_avatars_in_matrix_wraps_router_login_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Router login failures should surface as AvatarSyncError for the CLI."""
    raw_config = {
        "models": {"default": {"provider": "anthropic", "id": "claude-sonnet-4-6"}},
        "router": {"model": "default"},
        "agents": {
            "general": {
                "display_name": "General",
                "model": "default",
            },
        },
        "matrix_space": {"enabled": False},
    }
    router_account = SimpleNamespace(username="router")
    router_account.password = b"pw".decode()
    state = SimpleNamespace(get_account=lambda key: router_account if key == "agent_router" else None)

    monkeypatch.setattr(
        generate_avatars,
        "load_validated_config",
        lambda *_args, **_kwargs: _config_with_runtime_paths(raw_config, tmp_path),
    )
    monkeypatch.setattr(generate_avatars.MatrixState, "load", staticmethod(lambda **_kwargs: state))
    monkeypatch.setattr(
        generate_avatars,
        "login_agent_user",
        AsyncMock(side_effect=ValueError("Failed to login @router:localhost: M_FORBIDDEN")),
    )
    monkeypatch.setattr(
        generate_avatars.constants,
        "runtime_matrix_homeserver",
        lambda *_args, **_kwargs: "http://localhost:8008",
    )

    with pytest.raises(
        generate_avatars.AvatarSyncError,
        match="Failed to log in as router for avatar sync",
    ):
        await generate_avatars.set_room_avatars_in_matrix(_runtime_paths(tmp_path))
