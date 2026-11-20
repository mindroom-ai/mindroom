"""A deployment that ran with a router upgrades to the router-free end state.

One deployment carries everything the router left behind: a config with `router.access` and `router.accept_invites`,
a journal holding an approval card the router sent and nobody answered, a scheduled task the router owned, a thread
with an old visible relay and an old router voice echo, and a plugin hook filtered on the router's name. The flow
starts each owner of that data once and checks the state the upgrade must leave.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import nio
import pytest
from structlog.testing import capture_logs

from mindroom import constants
from mindroom.approval.recovery import ApprovalRecovery
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.dispatch_source import TRUSTED_INTERNAL_RELAY_SOURCE_KIND
from mindroom.event_journal import ApprovalCardView, DeliveryStage
from mindroom.hooks import EVENT_MESSAGE_RECEIVED, MessageReceivedContext, hook
from mindroom.orchestration.config_warnings import warn_about_config_risks
from mindroom.scheduling import runtime as scheduling_runtime
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.identity_helpers import entity_ids
from tests.scheduling_helpers import scheduling_bot
from tests.test_hook_execution import _emit_message_as, _stewarded_registry
from tests.test_legacy_approval_records import (  # noqa: F401 - recorded_journal is a pytest fixture
    _ROOM,
    _ROUTER,
    _manager,
    recorded_journal,
)
from tests.test_scheduled_task_runtime import ROOM_ID, TASK_ID, _delivered, _runtime, _SharedRoom, _workflow
from tests.test_turns_focused import (
    _ROOM_ID,
    _SENDER,
    _build_harness,
    _entity_user_id,
    _room_with_members,
    _voice_echo_event,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.event_journal import MatrixDelivery
    from tests.test_legacy_approval_records import _Journal

pytestmark = pytest.mark.asyncio


def _old_config(tmp_path: Path) -> Config:
    """Return the config a deployment with a router carries, with the two router fields an operator may have set."""
    return bind_runtime_paths(
        Config.model_validate(
            {
                "agents": {"general": {"display_name": "General"}, "helper": {"display_name": "Helper"}},
                "router": {"access": {"users": [_SENDER], "current_room_members": True}, "accept_invites": True},
            },
        ),
        test_runtime_paths(tmp_path / "runtime"),
    )


def _old_relay(config: Config) -> nio.RoomMessageText:
    """Return the visible relay the router posted for a person's message before the upgrade."""
    return nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "@general could you help with this?",
                "msgtype": "m.text",
                "m.mentions": {"user_ids": [_entity_user_id(config, "general")]},
                constants.SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
                constants.ORIGINAL_SENDER_KEY: _SENDER,
            },
            "event_id": "$router-relay:localhost",
            "sender": _entity_user_id(config, ROUTER_AGENT_NAME),
            "origin_server_ts": 1_000_000,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
        },
    )


async def _the_routers_schedule_fires_through_an_agent(config: Config) -> None:
    """The schedule the router owned fires through the agent its message mentions."""
    runtime_paths = runtime_paths_for(config)
    ids = {name: matrix_id.full_id for name, matrix_id in entity_ids(config, runtime_paths).items()}
    room = _SharedRoom({ids["general"]: 100, ids["helper"]: 100})
    workflow = _workflow().model_copy(update={"message": "@helper water the plants"})
    task = {"task_id": TASK_ID, "workflow": workflow.model_dump_json(), "status": "pending", "owner": ids["router"]}
    room._put("com.mindroom.scheduled.task", TASK_ID, task, ids["router"])
    general = scheduling_bot(room.client(ids["general"]), agent_name="general")
    helper = scheduling_bot(room.client(ids["helper"]), agent_name="helper")
    schedules = _runtime(runtime_paths, config, {"general": general, "helper": helper})
    with patch("mindroom.scheduling.executor.execute_scheduled_workflow", new=_delivered()) as execute:
        assert [await schedules.restore_room(bot, ROOM_ID) for bot in (general, helper)] == [0, 1]
        await asyncio.wait_for(schedules._runners[TASK_ID][1], timeout=2)
    execute.assert_awaited_once()
    assert execute.await_args.args[0] is helper.client
    assert room.task()["content"]["status"] == "completed"


async def _the_thread_reads_as_before(config: Config, tmp_path: Path) -> None:
    """The old relay is the person's message to the agent it names, and the old echo is no turn."""
    harness = _build_harness(config, tmp_path / "turns")
    thread_room = _room_with_members(config, "general", "helper", ROUTER_AGENT_NAME)
    await harness.deliver(thread_room, _old_relay(config))
    await harness.deliver(thread_room, _voice_echo_event(config, ROUTER_AGENT_NAME))
    [request] = harness.runner.requests
    assert request.response_envelope.requester_id == _SENDER
    assert request.response_envelope.target.reply_to_event_id == "$router-relay:localhost"
    assert harness.gateway.sent == []


async def test_an_upgraded_deployment_reaches_the_router_free_end_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorded_journal: Callable[[str], _Journal],  # noqa: F811
) -> None:
    """The card ends once, the schedule fires through an agent, the thread reads as before, and each warning is one."""
    monkeypatch.setattr(scheduling_runtime, "_OWNER_POLL_INTERVAL_SECONDS", 0)
    alias_warnings: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "mindroom.hooks.registry.logger",
        SimpleNamespace(warning=lambda _message, **fields: alias_warnings.append(fields)),
    )

    # The config loads, and its ignored field warns once.
    config = _old_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    with capture_logs() as logs:
        warn_about_config_risks(config, runtime_paths)
    assert len([entry for entry in logs if "router" in entry["event"]]) == 1
    assert config.router.access is not None

    store = recorded_journal("01_waiting_card_acknowledged.sql").open()
    sent: list[MatrixDelivery] = []
    manager = _manager(tmp_path, store, sent)
    recovery = ApprovalRecovery(deliver_unavailable_notice=AsyncMock(), owner_absent=lambda: False)
    recovery.bind(manager)
    try:
        # The router's pending card is ended once, through the owner account that sent it.
        assert await recovery.end_cards_without_sender() == 1
        assert await recovery.end_cards_without_sender() == 0
        card = await store.principal(_ROUTER).approvals.approval_card(room_id=_ROOM, card_event_id="$waiting-0-0")
        assert isinstance(card, ApprovalCardView)
        assert (card.decided, card.retired) == (True, True)
        assert [delivery.stage for delivery in sent] == [DeliveryStage.FINAL]

        # The plugin hook that named the router runs once per message, whichever bots see the message.
        calls: list[str] = []

        @hook(EVENT_MESSAGE_RECEIVED, agents=(ROUTER_AGENT_NAME,))
        async def once_per_message(ctx: MessageReceivedContext) -> None:
            calls.append(ctx.envelope.agent_name)

        hooks = _stewarded_registry([once_per_message], {"router": 100, "general": 0, "helper": 0}, store)
        for agent_name in ("general", "helper", "router"):
            await _emit_message_as(hooks, tmp_path, agent_name)
        assert calls == ["general"]
        assert len(alias_warnings) == 1
    finally:
        await manager.shutdown()
        await store.close()

    await _the_routers_schedule_fires_through_an_agent(config)
    await _the_thread_reads_as_before(config, tmp_path)
