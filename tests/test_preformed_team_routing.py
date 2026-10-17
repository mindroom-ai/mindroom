"""Tests for predefined team mentions and routing behavior.

These tests ensure that mentioning a predefined team:
- Does NOT trigger router routing
- Does cause the TeamBot to respond using its configured team members
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
from mindroom.constants import STREAM_STATUS_KEY
from mindroom.matrix.client import DeliveredMatrixEvent
from mindroom.matrix.thread_history_result import thread_history_result
from mindroom.matrix.users import AgentMatrixUser
from mindroom.response_runner import ResponseRequest
from mindroom.response_sources import ResponseSources
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, serialize_tool_execution_identity
from mindroom.turn_origin import TurnIntent
from tests.access_schema_support import with_current_room_member_access
from tests.approval_continuation_helpers import approval_continuation
from tests.bot_helpers import make_test_agent_bot, make_test_team_bot
from tests.conftest import (
    bind_runtime_paths,
    drain_coalescing,
    install_runtime_journal_support,
    make_matrix_client_mock,
    make_visible_message,
    message_origin,
    patch_response_runner_module,
    request_envelope,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.identity_helpers import entity_ids
from tests.response_attempt_helpers import install_direct_response_admission

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from contextvars import Context
    from pathlib import Path

    from mindroom.final_delivery import FinalDeliveryOutcome
    from mindroom.history.turn_recorder import TurnRecorder


def _bind_runtime_paths(config: Config, tmp_path: Path) -> Config:
    return bind_runtime_paths(config, test_runtime_paths(tmp_path))


async def _empty_event_iterator() -> AsyncGenerator[object, None]:
    if False:
        yield None


@asynccontextmanager
async def _noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncGenerator[None]:
    yield


def _make_matrix_client_mock() -> AsyncMock:
    client = make_matrix_client_mock()
    client.room_get_event_relations = MagicMock(return_value=_empty_event_iterator())
    return client


@pytest.fixture
def config_with_team() -> Config:
    """Minimal config with two agents and one predefined team in a room."""
    return with_current_room_member_access(
        Config(
            agents={
                "a1": AgentConfig(display_name="Agent One", role="", rooms=["room_x"]),
                "a2": AgentConfig(display_name="Agent Two", role="", rooms=["room_x"]),
            },
            teams={
                "t1": TeamConfig(
                    display_name="Team One",
                    role="Test preformed team",
                    agents=["a1", "a2"],
                    rooms=["room_x"],
                    mode="coordinate",
                ),
            },
            router=RouterConfig(model="default"),
        ),
    )


def _mock_room(room_id: str, member_ids: list[str]) -> MagicMock:
    room = MagicMock()
    room.room_id = room_id
    room.name = room_id
    room.users = {user_id: nio.MatrixUser(user_id) for user_id in member_ids}
    return room


def _mock_event_with_team_mention(team_user_id: str, body: str = "@team please help") -> MagicMock:
    ev = MagicMock()
    ev.sender = "@user:localhost"
    ev.body = body
    ev.event_id = "$evt1"
    ev.source = {
        "content": {
            "body": body,
            "m.mentions": {"user_ids": [team_user_id]},
        },
    }
    return ev


def _handled_response_event_id(outcome: FinalDeliveryOutcome) -> str | None:
    return outcome.event_id if outcome.mark_handled and outcome.is_visible_response and not outcome.suppressed else None


@pytest.mark.asyncio
async def test_router_does_not_route_when_preformed_team_is_mentioned(config_with_team: Config, tmp_path: Path) -> None:
    """Router must not route if the message mentions a predefined team."""
    config_with_team = _bind_runtime_paths(config_with_team, tmp_path)
    runtime_paths = runtime_paths_for(config_with_team)
    ids = entity_ids(config_with_team, runtime_paths)
    # Router bot setup
    # Use config-derived IDs to match domain in this environment
    router_user = AgentMatrixUser(
        agent_name="router",
        user_id=ids["router"].full_id,
        display_name="Router",
        password="p",  # noqa: S106
    )
    router = make_test_agent_bot(router_user, tmp_path, config_with_team, runtime_paths)
    router.client = _make_matrix_client_mock()
    install_runtime_journal_support(router)

    # Room has router + team + two agents and the human user
    team_user_id = ids["t1"].full_id
    a1_id = ids["a1"].full_id
    a2_id = ids["a2"].full_id
    room = _mock_room("!room:localhost", [router_user.user_id, team_user_id, a1_id, a2_id, "@user:localhost"])

    # Event mentions the team
    event = _mock_event_with_team_mention(team_user_id)

    # Also patch suggest_responder_for_message to detect accidental routing
    with patch("mindroom.router_relay.suggest_responder_for_message", new=AsyncMock(return_value="a1")):
        await router._on_message(room, event)

    # Router must not send any message (i.e., must not route)
    router.client.room_send.assert_not_called()


@pytest.mark.asyncio
async def test_preformed_team_bot_responds_when_mentioned(config_with_team: Config, tmp_path: Path) -> None:
    """TeamBot should respond with team response when the team is mentioned."""
    config_with_team = _bind_runtime_paths(config_with_team, tmp_path)
    runtime_paths = runtime_paths_for(config_with_team)
    ids = entity_ids(config_with_team, runtime_paths)
    team_user = AgentMatrixUser(
        agent_name="t1",
        user_id=ids["t1"].full_id,
        display_name="Team One",
        password="p",  # noqa: S106
    )
    bot = make_test_team_bot(
        agent_user=team_user,
        storage_path=tmp_path,
        config=config_with_team,
        runtime_paths=runtime_paths,
        rooms=["!room:localhost"],
        enable_streaming=False,
    )
    install_direct_response_admission(bot)
    bot.client = _make_matrix_client_mock()
    install_runtime_journal_support(bot)

    async def fake_team_response(*_args: Any, **_kwargs: Any) -> str:  # noqa: ANN401
        return "🤝 Team Response (a1, a2):\n\n**a1**: ok\n\n**a2**: ok"

    # Minimal orchestrator stub is fine because we patch team_response
    bot.orchestrator = MagicMock()

    team_user_id = ids["t1"].full_id
    room = _mock_room("!room:localhost", [team_user_id, "@user:localhost"])
    event = _mock_event_with_team_mention(team_user_id)

    bot.client.room_send.side_effect = [
        nio.RoomSendResponse.from_dict({"event_id": "$placeholder"}, room.room_id),
        nio.RoomSendResponse.from_dict({"event_id": "$edit"}, room.room_id),
    ]
    with patch_response_runner_module(
        team_response=fake_team_response,
        should_use_streaming=AsyncMock(return_value=False),
        typing_indicator=_noop_typing_indicator,
    ):
        await bot._on_message(room, event)
        await drain_coalescing(bot)

    # Team bot should send a visible pending placeholder and the final team message.
    sent_contents = [call.kwargs["content"] for call in bot.client.room_send.call_args_list]
    assert len(sent_contents) == 2
    assert sent_contents[0][STREAM_STATUS_KEY] == "pending"
    assert sent_contents[0]["body"].startswith("🤝 Team Response: Thinking...")
    assert sent_contents[1]["m.relates_to"]["rel_type"] == "m.replace"
    assert sent_contents[1]["m.new_content"]["body"] == "🤝 Team Response (a1, a2):\n\n**a1**: ok\n\n**a2**: ok"
    bot.client.room_send.side_effect = None


async def _team_memory_saves(
    config: Config,
    tmp_path: Path,
    *,
    entity_name: str = "t1",
    completes: bool = True,
    edited: bool = False,
) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    """Answer one message as ``entity_name`` with file-memory members and return the conversation saves it queued."""
    config = _bind_runtime_paths(config, tmp_path)
    runtime_paths = runtime_paths_for(config)
    ids = entity_ids(config, runtime_paths)
    config.memory.backend = "mem0"
    config.agents["a1"].memory_backend = "file"
    config.agents["a2"].memory_backend = "file"
    user = AgentMatrixUser(
        agent_name=entity_name,
        user_id=ids[entity_name].full_id,
        display_name=entity_name,
        password="p",  # noqa: S106
    )
    make_bot = make_test_team_bot if entity_name in config.teams else make_test_agent_bot
    bot = make_bot(
        agent_user=user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths,
        rooms=["!room:localhost"],
        enable_streaming=False,
    )
    bot.client = _make_matrix_client_mock()
    install_runtime_journal_support(bot)
    bot.orchestrator = MagicMock()

    store_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    scheduled_tasks: list[asyncio.Task[Any]] = []

    async def fake_store_conversation_memory(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        store_calls.append((args, kwargs))

    def schedule_background_task(
        coro: object,
        name: str | None = None,
        error_handler: object | None = None,  # noqa: ARG001
        owner: object | None = None,  # noqa: ARG001
        context: Context | None = None,
    ) -> asyncio.Task[Any]:
        assert asyncio.iscoroutine(coro)
        task = asyncio.create_task(coro, name=name, context=context)
        scheduled_tasks.append(task)
        return task

    async def team_response(*_args: Any, turn_recorder: TurnRecorder, **_kwargs: Any) -> str:  # noqa: ANN401
        if completes:
            turn_recorder.mark_completed()
        return "team response"

    request = ResponseRequest(
        sources=ResponseSources(pending_event_ids=("$evt1",), logical_source_event_ids=("$evt1",)),
        prompt="@team remember this",
        thread_history=[make_visible_message(sender="@bob:localhost", body="Earlier note", event_id="$bob1")],
        user_id="@user:localhost",
        response_envelope=request_envelope(
            room_id="!room:localhost",
            reply_to_event_id="$evt1",
            prompt="@team remember this",
            user_id="@user:localhost",
            agent_name=bot.agent_name,
        ),
    )
    with patch_response_runner_module(
        should_use_streaming=AsyncMock(return_value=False),
        typing_indicator=_noop_typing_indicator,
        team_response=team_response,
        store_conversation_memory=fake_store_conversation_memory,
        create_background_task=schedule_background_task,
    ):
        bot.client.room_send.side_effect = [
            nio.RoomSendResponse.from_dict({"event_id": "$placeholder"}, "!room:localhost"),
            nio.RoomSendResponse.from_dict({"event_id": "$edit"}, "!room:localhost"),
        ]
        if edited:
            await bot._run_regenerated_response(request)
        else:
            await bot._response_runner.generate_team_response_helper(
                request,
                team_agents=[ids["a1"], ids["a2"]],
                team_mode="coordinate",
            )

    if scheduled_tasks:
        await asyncio.gather(*scheduled_tasks)
    return store_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("edited", [False, True])
async def test_completed_team_reply_saves_the_conversation_to_its_members_memory(
    config_with_team: Config,
    tmp_path: Path,
    edited: bool,
) -> None:
    """A configured team's reply, first or after an edit, saves the conversation once for all its members."""
    store_calls = await _team_memory_saves(config_with_team, tmp_path, edited=edited)

    [(args, kwargs)] = store_calls
    assert args[0] == "@team remember this"
    assert args[1] == ["a1", "a2"]
    assert [message.body for message in args[6]] == ["Earlier note"]
    assert kwargs["execution_identity"].requester_id == "@user:localhost"


@pytest.mark.asyncio
async def test_team_reply_that_did_not_complete_saves_no_memory(config_with_team: Config, tmp_path: Path) -> None:
    """Like an agent's, a team's conversation is saved only once its reply completes."""
    assert await _team_memory_saves(config_with_team, tmp_path, completes=False) == []


@pytest.mark.asyncio
async def test_agent_led_team_reply_saves_no_team_memory(config_with_team: Config, tmp_path: Path) -> None:
    """Only a configured team keeps shared memory; an agent answering with other agents adds none."""
    assert await _team_memory_saves(config_with_team, tmp_path, entity_name="a1") == []


@pytest.mark.asyncio
async def test_team_reply_resumed_after_approval_saves_the_conversation(
    config_with_team: Config,
    tmp_path: Path,
) -> None:
    """A team reply that waited for a tool approval saves its conversation when it completes."""
    config = _bind_runtime_paths(config_with_team, tmp_path)
    runtime_paths = runtime_paths_for(config)
    ids = entity_ids(config, runtime_paths)
    bot = make_test_team_bot(
        agent_user=AgentMatrixUser(agent_name="t1", user_id=ids["t1"].full_id, display_name="t1", password="p"),  # noqa: S106
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths,
        rooms=["!room:localhost"],
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="t1",
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    continuation = approval_continuation(
        entity_kind="team",
        entity_name="t1",
        team_member_names=("a1", "a2"),
        execution_identity=serialize_tool_execution_identity(identity),
        memory_prompt="@team remember this",
    )
    store_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def fake_store_conversation_memory(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        store_calls.append((args, kwargs))

    queue = bot._response_runner._approval_memory_persistence(continuation)
    assert queue is not None
    with patch_response_runner_module(store_conversation_memory=fake_store_conversation_memory):
        queue()
        await asyncio.sleep(0)

    [(args, _kwargs)] = store_calls
    assert (args[0], args[1], args[3]) == ("@team remember this", ["a1", "a2"], "session-1")


@pytest.mark.asyncio
async def test_preformed_team_rejection_edits_existing_message(config_with_team: Config, tmp_path: Path) -> None:
    """Configured-team rejection during regeneration should edit the existing response."""
    config_with_team = _bind_runtime_paths(config_with_team, tmp_path)
    runtime_paths = runtime_paths_for(config_with_team)
    ids = entity_ids(config_with_team, runtime_paths)
    team_user = AgentMatrixUser(
        agent_name="t1",
        user_id=ids["t1"].full_id,
        display_name="Team One",
        password="p",  # noqa: S106
    )
    bot = make_test_team_bot(
        agent_user=team_user,
        storage_path=tmp_path,
        config=config_with_team,
        runtime_paths=runtime_paths,
        rooms=["!room:localhost"],
        enable_streaming=False,
    )
    install_direct_response_admission(bot)
    bot.client = _make_matrix_client_mock()
    install_runtime_journal_support(bot)
    bot.orchestrator = MagicMock()
    bot.orchestrator.agent_bots = {"a1": MagicMock()}

    with patch(
        "mindroom.delivery_gateway.send_message_outcome",
        new=AsyncMock(
            return_value=DeliveredMatrixEvent(
                event_id="$existing_response",
                content_sent={"body": "Team 't1' includes agent 'a2' that could not be materialized for this request."},
            ),
        ),
    ) as mock_edit:
        resolution = await bot._run_regenerated_response(
            ResponseRequest(
                sources=ResponseSources(
                    pending_event_ids=("$evt1",),
                    logical_source_event_ids=("$evt1",),
                ),
                prompt="@t1 please retry",
                thread_history=[],
                existing_event_id="$existing_response",
                user_id="@user:localhost",
                response_envelope=request_envelope(
                    room_id="!room:localhost",
                    reply_to_event_id="$evt1",
                    prompt="@t1 please retry",
                    user_id="@user:localhost",
                    agent_name=bot.agent_name,
                ),
            ),
        )

    assert resolution == "$existing_response"
    # The outbox sends the finished replace event, so the edit target and its
    # text are read out of the envelope rather than off the call.
    envelope = mock_edit.await_args.args[2]
    assert envelope["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$existing_response"}
    assert (
        envelope["m.new_content"]["body"]
        == "Team 't1' includes agent 'a2' that could not be materialized for this request."
    )


@pytest.mark.asyncio
async def test_preformed_team_plain_reply_does_not_continue_existing_thread_root(
    config_with_team: Config,
    tmp_path: Path,
) -> None:
    """TeamBot should answer the prompt event instead of following a stale plain-reply target."""
    config_with_team = _bind_runtime_paths(config_with_team, tmp_path)
    runtime_paths = runtime_paths_for(config_with_team)
    ids = entity_ids(config_with_team, runtime_paths)
    team_user = AgentMatrixUser(
        agent_name="t1",
        user_id=ids["t1"].full_id,
        display_name="Team One",
        password="p",  # noqa: S106
    )
    bot = make_test_team_bot(
        agent_user=team_user,
        storage_path=tmp_path,
        config=config_with_team,
        runtime_paths=runtime_paths,
        rooms=["!room:localhost"],
        enable_streaming=False,
    )
    bot.client = _make_matrix_client_mock()
    install_runtime_journal_support(bot)
    bot.orchestrator = MagicMock()

    team_user_id = ids["t1"].full_id
    room = _mock_room("!room:localhost", [team_user_id, "@user:localhost"])
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$evt_plain_reply",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567890,
            "room_id": room.room_id,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.text",
                "body": "@t1 please continue",
                "m.mentions": {"user_ids": [team_user_id]},
                "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_msg"}},
            },
        },
    )

    async def fake_team_response(*_args: Any, **_kwargs: Any) -> str:  # noqa: ANN401
        return "🤝 Team Response (a1, a2):\n\n**a1**: ok\n\n**a2**: ok"

    with (
        patch_response_runner_module(
            team_response=fake_team_response,
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing_indicator,
        ),
        patch.object(
            bot._conversation_resolver,
            "fetch_thread_history",
            new=AsyncMock(return_value=thread_history_result([], is_full_history=True)),
        ),
    ):
        await bot._on_message(room, event)
        await drain_coalescing(bot)

    assert bot.client.room_send.call_count >= 1
    first_content = bot.client.room_send.call_args_list[0].kwargs["content"]
    assert first_content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$evt_plain_reply"
    assert first_content["m.relates_to"]["rel_type"] == "m.thread"
    assert first_content["m.relates_to"]["event_id"] == "$evt_plain_reply"


@pytest.mark.asyncio
async def test_team_does_not_respond_to_different_domain_mention(config_with_team: Config, tmp_path: Path) -> None:
    """TeamBot should NOT respond to mentions of the same username on a different domain.

    This is a security test - @mindroom_t1:evil.org should not trigger @mindroom_t1:localhost.
    """
    config_with_team = _bind_runtime_paths(config_with_team, tmp_path)
    runtime_paths = runtime_paths_for(config_with_team)
    ids = entity_ids(config_with_team, runtime_paths)
    team_user = AgentMatrixUser(
        agent_name="t1",
        user_id=ids["t1"].full_id,
        display_name="Team One",
        password="p",  # noqa: S106
    )
    bot = make_test_team_bot(
        agent_user=team_user,
        storage_path=tmp_path,
        config=config_with_team,
        runtime_paths=runtime_paths,
        rooms=["!room:localhost"],
        enable_streaming=False,
    )
    bot.client = _make_matrix_client_mock()
    install_runtime_journal_support(bot)
    bot.orchestrator = MagicMock()

    async def fake_team_response(*_args: Any, **_kwargs: Any) -> str:  # noqa: ANN401
        return "🤝 Team Response (a1, a2): ok"

    # Craft a mention using a DIFFERENT domain than the bot's MatrixID
    # This simulates someone trying to impersonate the team
    other_domain = "evil.org"
    if team_user.matrix_id.domain == other_domain:
        other_domain = "attacker.com"
    mentioned_id = f"@mindroom_t1:{other_domain}"

    room = _mock_room("!room:localhost", [team_user.user_id, "@user:localhost"])
    event = _mock_event_with_team_mention(mentioned_id, body=f"{mentioned_id} ping")

    with patch_response_runner_module(
        team_response=fake_team_response,
        should_use_streaming=AsyncMock(return_value=False),
        typing_indicator=_noop_typing_indicator,
    ):
        await bot._on_message(room, event)

    # Team bot should NOT have responded - different domain!
    assert bot.client.room_send.call_count == 0
    config_with_team = _bind_runtime_paths(config_with_team, tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(("backend", "saves"), [("mem0", 1), ("file", 0)])
async def test_agent_reply_saves_its_conversation_only_with_mem0(
    config_with_team: Config,
    tmp_path: Path,
    backend: str,
    saves: int,
) -> None:
    """A Mem0 agent saves each completed conversation; a file-memory agent leaves it to auto-flush."""
    config = _bind_runtime_paths(config_with_team, tmp_path)
    config.agents["a1"].memory_backend = backend
    runtime_paths = runtime_paths_for(config)
    ids = entity_ids(config, runtime_paths)
    bot = make_test_agent_bot(
        agent_user=AgentMatrixUser(agent_name="a1", user_id=ids["a1"].full_id, display_name="a1", password="p"),  # noqa: S106
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths,
        rooms=["!room:localhost"],
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="a1",
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-1",
    )
    store = AsyncMock()
    mark_dirty = MagicMock()
    queue = bot._response_runner._memory_persistence(
        agent_name="a1",
        session_id="session-1",
        execution_identity=identity,
        prompt="remember this",
        thread_history=[],
        user_id="@user:localhost",
        hook_source=None,
        origin=message_origin(),
    )
    assert queue is not None
    with patch_response_runner_module(store_conversation_memory=store, mark_auto_flush_dirty_session=mark_dirty):
        queue()
        await asyncio.sleep(0)

    assert store.await_count == saves
    mark_dirty.assert_called_once()


@pytest.mark.parametrize("agent_name", ["a1", ["a1", "a2"]], ids=["agent", "team"])
def test_job_follow_up_saves_no_memory(
    config_with_team: Config,
    tmp_path: Path,
    agent_name: str | list[str],
) -> None:
    """A job follow-up's prompt is MindRoom's instruction to collect results, not part of the conversation."""
    config = _bind_runtime_paths(config_with_team, tmp_path)
    runtime_paths = runtime_paths_for(config)
    ids = entity_ids(config, runtime_paths)
    bot = make_test_team_bot(
        agent_user=AgentMatrixUser(agent_name="t1", user_id=ids["t1"].full_id, display_name="t1", password="p"),  # noqa: S106
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths,
        rooms=["!room:localhost"],
    )
    follow_up = replace(message_origin(), intent=TurnIntent.JOB_FOLLOW_UP)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="t1",
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )

    assert (
        bot._response_runner._memory_persistence(
            agent_name=agent_name,
            session_id="session-1",
            execution_identity=identity,
            prompt="Background jobs you started earlier ended.",
            thread_history=[],
            user_id="@user:localhost",
            hook_source=None,
            origin=follow_up,
        )
        is None
    )
    continuation = approval_continuation(
        entity_kind="team",
        entity_name="t1",
        team_member_names=("a1", "a2"),
        execution_identity=serialize_tool_execution_identity(identity),
        origin=follow_up,
    )
    assert bot._response_runner._approval_memory_persistence(continuation) is None
