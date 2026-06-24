"""External trigger runtime coordinator tests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.api import main as api_main
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME, resolve_primary_runtime_paths
from mindroom.external_triggers.store import ExternalTriggerTarget, TriggerDeliverySnapshot
from mindroom.orchestration.external_trigger_runtime import ExternalTriggerRuntimeCoordinator

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})


def _config() -> Config:
    return Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-5.5"}},
            "agents": {"code": {"display_name": "Code", "role": "Write code", "rooms": ["campground"]}},
            "rooms": {"campground": {"display_name": "Campground"}},
        },
    )


def _snapshot(
    *,
    target_agent: str = "code",
    resolved_room_id: str = "!campground:example.org",
) -> TriggerDeliverySnapshot:
    return TriggerDeliverySnapshot(
        trigger_id="campground",
        uid="uid",
        version=1,
        auth_epoch=1,
        config_generation=7,
        enabled=True,
        description="Campground",
        owner_user_id="@owner:example.org",
        created_by_agent_name="code",
        created_in_room_id=resolved_room_id,
        target=ExternalTriggerTarget(room_id="campground", agent=target_agent),
        resolved_room_id=resolved_room_id,
        auth="ed25519",
        key_id="default",
        public_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        public_key_fingerprint="sha256:test",
        allowed_kinds=("campground.availability",),
        replay_window_seconds=300,
        max_body_bytes=65536,
        replay_scope="uid:1",
    )


def test_runtime_coordinator_binds_router_with_snapshot_readiness_gate(tmp_path: Path) -> None:
    """Coordinator binds router delivery with the authoritative snapshot readiness callback."""
    coordinator = ExternalTriggerRuntimeCoordinator(runtime_paths=_runtime_paths(tmp_path))

    router_bot = MagicMock()
    router_bot.agent_name = ROUTER_AGENT_NAME
    router_bot.running = True
    router_bot.client = object()
    router_bot._conversation_cache = object()
    bots = {ROUTER_AGENT_NAME: router_bot}

    with patch("mindroom.api.main.bind_external_trigger_runtime") as mock_bind:
        coordinator.bind_if_ready(_config(), bots)

    mock_bind.assert_called_once()
    assert mock_bind.call_args.args == (api_main.app,)
    assert mock_bind.call_args.kwargs["client"] is router_bot.client
    assert mock_bind.call_args.kwargs["conversation_cache"] is router_bot._conversation_cache
    assert callable(mock_bind.call_args.kwargs["is_trigger_snapshot_ready"])


@pytest.mark.asyncio
async def test_runtime_coordinator_is_ready_uses_snapshot_room_and_target(tmp_path: Path) -> None:
    """Coordinator readiness comes from live Matrix joined-room state."""
    coordinator = ExternalTriggerRuntimeCoordinator(runtime_paths=_runtime_paths(tmp_path))
    router_client = object()
    target_client = object()
    router_bot = MagicMock(agent_name=ROUTER_AGENT_NAME, client=router_client, running=True)
    target_bot = MagicMock(agent_name="code", client=target_client, running=True)
    bots = {ROUTER_AGENT_NAME: router_bot, "code": target_bot}

    async def get_joined_room_ids(client: object) -> list[str]:
        return {
            router_client: ["!campground:example.org"],
            target_client: ["!campground:example.org"],
        }[client]

    with patch(
        "mindroom.orchestration.external_trigger_runtime.get_joined_rooms",
        side_effect=get_joined_room_ids,
    ):
        assert await coordinator.is_ready(_snapshot(), bots) is True


@pytest.mark.asyncio
async def test_runtime_coordinator_is_ready_rejects_unjoined_room(tmp_path: Path) -> None:
    """Coordinator rejects triggers when router or target is not joined to the snapshot room."""
    coordinator = ExternalTriggerRuntimeCoordinator(runtime_paths=_runtime_paths(tmp_path))
    router_client = object()
    target_client = object()
    router_bot = MagicMock(agent_name=ROUTER_AGENT_NAME, client=router_client, running=True)
    target_bot = MagicMock(agent_name="code", client=target_client, running=True)
    bots = {ROUTER_AGENT_NAME: router_bot, "code": target_bot}

    async def get_joined_room_ids(client: object) -> list[str]:
        return {
            router_client: ["!campground:example.org"],
            target_client: ["!other:example.org"],
        }[client]

    with patch(
        "mindroom.orchestration.external_trigger_runtime.get_joined_rooms",
        side_effect=get_joined_room_ids,
    ):
        assert await coordinator.is_ready(_snapshot(), bots) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot", "router_running", "router_client", "target_running", "target_client"),
    [
        (_snapshot(target_agent="missing"), True, object(), True, object()),
        (_snapshot(), False, object(), True, object()),
        (_snapshot(), True, None, True, object()),
        (_snapshot(), True, object(), False, object()),
        (_snapshot(), True, object(), True, None),
    ],
)
async def test_runtime_coordinator_is_ready_rejects_inactive_runtime(
    tmp_path: Path,
    snapshot: TriggerDeliverySnapshot,
    router_running: bool,
    router_client: object | None,
    target_running: bool,
    target_client: object | None,
) -> None:
    """Coordinator rejects stopped or client-less runtime participants."""
    coordinator = ExternalTriggerRuntimeCoordinator(runtime_paths=_runtime_paths(tmp_path))
    router_bot = MagicMock(agent_name=ROUTER_AGENT_NAME, client=router_client, running=router_running)
    target_bot = MagicMock(agent_name="code", client=target_client, running=target_running)
    bots = {ROUTER_AGENT_NAME: router_bot, "code": target_bot}

    with patch("mindroom.orchestration.external_trigger_runtime.get_joined_rooms") as mock_get_joined_rooms:
        assert await coordinator.is_ready(snapshot, bots) is False

    mock_get_joined_rooms.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_coordinator_sync_api_config_snapshot_runs_for_policy_changes(tmp_path: Path) -> None:
    """Coordinator publishes API snapshots even when no authored trigger records exist."""
    config = _config()
    coordinator = ExternalTriggerRuntimeCoordinator(runtime_paths=_runtime_paths(tmp_path))

    with patch(
        "mindroom.orchestration.external_trigger_runtime.asyncio.to_thread",
        new=AsyncMock(return_value=True),
    ) as mock_to_thread:
        await coordinator.sync_api_config_snapshot(config, config)

    mock_to_thread.assert_awaited_once_with(
        api_main.config_lifecycle._publish_runtime_config_into_app,
        config,
        coordinator.runtime_paths,
        api_main.app,
    )
