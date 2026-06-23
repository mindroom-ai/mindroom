"""External trigger runtime coordinator tests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.api import main as api_main
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME, resolve_runtime_paths
from mindroom.matrix.state import MatrixState
from mindroom.orchestration.external_trigger_runtime import ExternalTriggerRuntimeCoordinator

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})


def _config_with_external_trigger(
    tmp_path: Path,
    *,
    room_id: str = "!campground:example.org",
    enabled: bool = True,
) -> Config:
    return Config.validate_with_runtime(
        {
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Write code",
                },
            },
            "external_triggers": {
                "campground": {
                    "enabled": enabled,
                    "public_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                    "target": {
                        "room_id": room_id,
                        "agent": "code",
                    },
                },
            },
        },
        _runtime_paths(tmp_path),
    )


def test_runtime_coordinator_binds_router_with_live_readiness_gate(tmp_path: Path) -> None:
    """Coordinator binds router delivery with the authoritative readiness callback."""
    config = _config_with_external_trigger(tmp_path)
    coordinator = ExternalTriggerRuntimeCoordinator(
        runtime_paths=_runtime_paths(tmp_path),
    )

    router_bot = MagicMock()
    router_bot.agent_name = ROUTER_AGENT_NAME
    router_bot.running = True
    router_bot.client = object()
    router_bot._conversation_cache = object()

    target_bot = MagicMock()
    target_bot.agent_name = "code"
    target_bot.running = False
    target_bot.client = object()
    bots = {
        ROUTER_AGENT_NAME: router_bot,
        "code": target_bot,
    }

    with patch.object(coordinator, "_bind_from_started_bots") as mock_bind:
        coordinator.bind_if_ready(config, bots)
    mock_bind.assert_called_once_with(
        (router_bot,),
        is_trigger_ready=mock_bind.call_args.kwargs["is_trigger_ready"],
    )


@pytest.mark.asyncio
async def test_runtime_coordinator_is_ready_uses_live_joined_rooms_and_aliases(tmp_path: Path) -> None:
    """Coordinator readiness comes from live Matrix joined-room state."""
    runtime_paths = _runtime_paths(tmp_path)
    state = MatrixState()
    state.add_room("campground", "!campground:example.org", "#campground:example.org", "Campground")
    state.save(runtime_paths)
    config = _config_with_external_trigger(tmp_path, room_id="campground")
    coordinator = ExternalTriggerRuntimeCoordinator(
        runtime_paths=runtime_paths,
    )
    router_client = object()
    router_bot = MagicMock()
    router_bot.agent_name = ROUTER_AGENT_NAME
    router_bot.client = router_client
    router_bot.running = True
    target_client = object()
    target_bot = MagicMock()
    target_bot.agent_name = "code"
    target_bot.client = target_client
    target_bot.running = True
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
        assert await coordinator.is_ready("campground", config, bots) is True


@pytest.mark.asyncio
async def test_runtime_coordinator_is_ready_rejects_unjoined_room(tmp_path: Path) -> None:
    """Coordinator rejects triggers when router or target is not joined to the trigger room."""
    config = _config_with_external_trigger(tmp_path)
    coordinator = ExternalTriggerRuntimeCoordinator(runtime_paths=_runtime_paths(tmp_path))
    router_client = object()
    target_client = object()
    router_bot = MagicMock()
    router_bot.agent_name = ROUTER_AGENT_NAME
    router_bot.client = router_client
    router_bot.running = True
    target_bot = MagicMock()
    target_bot.agent_name = "code"
    target_bot.client = target_client
    target_bot.running = True
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
        assert await coordinator.is_ready("campground", config, bots) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trigger_id", "enabled", "router_running", "router_client", "target_running", "target_client"),
    [
        ("missing", True, True, object(), True, object()),
        ("campground", False, True, object(), True, object()),
        ("campground", True, False, object(), True, object()),
        ("campground", True, True, None, True, object()),
        ("campground", True, True, object(), False, object()),
        ("campground", True, True, object(), True, None),
    ],
)
async def test_runtime_coordinator_is_ready_rejects_inactive_runtime(
    tmp_path: Path,
    trigger_id: str,
    enabled: bool,
    router_running: bool,
    router_client: object | None,
    target_running: bool,
    target_client: object | None,
) -> None:
    """Coordinator rejects missing, disabled, stopped, or client-less runtime participants."""
    config = _config_with_external_trigger(tmp_path, enabled=enabled)
    coordinator = ExternalTriggerRuntimeCoordinator(runtime_paths=_runtime_paths(tmp_path))
    router_bot = MagicMock()
    router_bot.agent_name = ROUTER_AGENT_NAME
    router_bot.client = router_client
    router_bot.running = router_running
    target_bot = MagicMock()
    target_bot.agent_name = "code"
    target_bot.client = target_client
    target_bot.running = target_running
    bots = {ROUTER_AGENT_NAME: router_bot, "code": target_bot}

    with patch("mindroom.orchestration.external_trigger_runtime.get_joined_rooms") as mock_get_joined_rooms:
        assert await coordinator.is_ready(trigger_id, config, bots) is False

    mock_get_joined_rooms.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_coordinator_sync_api_config_snapshot_runs_off_event_loop(tmp_path: Path) -> None:
    """Coordinator publishes API snapshots off the orchestrator event loop."""
    config = _config_with_external_trigger(tmp_path)
    coordinator = ExternalTriggerRuntimeCoordinator(
        runtime_paths=_runtime_paths(tmp_path),
    )

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
