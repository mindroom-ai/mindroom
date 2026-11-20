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
from mindroom.config.budgets import BudgetsConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig, DefaultsConfig, ModelConfig, ModelPricing
from mindroom.history.session_context import create_scope_session_storage, scope_session_database_path
from mindroom.history.storage import has_queued_force_compaction, new_scope_session
from mindroom.history.types import HistoryScope
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.tool_system.worker_routing import build_tool_execution_identity
from tests import response_runner_helpers
from tests.authorization_helpers import isolated_membership_index, make_test_command_handler_context
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    make_conversation_reader_mock,
    seed_session_row,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)
from tests.history_helpers import _runtime_paths

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.response_runner import ResponseRunner

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
async def test_named_entity_compacts_before_its_next_reply_in_this_conversation(tmp_path: Path, name: str) -> None:
    config, paths = _config(tmp_path)
    _seed(config, paths, name)
    result = await _compact(config, paths, name)
    assert result == f"✅ `{name}` compacts its context before its next reply in this thread."
    assert _queued(config, paths, name)


@pytest.mark.asyncio
async def test_named_room_mode_agent_compacts_the_room_session(tmp_path: Path) -> None:
    config, paths = _config(tmp_path)
    _seed(config, paths, "lobby", room_mode=True)
    result = await _compact(config, paths, "lobby", target=_target(in_thread=False))
    assert result == "✅ `lobby` compacts its context before its next reply in this room."
    assert _queued(config, paths, "lobby", room_mode=True)


@pytest.mark.asyncio
async def test_named_thread_agent_outside_a_thread_is_refused(tmp_path: Path) -> None:
    config, paths = _config(tmp_path)
    _seed(config, paths, "code")
    result = await _compact(config, paths, "code", target=_target(in_thread=False))
    assert result == "`code` has nothing to compact here yet."
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
    _seed(config, paths, "code")
    result = await _compact(config, paths, "code")
    assert result == (
        "❌ `code` cannot compact its context because no context_window is configured on the active model."
    )
    assert not _queued(config, paths, "code")


@pytest.mark.asyncio
async def test_compaction_availability_checks_the_requesters_budget_fallback(tmp_path: Path) -> None:
    config, paths = _config(tmp_path)
    config.models["default"].pricing = ModelPricing(input=5, output=30)
    config.models["fallback"] = ModelConfig(provider="openai", id="test-fallback")
    config.budgets = BudgetsConfig(fallback_model="fallback", monthly_limit_usd=0)
    _seed(config, paths, "code")
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
    _seed(config, paths, "code")
    assert (await _compact(config, paths, "code")).startswith("✅")
    assert _queued(config, paths, "code")
    assert not _queued(config, paths, "code", requester_id="@bob:example.test")


@pytest.mark.asyncio
async def test_bare_compact_flags_only_addressable_entities_with_a_session_here(tmp_path: Path) -> None:
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
async def test_bare_compact_continues_after_an_unreadable_store(tmp_path: Path) -> None:
    config, paths = _config(tmp_path)
    database = scope_session_database_path(
        scope=_scope(config, "docs"),
        config=config,
        runtime_paths=paths,
        execution_identity=None,
    )
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_text("not a database", encoding="utf-8")
    _seed(config, paths, "squad")

    assert (await _compact(config, paths, "")).splitlines() == [
        "❌ `docs`: its history could not be read.",
        "✅ `squad` compacts its context before its next reply in this thread.",
    ]
    assert _queued(config, paths, "squad")


@pytest.mark.asyncio
async def test_bare_compact_outside_a_thread_reaches_room_mode_entities(tmp_path: Path) -> None:
    config, paths = _config(tmp_path)
    _seed(config, paths, "lobby", room_mode=True)
    result = await _compact(config, paths, "", target=_target(in_thread=False))
    assert result == "✅ `lobby` compacts its context before its next reply in this room."
    assert _queued(config, paths, "lobby", room_mode=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "expected"),
    [("", "Nothing to compact here."), ("code", "`code` has nothing to compact here yet.")],
)
async def test_compact_without_sessions_does_not_provision_storage(tmp_path: Path, name: str, expected: str) -> None:
    config, paths = _config(tmp_path)
    config.agents["code"].private = AgentPrivateConfig(per="user")

    def stored_files() -> set[Path]:
        return {path for path in paths.storage_root.rglob("*") if path.is_file()}

    before = stored_files()
    assert await _compact(config, paths, name) == expected
    # No conversation database and no private owner record for the entities without a session here.
    assert stored_files() == before


REQUESTER = "@user:localhost"


def _reply_runner(tmp_path: Path, entity_name: str) -> ResponseRunner:
    """Return the response runner of ``entity_name``'s bot: the private agent `general`, or the configured team `squad`."""
    config = response_runner_helpers._config(tmp_path)
    config.models["default"].context_window = 48_000
    config.administrators = [REQUESTER]
    config.agents["general"].private = AgentPrivateConfig(per="user")
    config.agents["helper"] = AgentConfig(display_name="Helper", rooms=["!room:localhost"])
    config.teams["squad"] = TeamConfig(
        display_name="Squad",
        role="Pair up",
        agents=["helper"],
        rooms=["!room:localhost"],
    )
    agent_user = AgentMatrixUser(
        agent_name=entity_name,
        password=TEST_PASSWORD,
        display_name=entity_name,
        user_id=f"@mindroom_{entity_name}:localhost",
    )
    bot = make_test_agent_bot(agent_user, config, rooms=["!room:localhost"])
    wrap_extracted_collaborators(bot)
    return unwrap_extracted_collaborator(bot._response_runner)


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_name", ["general", "squad"])
async def test_the_reply_path_reads_the_flag_the_command_writes(tmp_path: Path, entity_name: str) -> None:
    runner = _reply_runner(tmp_path, entity_name)
    config, paths = runner.deps.runtime.config, runner.deps.runtime_paths
    target = MessageTarget.resolve("!room:localhost", "$thread", "$command")
    scope = runner.deps.state_writer.history_scope()
    identity = runner.deps.tool_runtime.build_execution_identity(target=target, user_id=REQUESTER)
    seed_session_row(
        create_scope_session_storage(scope=scope, config=config, runtime_paths=paths, execution_identity=identity),
        new_scope_session(session_id=target.session_id, scope=scope),
    )
    result = await handle_compact_command(
        entity_name,
        config=config,
        runtime_paths=paths,
        target=target,
        requester_id=REQUESTER,
        membership_index=runner.deps.runtime.agent_reply_memberships,
    )
    assert result.startswith("✅"), result

    async def reply_path_queued(requester_id: str) -> bool:
        # The next reply's target, identity, scope, and store, each resolved as the reply path does.
        target = MessageTarget.resolve("!room:localhost", "$thread", "$next")
        identity = runner.deps.tool_runtime.build_execution_identity(target=target, user_id=requester_id)
        return await runner._has_queued_forced_compaction(
            session_id=target.session_id,
            scope=scope,
            execution_identity=identity,
        )

    assert await reply_path_queued(REQUESTER)
    # The private agent's request is the requester's own; the configured team's history is shared.
    assert await reply_path_queued("@other:localhost") is (entity_name == "squad")


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
    _seed(config, paths, "code")
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
