"""`!compact` asks the agents and teams of one conversation to compact their context before their next reply."""

from __future__ import annotations

# ruff: noqa: D103
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.commands.compact_commands import handle_compact_command
from mindroom.commands.handler import COMMAND_TYPES_WITH_SIDE_EFFECTS, handle_command
from mindroom.commands.parsing import CommandType, _CommandParser, get_command_help
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig, DefaultsConfig, ModelConfig
from mindroom.history.session_context import create_scope_session_storage
from mindroom.history.storage import has_queued_force_compaction, new_scope_session
from mindroom.history.types import HistoryScope
from mindroom.message_target import MessageTarget
from mindroom.tool_system.worker_routing import build_tool_execution_identity
from tests.authorization_helpers import isolated_membership_index, make_test_command_handler_context
from tests.conftest import bind_runtime_paths, make_conversation_reader_mock, seed_session_row
from tests.history_helpers import _runtime_paths

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

ALICE = "@alice:example.test"
ROOM = "!room:example.test"
THREAD = "$thread:example.test"


def _config(tmp_path: Path, *, context_window: int | None = 48_000) -> tuple[Config, RuntimePaths]:
    paths = _runtime_paths(tmp_path)
    alice = ResponderAccessConfig(users=[ALICE])
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(display_name="Code", access=alice),
                "docs": AgentConfig(display_name="Docs", access=alice),
                "lobby": AgentConfig(display_name="Lobby", access=alice, thread_mode="room"),
                "secret": AgentConfig(display_name="Secret", access=ResponderAccessConfig(users=["@bob:example.test"])),
            },
            teams={"squad": TeamConfig(display_name="Squad", role="Pair up", agents=["code", "docs"], access=alice)},
            defaults=DefaultsConfig(tools=[], compaction=CompactionConfig()),
            models={"default": ModelConfig(provider="openai", id="test-model", context_window=context_window)},
        ),
        paths,
    )
    return config, paths


def _scope(config: Config, name: str) -> HistoryScope:
    return HistoryScope(kind="team" if name in config.teams else "agent", scope_id=name)


def _target(*, in_thread: bool = True, room_mode: bool = False) -> MessageTarget:
    return MessageTarget.resolve(ROOM, THREAD if in_thread else None, "$command", room_mode=room_mode)


def _storage(config: Config, paths: RuntimePaths, name: str, *, requester_id: str = ALICE, room_mode: bool = False):  # noqa: ANN202
    target = _target(room_mode=room_mode)
    identity = build_tool_execution_identity(
        channel="matrix",
        agent_name=name,
        runtime_paths=paths,
        requester_id=requester_id,
        room_id=ROOM,
        thread_id=target.resolved_thread_id,
        resolved_thread_id=target.resolved_thread_id,
        session_id=target.session_id,
    )
    return create_scope_session_storage(
        scope=_scope(config, name),
        config=config,
        runtime_paths=paths,
        execution_identity=identity,
    )


def _seed(config: Config, paths: RuntimePaths, name: str, *, room_mode: bool = False) -> None:
    session_id = _target(room_mode=room_mode).session_id
    seed_session_row(
        _storage(config, paths, name, room_mode=room_mode),
        new_scope_session(session_id=session_id, scope=_scope(config, name)),
    )


def _queued(
    config: Config,
    paths: RuntimePaths,
    name: str,
    *,
    requester_id: str = ALICE,
    room_mode: bool = False,
) -> bool:
    session_id = _target(room_mode=room_mode).session_id
    storage = _storage(config, paths, name, requester_id=requester_id, room_mode=room_mode)
    return has_queued_force_compaction(storage, session_id)


async def _compact(
    config: Config,
    paths: RuntimePaths,
    args_text: str,
    *,
    target: MessageTarget | None = None,
    requester_id: str = ALICE,
) -> str:
    return await handle_compact_command(
        args_text,
        config=config,
        runtime_paths=paths,
        target=target or _target(),
        requester_id=requester_id,
        membership_index=isolated_membership_index(),
    )


@pytest.mark.parametrize("text", ["!compact", "/compact", "!COMPACT", "!compact code", "/Compact  code "])
def test_compact_parses_with_an_optional_entity(text: str) -> None:
    command = _CommandParser().parse(text)
    assert command is not None
    assert command.type == CommandType.COMPACT
    assert command.args == {"args_text": "code" if "code" in text else ""}


def test_compact_is_a_listed_side_effect_command() -> None:
    assert CommandType.COMPACT in COMMAND_TYPES_WITH_SIDE_EFFECTS
    assert "/compact [agent_or_team]" in get_command_help()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["code", "squad"])
async def test_named_entity_compacts_before_its_next_reply_in_this_thread(tmp_path: Path, name: str) -> None:
    config, paths = _config(tmp_path)
    result = await _compact(config, paths, name)
    assert result == f"✅ `{name}` compacts its context before its next reply in this thread."
    assert _queued(config, paths, name)


@pytest.mark.asyncio
async def test_named_room_mode_agent_compacts_the_room_session(tmp_path: Path) -> None:
    config, paths = _config(tmp_path)
    result = await _compact(config, paths, "lobby", target=_target(in_thread=False))
    assert result == "✅ `lobby` compacts its context before its next reply in this room."
    assert _queued(config, paths, "lobby", room_mode=True)


@pytest.mark.asyncio
async def test_named_thread_agent_outside_a_thread_is_refused(tmp_path: Path) -> None:
    config, paths = _config(tmp_path)
    result = await _compact(config, paths, "code", target=_target(in_thread=False))
    assert "inside an existing thread" in result
    assert not _queued(config, paths, "code")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args_text", "expected"),
    [
        ("secret", "Access denied for `secret`."),
        ("nobody", "Unknown agent or team `nobody`."),
        ("code docs", "Use `/compact [agent_or_team]`."),
    ],
)
async def test_named_entity_refusals_set_no_flag(tmp_path: Path, args_text: str, expected: str) -> None:
    config, paths = _config(tmp_path)
    assert await _compact(config, paths, args_text) == expected
    assert not any(_queued(config, paths, name) for name in ("code", "docs", "secret"))


@pytest.mark.asyncio
async def test_unavailable_compaction_reports_the_reason(tmp_path: Path) -> None:
    config, paths = _config(tmp_path, context_window=None)
    result = await _compact(config, paths, "code")
    assert result == (
        "❌ `code` cannot compact its context because no context_window is configured on the active model."
    )
    assert not _queued(config, paths, "code")


@pytest.mark.asyncio
async def test_private_agent_compacts_only_the_requesters_own_store(tmp_path: Path) -> None:
    config, paths = _config(tmp_path)
    config.agents["code"].private = AgentPrivateConfig(per="user")
    config.agents["code"].access = ResponderAccessConfig(users=[ALICE, "@bob:example.test"])
    assert (await _compact(config, paths, "code")).startswith("✅")
    assert _queued(config, paths, "code")
    assert not _queued(config, paths, "code", requester_id="@bob:example.test")


@pytest.mark.asyncio
async def test_bare_compact_flags_only_addressable_entities_that_spoke_here(tmp_path: Path) -> None:
    config, paths = _config(tmp_path)
    for name in ("code", "squad", "secret"):
        _seed(config, paths, name)
    result = await _compact(config, paths, "")
    assert result.splitlines() == [
        "✅ `code` compacts its context before its next reply in this thread.",
        "✅ `squad` compacts its context before its next reply in this thread.",
    ]
    assert _queued(config, paths, "code")
    assert _queued(config, paths, "squad")
    assert not _queued(config, paths, "secret")
    # An entity that never spoke here gets no session.
    assert not _storage(config, paths, "docs").has_session(_target().session_id)


@pytest.mark.asyncio
async def test_bare_compact_outside_a_thread_reaches_room_mode_entities(tmp_path: Path) -> None:
    config, paths = _config(tmp_path)
    _seed(config, paths, "lobby", room_mode=True)
    result = await _compact(config, paths, "", target=_target(in_thread=False))
    assert result == "✅ `lobby` compacts its context before its next reply in this room."
    assert _queued(config, paths, "lobby", room_mode=True)


@pytest.mark.asyncio
async def test_bare_compact_without_sessions_says_nothing_to_compact(tmp_path: Path) -> None:
    config, paths = _config(tmp_path)
    assert await _compact(config, paths, "") == "Nothing to compact here."
    assert not _storage(config, paths, "code").has_session(_target().session_id)


def _event(body: str) -> nio.RoomMessageText:
    return nio.RoomMessageText.from_dict(
        {
            "event_id": "$command",
            "sender": ALICE,
            "origin_server_ts": 1,
            "type": "m.room.message",
            "content": {
                "body": body,
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": THREAD},
            },
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("acts_for_requester", [False, True])
async def test_handler_runs_compact_only_for_the_human_in_person(tmp_path: Path, acts_for_requester: bool) -> None:
    config, paths = _config(tmp_path)
    send_response = AsyncMock(return_value="$reply")
    record_command_result = AsyncMock()
    context = make_test_command_handler_context(
        client=MagicMock(),
        config=config,
        runtime_paths=paths,
        logger=MagicMock(),
        conversation_reader=make_conversation_reader_mock(),
        stable_target=MessageTarget.resolve(ROOM, THREAD, "$command"),
        record_handled_turn=AsyncMock(),
        record_command_result=record_command_result,
        send_response=send_response,
    )
    event = _event("!compact code")
    await handle_command(
        context=context,
        room=nio.MatrixRoom(ROOM, "@router:example.test"),
        event=event,
        command=_CommandParser().parse(event.body),
        requester_user_id=ALICE,
        acts_for_requester=acts_for_requester,
    )
    reply = send_response.await_args.args[0]
    if acts_for_requester:
        assert reply == "❌ Agents cannot run this command for you. Send it yourself."
        assert not _queued(config, paths, "code")
    else:
        assert reply == "✅ `code` compacts its context before its next reply in this thread."
        record_command_result.assert_awaited_once_with(reply)
        assert _queued(config, paths, "code")
