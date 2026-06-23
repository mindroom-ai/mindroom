"""External trigger runtime coordinator tests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.api import main as api_main
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME, resolve_runtime_paths
from mindroom.orchestration.external_trigger_runtime import ExternalTriggerRuntimeCoordinator

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})


def _config_with_external_trigger(tmp_path: Path) -> Config:
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
                    "public_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                    "target": {
                        "room_id": "!campground:example.org",
                        "agent": "code",
                    },
                },
            },
        },
        _runtime_paths(tmp_path),
    )


def test_runtime_coordinator_binds_router_with_joined_target_snapshot(tmp_path: Path) -> None:
    """Coordinator binds trigger runtime only for targets joined to their trigger room."""
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
        target_bot.running = True
        coordinator.bind_if_ready(config, bots)
        coordinator.joined_room_ids["code"] = frozenset({"!campground:example.org"})
        coordinator.bind_if_ready(config, bots)
        coordinator.joined_room_ids[ROUTER_AGENT_NAME] = frozenset({"!campground:example.org"})
        coordinator.bind_if_ready(config, bots)

    assert [call.kwargs["ready_trigger_ids"] for call in mock_bind.call_args_list] == [
        frozenset(),
        frozenset(),
        frozenset(),
        frozenset({"campground"}),
    ]


@pytest.mark.asyncio
async def test_runtime_coordinator_refreshes_joined_room_snapshot_from_matrix(tmp_path: Path) -> None:
    """Coordinator readiness cache comes from actual Matrix joined-room state."""
    config = _config_with_external_trigger(tmp_path)
    coordinator = ExternalTriggerRuntimeCoordinator(
        runtime_paths=_runtime_paths(tmp_path),
    )
    router_client = object()
    router_bot = MagicMock()
    router_bot.agent_name = ROUTER_AGENT_NAME
    router_bot.client = router_client
    target_client = object()
    target_bot = MagicMock()
    target_bot.agent_name = "code"
    target_bot.client = target_client
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
        await coordinator.refresh_joined_room_ids(config, [target_bot], bots)

    assert coordinator.joined_room_ids == {
        ROUTER_AGENT_NAME: frozenset({"!campground:example.org"}),
        "code": frozenset({"!campground:example.org"}),
    }


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
