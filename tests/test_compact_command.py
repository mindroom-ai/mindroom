"""Matrix commands and their target-owned history compaction."""

from __future__ import annotations

# ruff: noqa: D103
import asyncio
import json
import threading
import time
from dataclasses import replace
from itertools import count
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, Mock

import nio
import pytest

from mindroom import pending_event_worker
from mindroom import response_runner as runner_module
from mindroom.commands.compact_commands import handle_compact_command
from mindroom.commands.handler import handle_command
from mindroom.commands.parsing import (
    COMMAND_TYPES_WITH_SIDE_EFFECTS,
    CommandType,
    _CommandParser,
    get_command_help,
)
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.budgets import BudgetsConfig
from mindroom.config.main import Config
from mindroom.config.memory import MemoryConfig
from mindroom.config.models import CompactionConfig, DefaultsConfig, ModelConfig, ModelPricing
from mindroom.config.personal_agents import PersonalAgentRoster, PersonalAgentRosterEntry, personal_agent_roster_path
from mindroom.constants import AI_RUN_METADATA_KEY, FORK_CONTENT_KEY, MATRIX_EVENT_ID_METADATA_KEY, SKIP_MENTIONS_KEY
from mindroom.contacts.store import ContactParty, ContactRecord, ContactSide, update_contacts
from mindroom.event_journal import EventClass, EventKind, InboundEvent, journal
from mindroom.event_journal.approval_signals import PausedReply
from mindroom.event_journal.projection import ProjectedEvent
from mindroom.history import session_context as session_context_module
from mindroom.history.runtime import _manual_summary_cap
from mindroom.history.session_context import create_scope_session_storage, scope_session_database_path
from mindroom.history.storage import has_queued_force_compaction, new_scope_session
from mindroom.history.types import HistoryScope
from mindroom.hooks import (
    EVENT_COMPACTION_BEFORE,
    EVENT_MESSAGE_RECEIVED,
    CompactionHookContext,
    HookRegistry,
    MessageReceivedContext,
    hook,
)
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelIdentity
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.thread_diagnostics import THREAD_HISTORY_DEGRADED_DIAGNOSTIC
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.response_admission import ResponseAdmissionRefusedError
from mindroom.runtime_shutdown import ENTITY_REMOVED_SHUTDOWN, SYNC_RESTART_SHUTDOWN
from mindroom.session_storage import summary_rows
from mindroom.session_storage.usage_reader import stored_usage
from mindroom.tool_system.worker_routing import build_tool_execution_identity
from tests import response_runner_helpers
from tests.access_schema_support import refresh_memberships
from tests.authorization_helpers import isolated_membership_index, make_test_command_handler_context
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    drain_coalescing,
    install_model_providers,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    seed_session,
    seed_session_row,
    serve_conversation_reader,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)
from tests.fakes.scripted_model import Await, usage
from tests.fakes.summary_model import summary_model, summary_steps, summary_turn
from tests.history_helpers import _completed_run, _completed_team_run, _plugin, _runtime_paths, summary_rows_of
from tests.identity_helpers import persist_entity_accounts
from tests.journal_helpers import admit_dispatch_event, record_room_history_recovery
from tests.journal_membership_helpers import admit_room_membership
from tests.runtime_fakes import ReadyRuntime, ready_runtime

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.constants import RuntimePaths
    from mindroom.response_runner import ResponseRunner

ALICE = "@alice:example.test"
BOB = "@bob:example.test"
ROOM = "!room:example.test"
THREAD = "$thread:example.test"
_DEFERRED = "Couldn't compact now. Try /compact again later."


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
            memory=MemoryConfig(backend="none"),
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
async def test_named_compact_asks_to_retry_while_membership_is_pending(tmp_path: Path) -> None:
    config, paths = _config(tmp_path)
    config.agents["code"].access = ResponderAccessConfig(current_room_members=True)
    _seed(config, paths, "code")

    assert await _compact(config, paths, "code") == (
        "Room membership for `code` is still loading; try `/compact code` again in a moment."
    )
    assert not _queued(config, paths, "code")


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
async def test_bare_compact_reports_pending_membership_once_without_naming_entities(tmp_path: Path) -> None:
    config, paths = _config(tmp_path)
    pending_names = ("code", "docs")
    for name in pending_names:
        config.agents[name].access = ResponderAccessConfig(current_room_members=True)
        _seed(config, paths, name)

    notice = "Some agents' room membership is still loading; run `/compact` again in a moment."
    assert await _compact(config, paths, "") == notice

    _seed(config, paths, "squad")
    assert (await _compact(config, paths, "")).splitlines() == [
        "✅ `squad` compacts its context before its next reply in this thread.",
        notice,
    ]
    assert _queued(config, paths, "squad")
    assert not any(_queued(config, paths, name) for name in pending_names)


@pytest.mark.asyncio
@pytest.mark.parametrize("args_text", ["", "docs"], ids=["bare", "named"])
async def test_compact_reports_an_unreadable_store(tmp_path: Path, args_text: str) -> None:
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

    expected = ["❌ `docs`: its history could not be read."]
    if not args_text:
        expected.append("✅ `squad` compacts its context before its next reply in this thread.")
    assert (await _compact(config, paths, args_text)).splitlines() == expected
    assert _queued(config, paths, "squad") is (not args_text)


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


def _event(body: str, *, event_id: str = "$command") -> nio.RoomMessageText:
    return nio.RoomMessageText.from_dict(
        {
            "event_id": event_id,
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
        record_command_result.assert_awaited_once_with(reply, extra_content=None)
        assert _queued(config, paths, "code")


def _room_with_members(*user_ids: str, room_id: str) -> nio.MatrixRoom:
    room = nio.MatrixRoom(room_id, "@mindroom_general:localhost")
    for user_id in user_ids:
        room.add_member(user_id, user_id, None)
    return room


def _live_bot(runtime: ReadyRuntime, name: str) -> AgentBot:
    user = AgentMatrixUser(
        agent_name=name,
        password=TEST_PASSWORD,
        display_name=name,
        user_id=f"@mindroom_{name}:localhost",
    )
    bot = make_test_agent_bot(user, rooms=[ROOM], orchestrator=runtime)
    bot.client = make_matrix_client_mock(user_id=user.user_id)
    event_ids = count()
    bot.client.room_send = AsyncMock(side_effect=lambda **_: nio.RoomSendResponse(f"${name}-{next(event_ids)}", ROOM))
    bot.running = True
    return bot


def _seed_history(
    config: Config,
    paths: RuntimePaths,
    entity: str,
    *,
    requester: str = ALICE,
    room_mode: bool = False,
    redacted: bool = False,
    source_event_id: str = "$earlier",
    previous_tokens: int | None = 1024,
) -> None:
    scope = _scope(config, entity)
    session = new_scope_session(session_id=_target(room_mode=room_mode).session_id, scope=scope)
    run = (
        _completed_team_run("earlier", team_id=entity)
        if scope.kind == "team"
        else _completed_run("earlier", agent_id=entity)
    )
    session.runs = [
        replace(
            run,
            session_id=session.session_id,
            model="test-model",
            model_provider="openai",
            messages=[Message(role="user", content="history " * 500), Message(role="assistant", content="response")],
            metadata={
                MATRIX_EVENT_ID_METADATA_KEY: source_event_id,
                AI_RUN_METADATA_KEY: {
                    "prepared_context": {"tokens": previous_tokens},
                    "compaction": {"replay_plan": {"estimated_tokens": 800}},
                },
            },
        ),
    ]
    if redacted:
        session.runs.append(
            replace(
                session.runs[0],
                run_id="removed",
                messages=[Message(role="user", content="removed transcript")],
                metadata={MATRIX_EVENT_ID_METADATA_KEY: "$removed"},
            ),
        )
    seed_session(_storage(config, paths, entity, requester_id=requester, room_mode=room_mode), session)


async def _dispatch_message(bot: AgentBot, room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:
    assert bot.client is not None
    bot.client.rooms[ROOM] = room
    await admit_dispatch_event(bot._journal_dispatcher, room, event, EventKind.MESSAGE, EventClass.ACTIONABLE)
    assert await bot._journal_dispatcher.drain_once() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("entity", ["code", "squad"])
@pytest.mark.parametrize("owns_command", [True, False])
async def test_compact_owner_admits_a_separate_target_source(tmp_path: Path, entity: str, owns_command: bool) -> None:
    config, paths = _config(tmp_path)
    runtime = ready_runtime(config, paths)
    other, target = _live_bot(runtime, "docs"), _live_bot(runtime, entity)
    owner = target if owns_command else other
    room = _room_with_members(ALICE, other.matrix_id.full_id, target.matrix_id.full_id, room_id=ROOM)
    room.members_synced = True
    target.client.rooms[ROOM] = room
    _seed_history(config, paths, entity)
    command = _event(f"/compact {entity}")
    command.source["content"]["m.mentions"] = {"user_ids": [owner.matrix_id.full_id]}
    await _dispatch_message(owner, room, command)

    row = await target.journal_principal().load_event(f"compact:$command:{entity}")
    assert row is not None
    assert row.room_id == ROOM
    assert row.thread_id == THREAD
    assert row.source == {"requester_id": ALICE, "origin_event_id": "$command"}
    assert "compacting now" in str(owner.client.room_send.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(("entity", "requester"), [("code", ALICE), ("code", BOB), ("squad", ALICE)])
async def test_pending_compaction_folds_only_after_target_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entity: str,
    requester: str,
) -> None:
    config, paths = _config(tmp_path)
    config.agents["code"].private = AgentPrivateConfig(per="user")
    for name in ("code", "docs"):
        config.agents[name].access = ResponderAccessConfig(users=[ALICE, BOB])
    runtime = ready_runtime(config, paths)
    owner, target = _live_bot(runtime, "docs"), _live_bot(runtime, entity)
    room = _room_with_members(ALICE, BOB, owner.matrix_id.full_id, target.matrix_id.full_id, room_id=ROOM)
    room.members_synced = True
    target.client.rooms[ROOM] = room
    other_requester = BOB if requester == ALICE else ALICE
    _seed_history(config, paths, entity, requester=requester, redacted=True, previous_tokens=None)
    if entity == "code":
        _seed_history(config, paths, entity, requester=other_requester)
    _seed_history(config, paths, "docs")
    model = summary_model(summary_turn("short summary", usage=usage(120, 12)), model_id="test-model", provider="openai")
    install_model_providers(monkeypatch, {"default": model})
    command = _event(f"/compact {entity}")
    command.sender = command.source["sender"] = requester
    command.source["content"]["m.mentions"] = {"user_ids": [owner.matrix_id.full_id]}
    await _dispatch_message(owner, room, command)

    # A fresh target dispatcher, with no in-memory compaction task, replays the admitted row.
    target = _live_bot(runtime, entity)
    target.client.rooms[ROOM] = room
    await _hydrate(target)
    fold = await _start_fold(target)
    row_id = f"compact:$command:{entity}"
    assert target._response_runner.inbox.has_live_inbox_response(row_id)
    done, _ = await asyncio.wait({fold}, timeout=0.15)
    assert not done
    assert not model.requests
    await runtime.event_journal_store().backend.write(
        lambda tx: journal.redact_for_agent(tx, entity, room_id=ROOM, event_id="$removed", receipt_order=0),
    )
    await _dispatch_message(target, room, command)
    await asyncio.wait_for(fold, 30)
    assert await target._journal_dispatcher.drain_once() == 0
    assert row_id not in await target.journal_principal().unsettled_event_ids()
    assert len(model.requests) == 1
    assert "removed transcript" not in repr(model.requests)
    assert _summaries(config, paths, entity, requester=requester) == ["short summary"]
    storage = _storage(config, paths, entity, requester_id=requester)
    with storage.transaction(write=False) as connection:
        [row] = stored_usage(connection, storage.names, _target().session_id, since=None)
    assert isinstance(row, str)
    snapshot = json.loads(row)
    assert snapshot["kind"] == "compaction_summary"
    assert snapshot["user_id"] == requester
    assert not _summaries(config, paths, "docs")
    if entity == "code":
        assert not _summaries(config, paths, entity, requester=other_requester)
    assert not _queued(config, paths, entity, requester_id=requester)
    assert "→" in str(target.client.room_send.await_args_list)


def _summaries(
    config: Config,
    paths: RuntimePaths,
    entity: str,
    *,
    requester: str = ALICE,
    room_mode: bool = False,
) -> list[str]:
    storage = _storage(config, paths, entity, requester_id=requester, room_mode=room_mode)
    return [
        row.summary
        for row in summary_rows_of(storage, _scope(config, entity).key, _target(room_mode=room_mode).session_id)
    ]


_ROW_ID = "compact:$command:code"


def _compact_pair(
    config: Config,
    paths: RuntimePaths,
    *,
    previous_tokens: int | None = 1024,
    source_event_id: str = "$earlier",
) -> tuple[AgentBot, AgentBot, nio.MatrixRoom, nio.RoomMessageText]:
    runtime = ready_runtime(config, paths)
    other, target = _live_bot(runtime, "docs"), _live_bot(runtime, "code")
    room = _room_with_members(ALICE, other.matrix_id.full_id, target.matrix_id.full_id, room_id=ROOM)
    room.members_synced = True
    target.client.rooms[ROOM] = room
    _seed_history(config, paths, "code", previous_tokens=previous_tokens, source_event_id=source_event_id)
    return other, target, room, _addressed(target, "/compact code", "$command")


def _addressed(bot: AgentBot, body: str, event_id: str) -> nio.RoomMessageText:
    event = _event(body, event_id=event_id)
    event.source["content"]["m.mentions"] = {"user_ids": [bot.matrix_id.full_id]}
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize("addressed_first", [False, True])
async def test_fold_queues_between_replies_without_claiming_unmentioned_messages(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    addressed_first: bool,
) -> None:
    config, paths = _config(tmp_path)
    other, target, room, command = _compact_pair(config, paths)
    bob = "@bob:example.test"
    room.add_member(bob, "Bob", None)
    # In a shared human thread, don't elect an ad-hoc team for unmentioned messages.
    messages = tuple(
        ResolvedVisibleMessage(
            sender=sender,
            body="earlier message",
            timestamp=0,
            event_id=f"$visible-{index}",
            content={"body": "earlier message", "msgtype": "m.text"},
            thread_id=THREAD,
            latest_event_id=f"$visible-{index}",
        )
        for index, sender in enumerate((ALICE, bob, other.matrix_id.full_id, target.matrix_id.full_id))
    )
    reader = make_conversation_reader_mock()
    serve_conversation_reader(reader, messages, room_id=ROOM, thread_id=THREAD)
    target._conversation_resolver.deps = replace(target._conversation_resolver.deps, conversation_reader=reader)
    reply_started, finish_reply = asyncio.Event(), asyncio.Event()
    fold_started, finish_fold = asyncio.Event(), asyncio.Event()

    async def block_reply() -> None:
        reply_started.set()
        await finish_reply.wait()

    async def block_fold() -> None:
        fold_started.set()
        await finish_fold.wait()

    model = summary_model(
        [Await(block_reply), *summary_steps("first answer")],
        [Await(block_fold), *summary_steps("folded history")],
        "later answer",
        "follow-up answer",
        model_id="test-model",
        provider="openai",
    )
    install_model_providers(monkeypatch, {"default": model})
    await _hydrate(target)
    await _dispatch_message(target, room, _addressed(target, "first question", "$first"))
    try:
        await asyncio.wait_for(reply_started.wait(), 30)
        await _dispatch_message(target, room, command)
        await _start_fold(target)
        assert target._response_runner.inbox.has_live_inbox_response(_ROW_ID)
        assert not fold_started.is_set()
        finish_reply.set()
        await asyncio.wait_for(fold_started.wait(), 30)
        assert "first answer" in repr(model.requests[1])
        assert not target._response_runner.lifecycle_coordinator.has_active_response_for_target(_target())

        unmentioned = _event("unmentioned follow-up", event_id="$unmentioned")
        if not addressed_first:
            await _dispatch_message(target, room, unmentioned)
            await asyncio.wait_for(target._coalescing_gate.drain_all(), 30)
        coordinator = target._response_runner.lifecycle_coordinator
        await _dispatch_message(target, room, _addressed(target, "later question", "$later"))
        lock = coordinator._conversation_for(_target()).lock
        async with asyncio.timeout(30):  # The later reply queues for the lock the fold holds.
            while len(lock._waiters or ()) != 1:  # noqa: ASYNC110
                await asyncio.sleep(0)
        assert coordinator.has_active_response_for_target(_target())
        if addressed_first:
            await _dispatch_message(target, room, unmentioned)
            assert target._coalescing_gate.has_pending_source_event("$unmentioned")
        assert len(model.requests) == 2
        assert not _summaries(config, paths, "code")
    finally:
        finish_reply.set()
        finish_fold.set()
        await asyncio.wait_for(drain_coalescing(target), 30)
    assert len(model.requests) == (4 if addressed_first else 3), target.client.room_send.await_args_list
    assert "folded history" in repr(model.requests[2])
    assert "later answer" in str(target.client.room_send.await_args_list)
    if addressed_first:
        assert "unmentioned follow-up" in repr(model.requests[3])


@pytest.mark.asyncio
@pytest.mark.parametrize("pause_while_waiting", [False, True])
async def test_compaction_waits_for_paused_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pause_while_waiting: bool,
) -> None:
    config, paths = _config(tmp_path)
    _, target, room, command = _compact_pair(config, paths)
    coordinator = target._response_runner.lifecycle_coordinator
    await _hydrate(target)
    hold = PausedReply(target.journal_principal().principal_id, "approval", ROOM, THREAD)
    lock = coordinator._conversation_for(_target()).lock
    gate = target.orchestrator.response_admission_gate
    if pause_while_waiting:
        await lock.acquire()  # A reply has the lock, but has not paused yet.
    else:
        coordinator._hold_for_approval(hold)
    model = summary_model("finished fold", model_id="test-model", provider="openai")
    install_model_providers(monkeypatch, {"default": model})
    await _dispatch_message(target, room, command)
    fold = await _start_fold(target)
    try:
        if pause_while_waiting:
            await asyncio.sleep(0)
            assert not gate.close_if_idle(), "the fold must have already entered admission"
            coordinator._hold_for_approval(hold)
            lock.release()
        done, _ = await asyncio.wait({fold}, timeout=0.25)
        assert not done
        assert not model.requests
        assert gate.close_if_idle(), "a parked compaction must not defer a runtime replacement"
        gate.reopen()
    finally:
        if lock.locked() and pause_while_waiting:
            lock.release()
        coordinator._release_approval_hold("approval")
        await asyncio.wait_for(fold, 30)
    assert _summaries(config, paths, "code") == ["finished fold"]


@pytest.mark.asyncio
async def test_pending_compaction_grant_does_not_delay_another_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths = _config(tmp_path)
    config.agents["code"].access = ResponderAccessConfig(current_room_members=True, users=[BOB])
    _, target, room, command = _compact_pair(config, paths)
    room.add_member(BOB, "Bob", None)
    await _hydrate(target)
    index = target.orchestrator.agent_reply_memberships
    target.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[ROOM])
    target.client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[nio.RoomMember(ALICE, None, None)],
        room_id=ROOM,
    )
    await refresh_memberships(index, config, paths, target.client)
    coordinator = target._response_runner.lifecycle_coordinator
    coordinator._hold_for_approval(PausedReply(target.journal_principal().principal_id, "held", ROOM, THREAD))
    model = summary_model("after refresh", model_id="test-model", provider="openai")
    install_model_providers(monkeypatch, {"default": model})
    await _dispatch_message(target, room, command)
    fold = await _start_fold(target)
    index.invalidate(config, reason="observer_refreshing")
    coordinator._release_approval_hold("held")
    await asyncio.wait_for(fold, 30)
    store = target.journal_principal()
    _assert_deferred(target)
    assert target.client.room_send.await_count == 2  # Command acknowledgement and one deferral.
    assert _queued(config, paths, "code")
    assert not await store.is_pending(_ROW_ID)
    assert not model.requests
    gate = target.orchestrator.response_admission_gate
    assert gate.close_if_idle()
    gate.reopen()
    bob = _addressed(target, "/help", "$unrelated")
    bob.sender = bob.source["sender"] = BOB
    bob.source["content"]["m.relates_to"]["event_id"] = "$other-thread"
    await _dispatch_message(target, room, bob)
    assert not await store.is_pending(bob.event_id)
    assert target.client.room_send.await_count == 3
    assert not target._journal_dispatcher._worker._room_is_backing_off(ROOM)


@pytest.mark.asyncio
async def test_revoked_compaction_grant_while_waiting_settles_silently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths = _config(tmp_path)
    _, target, room, command = _compact_pair(config, paths)
    await _hydrate(target)
    coordinator = target._response_runner.lifecycle_coordinator
    coordinator._hold_for_approval(PausedReply(target.journal_principal().principal_id, "held", ROOM, THREAD))
    await _dispatch_message(target, room, command)
    fold = await _start_fold(target)
    await asyncio.sleep(0)
    assert not fold.done()
    config.agents["code"].access = ResponderAccessConfig(users=[BOB])
    build, load, hook_called = MagicMock(return_value=summary_model("must not fold")), MagicMock(), AsyncMock()
    monkeypatch.setattr(runner_module.provider_registry, "build_model_provider", build)
    monkeypatch.setattr(runner_module, "load_scope_session", load)

    @hook(EVENT_COMPACTION_BEFORE, scope="agent", agents=["code"])
    async def before(ctx: CompactionHookContext) -> None:
        await hook_called(ctx)

    target.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("check-no-fold", [before])])
    target.client.room_send.reset_mock()
    coordinator._release_approval_hold("held")
    await asyncio.wait_for(fold, 30)
    load.assert_not_called()
    build.assert_not_called()
    hook_called.assert_not_awaited()
    target.client.room_send.assert_not_awaited()
    assert not _summaries(config, paths, "code")
    assert _queued(config, paths, "code")
    assert _ROW_ID not in await target.journal_principal().unsettled_event_ids()


@pytest.mark.asyncio
async def test_malformed_compaction_row_settles_once(tmp_path: Path) -> None:
    config, paths = _config(tmp_path)
    _, target, _, _ = _compact_pair(config, paths)
    store = target.journal_principal()
    await store.admit(
        InboundEvent(
            event_id=_ROW_ID,
            room_id=ROOM,
            thread_id=THREAD,
            kind=EventKind.COMPACTION,
            event_class=EventClass.ACTIONABLE,
            sender=target.matrix_id.full_id,
            origin_server_ts=1,
            source={"requester_id": ALICE},
        ),
    )
    target._journal_dispatcher.wake()
    await asyncio.wait_for(await _start_fold(target), 30)
    assert await target._journal_dispatcher.drain_once() == 0
    assert _ROW_ID not in await store.unsettled_event_ids()
    target.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_rejects_handoff_after_last_drain_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths = _config(tmp_path)
    _, target, room, command = _compact_pair(config, paths)
    runner = target._response_runner
    coordinator = runner.lifecycle_coordinator
    coordinator._hold_for_approval(PausedReply(target.journal_principal().principal_id, "held", ROOM, THREAD))
    await _dispatch_message(target, room, command)
    parked = await _start_fold(target)
    await asyncio.sleep(0)
    assert not parked.done()
    snapshot = asyncio.Event()
    released = asyncio.Event()
    drain = runner.inbox.drain_inbox_responses

    async def pause_after_drain(**kwargs: Any) -> bool:  # noqa: ANN401
        result = await drain(**kwargs)
        snapshot.set()
        await released.wait()
        return result

    monkeypatch.setattr(runner.inbox, "drain_inbox_responses", pause_after_drain)
    stopping = asyncio.create_task(target.stop(shutdown_intent=SYNC_RESTART_SHUTDOWN))
    late_id = "compact:$late:code"
    try:
        await asyncio.wait_for(snapshot.wait(), 30)
        assert isinstance(parked.exception(), ResponseAdmissionRefusedError)
        assert await target.admit_compaction(MessageTarget.resolve(ROOM, THREAD, "$late"), ALICE)
        await target._journal_dispatcher.drain_once()
        assert not runner.inbox.has_live_inbox_response(late_id)
    finally:
        if not snapshot.is_set() or runner.inbox.has_live_inbox_response(late_id):
            coordinator._release_approval_hold("held")  # Only unblock cleanup on a regression.
        released.set()
        await asyncio.wait_for(stopping, 30)
    assert {_ROW_ID, late_id} <= set(await target.journal_principal().unsettled_event_ids())


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", ["at_handoff", "during_handoff", "after_waiting"])
async def test_draining_parks_compaction_without_delaying_later_room_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    refused: str,
) -> None:
    """A fold the draining bot will not start stays the dispatcher's parked source, not a lost one that cools its room."""
    monkeypatch.setattr(pending_event_worker, "_INITIAL_RETRY_DELAY_SECONDS", 0.01)
    config, paths = _config(tmp_path)
    _, target, room, command = _compact_pair(config, paths)
    await _hydrate(target)
    dispatcher, runner, store = target._journal_dispatcher, target._response_runner, target.journal_principal()
    stop_other_work = AsyncMock()
    dispatcher.callbacks = replace(dispatcher.callbacks, on_redaction=stop_other_work)
    if refused == "after_waiting":
        runner.lifecycle_coordinator._hold_for_approval(PausedReply(store.principal_id, "held", ROOM, THREAD))
    await _dispatch_message(target, room, command)
    registered = Mock(wraps=runner.inbox.track_inbox_response)
    monkeypatch.setattr(runner.inbox, "track_inbox_response", registered)
    if refused == "after_waiting":
        waiting = await _start_fold(target)
        target._runtime_view.begin_draining()  # The dispatcher keeps running while the bot drains.
        with pytest.raises(ResponseAdmissionRefusedError):
            await asyncio.wait_for(waiting, 30)
        runner.lifecycle_coordinator._release_approval_hold("held")
    elif refused == "during_handoff":
        reading, read = asyncio.Event(), asyncio.Event()

        async def held_settings_read(_room_id: str) -> None:
            reading.set()
            await read.wait()

        dispatcher.callbacks = replace(dispatcher.callbacks, before_room_event=held_settings_read)
        handoff = asyncio.create_task(dispatcher.drain_once())
        await asyncio.wait_for(reading.wait(), 30)
        target._runtime_view.begin_draining()  # The dispatcher already decided to hand the fold over.
        read.set()
        await asyncio.wait_for(handoff, 30)
    else:
        target._runtime_view.begin_draining()
    redaction = nio.RedactionEvent.from_dict(
        {
            "event_id": "$redaction",
            "sender": ALICE,
            "origin_server_ts": 2,
            "type": "m.room.redaction",
            "redacts": "$elsewhere",
            "content": {},
        },
    )
    await admit_dispatch_event(dispatcher, room, redaction, EventKind.REDACTION, EventClass.ACTIONABLE)
    await dispatcher.drain_once()
    stop_other_work.assert_awaited_once()
    assert not dispatcher._worker._room_retries
    assert registered.call_count == (refused == "after_waiting")  # A draining bot registers no fold at all.
    assert not runner.inbox.has_live_inbox_response(_ROW_ID)
    assert await store.is_pending(_ROW_ID)

    assert target._runtime_view.resume()
    model = summary_model("resumed fold", model_id="test-model", provider="openai")
    install_model_providers(monkeypatch, {"default": model})
    async with asyncio.timeout(30):
        while await store.is_pending(_ROW_ID):
            await dispatcher.drain_once()
            await asyncio.sleep(0.02)
    assert _summaries(config, paths, "code")


@pytest.mark.asyncio
async def test_removed_target_settles_the_compaction_it_parked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fold whose target is removed has no later generation to run it, so removal settles its row unanswered."""
    config, paths = _config(tmp_path)
    _, target, room, command = _compact_pair(config, paths)
    await _hydrate(target)
    runner, store = target._response_runner, target.journal_principal()
    build = MagicMock(side_effect=AssertionError("a removed target folds nothing"))
    monkeypatch.setattr(runner_module.provider_registry, "build_model_provider", build)
    runner.lifecycle_coordinator._hold_for_approval(PausedReply(store.principal_id, "held", ROOM, THREAD))
    await _dispatch_message(target, room, command)
    waiting = await _start_fold(target)
    sent = target.client.room_send.await_count

    await target.prepare_for_sync_shutdown(shutdown_intent=ENTITY_REMOVED_SHUTDOWN)
    await target.stop(shutdown_intent=ENTITY_REMOVED_SHUTDOWN)
    assert isinstance(waiting.exception(), ResponseAdmissionRefusedError)
    assert await store.is_pending(_ROW_ID)  # Stopping alone keeps the row, which a replacement replays.

    # What the fleet does for an entity it removed, once its bot is gone.
    await target.orchestrator.event_journal_store().end_entity_replies({"code"}.__contains__, now_ns=time.time_ns())
    assert not await store.is_pending(_ROW_ID)
    build.assert_not_called()
    assert target.client.room_send.await_count == sent


@pytest.mark.asyncio
async def test_stopping_during_final_append_drains_write_and_replays_as_no_op(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths = _config(tmp_path)
    _, target, room, event = _compact_pair(config, paths)
    runner = target._response_runner
    model = summary_model("accepted summary", model_id="test-model", provider="openai")
    install_model_providers(monkeypatch, {"default": model})
    loop, appending, finish = asyncio.get_running_loop(), asyncio.Event(), threading.Event()
    append = summary_rows.append_summary

    def blocked_append(*args: Any, **kwargs: Any) -> int | None:  # noqa: ANN401
        loop.call_soon_threadsafe(appending.set)
        assert finish.wait(30)
        return append(*args, **kwargs)

    monkeypatch.setattr(summary_rows, "append_summary", blocked_append)
    await _hydrate(target)
    await _dispatch_message(target, room, event)
    fold = await _start_fold(target)
    try:
        await asyncio.wait_for(appending.wait(), 30)
        target.begin_process_shutdown()
        fold.cancel()
        done, _ = await asyncio.wait({fold}, timeout=0.05)
        assert not done
        assert runner.inbox.has_live_inbox_response(_ROW_ID)
        assert _ROW_ID in await target.journal_principal().unsettled_event_ids()
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await fold
    assert not _queued(config, paths, "code")
    storage = _storage(config, paths, "code")
    assert [row.summary for row in summary_rows_of(storage, "agent:code", _target().session_id)] == ["accepted summary"]
    edits = [
        call.kwargs["content"]["m.new_content"]
        for call in target.client.room_send.await_args_list
        if "m.new_content" in call.kwargs["content"]
    ]
    [edit] = edits
    assert edit["io.mindroom.compaction"]["status"] == "success"

    replacement = _live_bot(target.orchestrator, "code")
    replacement.client.rooms[ROOM] = room
    load = MagicMock(side_effect=AssertionError("cleared flag should not load history"))
    monkeypatch.setattr(runner_module, "load_scope_session", load)
    await asyncio.wait_for(await _start_fold(replacement), 30)
    load.assert_not_called()
    assert _summaries(config, paths, "code") == ["accepted summary"]
    assert _ROW_ID not in await replacement.journal_principal().unsettled_event_ids()
    assert len(model.requests) == 1
    assert not replacement.client.room_send.await_count


@pytest.mark.parametrize(("total", "history"), [(True, 0), (1000, False)])
def test_manual_summary_cap_skips_boolean_sizing_metadata(total: int, history: int) -> None:
    identity = ModelIdentity(id="test-model", provider="openai")

    def run(run_id: str, total: int, history: int):  # noqa: ANN202
        return replace(
            _completed_run(run_id),
            model=identity.id,
            model_provider=identity.provider,
            metadata={
                AI_RUN_METADATA_KEY: {
                    "prepared_context": {"tokens": total},
                    "compaction": {"replay_plan": {"estimated_tokens": history}},
                },
            },
        )

    assert _manual_summary_cap([run("older", 900, 100), run("malformed", total, history)], 1000, identity) == 200


@pytest.mark.asyncio
async def test_compaction_over_cap_edits_notice_to_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, paths = _config(tmp_path)
    _, target, room, command = _compact_pair(config, paths, previous_tokens=48_800)
    await _hydrate(target)
    model = summary_model("too large", model_id="test-model", provider="openai")
    install_model_providers(monkeypatch, {"default": model})
    await _dispatch_message(target, room, command)
    await asyncio.wait_for(await _start_fold(target), 30)
    assert not _summaries(config, paths, "code")
    assert not _queued(config, paths, "code")
    assert "Compaction failed" in str(target.client.room_send.await_args_list)
    assert "exceeds the history budget" in str(target.client.room_send.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("old_flag", [False, True])
async def test_command_owner_hook_veto_creates_no_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    old_flag: bool,
) -> None:
    config, paths = _config(tmp_path)
    _, target, room, event = _compact_pair(config, paths)
    if old_flag:
        await _compact(config, paths, "code")
    vetoed = asyncio.Event()

    @hook(EVENT_MESSAGE_RECEIVED, scope="agent", agents=["code"])
    async def veto(ctx: MessageReceivedContext) -> None:
        vetoed.set()
        ctx.suppress = True

    target.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("veto-compact", [veto])])
    model = summary_model("unused", model_id="test-model", provider="openai")
    install_model_providers(monkeypatch, {"default": model})
    await _dispatch_message(target, room, event)
    assert vetoed.is_set()
    assert not target._response_runner.inbox.pending_inbox_response_count
    assert not model.requests
    assert _queued(config, paths, "code") is old_flag
    assert await target.journal_principal().load_event(_ROW_ID) is None


@pytest.mark.asyncio
async def test_contact_room_sets_flag_without_admitting_work(
    tmp_path: Path,
) -> None:
    _, paths = _config(tmp_path)
    roster = personal_agent_roster_path(paths)
    roster.parent.mkdir(parents=True, exist_ok=True)
    roster.write_text(
        PersonalAgentRoster(
            entries=[
                PersonalAgentRosterEntry(
                    template="personal",
                    user_id=ALICE,
                    username="alice",
                    agent_name="code",
                    matrix_username=None,
                    user_display_name="Alice",
                    source_room_id="!origin:localhost",
                ),
            ],
        ).model_dump_json(),
        encoding="utf-8",
    )
    config = Config.validate_with_runtime(
        {
            "personal_agents": {"personal": {"agent": {"display_name": "Code", "contacts": {}}}},
            "agents": {"docs": {"display_name": "Docs"}},
            "models": {"default": {"provider": "openai", "id": "test-model", "context_window": 48000}},
        },
        paths,
    )
    persist_entity_accounts(config, paths)
    bob = "@bob:example.test"
    record = ContactRecord(
        id="contact",
        status="active",
        requester=ContactParty(kind="person", id=bob, owner=bob),
        target=ContactParty(kind="agent", id="code", owner=ALICE),
        reason="Chat",
        requester_side=ContactSide(),
        target_side=ContactSide(),
        origin_room_id="!origin:localhost",
        origin_thread_id=None,
        room_id=ROOM,
    )
    update_contacts(paths, lambda book: book.contacts.append(record))
    _, target, room, event = _compact_pair(config, paths)
    room.add_member(bob, "Bob", None)
    await _dispatch_message(target, room, event)
    assert _queued(config, paths, "code")
    assert await target.journal_principal().load_event(_ROW_ID) is None
    assert target.client.room_send.await_count == 0


async def _hydrate(bot: AgentBot, *, thread_id: str | None = THREAD) -> None:
    store = bot.journal_principal()
    assert await store.install_hydrated_conversation(
        room_id=ROOM,
        thread_id=thread_id,
        events=(),
        complete=True,
        expected_membership_epoch=await store.membership_epoch(ROOM),
    )


async def _start_fold(bot: AgentBot) -> asyncio.Task[None]:
    assert await bot._journal_dispatcher.drain_once() == 1
    [fold] = [
        task for task in bot._response_runner.inbox._inbox_response_tasks if task.get_name().startswith("compaction:")
    ]
    return fold


@pytest.mark.asyncio
@pytest.mark.parametrize("room_mode", [False, True])
async def test_compact_without_active_window_uses_explicit_summary_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    room_mode: bool,
) -> None:
    config, paths = _config(tmp_path, context_window=None)
    config.defaults.compaction = CompactionConfig(model="summary-model")
    config.models["summary-model"] = ModelConfig(provider="openai", id="summary-model", context_window=32_000)
    name = "lobby" if room_mode else "code"
    target = _live_bot(ready_runtime(config, paths), name)
    room = _room_with_members(ALICE, target.matrix_id.full_id, room_id=ROOM)
    room.members_synced = True
    if not room_mode:
        await _hydrate(target)
        _seed_history(config, paths, name)
    model = summary_model("summary without replay cap", model_id="summary-model", provider="openai")
    install_model_providers(monkeypatch, {"default": summary_model("room reply"), "summary-model": model})
    command = _addressed(target, f"/compact {name}", "$command")
    if room_mode:
        message = _addressed(target, "room history " * 500, "$first")
        for event in (message, command):
            del event.source["content"]["m.relates_to"]
        await _dispatch_message(target, room, message)
        await asyncio.wait_for(drain_coalescing(target), 30)
        assert "room reply" in str(target.client.room_send.await_args_list)
        assert not await target.journal_principal().conversation_is_hydrated(room_id=ROOM, thread_id=None)
    await _dispatch_message(target, room, command)
    await asyncio.wait_for(await _start_fold(target), 30)
    assert _summaries(config, paths, name, room_mode=room_mode) == ["summary without replay cap"]
    assert "→" in str(target.client.room_send.await_args_list)


@pytest.mark.asyncio
async def test_cancelled_flag_probe_keeps_ownership_until_store_open_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths = _config(tmp_path)
    _, target, room, command = _compact_pair(config, paths)
    await _dispatch_message(target, room, command)
    runner = target._response_runner
    loop, opening, finish = asyncio.get_running_loop(), asyncio.Event(), threading.Event()
    writer = runner.deps.state_writer
    create_storage = writer.create_storage

    def blocked_open(*args: Any, **kwargs: Any):  # noqa: ANN202, ANN401
        loop.call_soon_threadsafe(opening.set)
        assert finish.wait(30)
        return create_storage(*args, **kwargs)

    monkeypatch.setattr(writer, "create_storage", blocked_open)
    fold = await _start_fold(target)
    try:
        await asyncio.wait_for(opening.wait(), 30)
        fold.cancel()
        done, _ = await asyncio.wait({fold}, timeout=0.05)
        assert not done
        assert runner.inbox.has_live_inbox_response(_ROW_ID)
        assert runner.lifecycle_coordinator._conversation_for(_target()).lock.locked()
        assert not target.orchestrator.response_admission_gate.close_if_idle()
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(fold, 30)
    assert await target.journal_principal().is_pending(_ROW_ID)
    assert _queued(config, paths, "code")


@pytest.mark.asyncio
async def test_flag_read_failure_defers_once_in_current_membership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths = _config(tmp_path)
    _, target, room, command = _compact_pair(config, paths)
    await _dispatch_message(target, room, command)
    read = MagicMock(side_effect=[OSError("temporary metadata read failure"), True])
    monkeypatch.setattr(runner_module, "has_queued_force_compaction", read)
    build = MagicMock()
    monkeypatch.setattr(runner_module.provider_registry, "build_model_provider", build)
    await asyncio.wait_for(await _start_fold(target), 30)
    assert await target._journal_dispatcher.drain_once() == 0
    read.assert_called_once()
    build.assert_not_called()
    _assert_deferred(target)
    assert _queued(config, paths, "code")
    assert not await target.journal_principal().is_pending(_ROW_ID)


@pytest.mark.asyncio
async def test_pre_fold_provider_error_settles_without_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, paths = _config(tmp_path)
    _, target, room, command = _compact_pair(config, paths)
    await _hydrate(target)
    build = MagicMock(side_effect=RuntimeError("provider unavailable"))
    monkeypatch.setattr(runner_module.provider_registry, "build_model_provider", build)
    await _dispatch_message(target, room, command)
    await asyncio.wait_for(await _start_fold(target), 30)
    assert await target._journal_dispatcher.drain_once() == 0
    assert build.call_count == 1
    assert _ROW_ID not in await target.journal_principal().unsettled_event_ids()
    assert _queued(config, paths, "code")
    assert target.client.room_send.await_count == 2
    _assert_deferred(target)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_tenure", ["cold", "fork", "stale_origin", "stale_row"])
async def test_compaction_requires_current_hydrated_membership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_tenure: str,
) -> None:
    config, paths = _config(tmp_path)
    # A cached reply derived from the thread root. The target leaves before seeing its redaction.
    redacted_root = "$parent" if changed_tenure == "fork" else THREAD
    owner, target, room, _ = _compact_pair(config, paths, source_event_id=redacted_root)
    await _hydrate(target)
    origin = _addressed(owner, "/compact code", "$command")
    store = target.journal_principal()
    if changed_tenure not in {"cold", "fork"}:
        await admit_dispatch_event(target._journal_dispatcher, room, origin, EventKind.MESSAGE, EventClass.CONTEXT_ONLY)
    if changed_tenure == "stale_row":
        await _dispatch_message(owner, room, origin)
    await admit_room_membership(store, ROOM, "leave")
    await admit_room_membership(store, ROOM, "join")
    assert not await store.conversation_is_hydrated(room_id=ROOM, thread_id=THREAD)
    assert await store.room_history_recovery(ROOM) is None
    if changed_tenure not in {"cold", "fork"}:
        await _hydrate(target)
    else:
        if changed_tenure == "fork":
            child = ProjectedEvent(
                THREAD,
                ROOM,
                None,
                ALICE,
                1,
                {
                    "body": "fork root",
                    "msgtype": "m.text",
                    FORK_CONTENT_KEY: {"thread_id": redacted_root, "event_id": redacted_root},
                },
                None,
                None,
            )
            assert await store.install_hydrated_conversation(
                room_id=ROOM,
                thread_id=THREAD,
                events=(child,),
                complete=True,
                expected_membership_epoch=await store.membership_epoch(ROOM),
            )
            assert not await store.conversation_is_hydrated(room_id=ROOM, thread_id=redacted_root)
        root = nio.RoomGetEventResponse()
        root.event = nio.Event.parse_event(
            {
                "type": "m.room.message",
                "event_id": redacted_root,
                "sender": ALICE,
                "origin_server_ts": 0,
                "content": {},
                "unsigned": {"redacted_because": {"type": "m.room.redaction", "sender": ALICE, "content": {}}},
            },
        )
        target.client.room_get_event = AsyncMock(return_value=root)
        await admit_dispatch_event(target._journal_dispatcher, room, origin, EventKind.MESSAGE, EventClass.CONTEXT_ONLY)
    if changed_tenure != "stale_row":
        await _dispatch_message(owner, room, origin)
    compaction_hook = AsyncMock()

    @hook(EVENT_COMPACTION_BEFORE, scope="agent", agents=["code"])
    async def before(ctx: CompactionHookContext) -> None:
        await compaction_hook(ctx)

    target.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("check-no-fold", [before])])
    build = MagicMock(return_value=summary_model("should not fold"))
    monkeypatch.setattr(runner_module.provider_registry, "build_model_provider", build)
    await asyncio.wait_for(await _start_fold(target), 30)
    assert build.call_count == 0
    compaction_hook.assert_not_awaited()
    assert _ROW_ID not in await store.unsettled_event_ids()
    if changed_tenure in {"cold", "fork"}:
        target.client.room_get_event.assert_awaited_once_with(ROOM, redacted_root)
        assert not _queued(config, paths, "code")
        assert "Nothing to compact" in str(target.client.room_send.await_args_list)
    else:
        assert _queued(config, paths, "code")
        target.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    ["first_recovery_check", "after_scrub", "origin_timeout", "degraded", "read_failed", "rejoin_during_read"],
)
async def test_compaction_defers_when_target_history_is_not_ready(  # noqa: C901
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    config, paths = _config(tmp_path)
    owner, target, room, _ = _compact_pair(config, paths)
    store = target.journal_principal()
    await _hydrate(target)
    origin = _addressed(owner, "/compact code", "$command")
    if boundary != "origin_timeout":
        await admit_dispatch_event(target._journal_dispatcher, room, origin, EventKind.MESSAGE, EventClass.CONTEXT_ONLY)
    await _dispatch_message(owner, room, origin)
    scrub = session_context_module.remove_history_of_redacted_events
    if boundary == "first_recovery_check":
        assert await record_room_history_recovery(store, ROOM) is not None
        load = MagicMock(side_effect=AssertionError("open recovery should not load history"))
        monkeypatch.setattr(runner_module, "load_scope_session", load)
    elif boundary == "after_scrub":

        async def scrub_then_gap(*args: Any) -> None:  # noqa: ANN401
            await scrub(*args)
            assert await record_room_history_recovery(store, ROOM) is not None

        record_gap = AsyncMock(side_effect=scrub_then_gap)
        monkeypatch.setattr(session_context_module, "remove_history_of_redacted_events", record_gap)
    elif boundary in {"degraded", "read_failed", "rejoin_during_read"}:

        async def fail_read(_room: str, _thread: str) -> None:
            if boundary == "rejoin_during_read":
                await admit_room_membership(store, ROOM, "leave")
                await admit_room_membership(store, ROOM, "join")
            msg = "read failed"
            raise RuntimeError(msg)

        thread_read = AsyncMock(
            return_value=ThreadHistoryResult([], False, {THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True}),
            side_effect=None if boundary == "degraded" else fail_read,
        )
        monkeypatch.setattr(target._response_runner.deps.resolver, "fetch_thread_history", thread_read)
    build = MagicMock(return_value=summary_model("should not fold"))
    monkeypatch.setattr(runner_module.provider_registry, "build_model_provider", build)
    await asyncio.wait_for(await _start_fold(target), 30)
    assert not build.call_count
    if boundary in {"degraded", "read_failed", "rejoin_during_read"}:
        thread_read.assert_awaited_once_with(ROOM, THREAD)
    if boundary == "first_recovery_check":
        load.assert_not_called()
    if boundary == "after_scrub":
        record_gap.assert_awaited_once()
    assert _queued(config, paths, "code")
    assert _ROW_ID not in await store.unsettled_event_ids()
    if boundary in {"origin_timeout", "rejoin_during_read"}:
        target.client.room_send.assert_not_awaited()
    else:
        _assert_deferred(target)


def _assert_deferred(bot: AgentBot) -> None:
    [notice] = [call.kwargs["content"] for call in bot.client.room_send.await_args_list if _DEFERRED in str(call)]
    assert notice["msgtype"] == "m.notice"
    assert notice[SKIP_MENTIONS_KEY] is True


@pytest.mark.asyncio
async def test_empty_manual_fold_reports_nothing_to_compact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, paths = _config(tmp_path)
    target = _live_bot(ready_runtime(config, paths), "code")
    room = _room_with_members(ALICE, target.matrix_id.full_id, room_id=ROOM)
    room.members_synced = True
    await _hydrate(target)
    _seed(config, paths, "code")
    build = MagicMock(side_effect=AssertionError("empty history does not need a model"))
    monkeypatch.setattr(runner_module.provider_registry, "build_model_provider", build)
    await _dispatch_message(target, room, _addressed(target, "/compact code", "$command"))
    await asyncio.wait_for(await _start_fold(target), 30)
    build.assert_not_called()
    notice = target.client.room_send.await_args.kwargs["content"]
    assert "Nothing to compact" in notice["body"]
    assert notice[SKIP_MENTIONS_KEY] is True
    assert not _queued(config, paths, "code")


@pytest.mark.asyncio
@pytest.mark.parametrize("settled", [False, True])
async def test_duplicate_compaction_row_only_promises_immediate_work_when_pending(
    tmp_path: Path,
    settled: bool,
) -> None:
    config, paths = _config(tmp_path)
    owner, target, room, _ = _compact_pair(config, paths)
    assert await target.admit_compaction(_target(), ALICE)
    store = target.journal_principal()
    if settled:
        await store.settle(_ROW_ID)
    await _dispatch_message(owner, room, _addressed(owner, "/compact code", "$command"))
    expected = "before its next reply" if settled else "compacting now"
    assert expected in str(owner.client.room_send.await_args_list)
    assert await store.unsettled_event_ids() == (set() if settled else {_ROW_ID})


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_row", [False, True])
async def test_departed_target_with_stale_client_room_does_not_fold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_row: bool,
) -> None:
    config, paths = _config(tmp_path)
    runtime = ready_runtime(config, paths)
    owner, target = _live_bot(runtime, "docs"), _live_bot(runtime, "lobby")
    room = _room_with_members(ALICE, owner.matrix_id.full_id, target.matrix_id.full_id, room_id=ROOM)
    room.members_synced = True
    target.client.rooms[ROOM] = room
    _seed_history(config, paths, "lobby", room_mode=True)
    store = target.journal_principal()
    await admit_room_membership(store, ROOM, "join")
    epoch = await admit_room_membership(store, ROOM, "leave")
    command = _addressed(owner, "/compact lobby", "$command")
    del command.source["content"]["m.relates_to"]
    # A fenced observation keeps the origin's identity, not live work.
    await admit_dispatch_event(target._journal_dispatcher, room, command, EventKind.MESSAGE, EventClass.CONTEXT_ONLY)
    assert await store.admitted_membership_owner(command.event_id) == (ROOM, epoch)
    source = "compact:$command:lobby"
    if legacy_row:
        event = InboundEvent(
            event_id=source,
            room_id=ROOM,
            thread_id=None,
            kind=EventKind.COMPACTION,
            event_class=EventClass.ACTIONABLE,
            sender=target.matrix_id.full_id,
            origin_server_ts=1,
            source={"requester_id": ALICE, "origin_event_id": command.event_id},
        )
        await store._write(journal._admit, event, None)  # A row admitted by the old binary while fenced.
        assert await store.admitted_membership_owner(source) == (ROOM, epoch)
    await _dispatch_message(owner, room, command)
    build, called = MagicMock(), AsyncMock()
    monkeypatch.setattr(runner_module.provider_registry, "build_model_provider", build)

    @hook(EVENT_COMPACTION_BEFORE, scope="agent", agents=["lobby"])
    async def before(ctx: CompactionHookContext) -> None:
        await called(ctx)

    target.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("fenced", [before])])
    if legacy_row:
        await asyncio.wait_for(await _start_fold(target), 30)
    else:
        assert await target._journal_dispatcher.drain_once() == 0
        assert await store.load_event(source) is None
    build.assert_not_called()
    called.assert_not_awaited()
    target.client.room_send.assert_not_awaited()
    assert _queued(config, paths, "lobby", room_mode=True)
    assert not await store.is_pending(source)
    assert "before its next reply" in str(owner.client.room_send.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", ["not_joined", "admission_failed"])
async def test_unavailable_target_only_gets_the_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unavailable: str,
) -> None:
    config, paths = _config(tmp_path)
    owner, target, room, _ = _compact_pair(config, paths)
    if unavailable == "not_joined":
        room.remove_member(target.matrix_id.full_id)
        target.client.rooms = {}  # The generic test room cache auto-creates rooms on access.
    else:
        monkeypatch.setattr(target, "admit_compaction", AsyncMock(side_effect=OSError("journal unavailable")))
    await _dispatch_message(owner, room, _addressed(owner, "/compact code", "$command"))
    assert await target.journal_principal().load_event(_ROW_ID) is None
    assert _queued(config, paths, "code")
    assert "before its next reply" in str(owner.client.room_send.await_args_list)
