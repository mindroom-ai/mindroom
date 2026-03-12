"""Tests for the avatar generation module."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from google.genai import types

from mindroom import avatar_generation as generate_avatars

if TYPE_CHECKING:
    from pathlib import Path


def _workspace_avatar_path(
    tmp_path: Path,
    entity_type: str,
    entity_name: str,
    *,
    config_path: Path | None = None,
) -> Path:
    del config_path
    return tmp_path / "avatars" / entity_type / f"{entity_name}.png"


def test_load_config_uses_config_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # noqa: ANN001
    """The avatar generation module should read the active MindRoom config path."""
    config_path = tmp_path / "custom-config.yaml"
    config_path.write_text("agents:\n  general:\n    role: helper\n", encoding="utf-8")

    monkeypatch.setattr(generate_avatars, "CONFIG_PATH", config_path)

    assert generate_avatars.load_config() == {"agents": {"general": {"role": "helper"}}}


def test_get_avatar_path_uses_workspace_avatars_dir(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # noqa: ANN001
    """Generated avatars should land in the workspace avatars directory."""

    def _avatars_dir(**_kwargs: object) -> Path:
        return tmp_path / "avatars"

    monkeypatch.setattr(generate_avatars, "avatars_dir", _avatars_dir)

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


def test_has_missing_managed_avatars_detects_complete_avatar_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Existing managed avatars should not require a Google key just for sync."""
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
    config = generate_avatars.Config.model_validate(raw_config)
    for entity_type, entity_name in (("agents", "general"), ("agents", "router")):
        avatar_path = tmp_path / "avatars" / entity_type / f"{entity_name}.png"
        avatar_path.parent.mkdir(parents=True, exist_ok=True)
        avatar_path.write_bytes(b"avatar")

    def _avatars_dir(**_kwargs: object) -> Path:
        return tmp_path / "avatars"

    monkeypatch.setattr(generate_avatars, "avatars_dir", _avatars_dir)
    monkeypatch.setattr(
        generate_avatars,
        "resolve_avatar_path",
        lambda entity_type, entity_name, *, config_path=None: _workspace_avatar_path(
            tmp_path,
            entity_type,
            entity_name,
            config_path=config_path,
        ),
    )

    assert not generate_avatars.has_missing_managed_avatars(config)


def test_has_missing_managed_avatars_ignores_direct_room_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
    config = generate_avatars.Config.model_validate(raw_config)
    for entity_type, entity_name in (("agents", "general"), ("agents", "router")):
        avatar_path = tmp_path / "avatars" / entity_type / f"{entity_name}.png"
        avatar_path.parent.mkdir(parents=True, exist_ok=True)
        avatar_path.write_bytes(b"avatar")

    def _avatars_dir(**_kwargs: object) -> Path:
        return tmp_path / "avatars"

    monkeypatch.setattr(generate_avatars, "avatars_dir", _avatars_dir)
    monkeypatch.setattr(
        generate_avatars,
        "resolve_avatar_path",
        lambda entity_type, entity_name, *, config_path=None: _workspace_avatar_path(
            tmp_path,
            entity_type,
            entity_name,
            config_path=config_path,
        ),
    )

    assert not generate_avatars.has_missing_managed_avatars(config)


def test_has_missing_managed_avatars_ignores_full_room_aliases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
    config = generate_avatars.Config.model_validate(raw_config)
    for entity_type, entity_name in (("agents", "general"), ("agents", "router")):
        avatar_path = tmp_path / "avatars" / entity_type / f"{entity_name}.png"
        avatar_path.parent.mkdir(parents=True, exist_ok=True)
        avatar_path.write_bytes(b"avatar")

    def _avatars_dir(**_kwargs: object) -> Path:
        return tmp_path / "avatars"

    monkeypatch.setattr(generate_avatars, "avatars_dir", _avatars_dir)
    monkeypatch.setattr(
        generate_avatars,
        "resolve_avatar_path",
        lambda entity_type, entity_name, *, config_path=None: _workspace_avatar_path(
            tmp_path,
            entity_type,
            entity_name,
            config_path=config_path,
        ),
    )

    assert not generate_avatars.has_missing_managed_avatars(config)


@pytest.mark.asyncio
async def test_run_avatar_generation_skips_google_key_when_all_managed_avatars_exist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Existing managed avatars should allow sync-only startup without generation credentials."""
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
        avatar_path = tmp_path / "avatars" / entity_type / f"{entity_name}.png"
        avatar_path.parent.mkdir(parents=True, exist_ok=True)
        avatar_path.write_bytes(b"avatar")

    monkeypatch.setattr(
        generate_avatars,
        "load_validated_config",
        lambda: generate_avatars.Config.model_validate(raw_config),
    )
    monkeypatch.setattr(generate_avatars, "avatars_dir", lambda: tmp_path / "avatars")
    monkeypatch.setattr(
        generate_avatars,
        "resolve_avatar_path",
        lambda entity_type, entity_name, *, config_path=None: _workspace_avatar_path(
            tmp_path,
            entity_type,
            entity_name,
            config_path=config_path,
        ),
    )
    monkeypatch.setattr(generate_avatars.genai, "Client", lambda **_kwargs: pytest.fail("generation should be skipped"))
    sync_room_avatars = AsyncMock()
    monkeypatch.setattr(generate_avatars, "set_room_avatars_in_matrix", sync_room_avatars)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    await generate_avatars.run_avatar_generation(sync_room_avatars=True)

    sync_room_avatars.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_avatar_generation_raises_when_missing_avatars_still_fail_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
    router_avatar = tmp_path / "avatars" / "agents" / "router.png"
    router_avatar.parent.mkdir(parents=True, exist_ok=True)
    router_avatar.write_bytes(b"avatar")

    monkeypatch.setattr(
        generate_avatars,
        "load_validated_config",
        lambda: generate_avatars.Config.model_validate(raw_config),
    )
    monkeypatch.setattr(generate_avatars, "avatars_dir", lambda: tmp_path / "avatars")
    monkeypatch.setattr(
        generate_avatars,
        "resolve_avatar_path",
        lambda entity_type, entity_name, *, config_path=None: _workspace_avatar_path(
            tmp_path,
            entity_type,
            entity_name,
            config_path=config_path,
        ),
    )
    monkeypatch.setattr(
        generate_avatars.genai,
        "Client",
        lambda **_kwargs: SimpleNamespace(aio=SimpleNamespace(aclose=AsyncMock())),
    )
    monkeypatch.setattr(generate_avatars, "generate_prompt", AsyncMock(side_effect=RuntimeError("boom")))
    sync_room_avatars = AsyncMock()
    monkeypatch.setattr(generate_avatars, "set_room_avatars_in_matrix", sync_room_avatars)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-key")

    with pytest.raises(generate_avatars.AvatarGenerationError, match="Avatar generation failed"):
        await generate_avatars.run_avatar_generation(sync_room_avatars=True)

    sync_room_avatars.assert_not_awaited()
    assert not (tmp_path / "avatars" / "agents" / "general.png").exists()


@pytest.mark.asyncio
async def test_run_avatar_generation_set_only_accepts_null_optional_sections(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
    monkeypatch.setattr(generate_avatars, "CONFIG_PATH", config_path)

    await generate_avatars.run_avatar_generation(set_only=True, sync_room_avatars=False)


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
async def test_generate_prompt_uses_room_style_for_spaces() -> None:
    """Space avatars should use the same icon-style prompt path as rooms."""
    generate_content = AsyncMock(return_value=SimpleNamespace(text="deep blue, doorway outline"))
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))

    prompt = await generate_avatars.generate_prompt(
        client,
        entity_type="spaces",
        entity_name="root_space",
        role="Workspace space that organizes rooms",
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


@pytest.mark.asyncio
async def test_run_avatar_generation_includes_team_rooms_and_root_space(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
        entity_type: str,
        entity_name: str,
        _entity_data: dict,
        _all_agents: dict | None = None,
    ) -> None:
        avatar_path = tmp_path / "avatars" / entity_type / f"{entity_name}.png"
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
        lambda: generate_avatars.Config.model_validate(raw_config),
    )
    monkeypatch.setattr(generate_avatars, "avatars_dir", lambda: tmp_path / "avatars")
    monkeypatch.setattr(
        generate_avatars,
        "resolve_avatar_path",
        lambda entity_type, entity_name, *, config_path=None: _workspace_avatar_path(
            tmp_path,
            entity_type,
            entity_name,
            config_path=config_path,
        ),
    )
    monkeypatch.setattr(generate_avatars.genai, "Client", _make_client)
    monkeypatch.setattr(generate_avatars, "generate_avatar", generated)
    monkeypatch.setattr(generate_avatars, "set_room_avatars_in_matrix", AsyncMock())
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-key")

    await generate_avatars.run_avatar_generation(sync_room_avatars=False)

    generated_entities = {(call.args[1], call.args[2]) for call in generated.await_args_list}
    assert ("rooms", "lobby") in generated_entities
    assert ("rooms", "war_room") in generated_entities
    assert ("spaces", generate_avatars.ROOT_SPACE_AVATAR_NAME) in generated_entities
    client.aio.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_room_avatars_in_matrix_includes_team_rooms_and_root_space(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
    room_avatar_path = tmp_path / "avatars" / "rooms" / "war_room.png"
    room_avatar_path.parent.mkdir(parents=True)
    room_avatar_path.write_bytes(b"room-bytes")
    space_avatar_path = tmp_path / "avatars" / "spaces" / "root_space.png"
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
    check_and_set_avatar = AsyncMock(return_value=True)

    def _get_room_id(room_name: str) -> str | None:
        return "!war:localhost" if room_name == "war_room" else None

    monkeypatch.setattr(
        generate_avatars,
        "load_validated_config",
        lambda: generate_avatars.Config.model_validate(raw_config),
    )
    monkeypatch.setattr(generate_avatars, "avatars_dir", lambda: tmp_path / "avatars")
    monkeypatch.setattr(
        generate_avatars,
        "resolve_avatar_path",
        lambda entity_type, entity_name, *, config_path=None: _workspace_avatar_path(
            tmp_path,
            entity_type,
            entity_name,
            config_path=config_path,
        ),
    )
    monkeypatch.setattr(generate_avatars.MatrixState, "load", staticmethod(lambda: state))
    monkeypatch.setattr(generate_avatars, "login_agent_user", AsyncMock(return_value=client))
    monkeypatch.setattr(generate_avatars, "check_and_set_avatar", check_and_set_avatar)
    monkeypatch.setattr(generate_avatars, "get_room_id", _get_room_id)
    monkeypatch.setattr(generate_avatars, "MATRIX_HOMESERVER", "http://localhost:8008")

    await generate_avatars.set_room_avatars_in_matrix()

    synced_targets = {(call.kwargs.get("room_id"), call.args[1].name) for call in check_and_set_avatar.await_args_list}
    assert ("!war:localhost", "war_room.png") in synced_targets
    assert ("!space:localhost", "root_space.png") in synced_targets
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_room_avatars_in_matrix_skips_stale_root_space_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
    space_avatar_path = tmp_path / "avatars" / "spaces" / "root_space.png"
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
    check_and_set_avatar = AsyncMock(return_value=True)

    monkeypatch.setattr(
        generate_avatars,
        "load_validated_config",
        lambda: generate_avatars.Config.model_validate(raw_config),
    )
    monkeypatch.setattr(generate_avatars, "avatars_dir", lambda: tmp_path / "avatars")
    monkeypatch.setattr(
        generate_avatars,
        "resolve_avatar_path",
        lambda entity_type, entity_name, *, config_path=None: _workspace_avatar_path(
            tmp_path,
            entity_type,
            entity_name,
            config_path=config_path,
        ),
    )
    monkeypatch.setattr(generate_avatars.MatrixState, "load", staticmethod(lambda: state))
    monkeypatch.setattr(generate_avatars, "login_agent_user", AsyncMock(return_value=client))
    monkeypatch.setattr(generate_avatars, "check_and_set_avatar", check_and_set_avatar)
    monkeypatch.setattr(generate_avatars, "MATRIX_HOMESERVER", "http://localhost:8008")

    await generate_avatars.set_room_avatars_in_matrix()

    synced_targets = {(call.kwargs.get("room_id"), call.args[1].name) for call in check_and_set_avatar.await_args_list}
    assert ("!space:localhost", "root_space.png") not in synced_targets
    client.close.assert_awaited_once()
