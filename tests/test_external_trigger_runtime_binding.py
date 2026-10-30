"""External trigger runtime coordinator tests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from mindroom.api import config_lifecycle
from mindroom.api import main as api_main
from mindroom.config.main import Config, load_config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.external_triggers.store import ExternalTriggerTarget, TriggerDeliverySnapshot
from tests.api.conftest import request_for, save_config
from tests.authorization_helpers import (
    make_test_external_trigger_runtime_coordinator,
)
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _config() -> Config:
    return Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
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
        capability_token_hash=None,
        delivery_mode="reusable",
        allowed_kinds=("campground.availability",),
        replay_window_seconds=300,
        max_body_bytes=65536,
        replay_scope="uid:1",
    )


def test_runtime_coordinator_binds_router_with_snapshot_readiness_gate() -> None:
    """Coordinator binds router delivery with the authoritative snapshot readiness callback."""
    coordinator = make_test_external_trigger_runtime_coordinator()

    router_bot = MagicMock()
    router_bot.agent_name = ROUTER_AGENT_NAME
    router_bot.running = True
    router_bot.client = object()
    router_bot._conversation_reader = object()
    bots = {ROUTER_AGENT_NAME: router_bot}

    with patch("mindroom.api.main.bind_external_trigger_runtime") as mock_bind:
        coordinator.bind_if_ready(_config(), bots)

    mock_bind.assert_called_once()
    assert mock_bind.call_args.args == (api_main.app,)
    assert mock_bind.call_args.kwargs["client"] is router_bot.client
    assert mock_bind.call_args.kwargs["conversation_reader"] is router_bot._conversation_reader
    assert callable(mock_bind.call_args.kwargs["is_trigger_snapshot_ready"])


def test_runtime_coordinator_leaves_delivery_unavailable_until_the_bots_run_the_dashboard_config(
    tmp_path: Path,
) -> None:
    """A dashboard save the bots have not applied yet must not authorize triggers the receiving bot would refuse."""
    runtime_paths = test_runtime_paths(tmp_path)
    api_main.initialize_api_app(api_main.app, runtime_paths)
    assert config_lifecycle.load_config_into_app(runtime_paths, api_main.app).loaded
    running_config = load_config(runtime_paths)
    saved_payload = {**running_config.authored_model_dump(), "timezone": "Europe/Amsterdam"}
    save_config(request_for(api_main.app), saved_payload)
    router_bot = MagicMock()
    router_bot.running = True
    router_bot.client = object()
    bots = {ROUTER_AGENT_NAME: router_bot}
    coordinator = make_test_external_trigger_runtime_coordinator()
    try:
        coordinator.bind_if_ready(running_config, bots)
        assert config_lifecycle.app_state(api_main.app).external_trigger_runtime is None

        snapshot = config_lifecycle.require_api_state(api_main.app).snapshot
        assert snapshot.runtime_config is not None
        coordinator.bind_if_ready(snapshot.runtime_config, bots)
        runtime = config_lifecycle.app_state(api_main.app).external_trigger_runtime
        assert runtime is not None
        assert runtime.config_generation == snapshot.generation
    finally:
        coordinator.unbind()


@pytest.mark.asyncio
async def test_runtime_coordinator_is_ready_uses_snapshot_room_and_target() -> None:
    """Coordinator readiness comes from live Matrix joined-room state."""
    coordinator = make_test_external_trigger_runtime_coordinator()
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
        assert await coordinator._is_ready(_snapshot(), bots) is True


@pytest.mark.asyncio
async def test_runtime_coordinator_is_ready_rejects_unjoined_room() -> None:
    """Coordinator rejects triggers when router or target is not joined to the snapshot room."""
    coordinator = make_test_external_trigger_runtime_coordinator()
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
        assert await coordinator._is_ready(_snapshot(), bots) is False


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
    snapshot: TriggerDeliverySnapshot,
    router_running: bool,
    router_client: object | None,
    target_running: bool,
    target_client: object | None,
) -> None:
    """Coordinator rejects stopped or client-less runtime participants."""
    coordinator = make_test_external_trigger_runtime_coordinator()
    router_bot = MagicMock(agent_name=ROUTER_AGENT_NAME, client=router_client, running=router_running)
    target_bot = MagicMock(agent_name="code", client=target_client, running=target_running)
    bots = {ROUTER_AGENT_NAME: router_bot, "code": target_bot}

    with patch("mindroom.orchestration.external_trigger_runtime.get_joined_rooms") as mock_get_joined_rooms:
        assert await coordinator._is_ready(snapshot, bots) is False

    mock_get_joined_rooms.assert_not_called()
