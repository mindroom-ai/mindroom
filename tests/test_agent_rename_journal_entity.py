"""Tests for renaming an agent's journal principal and every journal value keyed by its config name."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from agno.models.response import ToolExecution

from mindroom import reply_lifecycle as rl
from mindroom.agent_rename.journal_entity import journal_entity_steps
from mindroom.agent_rename.steps import RenamePlan, RenameRefusedError, SqliteEdit, apply_plan
from mindroom.delegation.state import DelegationChild
from mindroom.event_journal import (
    ApprovalCall,
    ApprovalContinuation,
    ApprovalDecision,
    EventClass,
    EventJournalStore,
    EventKind,
    InboundEvent,
    IngestionBatchAdmission,
    PrincipalStore,
    ProjectedEvent,
    encode_thread_id,
)
from mindroom.event_journal.approval_continuations import approval_arguments_digest
from mindroom.event_journal.models import IngestionConsumer
from mindroom.event_journal.opening import event_journal_sqlite_path
from mindroom.event_journal.replies import PreparedReplyRow, ReplyRowRequest
from mindroom.handled_turns import HandledTurnLedger, TurnRecord, _reset_handled_turn_ledger_runtime
from mindroom.history.types import HistoryScope
from mindroom.reply_presentation import Presentation, Segment, current_answer, decode_presentation, encode_presentation
from mindroom.response_sources import ResponseSources
from mindroom.teams import _TeamStreamPresentation
from mindroom.tool_jobs.runtime import BackgroundJob
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.agent_rename_fixtures.journal import (
    ROOM,
    bound_principal,
    membership_admission,
    message_admission,
    record_turn,
)
from tests.agent_rename_helpers import tree_snapshot
from tests.approval_continuation_helpers import approval_continuation
from tests.conftest import message_origin
from tests.reply_span_helpers import paused_for_approval, reply_shown_for_approval

if TYPE_CHECKING:
    from pathlib import Path

OLD = "code"
NEW = "zeta"
# Shares OLD's name as a prefix; renaming OLD must leave all of it alone.
PREFIX = "code_team"
HELPER = "helper"
USER_ID = "@code:x"
# bot.py joins the agent name and the Matrix ID with one "@", and the ID starts with "@".
OLD_PRINCIPAL = "code@@code:x"
NEW_PRINCIPAL = "zeta@@code:x"
PREFIX_PRINCIPAL = "code_team@@code_team:x"
HELPER_PRINCIPAL = "helper@@helper:x"
UNTHREADED = encode_thread_id(None)
NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
# The scope a requester-partitioned ad-hoc team gets when one member is private (history/team_scope.py).
REQUESTER_SUFFIX = "_requester_0123456789ab"
# A configured team whose name is shaped like an ad-hoc scope id of the agent alone.
CONFIGURED_TEAM = f"team_{OLD}"
CONFIGURED_TEAMS = frozenset({CONFIGURED_TEAM})


def _steps(database: Path) -> list[SqliteEdit]:
    return journal_entity_steps(
        database,
        old_name=OLD,
        new_name=NEW,
        user_id=USER_ID,
        configured_teams=CONFIGURED_TEAMS,
    )


def _apply(steps: list[SqliteEdit], storage_root: Path) -> None:
    apply_plan(RenamePlan(storage_root=storage_root, steps=tuple(steps), reports=()), kind="rename", now=NOW)


def _query(database: Path, sql: str, *params: object) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute(sql, params).fetchall()


def _rows(database: Path) -> dict[str, list[tuple[object, ...]]]:
    """Every row of every table, for comparing a database before and after."""
    with closing(sqlite3.connect(database)) as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {table: sorted(connection.execute(f'SELECT * FROM "{table}"'), key=repr) for table in tables}  # noqa: S608


def _holds(row: tuple[object, ...], needle: str) -> bool:
    """Whether a text cell of ``row`` is ``needle`` or holds it as a JSON string."""
    return any(isinstance(value, str) and (value == needle or f'"{needle}"' in value) for value in row)


def _rows_holding(database: Path, *needles: str, without: tuple[str, ...] = ()) -> list[tuple[str, tuple[object, ...]]]:
    """Every row holding one of ``needles`` and none of ``without``."""
    return sorted(
        (table, row)
        for table, rows in _rows(database).items()
        for row in rows
        if any(_holds(row, needle) for needle in needles) and not any(_holds(row, other) for other in without)
    )


def _insert(connection: sqlite3.Connection, table: str, **values: object) -> None:
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values()))  # noqa: S608


async def _synced_journal(database: Path) -> tuple[UUID, UUID]:
    """Sync one batch for the agent and for the prefix agent; return the agent's stream and consumer generation."""
    stream = uuid4()
    store = EventJournalStore.open_sqlite(database)
    try:
        for principal_id, principal_stream in ((OLD_PRINCIPAL, stream), (PREFIX_PRINCIPAL, uuid4())):
            principal = await bound_principal(store, principal_id, principal_stream)
            batch = (membership_admission(None, "join", 0, 0), message_admission("$from-alice"))
            await principal.admit_ingestion_batch(IngestionBatchAdmission(principal_stream, 1, batch))
        consumer = await store.principal(OLD_PRINCIPAL).load_or_create_ingestion_consumer(new_generation=uuid4())
    finally:
        await store.close()
    return stream, consumer.generation


_PRINCIPAL_CELLS = """
    SELECT 'matrix_sync_consumers', principal_id FROM matrix_sync_consumers
    UNION ALL SELECT 'matrix_ingestion_membership', principal_id FROM matrix_ingestion_membership
    UNION ALL SELECT 'journal_events', principal_id FROM journal_events
    UNION ALL SELECT 'room_membership', principal_id FROM room_membership
"""


@pytest.mark.asyncio
async def test_principal_and_its_sync_stream_are_renamed_and_the_stream_continues(tmp_path: Path) -> None:
    """The principal keeps its consumer and producer positions, so the same device's stream goes on admitting.

    The Matrix account and device stay, so the next sync continues the stream at its next sequence; re-delivered
    events deduplicate instead of failing with IngestionBatchIntegrityError.
    """
    database = event_journal_sqlite_path(tmp_path)
    stream, generation = await _synced_journal(database)
    consumer = "SELECT consumer_generation, stream_id, next_sequence FROM matrix_sync_consumers WHERE principal_id = ?"
    producer = "SELECT room_id, membership, membership_epoch FROM matrix_ingestion_membership WHERE principal_id = ?"
    old_consumer, old_producer = _query(database, consumer, OLD_PRINCIPAL), _query(database, producer, OLD_PRINCIPAL)
    prefix_rows = _rows_holding(database, PREFIX_PRINCIPAL)
    assert prefix_rows
    assert old_consumer == [(str(generation), str(stream), 2)]
    assert old_producer == [(ROOM, "join", 0)]

    _apply(_steps(database), tmp_path)

    assert _query(database, consumer, NEW_PRINCIPAL) == old_consumer
    assert _query(database, producer, NEW_PRINCIPAL) == old_producer
    assert _rows_holding(database, OLD_PRINCIPAL) == []
    assert _rows_holding(database, PREFIX_PRINCIPAL) == prefix_rows
    assert {table for table, principal in _query(database, _PRINCIPAL_CELLS) if principal == NEW_PRINCIPAL} == {
        "matrix_sync_consumers",
        "matrix_ingestion_membership",
        "journal_events",
        "room_membership",
    }
    store = EventJournalStore.open_sqlite(database)
    try:
        renamed = store.principal(NEW_PRINCIPAL)
        assert await renamed.load_or_create_ingestion_consumer(new_generation=uuid4()) == IngestionConsumer(
            generation,
            stream,
        )
        await renamed.bind_ingestion_stream(generation=generation, stream_id=stream)
        redelivered = await renamed.admit_ingestion_batch(
            IngestionBatchAdmission(stream, 2, (message_admission("$from-alice"),)),
        )
        fresh = await renamed.admit_ingestion_batch(IngestionBatchAdmission(stream, 3, (message_admission("$next"),)))
        history = await renamed.load_event("$from-alice")
    finally:
        await store.close()
    assert redelivered.receipt_new
    assert not redelivered.semantic_event_new
    assert fresh.semantic_event_new
    assert history is not None


async def _turn_record(database: Path, agent: str, event_id: str) -> TurnRecord | None:
    """Read ``agent``'s record for ``event_id`` through a freshly loaded handled-turn ledger."""
    _reset_handled_turn_ledger_runtime()
    store = EventJournalStore.open_sqlite(database)
    try:
        ledger = HandledTurnLedger(agent, records=store.turn_records(agent))
        await ledger.load()
        return ledger.get_turn_record(event_id) if ledger.has_responded(event_id) else None
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.ledger_loads_from_disk
async def test_turn_handled_under_the_old_name_is_handled_under_the_new_name(tmp_path: Path) -> None:
    """The renamed agent's ledger finds the turns it answered as OLD, owned by it and in its renamed scope."""
    database = event_journal_sqlite_path(tmp_path)
    store = EventJournalStore.open_sqlite(database)
    try:
        await record_turn(store, OLD, "$from-alice", HistoryScope(kind="agent", scope_id=OLD))
        await record_turn(store, PREFIX, "$from-alice", HistoryScope(kind="agent", scope_id=PREFIX))
    finally:
        await store.close()
    prefix_record = await _turn_record(database, PREFIX, "$from-alice")

    _apply(_steps(database), tmp_path)

    renamed = await _turn_record(database, NEW, "$from-alice")
    assert renamed is not None
    assert renamed.response_owner == NEW
    assert renamed.history_scope == HistoryScope(kind="agent", scope_id=NEW)
    assert renamed.response_event_id == f"$answer-{OLD}-$from-alice"
    assert await _turn_record(database, OLD, "$from-alice") is None
    assert await _turn_record(database, PREFIX, "$from-alice") == prefix_record


@pytest.mark.asyncio
@pytest.mark.ledger_loads_from_disk
async def test_ad_hoc_team_scopes_take_the_recomputed_id(tmp_path: Path) -> None:
    """An ad-hoc team scope with the agent gets the id of the renamed member set, sorted again, suffix kept.

    The member that answered keeps its own name. A configured team's own scope is its name, even one shaped like
    an ad-hoc id, and a scope naming the prefix agent is a different member set.
    """
    team_name = CONFIGURED_TEAM
    scopes = {
        "$team": (HELPER, f"team_{OLD}+{HELPER}"),
        "$private-team": (HELPER, f"team_{OLD}+{HELPER}{REQUESTER_SUFFIX}"),
        "$prefix-team": (HELPER, f"team_{PREFIX}+{HELPER}"),
        "$configured-team": (team_name, team_name),
    }
    database = event_journal_sqlite_path(tmp_path)
    store = EventJournalStore.open_sqlite(database)
    try:
        for event_id, (agent, scope_id) in scopes.items():
            await record_turn(store, agent, event_id, HistoryScope(kind="team", scope_id=scope_id))
    finally:
        await store.close()

    _apply(_steps(database), tmp_path)

    renamed_scopes = {}
    for event_id, (agent, _scope_id) in scopes.items():
        record = await _turn_record(database, agent, event_id)
        assert record is not None
        assert record.response_owner == agent
        renamed_scopes[event_id] = record.history_scope
    # `zeta` sorts after `helper`, so the members are sorted again.
    assert renamed_scopes == {
        "$team": HistoryScope(kind="team", scope_id=f"team_{HELPER}+{NEW}"),
        "$private-team": HistoryScope(kind="team", scope_id=f"team_{HELPER}+{NEW}{REQUESTER_SUFFIX}"),
        "$prefix-team": HistoryScope(kind="team", scope_id=f"team_{PREFIX}+{HELPER}"),
        "$configured-team": HistoryScope(kind="team", scope_id=team_name),
    }


def _identity(agent: str, *, transport: str | None = None) -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent,
        requester_id="@alice:x",
        room_id=ROOM,
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-1",
        transport_agent_name=transport,
    )


def _bindings(*agents: str) -> dict[str, dict[str, object]]:
    """Delegation storage bindings as delegation/storage.py freezes them."""
    return {agent: {"display_name": agent, "private": None, "worker_scope": None} for agent in agents}


def _call(tool_call_id: str, agent: str) -> ApprovalCall:
    return ApprovalCall(
        tool_call_id=tool_call_id,
        tool_name="shell",
        invoking_agent=agent,
        expires_at_ns=time.time_ns() + 60_000_000_000,
        decision=ApprovalDecision.APPROVED,
        human_approval_required=True,
        arguments_digest=approval_arguments_digest({"cmd": "ls"}),
    )


async def _admit_source(principal: PrincipalStore, event_id: str) -> None:
    """Admit alice's message that a paused response answers."""
    content = {"msgtype": "m.text", "body": "run it"}
    inbound = InboundEvent(
        event_id=event_id,
        room_id=ROOM,
        thread_id=None,
        kind=EventKind.MESSAGE,
        event_class=EventClass.ACTIONABLE,
        sender="@alice:x",
        origin_server_ts=10,
        source={"event_id": event_id, "content": content},
    )
    projected = ProjectedEvent(
        event_id=event_id,
        room_id=ROOM,
        thread_id=None,
        sender="@alice:x",
        origin_server_ts=10,
        content=content,
        replaces_event_id=None,
        redacts_event_id=None,
    )
    await principal.admit(inbound, projected)


TEAM_MEMBERS = (OLD, PREFIX, HELPER)
TEAM_SCOPE = f"team_{OLD}+{PREFIX}+{HELPER}"
# `zeta` sorts after the other members, so the renamed scope lists them sorted again.
RENAMED_TEAM_SCOPE = f"team_{PREFIX}+{HELPER}+{NEW}"


def _team_continuation() -> ApprovalContinuation:
    """A paused ad-hoc team answer by helper, with the agent and the prefix agent; their calls wait for approval."""
    return approval_continuation(
        approval_id="team-approval",
        entity_kind="team",
        entity_name=HELPER,
        room_id=ROOM,
        requester_id="@alice:x",
        origin=message_origin(sender_id=USER_ID, requester_id="@alice:x", sender_entity_name=OLD),
        response_event_id="$team-reply",
        sources=ResponseSources(("$team-source",), ("$team-source",)),
        calls=(_call("call-1", OLD), _call("call-2", PREFIX)),
        team_member_names=TEAM_MEMBERS,
        team_member_model_names=tuple((member, "model-a") for member in TEAM_MEMBERS),
        team_mode="coordinate",
        history_scope=HistoryScope(kind="team", scope_id=TEAM_SCOPE),
        execution_identity=asdict(_identity(OLD, transport=HELPER)),
        mentioned_agents=(OLD, PREFIX),
        delegation_storage_bindings=_bindings(OLD, PREFIX),
        cli_call={
            "kind": "agent_cli",
            "requirements": [{"member_agent_id": OLD}, {"member_agent_id": PREFIX}],
            "external_requirement": {"member_agent_id": OLD},
        },
        show_tool_calls=False,
        request_body=f"ask {OLD} and {PREFIX}",
    )


def _team_presentation(span_id: str) -> str:
    """The team document a paused team reply shows, written by MindRoom's team stream with hidden tool calls."""
    team = _TeamStreamPresentation.new(TEAM_MEMBERS, ["Code", "Code Team", "Helper"], show_tool_calls=False)
    for member_id in team.member_ids:
        team.append_member(member_id, f"{member_id} thinks")
    team.start_member_tool("code", ToolExecution(tool_call_id="call-1", tool_name="shell", tool_args={"cmd": "ls"}))
    team.start_member_tool(
        "code-team",
        ToolExecution(tool_call_id="call-2", tool_name="shell", tool_args={"cmd": "ls"}),
    )
    segment = Segment(
        kind="answer",
        text=team.render_body(),
        span_id=span_id,
        tool_trace=tuple(team.tool_trace),
        team_state=team.to_state(),
    )
    return encode_presentation(Presentation(segments=(segment,), show_tool_calls=False))


async def _pause_team_reply(principal: PrincipalStore, continuation: ApprovalContinuation) -> str:
    """Pause the team's reply for approval the way a response does, showing its team document; return the reply id."""
    span = await reply_shown_for_approval(principal, continuation)
    assert span is not None
    shown = _team_presentation(span.span_id)
    enqueued = await principal.pause_for_approval(
        continuation,
        ReplyRowRequest(
            reply_id=span.reply_id,
            span_id=span.span_id,
            decide=lambda reply, held: rl.pause(
                reply,
                held,
                rl.PauseWrite(shown=shown, prepared_revision=reply.revision, stage=rl.WriteStage.EDIT),
                in_place=False,
                now_ns=time.time_ns(),
            ),
        ),
        PreparedReplyRow(payload={"msgtype": "m.text", "body": "waiting"}),
    )
    assert enqueued is not None
    assert enqueued.transition.applied
    return span.reply_id


def _grant(connection: sqlite3.Connection, principal: str, grant_id: str, *, agent: str, responder: str) -> None:
    _insert(
        connection,
        "approval_grants",
        principal_id=principal,
        grant_id=grant_id,
        scope_key=f"scope-{grant_id}",
        room_id=ROOM,
        thread_id="$thread",
        card_event_id=f"$card-{grant_id}",
        requester_id="@alice:x",
        entity_name=agent,
        invoking_agent=agent,
        operation="shell:run",
        responder_principal_id=responder,
        responder_epoch=0,
        membership_epoch=0,
        expires_at_ns=1,
        resolution_json="{}",
        original_delivery_id=f"$delivery-{grant_id}",
    )


def _grant_card(connection: sqlite3.Connection, principal: str, *, entity: str, invoking: str, responder: str) -> None:
    _insert(
        connection,
        "approval_grant_cards",
        principal_id=principal,
        continuation_id="continuation-1",
        continuation_generation=0,
        tool_call_id=f"call-{invoking}",
        delivery_id=f"$card-delivery-{invoking}",
        scope_key=f"scope-card-{invoking}",
        room_id=ROOM,
        thread_id="$thread",
        requester_id="@alice:x",
        entity_name=entity,
        invoking_agent=invoking,
        operation="shell:run",
        responder_principal_id=responder,
        responder_epoch=0,
        membership_epoch=0,
    )


def _reply(connection: sqlite3.Connection, principal: str, agent: str) -> None:
    _insert(
        connection,
        "reply_messages",
        principal_id=principal,
        reply_id=f"reply-{agent}",
        entity_name=agent,
        room_id=ROOM,
        membership_epoch=0,
        state="completed",
        last_span_id="span-1",
        presentation_json="{}",
        revision=1,
        placeholder_only=False,
        reply_sequence=1,
        created_at_ns=1,
        updated_at_ns=1,
    )


def _scheduled_call(connection: sqlite3.Connection, principal: str, agent: str) -> None:
    _insert(
        connection,
        "scheduled_call_approvals",
        principal_id=principal,
        task_id=f"task-{agent}",
        delivery_id=f"$scheduled-{agent}",
        room_id=ROOM,
        thread_id="$thread",
        requester_id="@alice:x",
        agent_name=agent,
        toolkit_name="shell",
        tool_name="run",
        arguments_json="{}",
        workflow_digest="digest",
        execute_at_ns=1,
        membership_epoch=0,
    )


def _job(connection: sqlite3.Connection, job: BackgroundJob) -> None:
    # Saved like tool_jobs/runtime.py's _encoded.
    _insert(connection, "tool_jobs", job_id=job.job_id, job_json=json.dumps(asdict(job)))


def _delegation_job(job_id: str, *, caller: str, child: str) -> BackgroundJob:
    """A background delegation job snapshotting its child like delegation/background.py does."""
    snapshot = DelegationChild(
        delegation_id=job_id,
        parent_tool_call_id="call-1",
        caller_agent_name=caller,
        child_agent_name=child,
        task=f"ask {child}",
        session_id="session-1",
        run_id="run-1",
        model_name="model-a",
        depth=1,
        execution_identity=asdict(_identity(child)),
        storage_bindings=_bindings(caller, child),
    )
    return BackgroundJob(
        job_id=job_id,
        owner=_identity(caller),
        tool_name=child,
        depth=0,
        kind="delegation",
        adapter={"child": asdict(snapshot)},
    )


async def _approval_journal(storage_root: Path) -> Path:
    """Build a journal with paused approvals, replies, grants and jobs naming the agent and the prefix agent.

    The paused approvals are written through the journal store as a pausing response writes them.
    """
    database = event_journal_sqlite_path(storage_root)
    store = EventJournalStore.open_sqlite(database)
    try:
        own = store.principal(OLD_PRINCIPAL)
        await _admit_source(own, "$own-source")
        own_continuation = approval_continuation(
            approval_id="own-approval",
            entity_name=OLD,
            room_id=ROOM,
            requester_id="@alice:x",
            response_event_id="$own-reply",
            sources=ResponseSources(("$own-source",), ("$own-source",)),
            calls=(_call("call-own", OLD),),
            history_scope=HistoryScope(kind="agent", scope_id=OLD),
            execution_identity=asdict(_identity(OLD)),
        )
        assert await paused_for_approval(own, own_continuation, text="waiting") is not None
        helper = store.principal(HELPER_PRINCIPAL)
        await _admit_source(helper, "$team-source")
        await _pause_team_reply(helper, _team_continuation())
    finally:
        await store.close()
    with closing(sqlite3.connect(database)) as connection, connection:
        # A regeneration span of a finished team reply keeps the document to restore if it fails.
        rollback = {"presentation": _team_presentation("regenerated-span"), "state": "completed"}
        _insert(
            connection,
            "reply_spans",
            principal_id=HELPER_PRINCIPAL,
            span_id="regeneration-span",
            reply_id="finished-team-reply",
            kind="regeneration",
            delivery_id="$team-source",
            bot_generation="gen-old",
            base_sequence=0,
            # Encoded like event_journal/reply_spans.py's _rollback_json.
            rollback_json=json.dumps(rollback, separators=(",", ":"), sort_keys=True),
            claimed_at_ns=1,
        )
        for role in ("pending", "logical"):
            _insert(
                connection,
                "reply_span_sources",
                principal_id=HELPER_PRINCIPAL,
                span_id="regeneration-span",
                event_id="$team-source",
                role=role,
                ordinal=0,
            )
        _reply(connection, PREFIX_PRINCIPAL, PREFIX)
        for principal, agent in ((OLD_PRINCIPAL, OLD), (PREFIX_PRINCIPAL, PREFIX)):
            _scheduled_call(connection, principal, agent)
            _grant_card(connection, principal, entity=agent, invoking=agent, responder=principal)
        # helper's card for the agent's call in their team: its invoking agent and responder are the agent.
        _grant_card(connection, HELPER_PRINCIPAL, entity=HELPER, invoking=OLD, responder=OLD_PRINCIPAL)
        _grant(connection, OLD_PRINCIPAL, "own-grant", agent=OLD, responder=OLD_PRINCIPAL)
        _grant(connection, HELPER_PRINCIPAL, "team-grant-for-code", agent=OLD, responder=HELPER_PRINCIPAL)
        _grant(connection, HELPER_PRINCIPAL, "helper-grant", agent=HELPER, responder=HELPER_PRINCIPAL)
        _grant(connection, PREFIX_PRINCIPAL, "prefix-grant", agent=PREFIX, responder=PREFIX_PRINCIPAL)
        _insert(connection, "approval_grant_locks", principal_id=OLD_PRINCIPAL)
        _job(connection, BackgroundJob(job_id="own-job", owner=_identity(OLD), tool_name="shell", depth=0))
        _job(
            connection,
            BackgroundJob(job_id="team-job", owner=_identity(OLD, transport=HELPER), tool_name="shell", depth=0),
        )
        _job(connection, _delegation_job("delegation-job", caller=HELPER, child=OLD))
        _job(connection, _delegation_job("prefix-job", caller=HELPER, child=PREFIX))
    return database


def _without_times(job: dict[str, Any]) -> dict[str, Any]:
    """A saved job without the timestamps it takes when created."""
    return {key: value for key, value in job.items() if key not in {"created_at", "updated_at"}}


def _restore_team_document(stored: str, member_names: tuple[str, ...]) -> _TeamStreamPresentation:
    """Restore a stored team document the way an approval resume does (teams.py's continue_paused_team_run)."""
    segment = current_answer(decode_presentation(stored), None)
    assert segment is not None
    return _TeamStreamPresentation.restore(
        config_names=member_names,
        show_tool_calls=False,
        state=segment.team_state,
        tool_trace=segment.tool_trace,
    )


@pytest.mark.asyncio
async def test_paused_team_approval_with_the_agent_resumes_under_the_new_name(tmp_path: Path) -> None:
    """The continuation, its reply's team document and its tool scopes all name the new member, so resume restores.

    Renaming only the continuation's member names made the frozen document disagree with them, and the resume
    failed with "Team continuation presentation snapshot is invalid".
    """
    database = await _approval_journal(tmp_path)

    _apply(_steps(database), tmp_path)

    store = EventJournalStore.open_sqlite(database)
    try:
        helper = store.principal(HELPER_PRINCIPAL)
        continuation = await helper.approval_continuation("team-approval")
        assert continuation is not None
        assert continuation.span_id is not None
        paused_span = await helper.replies.span(continuation.span_id)
        assert paused_span is not None
        reply = await helper.replies.load(paused_span.reply_id)
        regeneration = await helper.replies.span("regeneration-span")
    finally:
        await store.close()
    assert continuation.team_member_names == (NEW, PREFIX, HELPER)
    assert continuation.team_member_model_names == ((NEW, "model-a"), (PREFIX, "model-a"), (HELPER, "model-a"))
    assert continuation.history_scope == HistoryScope(kind="team", scope_id=RENAMED_TEAM_SCOPE)
    assert [call.invoking_agent for call in continuation.calls] == [NEW, PREFIX]
    assert continuation.execution_identity == asdict(_identity(NEW, transport=HELPER))
    assert continuation.mentioned_agents == (NEW, PREFIX)
    assert continuation.delegation_storage_bindings == {**_bindings(NEW), **_bindings(PREFIX)}
    assert continuation.cli_call == {
        "kind": "agent_cli",
        "requirements": [{"member_agent_id": NEW}, {"member_agent_id": PREFIX}],
        "external_requirement": {"member_agent_id": NEW},
    }
    assert continuation.origin.sender_entity_name == NEW
    assert continuation.request_body == f"ask {OLD} and {PREFIX}"
    assert reply is not None
    assert reply.possibly_shown is not None
    assert regeneration is not None
    assert regeneration.rollback is not None
    for stored in (reply.presentation, reply.possibly_shown, regeneration.rollback.presentation):
        restored = _restore_team_document(stored, continuation.team_member_names)
        assert restored.config_names_by_id == {NEW: NEW, "code-team": PREFIX, HELPER: HELPER}
        assert restored.per_member[NEW] == "code thinks"
        assert [entry.scope_key for entry in restored.tool_trace] == [f"agent:{NEW}", "agent:code-team"]
        assert restored.separate_next_scopes == {f"agent:{NEW}", "agent:code-team"}


@pytest.mark.asyncio
async def test_approval_reply_and_job_rows_follow_the_agent(tmp_path: Path) -> None:
    """Every name column and agent field of the approval, reply and job rows takes the new name; grants go."""
    database = await _approval_journal(tmp_path)
    # Rows naming the agent as well change; every other row naming the prefix agent stays exactly as it is.
    prefix_rows = _rows_holding(database, PREFIX, PREFIX_PRINCIPAL, without=(OLD, OLD_PRINCIPAL))
    assert len(prefix_rows) == 6

    _apply(_steps(database), tmp_path)

    store = EventJournalStore.open_sqlite(database)
    try:
        own = await store.principal(NEW_PRINCIPAL).approval_continuation("own-approval")
    finally:
        await store.close()
    assert own is not None
    assert own.entity_name == NEW
    assert own.history_scope == HistoryScope(kind="agent", scope_id=NEW)
    assert own.execution_identity == asdict(_identity(NEW))
    assert [call.invoking_agent for call in own.calls] == [NEW]
    assert _query(database, "SELECT principal_id, entity_name FROM reply_messages ORDER BY entity_name") == [
        (PREFIX_PRINCIPAL, PREFIX),
        (HELPER_PRINCIPAL, HELPER),
        (NEW_PRINCIPAL, NEW),
    ]
    assert _query(database, "SELECT principal_id, agent_name FROM scheduled_call_approvals ORDER BY agent_name") == [
        (PREFIX_PRINCIPAL, PREFIX),
        (NEW_PRINCIPAL, NEW),
    ]
    # A card keeps its scope key: the grant a click on it creates still covers the calls it was posted for.
    card = (
        "SELECT principal_id, entity_name, invoking_agent, responder_principal_id, scope_key FROM approval_grant_cards"
    )
    assert sorted(_query(database, card)) == [
        (PREFIX_PRINCIPAL, PREFIX, PREFIX, PREFIX_PRINCIPAL, f"scope-card-{PREFIX}"),
        (HELPER_PRINCIPAL, HELPER, NEW, NEW_PRINCIPAL, f"scope-card-{OLD}"),
        (NEW_PRINCIPAL, NEW, NEW, NEW_PRINCIPAL, f"scope-card-{OLD}"),
    ]
    # A grant's scope key hashes the invoking agent and the responder principal, so no grant naming either matches again.
    assert _query(database, "SELECT grant_id FROM approval_grants ORDER BY grant_id") == [
        ("helper-grant",),
        ("prefix-grant",),
    ]
    assert _query(database, "SELECT principal_id FROM approval_grant_locks") == [(NEW_PRINCIPAL,)]

    jobs = {
        job_id: _without_times(json.loads(str(text)))
        for job_id, text in _query(database, "SELECT job_id, job_json FROM tool_jobs")
    }
    assert jobs["own-job"]["owner"] == asdict(_identity(NEW))
    assert jobs["team-job"]["owner"] == asdict(_identity(NEW, transport=HELPER))
    delegation = _without_times(asdict(_delegation_job("delegation-job", caller=HELPER, child=NEW)))
    # The task is prose the requester wrote, never rewritten.
    delegation["adapter"]["child"]["task"] = f"ask {OLD}"
    assert jobs["delegation-job"] == delegation

    # Only the rows of payloads Matrix already holds keep the delivery identity they were sent with; the outbox
    # matches a sent event by its event or transaction ID when that identity names another principal.
    leftovers = _rows_holding(database, OLD, OLD_PRINCIPAL)
    assert [(table, row[0]) for table, row in leftovers] == [("matrix_delivery_outbox", NEW_PRINCIPAL)] * 2
    assert set(prefix_rows) <= set(_rows_holding(database, PREFIX, PREFIX_PRINCIPAL))


@pytest.mark.asyncio
@pytest.mark.ledger_loads_from_disk
async def test_journal_naming_only_other_agents_needs_no_step(tmp_path: Path) -> None:
    """A journal without rows for the agent, nor values naming it, is left alone; so is a missing journal."""
    assert _steps(event_journal_sqlite_path(tmp_path)) == []
    database = event_journal_sqlite_path(tmp_path)
    store = EventJournalStore.open_sqlite(database)
    try:
        await bound_principal(store, PREFIX_PRINCIPAL, uuid4())
        await record_turn(store, PREFIX, "$from-alice", HistoryScope(kind="team", scope_id=f"team_{PREFIX}+{HELPER}"))
    finally:
        await store.close()

    assert _steps(database) == []


@pytest.mark.asyncio
async def test_planning_writes_nothing_and_describes_the_rename(tmp_path: Path) -> None:
    """Building the step only reads, and its description counts what it changes."""
    database = await _approval_journal(tmp_path)
    _stream, _generation = await _synced_journal(database)
    before = tree_snapshot(tmp_path)

    (step,) = _steps(database)

    assert tree_snapshot(tmp_path) == before
    assert step.description.startswith(f"Rewrite the event journal {database}: rename principal {OLD_PRINCIPAL} to ")
    assert "matrix_ingestion_membership: 1, matrix_sync_consumers: 1" in step.description
    # The principal's grants are dropped, not renamed.
    assert "approval_grants:" not in step.description
    assert "approval_continuations.context_json (2)" in step.description
    assert "reply_messages.presentation_json (1)" in step.description
    assert "reply_spans.rollback_json (1)" in step.description
    assert "drop the approval grants bound to agent code (2)" in step.description


@pytest.mark.asyncio
async def test_rows_already_under_the_new_principal_refuse(tmp_path: Path) -> None:
    """A principal of the new name, under any Matrix ID, means the name already has journal state."""
    database = event_journal_sqlite_path(tmp_path)
    await _synced_journal(database)
    store = EventJournalStore.open_sqlite(database)
    try:
        await bound_principal(store, "zeta@@zeta:x", uuid4())
    finally:
        await store.close()
    before = _rows(database)

    with pytest.raises(RenameRefusedError, match=r"already has rows for agent zeta \(matrix_sync_consumers: 1\)"):
        _steps(database)

    assert _rows(database) == before


@pytest.mark.asyncio
@pytest.mark.ledger_loads_from_disk
async def test_turn_records_already_under_the_new_name_refuse(tmp_path: Path) -> None:
    """Turn records of the new name mean an agent of that name already answered messages."""
    database = event_journal_sqlite_path(tmp_path)
    store = EventJournalStore.open_sqlite(database)
    try:
        await record_turn(store, OLD, "$from-alice", HistoryScope(kind="agent", scope_id=OLD))
        await record_turn(store, NEW, "$other", HistoryScope(kind="agent", scope_id=NEW))
    finally:
        await store.close()

    with pytest.raises(RenameRefusedError, match=r"already has rows for agent zeta \(turn_records: 1\)"):
        _steps(database)


@pytest.mark.asyncio
async def test_new_principal_rows_written_after_planning_refuse_at_apply(tmp_path: Path) -> None:
    """The edit checks the new name again in its transaction and leaves the journal unchanged."""
    database = event_journal_sqlite_path(tmp_path)
    await _synced_journal(database)
    steps = _steps(database)
    store = EventJournalStore.open_sqlite(database)
    try:
        await bound_principal(store, NEW_PRINCIPAL.replace(USER_ID, "@zeta:x"), uuid4())
    finally:
        await store.close()
    before = _rows(database)

    with pytest.raises(RenameRefusedError, match="already has rows for agent zeta"):
        _apply(steps, tmp_path)

    assert _rows(database) == before


@pytest.mark.asyncio
@pytest.mark.ledger_loads_from_disk
async def test_team_scope_listing_both_names_refuses(tmp_path: Path) -> None:
    """Renaming a member of a scope that already lists the new name would merge two member sets."""
    database = event_journal_sqlite_path(tmp_path)
    store = EventJournalStore.open_sqlite(database)
    try:
        await record_turn(store, HELPER, "$team", HistoryScope(kind="team", scope_id=f"team_{OLD}+{HELPER}+{NEW}"))
    finally:
        await store.close()

    with pytest.raises(RenameRefusedError, match=r"zeta is already one of its members"):
        _steps(database)
