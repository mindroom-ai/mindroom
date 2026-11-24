"""External trigger runtime coordinator tests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.api import config_lifecycle
from mindroom.api import main as api_main
from mindroom.config.main import Config, load_config
from mindroom.constants import SOURCE_KIND_KEY
from mindroom.dispatch_source import source_kind_allows_self_authored_ingress
from mindroom.external_triggers.executor import execute_external_trigger
from mindroom.external_triggers.models import ExternalTriggerPayload
from mindroom.external_triggers.store import ExternalTriggerTarget, TriggerDeliverySnapshot
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.orchestration.external_trigger_runtime import ExternalTriggerRuntimeCoordinator
from tests.api.conftest import request_for, save_config
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


def test_runtime_coordinator_binds_any_running_bot() -> None:
    """Coordinator binds delivery through each trigger's target once any bot runs, the owner account or not."""
    coordinator = ExternalTriggerRuntimeCoordinator()
    code_bot = MagicMock(agent_name="code", running=True, client=object())
    stopped_bot = MagicMock(agent_name="idle", running=False, client=object())
    clientless_bot = MagicMock(agent_name="clientless", running=True, client=None)
    bots = {"code": code_bot, "idle": stopped_bot, "clientless": clientless_bot}

    with patch("mindroom.api.main.bind_external_trigger_runtime") as mock_bind:
        coordinator.bind_if_ready(_config(), bots)

    mock_bind.assert_called_once()
    assert mock_bind.call_args.args == (api_main.app,)
    target_bot = mock_bind.call_args.kwargs["target_bot"]
    assert target_bot("code") is code_bot
    assert target_bot("idle") is None
    assert target_bot("clientless") is None
    assert target_bot("missing") is None


def test_runtime_coordinator_does_not_bind_while_no_bot_runs() -> None:
    """Coordinator leaves delivery unbound until a bot runs."""
    coordinator = ExternalTriggerRuntimeCoordinator()

    with patch("mindroom.api.main.bind_external_trigger_runtime") as mock_bind:
        coordinator.bind_if_ready(_config(), {"code": MagicMock(agent_name="code", running=False, client=object())})

    mock_bind.assert_not_called()


@pytest.mark.asyncio
async def test_trigger_posted_and_answered_by_target_without_owner(tmp_path: Path) -> None:
    """In a room with one person, one agent and no owner account, the target's own bot posts the trigger it answers."""
    runtime_paths = test_runtime_paths(tmp_path)
    api_main.initialize_api_app(api_main.app, runtime_paths)
    assert config_lifecycle.load_config_into_app(runtime_paths, api_main.app).loaded
    config = config_lifecycle.require_api_state(api_main.app).snapshot.runtime_config
    assert config is not None
    target_client = MagicMock(user_id="@mindroom_code:example.org")
    target_bot = MagicMock(agent_name="code", client=target_client, running=True)
    coordinator = ExternalTriggerRuntimeCoordinator()
    sent: list[tuple[object, str, dict[str, object]]] = []

    async def send(client: object, room_id: str, content: dict[str, object]) -> DeliveredMatrixEvent:
        sent.append((client, room_id, content))
        return DeliveredMatrixEvent(event_id="$trigger", content_sent=content)

    try:
        coordinator.bind_if_ready(config, {"code": target_bot})
        runtime = config_lifecycle.app_state(api_main.app).external_trigger_runtime
        assert runtime is not None
        poster = runtime.target_bot("code")
        assert poster is target_bot
        with (
            patch("mindroom.external_triggers.executor.send_matrix_message", send),
            patch(
                "mindroom.external_triggers.executor.format_entity_mention",
                return_value=("@code", ["@mindroom_code:example.org"], "[@code](https://matrix.to/#/x)"),
            ),
        ):
            event_id = await execute_external_trigger(
                client=poster.client,
                snapshot=_snapshot(),
                payload=ExternalTriggerPayload(kind="campground.availability", message="A site opened up"),
                config=config,
                runtime_paths=runtime_paths,
                conversation_reader=AsyncMock(),
            )
    finally:
        coordinator.unbind()

    assert event_id == "$trigger"
    [(posting_client, room_id, content)] = sent
    assert posting_client is target_client
    assert room_id == "!campground:example.org"
    assert content["m.mentions"] == {"user_ids": ["@mindroom_code:example.org"]}
    assert source_kind_allows_self_authored_ingress(content[SOURCE_KIND_KEY])


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
    bots = {"code": MagicMock(running=True, client=object())}
    coordinator = ExternalTriggerRuntimeCoordinator()
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
