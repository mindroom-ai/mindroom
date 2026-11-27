"""Background tool jobs live in the event journal, written only by the runtime generation that owns them."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from mindroom.event_journal import (
    JOB_ENDED_REASON,
    ApprovalCardReservation,
    BackgroundApprovalDecision,
    CallKey,
    DeliveryStage,
    ToolJobExistsError,
    ToolJobOwnershipLostError,
    ToolJobSnapshotUpdate,
    tool_job_run_id,
)
from mindroom.event_journal import tool_jobs as event_journal_tool_jobs
from tests.approval_card_helpers import pending_card, terminal_edit
from tests.journal_helpers import PlanRecorder, sqlite_plan_database

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.event_journal import EventJournalStore
    from mindroom.event_journal.backend import Transaction


@pytest.mark.asyncio
async def test_snapshot_and_payload_round_trip(journal_store: EventJournalStore) -> None:
    """A later snapshot keeps the payload its outcome saved; deletion forgets both."""
    jobs = journal_store.tool_jobs("first")
    await jobs.take_ownership()
    await jobs.accept("job", '{"status": "running"}')
    assert await jobs.load_payload("job") is None
    await jobs.save("job", '{"status": "completed"}', '{"value": "full result"}', approval_end_reason=JOB_ENDED_REASON)
    await jobs.save("job", '{"status": "completed", "consumed": true}', approval_end_reason=JOB_ENDED_REASON)
    assert [(job.job_id, job.job_json) for job in await jobs.load_all()] == [
        ("job", '{"status": "completed", "consumed": true}'),
    ]
    assert await jobs.load_payload("job") == '{"value": "full result"}'
    assert [job.job_id for job in await journal_store.tool_jobs("reader").load_all()] == ["job"]
    await jobs.delete("job")
    assert await jobs.load_all() == ()
    assert await jobs.load_payload("job") is None


@pytest.mark.asyncio
async def test_an_accepted_id_is_never_accepted_again(journal_store: EventJournalStore) -> None:
    """Admission refuses an ID any runtime accepted, even after a later runtime took over."""
    first = journal_store.tool_jobs("first")
    await first.take_ownership()
    await first.accept("job", "{}")
    second = journal_store.tool_jobs("second")
    await second.take_ownership()
    with pytest.raises(ToolJobExistsError):
        await second.accept("job", "{}")


@pytest.mark.asyncio
async def test_a_newer_runtime_fences_the_older_one(journal_database: Callable[[], EventJournalStore]) -> None:
    """Once another process takes over, every write of the older runtime is refused and changes nothing."""
    older = journal_database().tool_jobs("older")
    await older.take_ownership()
    await older.accept("job", '{"status": "running"}')
    newer = journal_database().tool_jobs("newer")
    await newer.take_ownership()
    for write in (
        older.require_ownership(),
        older.accept("other", "{}"),
        older.save("job", '{"status": "completed"}', '{"value": "late"}', approval_end_reason=JOB_ENDED_REASON),
        older.delete("job"),
    ):
        with pytest.raises(ToolJobOwnershipLostError):
            await write
    assert [(job.job_id, job.job_json) for job in await newer.load_all()] == [("job", '{"status": "running"}')]
    assert await newer.load_payload("job") is None
    await newer.save("job", '{"status": "interrupted"}', approval_end_reason=JOB_ENDED_REASON)
    assert [job.job_json for job in await newer.load_all()] == ['{"status": "interrupted"}']


@pytest.mark.asyncio
async def test_writes_need_ownership(journal_store: EventJournalStore) -> None:
    """A runtime that never took ownership cannot write."""
    with pytest.raises(ToolJobOwnershipLostError, match=r"Another tool job runtime took over this journal\."):
        await journal_store.tool_jobs("unowned").accept("job", "{}")


@pytest.mark.asyncio
async def test_the_save_of_a_jobs_end_denies_its_pending_cards(journal_store: EventJournalStore) -> None:
    """An ended job leaves no answerable card, without an approval runtime to deny it, and its running saves leave one."""
    router = journal_store.principal("router@shared")
    call = CallKey.detached(tool_job_run_id("job"), "call")
    delivery_id = "script-approval:tool-job:job:call"
    assert await router.approvals.reserve_detached_card(
        call=call,
        owner_kind="job",
        tool_name="write_report",
        toolkit_name="reports",
        invoking_agent="code",
        room_id="!room:test",
        thread_id="$thread",
        expires_at_ns=time.time_ns() + 60_000_000_000,
        card=ApprovalCardReservation(
            delivery_id=delivery_id,
            tool_call_id="call",
            event_type="io.mindroom.tool_approval",
            approver_user_id="@human:test",
            approvable=True,
            payload={"approval_id": delivery_id, "tool_name": "write_report", "status": "pending"},
        ),
    )
    await router.acknowledge_matrix_delivery(
        delivery_id=delivery_id,
        stage=DeliveryStage.INITIAL,
        event_id="$card",
        delivered_projections=(),
    )
    jobs = journal_store.tool_jobs("owner")
    await jobs.take_ownership()
    await jobs.accept("job", '{"status": "running"}')

    await jobs.save("job", '{"status": "awaiting_approval"}', approval_end_reason=None)
    waiting = await pending_card(router.approvals, room_id="!room:test", card_event_id="$card")
    assert waiting is not None
    assert not waiting.decided
    await jobs.save("job", '{"status": "cancelled"}', approval_end_reason=JOB_ENDED_REASON)

    ended = await pending_card(router.approvals, room_id="!room:test", card_event_id="$card")
    assert ended is not None
    assert ended.decided
    assert await router.approvals.detached_decision(call) == BackgroundApprovalDecision("denied", JOB_ENDED_REASON)
    edit = await terminal_edit(router, delivery_id)
    assert (edit["status"], edit["resolution_reason"]) == ("denied", JOB_ENDED_REASON)


@pytest.mark.asyncio
async def test_save_many_updates_atomically_respects_ownership_and_denies_ended_cards(
    journal_database: Callable[[], EventJournalStore],
) -> None:
    """Batched saves fence stale writers cleanly, update all rows in one transaction, and deny ended jobs' cards."""
    store = journal_database()
    router = store.principal("router@shared")
    for suffix in ("a", "b"):
        delivery_id = f"script-approval:tool-job:job-{suffix}:call"
        assert await router.approvals.reserve_detached_card(
            call=CallKey.detached(tool_job_run_id(f"job-{suffix}"), "call"),
            owner_kind="job",
            tool_name="write_report",
            toolkit_name="reports",
            invoking_agent="code",
            room_id="!room:test",
            thread_id="$thread",
            expires_at_ns=time.time_ns() + 60_000_000_000,
            card=ApprovalCardReservation(
                delivery_id=delivery_id,
                tool_call_id="call",
                event_type="io.mindroom.tool_approval",
                approver_user_id="@human:test",
                approvable=True,
                payload={"approval_id": delivery_id, "tool_name": "write_report", "status": "pending"},
            ),
        )
        await router.acknowledge_matrix_delivery(
            delivery_id=delivery_id,
            stage=DeliveryStage.INITIAL,
            event_id=f"$card-{suffix}",
            delivered_projections=(),
        )

    older = store.tool_jobs("older")
    await older.take_ownership()
    await older.accept("job-a", '{"status": "running"}')
    await older.accept("job-b", '{"status": "running"}')
    newer = journal_database().tool_jobs("newer")
    await newer.take_ownership()

    updates = (
        ToolJobSnapshotUpdate("job-a", '{"status": "completed", "consumed": true}', '{"value": "a"}', JOB_ENDED_REASON),
        ToolJobSnapshotUpdate("job-b", '{"status": "awaiting_approval"}', None, None),
    )
    with pytest.raises(ToolJobOwnershipLostError):
        await older.save_many(updates)
    assert [(job.job_id, job.job_json) for job in await newer.load_all()] == [
        ("job-a", '{"status": "running"}'),
        ("job-b", '{"status": "running"}'),
    ]
    assert await newer.load_payload("job-a") is None
    card_a_before = await pending_card(router.approvals, room_id="!room:test", card_event_id="$card-a")
    assert card_a_before is not None
    assert not card_a_before.decided

    await newer.save_many(())
    await newer.save_many(updates)
    assert [(job.job_id, job.job_json) for job in await newer.load_all()] == [
        ("job-a", '{"status": "completed", "consumed": true}'),
        ("job-b", '{"status": "awaiting_approval"}'),
    ]
    assert await newer.load_payload("job-a") == '{"value": "a"}'
    card_a = await pending_card(router.approvals, room_id="!room:test", card_event_id="$card-a")
    card_b = await pending_card(router.approvals, room_id="!room:test", card_event_id="$card-b")
    assert card_a is not None
    assert card_a.decided
    assert card_b is not None
    assert not card_b.decided
    edit = await terminal_edit(router, "script-approval:tool-job:job-a:call")
    assert (edit["status"], edit["resolution_reason"]) == ("denied", JOB_ENDED_REASON)

    await newer.save_many((ToolJobSnapshotUpdate("job-a", '{"status": "completed", "v": 2}', None, JOB_ENDED_REASON),))
    assert await newer.load_payload("job-a") == '{"value": "a"}'


def _legacy_row(job_id: str, **overrides: object) -> tuple[str, str]:
    payload: dict[str, object] = {
        "job_id": job_id,
        "status": "completed",
        "consumed": True,
        "returned_inline": False,
        "kind": "tool",
        "owner": {"agent_name": "code", "room_id": "!hot:test"},
        **overrides,
    }
    return job_id, json.dumps(payload)


@pytest.mark.asyncio
async def test_legacy_tool_jobs_table_upgrades_and_backfills_metadata(
    journal_database: Callable[[], EventJournalStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-metadata tool_jobs table upgrades in bounded batches and backfills metadata on SQLite and PostgreSQL."""
    now = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
    old_inline = (now - timedelta(days=2)).isoformat()
    old_ended = (now - timedelta(days=31)).isoformat()
    recent = (now - timedelta(hours=1)).isoformat()
    team_owner = {"agent_name": "member", "transport_agent_name": "team", "room_id": "!team:test"}
    wf_owner = {"agent_name": "code", "room_id": "!wf:test"}
    rows_to_seed = (
        _legacy_row("00000001active", status="running", consumed=False, updated_at=recent),
        _legacy_row("00000002unconsumed", consumed=False, updated_at=recent),
        _legacy_row(
            "abcdef01coldinline",
            returned_inline=True,
            updated_at=old_inline,
            owner={"agent_name": "code", "room_id": "!inline:test"},
        ),
        _legacy_row("abcdef01coldfuture", updated_at="2500-01-01T00:00:00+00:00", owner=team_owner),
        _legacy_row("abcdef01coldlist", status="failed", updated_at=old_ended, owner=team_owner),
        _legacy_row(
            "abcdef02deleg",
            kind="delegation",
            updated_at=recent,
            adapter={"child": {"delegation_id": "deleg-cold-1"}},
            owner={"agent_name": "code", "room_id": None},
        ),
        _legacy_row("wf000001main", kind="workflow", updated_at=recent, owner=wf_owner),
        _legacy_row(
            "wf000002node",
            kind="delegation",
            updated_at=recent,
            adapter={"child": {"delegation_id": "deleg-wf-1", "workflow_run_id": "run-1"}},
            owner=wf_owner,
        ),
        ("zzzzcorrupt", "{not valid json"),
        (
            "zzzzliststatus",
            '{"job_id": "zzzzliststatus", "status": [], "consumed": true, "owner": {"agent_name": "code"}}',
        ),
        ("zzzzmissingowner", '{"status": "completed", "consumed": true}'),
        (
            "zzzznul",
            '{"job_id": "zzzznul", "status": "completed", "consumed": true, '
            '"owner": {"agent_name": "code", "room_id": "\\u0000"}}',
        ),
        (
            "zzzzsurrogate",
            '{"job_id": "zzzzsurrogate", "status": "completed", "consumed": true, '
            '"owner": {"agent_name": "code", "room_id": "\\ud800"}}',
        ),
    )

    seed_store = journal_database()
    try:
        await seed_store.backend.write(
            lambda tx: (
                tx.execute("DROP TABLE tool_jobs"),
                tx.execute(
                    "CREATE TABLE tool_jobs ("
                    "job_id TEXT NOT NULL PRIMARY KEY, job_json TEXT NOT NULL, result_payload_json TEXT)",
                ),
                *[
                    tx.execute("INSERT INTO tool_jobs (job_id, job_json) VALUES (?, ?)", (job_id, job_json))
                    for job_id, job_json in rows_to_seed
                ],
            ),
        )
    finally:
        await seed_store.close()

    monkeypatch.setattr(event_journal_tool_jobs, "_BACKFILL_BATCH_SIZE", 2)
    upgraded = journal_database()
    try:
        jobs = upgraded.tool_jobs("reader")
        assert [row.job_id for row in await jobs.load_startup()] == [
            "00000001active",
            "00000002unconsumed",
            "wf000001main",
            "wf000002node",
            "zzzzcorrupt",
            "zzzzliststatus",
            "zzzzmissingowner",
            "zzzznul",
            "zzzzsurrogate",
        ]
        assert await jobs.load_ended_rooms() == frozenset({("code", "!inline:test"), ("team", "!team:test")})
        assert [row.job_id for row in await jobs.load_by_prefix("abcdef01")] == [
            "abcdef01coldfuture",
            "abcdef01coldlist",
        ]
        assert [row.job_id for row in await jobs.load_listable(recipient="team", room_id="!team:test")] == [
            "abcdef01coldfuture",
            "abcdef01coldlist",
        ]
        assert [row.job_id for row in await jobs.load_listable(recipient="code", room_id=None)] == ["abcdef02deleg"]
        assert {
            row.job_id
            for row in await jobs.load_expirable(
                inline_before=now - timedelta(days=1),
                ended_before=now - timedelta(days=30),
            )
        } == {"abcdef01coldinline", "abcdef01coldlist"}
        assert [row.job_id for row in await jobs.load_by_delegation_ids(("deleg-cold-1", "missing"))] == [
            "abcdef02deleg",
        ]
        assert await jobs.load_by_id("abcdef01coldinline", cold_only=True) is not None
        assert await jobs.load_by_id("00000001active", cold_only=True) is None
        assert await jobs.load_by_id("00000001active") is not None
        assert await jobs.load_by_prefix("a\U0010ffff") == ()
    finally:
        await upgraded.close()

    # Surrogates in recipient or delegation_id also demote the row to hot with NULL metadata instead of raising.
    bad_recipient = '{"job_id": "x", "status": "completed", "consumed": true, "owner": {"agent_name": "\\ud800", "room_id": "!r:t"}}'
    bad_deleg = (
        '{"job_id": "x", "status": "completed", "consumed": true, "kind": "delegation", '
        '"adapter": {"child": {"delegation_id": "\\ud800"}}, "owner": {"agent_name": "a"}}'
    )
    assert event_journal_tool_jobs._extract_metadata(bad_recipient)[:4] == (False, False, 0, None)
    assert event_journal_tool_jobs._extract_metadata(bad_deleg)[0] is False
    assert event_journal_tool_jobs._extract_metadata(bad_deleg)[5] is None


def test_tool_jobs_cold_queries_use_targeted_indexes_without_stat1(tmp_path: Path) -> None:
    """Every targeted tool_jobs query uses its index on an un-ANALYZEd SQLite journal, and prefix bounds are exact."""
    ej = event_journal_tool_jobs
    now = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
    pk_idx = "USING INDEX sqlite_autoindex_tool_jobs_1"
    cases: tuple[tuple[str, Callable[[Transaction], object], str, int], ...] = (
        ("load_startup", ej.load_startup, "USING INDEX tool_jobs_startup", 1),
        ("load_ended_rooms", ej.load_ended_rooms, "USING INDEX tool_jobs_ended_rooms", 1),
        (
            "list_room",
            lambda tx: ej.load_listable(tx, recipient="agent", room_id="!r:test"),
            "USING INDEX tool_jobs_list",
            1,
        ),
        (
            "list_null",
            lambda tx: ej.load_listable(tx, recipient="agent", room_id=None),
            "USING INDEX tool_jobs_list",
            1,
        ),
        (
            "expirable",
            lambda tx: ej.load_expirable(
                tx,
                inline_before=now - timedelta(days=1),
                ended_before=now - timedelta(days=30),
            ),
            "USING INDEX tool_jobs_expiry",
            2,
        ),
        ("deleg", lambda tx: ej.load_by_delegation_ids(tx, ("d-5", "d-10")), "USING INDEX tool_jobs_delegation", 1),
        ("prefix", lambda tx: ej.load_by_prefix(tx, "abcdef01"), "USING INDEX tool_jobs_prefix", 1),
        ("by_id", lambda tx: ej.load_by_id(tx, "abcdef010001"), pk_idx, 1),
        ("by_id_cold", lambda tx: ej.load_by_id(tx, "abcdef010001", cold_only=True), pk_idx, 1),
        ("backfill", lambda tx: tx.fetchall(ej._BACKFILL_NEXT_PAGE_SQL, ("abcdef010001", 10)), pk_idx, 1),
    )

    with sqlite_plan_database(tmp_path / "tool-jobs-plan.db") as database:
        assert database.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_stat1'").fetchone() is None
        recorder = PlanRecorder(database)
        for name, query, expected_index, expected_count in cases:
            captured = recorder.record(query)
            assert len(captured) == expected_count, (name, captured)
            assert all(expected_index in plan and "USE TEMP B-TREE" not in plan for plan in captured), (name, captured)

    assert event_journal_tool_jobs._prefix_upper_bound("a\U0010ffff") == "b"
    assert event_journal_tool_jobs._prefix_upper_bound("\U0010ffff") is None
    assert event_journal_tool_jobs._prefix_upper_bound("a\ud7ff\U0010ffff") == "a\ue000"
