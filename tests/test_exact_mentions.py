"""Hooks and tools can name a message's exact mentions, so agents its text only quotes are not mentioned and form no team."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import EXACT_MENTIONS_KEY, ROUTER_AGENT_NAME
from mindroom.hooks import (
    EVENT_AGENT_STARTED,
    EVENT_SCHEDULE_FIRED,
    HookContext,
    HookContextSupport,
    HookRegistry,
    ScheduleFiredContext,
)
from mindroom.hooks.registry import HookRegistryState
from mindroom.hooks.sender import build_hook_message_sender
from mindroom.logging_config import get_logger
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.large_messages import prepare_large_message
from mindroom.matrix.users import AgentMatrixUser
from mindroom.scheduling.tasks import ScheduledWorkflow
from mindroom.teams.formation import TeamOutcome, decide_team_formation
from mindroom.thread_utils import check_agent_mentioned, get_all_mentioned_agents_in_thread
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    create_mock_room,
    delivered_matrix_side_effect,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    make_visible_message,
    runtime_paths_for,
    sync_bot_runtime_state,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from pathlib import Path

ALPHA = "@mindroom_alpha:localhost"
BETA = "@mindroom_beta:localhost"
ROUTER = "@mindroom_router:localhost"
ALICE = "@alice:localhost"
CARD = "Review card: @alpha and @beta signed off"

# The bot's own sender, which tools also get as `context.hooks.message_sender`, and the scheduler's.
_Route = Literal["bot", "schedule"]


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Return a config with two agents in one room."""
    return bind_runtime_paths(
        Config(
            agents={
                "alpha": AgentConfig(display_name="Alpha", rooms=["!room:localhost"]),
                "beta": AgentConfig(display_name="Beta", rooms=["!room:localhost"]),
            },
        ),
        test_runtime_paths(tmp_path),
    )


def _hook_context(config: Config, route: _Route, client: AsyncMock) -> HookContext:
    """Return a hook context whose sender is the bot's own, or a schedule hook's whose sender is the scheduler's."""
    runtime_paths = runtime_paths_for(config)
    reader = make_conversation_reader_mock()
    base: dict[str, Any] = {"config": config, "runtime_paths": runtime_paths, "correlation_id": "corr"}
    if route == "bot":
        support = HookContextSupport(
            runtime=SimpleNamespace(client=client, config=config),
            runtime_paths=runtime_paths,
            agent_name=ROUTER_AGENT_NAME,
            hook_registry_state=HookRegistryState(HookRegistry.empty()),
            conversation_reader=reader,
            logger=get_logger("tests.exact_mentions"),
        )
        return HookContext(event_name=EVENT_AGENT_STARTED, message_sender=support.send_message, **base)
    return ScheduleFiredContext(
        event_name=EVENT_SCHEDULE_FIRED,
        message_sender=build_hook_message_sender(client, config, runtime_paths, conversation_reader=reader),
        task_id="task",
        workflow=ScheduledWorkflow(schedule_type="once", message="check in", description="check in"),
        room_id="!room:localhost",
        thread_id=None,
        created_by=None,
        message_text="check in",
        **base,
    )


async def _sent_card(
    config: Config,
    route: _Route,
    mentions: list[str] | None,
    *,
    wake_mentioned: bool = True,
) -> dict[str, Any]:
    """Send the card from a hook with ``mentions`` and ``wake_mentioned`` and return the Matrix content it sent."""
    client = make_matrix_client_mock(user_id=ROUTER)
    transport = AsyncMock(side_effect=delivered_matrix_side_effect("$card"))
    with patch("mindroom.hooks.sender.send_matrix_message", new=transport):
        event_id = await _hook_context(config, route, client).send_message(
            "!room:localhost",
            CARD,
            mentions=mentions,
            wake_mentioned=wake_mentioned,
        )
    assert event_id == "$card"
    sent_client, room_id, content = transport.await_args.args
    assert (sent_client, room_id) == (client, "!room:localhost")
    return content


def _mentioned(config: Config, content: dict[str, Any]) -> list[str]:
    """Return whom an agent reading ``content`` takes it to mention."""
    mentioned_agents, _, _ = check_agent_mentioned(
        {"content": content},
        None,
        config,
        runtime_paths_for(config),
        room=create_mock_room(config=config),
    )
    return [agent.full_id for agent in mentioned_agents]


def _mentioned_in_thread(config: Config, content: dict[str, Any]) -> list[MatrixID]:
    """Return the agents a thread holding ``content`` counts as mentioned when it forms a team."""
    message = make_visible_message(sender=ROUTER, body=content["body"], content=content)
    return get_all_mentioned_agents_in_thread([message], config, runtime_paths_for(config))


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["bot", "schedule"])
@pytest.mark.parametrize(
    ("mentions", "expected"),
    [(None, [ALPHA, BETA]), ([], []), ([ALPHA], [ALPHA])],
    ids=["parsed", "nobody", "one"],
)
async def test_hook_card_mentions_only_whom_the_hook_names(
    config: Config,
    route: _Route,
    mentions: list[str] | None,
    expected: list[str],
) -> None:
    """Agents a card only quotes are mentioned neither in its message nor later in its thread, so no team forms."""
    content = await _sent_card(config, route, mentions)

    # The quoted names still render as pills.
    assert f"https://matrix.to/#/{ALPHA}" in content["formatted_body"]
    assert f"https://matrix.to/#/{BETA}" in content["formatted_body"]
    assert _mentioned(config, content) == expected
    in_thread = _mentioned_in_thread(config, content)
    assert [agent.full_id for agent in in_thread] == expected
    team = decide_team_formation(
        [],
        [],
        in_thread,
        runtime_paths_for(config),
        config,
        is_dm_room=False,
        is_thread=True,
        available_responders_in_room=in_thread,
        materializable_agent_names={"alpha", "beta"},
    )
    assert (team.outcome is TeamOutcome.TEAM) is (len(expected) > 1)


def _room_with(*user_ids: str) -> nio.MatrixRoom:
    """Return a room whose complete membership is ``user_ids``."""
    room = nio.MatrixRoom("!room:localhost", ROUTER)
    for user_id in user_ids:
        room.add_member(user_id, None, None)
    room.members_synced = True
    return room


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["bot", "schedule"])
@pytest.mark.parametrize("wake_mentioned", [True, False])
@pytest.mark.parametrize(
    "mentions",
    [None, [ALICE, ALPHA], [BETA]],
    ids=["parsed", "person-and-agent", "other-agent"],
)
async def test_card_that_wakes_no_one_keeps_its_mentions_but_activates_no_agent(
    config: Config,
    route: _Route,
    wake_mentioned: bool,
    mentions: list[str] | None,
) -> None:
    """A card sent with wake_mentioned=False mentions the same people, but no agent counts as mentioned, now or later.

    A mentioned person still keeps agents from answering, and an agent the card names does not stop another one
    from continuing, so only the agents' activation by mention changes.
    """
    content = await _sent_card(config, route, mentions, wake_mentioned=wake_mentioned)

    listed = [ALPHA, BETA] if mentions is None else mentions
    assert content["m.mentions"] == {"user_ids": listed}
    mentioned_agents, am_i_mentioned, has_non_agent_mentions = check_agent_mentioned(
        {"content": content},
        MatrixID.parse(ALPHA),
        config,
        runtime_paths_for(config),
        room=_room_with(ALICE, ALPHA, BETA, ROUTER),
    )
    woken = [user_id for user_id in listed if user_id != ALICE] if wake_mentioned else []
    assert [agent.full_id for agent in mentioned_agents] == woken
    assert am_i_mentioned is (ALPHA in woken)
    assert has_non_agent_mentions is (ALICE in listed)
    assert [agent.full_id for agent in _mentioned_in_thread(config, content)] == woken


@pytest.mark.asyncio
@pytest.mark.parametrize("wake_mentioned", [True, False])
async def test_agent_reading_a_card_that_wakes_no_one_is_not_mentioned_but_sees_the_person(
    config: Config,
    wake_mentioned: bool,
) -> None:
    """The agent's own reading of the card leaves it unmentioned and still sees the person the card mentions."""
    content = await _sent_card(config, "bot", [ALICE, ALPHA], wake_mentioned=wake_mentioned)
    runtime_paths = runtime_paths_for(config)
    bot = make_test_agent_bot(
        AgentMatrixUser(agent_name="alpha", user_id=ALPHA, display_name="Alpha", password=TEST_PASSWORD),
        config=config,
        runtime_paths=runtime_paths,
    )
    bot.client = AsyncMock()
    bot.client.user_id = ALPHA
    sync_bot_runtime_state(bot)
    event = nio.RoomMessageText.from_dict(
        {
            "content": content,
            "sender": ROUTER,
            "event_id": "$card",
            "room_id": "!room:localhost",
            "origin_server_ts": 1,
        },
    )

    context = await bot._conversation_resolver.extract_message_context(
        _room_with(ALICE, ALPHA, BETA, ROUTER),
        event,
        thread_id=None,
    )

    assert context.am_i_mentioned is wake_mentioned
    assert [agent.full_id for agent in context.mentioned_agents] == ([ALPHA] if wake_mentioned else [])
    assert context.has_non_agent_mentions is True


@pytest.mark.asyncio
async def test_long_card_keeps_its_exact_mentions_on_the_preview(config: Config) -> None:
    """A card too long for one event shows a preview that still mentions only whom the hook named."""
    client = MagicMock(spec=nio.AsyncClient)
    client.upload.return_value = (nio.UploadResponse("mxc://server/sidecar"), None)
    content = {
        "msgtype": "m.text",
        "body": CARD * 3000,
        "m.mentions": {"user_ids": []},
        EXACT_MENTIONS_KEY: True,
    }

    preview = await prepare_large_message(client, "!room:localhost", content, room_encrypted=False)

    assert preview["body"] != content["body"]
    assert preview[EXACT_MENTIONS_KEY] is True
    assert _mentioned(config, preview) == []
