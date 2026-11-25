"""Scaling, transition, and full-scan equivalence tests for indexed tool-job reconciliation."""

from __future__ import annotations

import asyncio
import json
import random
from collections import Counter
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.event_journal import tool_jobs as journal_tool_jobs
from mindroom.matrix.identity import MatrixID
from mindroom.orchestration import tool_job_runtime as coord_module
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator, _key_order
from mindroom.reply_lifecycle import ReplyState
from mindroom.tool_jobs import publication
from mindroom.tool_jobs.conversation import ConversationKey, unheld_jobs
from mindroom.tool_jobs.follow_ups import (
    _FOLLOW_UP_LIMIT,
    _follow_up_event_id,
    follow_up_jobs,
    held_jobs,
    held_notice,
    next_follow_up_id,
)
from mindroom.tool_jobs.publication import _EMPTY, _jobs_state_content
from mindroom.tool_jobs.runtime import (
    TERMINAL_STATUSES,
    AcknowledgeWait,
    BackgroundJob,
    BackgroundOutcome,
    JobClaim,
    JobGrant,
    JobSpec,
    ToolJobRuntime,
    is_workflow_node_job,
)
from tests.conftest import test_runtime_paths
from tests.tool_job_helpers import (
    backdate_job,
    intercept_job_saves,
    job_coordinator,
    job_owner,
    tool_job_journal,
    tool_job_runtime,
    wait_for_status,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from pathlib import Path

    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


def _snap(job_id: str, owner: ToolExecutionIdentity, status: str = "completed", **kw: Any) -> BackgroundJob:  # noqa: ANN401
    return BackgroundJob(
        job_id=job_id,
        owner=owner,
        **{"tool_name": "t", "depth": 0, "adapter": {}, "status": status, **kw},
    )


async def _start(
    runtime: ToolJobRuntime,
    job_id: str,
    owner: ToolExecutionIdentity,
    op: Callable[[], Awaitable[BackgroundOutcome]],
    **kw: Any,  # noqa: ANN401
) -> JobClaim | None:
    _, claim = await runtime.start(
        job_id,
        owner=owner,
        operation=op,
        **{"tool_name": "t", "depth": 0, "adapter": {}, **kw},
    )
    return claim


async def _fast_ok() -> BackgroundOutcome:
    return BackgroundOutcome("completed", "ok")


def _bot(agent: str, rooms: set[str], *, open_replies: set[str] | None = None) -> MagicMock:
    bot = MagicMock()
    bot.matrix_id = MatrixID.parse(f"@mindroom_{agent}:test")
    bot.running = bot.first_sync_complete = True
    bot.client.rooms = {room_id: MagicMock() for room_id in rooms}
    bot.client.room_put_state = AsyncMock(
        side_effect=lambda **kw: nio.RoomPutStateResponse(event_id="$s", room_id=kw["room_id"]),
    )
    bot.admit_job_follow_up = AsyncMock()
    active = set() if open_replies is None else open_replies
    bot.journal_principal.return_value.replies.load = AsyncMock(
        side_effect=lambda rid: MagicMock(state=ReplyState.ACTIVE, current_span_id="s1") if rid in active else None,
    )
    return bot


def _coord(
    storage: Path,
    runtime: ToolJobRuntime,
    bot_provider: Callable[[str], MagicMock | None],
    *,
    config_provider: Callable[[], Config] = Config,
) -> ToolJobRuntimeCoordinator:
    paths = test_runtime_paths(storage)
    coord = ToolJobRuntimeCoordinator(
        runtime_paths=paths,
        config_provider=config_provider,
        bot_provider=bot_provider,
        agent_reply_memberships=MagicMock(),
        journal_provider=lambda: tool_job_journal(paths.storage_root),
    )
    coord._runtime = runtime
    return coord


async def _seed_jobs(runtime: ToolJobRuntime, jobs: Sequence[BackgroundJob]) -> None:
    store, gen = runtime._store, runtime._store._runtime_generation
    encoded = [(j.job_id, json.dumps(asdict(j))) for j in jobs]
    await store._backend.write(lambda tx: [journal_tool_jobs.accept(tx, gen, jid, raw) for jid, raw in encoded])
    await runtime.recover()


def _scan_candidates(
    jobs: Sequence[BackgroundJob],
) -> dict[tuple[str, str, str | None, str, bool], list[BackgroundJob]]:
    buckets: dict[tuple[str, str, str | None, str, bool], list[BackgroundJob]] = {}
    for j in jobs:
        if (
            j.status in TERMINAL_STATUSES
            and j.depth == 0
            and not j.consumed
            and not is_workflow_node_job(j)
            and j.owner.room_id is not None
            and j.owner.requester_id is not None
        ):
            key = (
                j.owner.recipient,
                j.owner.room_id,
                j.owner.resolved_thread_id,
                j.owner.requester_id,
                j.source_kind == SILENT_SCHEDULE_SOURCE_KIND,
            )
            buckets.setdefault(key, []).append(j)
    return buckets


def _assert_indexes(runtime: ToolJobRuntime) -> None:
    retained = runtime.retained_jobs()
    expected_live = {j.job_id for j in retained if j.status not in TERMINAL_STATUSES}
    assert {j.job_id for j in runtime.live_jobs()} == expected_live == set(runtime._live)
    assert runtime.awaited_job_ids() == {jid for jid, e in runtime._entries.items() if e.claim is not None}
    assert {j.job_id for j in runtime.awaiting_continuation()} == {
        e.job.job_id for e in runtime._entries.values() if runtime._awaits_continuation(e)
    }
    expected_rooms = Counter(
        (j.owner.recipient, j.owner.room_id)
        for j in retained
        if j.status in TERMINAL_STATUSES and j.owner.room_id is not None
    )
    assert runtime._ended_room_counts == dict(expected_rooms)
    assert runtime.ended_rooms() == frozenset(expected_rooms)
    expected_cands = {k: {j.job_id for j in g} for k, g in _scan_candidates(retained).items()}
    assert {k: {j.job_id for j in g} for k, (_r, g) in runtime.follow_up_candidates().items()} == expected_cands
    assert set(runtime._follow_up_revisions) == set(expected_cands)


@pytest.mark.asyncio
async def test_reconcile_work_scales_with_live_jobs_not_retained_ended_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With 5,000+ ended jobs and 3 live jobs, _reconcile touches only live jobs and skips idle cleared rooms."""
    ended_count, live_count, room_count = 5_000, 3, 20
    grant_calls: list[str] = []
    runtime = await tool_job_runtime(tmp_path, grant=lambda j: (grant_calls.append(j.job_id), "allowed")[1])
    gate = asyncio.Event()
    try:
        base = job_owner()
        await _seed_jobs(
            runtime,
            [
                _snap(
                    f"ended-{i:05d}",
                    replace(base, room_id=f"!room_{i % room_count}:test"),
                    consumed=True,
                    returned_inline=i % 2 == 0,
                )
                for i in range(ended_count)
            ],
        )

        async def blocked() -> BackgroundOutcome:
            await gate.wait()
            return BackgroundOutcome("completed", "done")

        live_owner = replace(base, room_id="!room_0:test")
        for i in range(live_count):
            await runtime.release_wait(f"live-{i}", await _start(runtime, f"live-{i}", live_owner, blocked))

        bot = _bot("parent", {f"!room_{i}:test" for i in range(room_count)})
        coordinator = job_coordinator(tmp_path, runtime, bot)
        retained_calls, job_state_calls, content_calls = [], [], []
        orig_ret, orig_state, orig_content = (
            runtime.retained_jobs,
            publication._job_state,
            publication._jobs_state_content,
        )
        monkeypatch.setattr(runtime, "retained_jobs", lambda: (retained_calls.append(1), orig_ret())[1])
        monkeypatch.setattr(publication, "_job_state", lambda j: (job_state_calls.append(j.job_id), orig_state(j))[1])
        monkeypatch.setattr(
            publication,
            "_jobs_state_content",
            lambda jobs, **kw: (
                content_calls.append((len(jobs), all(j.status not in TERMINAL_STATUSES for j in jobs))),
                orig_content(jobs, **kw),
            )[1],
        )

        grant_calls.clear()
        await coordinator._reconcile()
        assert (len(retained_calls), len(grant_calls), len(job_state_calls), bot.client.room_put_state.await_count) == (
            0,
            live_count,
            live_count,
            room_count,
        )
        assert sum(n for n, _ in content_calls) == live_count
        assert all(ok for _, ok in content_calls)

        grant_calls.clear()
        job_state_calls.clear()
        content_calls.clear()
        bot.client.room_put_state.reset_mock()
        await coordinator._reconcile()
        assert (len(retained_calls), len(grant_calls), len(job_state_calls), bot.client.room_put_state.await_count) == (
            0,
            live_count,
            live_count,
            0,
        )
        assert content_calls == [(live_count, True)]
    finally:
        gate.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_index_transitions_lifecycle_and_claims(tmp_path: Path) -> None:
    """start, span_stopped, reattach, waits, save_adapter, claims, cancel, ack, and expiry keep indexes exact."""
    grants: dict[str, JobGrant] = {}
    owner_a, owner_b = replace(job_owner(), room_id="!room_a:test"), replace(job_owner(), room_id="!room_b:test")
    gate_a, gate_cancel = asyncio.Event(), asyncio.Event()
    runtime = await tool_job_runtime(tmp_path, grant=lambda j: grants.get(j.job_id, "allowed"))
    try:

        async def gated(ev: asyncio.Event) -> BackgroundOutcome:
            await ev.wait()
            return BackgroundOutcome("completed", "ok")

        claim_a = await _start(runtime, "job-a", owner_a, lambda: gated(gate_a), adapter={"v": 1})
        await runtime.set_awaiting_approval("job-a", awaiting=True)
        await runtime.set_awaiting_approval("job-a", awaiting=False)
        async with runtime.human_decision_wait("job-a"):
            _assert_indexes(runtime)
        await runtime.save_adapter("job-a", v=2)

        grants["job-a"] = "pending"
        assert (await runtime.wait("job-a", owner=owner_a, depth=0, claim=claim_a)).claim is None
        _assert_indexes(runtime)
        grants["job-a"] = "allowed"
        claim_a = await _start(runtime, "job-a", owner_a, lambda: gated(gate_a), adapter={"v": 2}, reattach=True)
        await runtime.release_waits((("job-a", claim_a),))
        gate_a.set()
        await wait_for_status(runtime, "job-a", "completed")

        await runtime.release_wait(
            "job-stopped",
            await _start(runtime, "job-stopped", owner_a, lambda: gated(gate_a), span_stopped=lambda: True),
        )
        await wait_for_status(runtime, "job-stopped", "cancelled")

        await runtime.release_wait(
            "job-cancel",
            await _start(runtime, "job-cancel", owner_b, lambda: gated(gate_cancel)),
        )
        await runtime.cancel("job-cancel", owner=owner_b, depth=0)
        await wait_for_status(runtime, "job-cancel", "cancelled")
        await runtime.acknowledge_waits(
            (AcknowledgeWait("job-a", (await runtime.wait("job-a", owner=owner_a, depth=0)).claim),),
        )
        _assert_indexes(runtime)

        b_key = (owner_b.recipient, "!room_b:test", owner_b.resolved_thread_id, str(owner_b.requester_id), False)
        old_rev = runtime.follow_up_candidates()[b_key][0]
        backdate_job(runtime, "job-cancel", datetime.now(UTC) - timedelta(days=31))
        expired = await runtime.expire_ended(now=datetime.now(UTC), source_finished=AsyncMock(return_value=True))
        assert [j.job_id for j in expired] == ["job-cancel"]
        assert (owner_b.recipient, "!room_b:test") not in runtime._ended_room_counts
        _assert_indexes(runtime)

        await runtime.release_wait("job-b2", await _start(runtime, "job-b2", owner_b, _fast_ok))
        await wait_for_status(runtime, "job-b2", "completed")
        assert runtime.follow_up_candidates()[b_key][0] > old_rev
        _assert_indexes(runtime)
    finally:
        gate_a.set()
        gate_cancel.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_index_transitions_outcome_save_failure_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed outcome saves and retry via cancel drain keep all indexes exact without double-counting."""
    owner_a = replace(job_owner(), room_id="!room_a:test")
    gate_fail = asyncio.Event()
    runtime = await tool_job_runtime(tmp_path)
    try:

        async def op_fail() -> BackgroundOutcome:
            await gate_fail.wait()
            msg = "boom"
            raise RuntimeError(msg)

        await runtime.release_wait("job-fail", await _start(runtime, "job-fail", owner_a, op_fail))
        fail_save = True

        def maybe_fail(snap: dict[str, object], _payload: object | None) -> None:
            if fail_save and snap["job_id"] == "job-fail" and snap["status"] == "failed":
                msg = "disk"
                raise OSError(msg)

        intercept_job_saves(monkeypatch, before=maybe_fail)
        gate_fail.set()
        await wait_for_status(runtime, "job-fail", "failed")
        assert not runtime.outcome_saved("job-fail")
        _assert_indexes(runtime)
        fail_save = False
        await runtime.cancel("job-fail", owner=owner_a, depth=0)
        if (drain := runtime._entries["job-fail"].drain) is not None:
            await drain
        assert runtime.outcome_saved("job-fail")
        intercept_job_saves(monkeypatch)
        _assert_indexes(runtime)
    finally:
        gate_fail.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_index_transitions_recovery_and_continuation(tmp_path: Path) -> None:
    """recover() and continue_stopped(relaunch / outcome / None) keep indexes consistent without double-counting."""
    owner_a, owner_b = replace(job_owner(), room_id="!room_a:test"), replace(job_owner(), room_id="!room_b:test")
    runtime = await tool_job_runtime(tmp_path, continues=lambda j: j.kind == "delegation")
    gate = asyncio.Event()
    try:
        await _seed_jobs(
            runtime,
            [
                _snap("cont-relaunch", owner_a, "running", kind="delegation", tool_name="w"),
                _snap("cont-outcome", owner_a, "running", kind="delegation", tool_name="w"),
                _snap("cont-none", owner_a, "running", kind="delegation", tool_name="w"),
                _snap("tool-running", owner_a, "running"),
                _snap("ended-unconsumed", owner_a, "completed", consumed=False),
                _snap("ended-consumed", owner_b, "completed", consumed=True),
            ],
        )
        assert {j.job_id for j in runtime.awaiting_continuation()} == {"cont-relaunch", "cont-outcome", "cont-none"}
        tool_job = runtime.retained_job("tool-running")
        assert tool_job is not None
        assert tool_job.status == "interrupted"
        _assert_indexes(runtime)

        async def resumed() -> BackgroundOutcome:
            await gate.wait()
            return BackgroundOutcome("completed", "resumed")

        await runtime.continue_stopped(
            "cont-relaunch",
            JobSpec(tool_name="w", kind="delegation", adapter={"r": 1}, operation=resumed),
        )
        await runtime.continue_stopped("cont-outcome", BackgroundOutcome("completed", "direct"))
        await runtime.continue_stopped("cont-none", None)
        assert runtime.awaiting_continuation() == ()
        _assert_indexes(runtime)
        gate.set()
        await wait_for_status(runtime, "cont-relaunch", "completed")
        _assert_indexes(runtime)
    finally:
        gate.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_startup_room_clearing_retries_and_quiesce_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent bots or transient failures retry clearing, stay quiet once cleared, and quiesce(disabled) clears live rooms."""
    runtime = await tool_job_runtime(tmp_path, continues=lambda j: j.kind == "delegation")
    gate = asyncio.Event()
    try:
        owner_a = replace(job_owner(), agent_name="agent_a", room_id="!room_a:test")
        owner_b = replace(job_owner(), agent_name="agent_b", room_id="!room_b:test")
        await _seed_jobs(
            runtime,
            [_snap("e-a", owner_a, "interrupted", consumed=True), _snap("e-b", owner_b, "interrupted", consumed=True)],
        )
        bot_a, bot_b = _bot("agent_a", {"!room_a:test", "!room_c:test"}), _bot("agent_b", {"!room_b:test"})
        bot_a_ready, fail_b, enabled = False, True, True
        bot_b.client.room_put_state = AsyncMock(
            side_effect=lambda **kw: (
                nio.RoomPutStateError(message="tmp", status_code="M_UNKNOWN")
                if fail_b
                else nio.RoomPutStateResponse(event_id="$ok", room_id=kw["room_id"])
            ),
        )
        coordinator = _coord(
            tmp_path,
            runtime,
            lambda name: (bot_a if bot_a_ready else None) if name == "agent_a" else bot_b,
            config_provider=lambda: Config(background_tool_jobs=BackgroundToolJobsConfig(enabled=enabled)),
        )

        await coordinator._reconcile()
        assert (
            bot_a.client.room_put_state.await_count,
            bot_b.client.room_put_state.await_count,
            coordinator._publisher._published,
        ) == (0, 1, {})

        bot_a_ready, fail_b = True, False
        await coordinator._reconcile()
        assert bot_a.client.room_put_state.await_args.kwargs["content"] == _EMPTY
        assert bot_b.client.room_put_state.await_args.kwargs["content"] == _EMPTY

        content_calls: list[int] = []
        orig_content = publication._jobs_state_content
        monkeypatch.setattr(
            publication,
            "_jobs_state_content",
            lambda jobs, **kw: (content_calls.append(len(jobs)), orig_content(jobs, **kw))[1],
        )
        bot_a.client.room_put_state.reset_mock()
        bot_b.client.room_put_state.reset_mock()
        await coordinator._reconcile()
        assert (content_calls, bot_a.client.room_put_state.await_count, bot_b.client.room_put_state.await_count) == (
            [],
            0,
            0,
        )

        async def blocked() -> BackgroundOutcome:
            await gate.wait()
            return BackgroundOutcome("completed", "ok")

        claim = await _start(
            runtime,
            "kept-live",
            replace(owner_a, room_id="!room_c:test"),
            blocked,
            tool_name="w",
            kind="delegation",
            adapter={"child": {"subagent_id": "s1", "delegation_id": "d1"}},
        )
        await runtime.release_wait("kept-live", claim)
        await coordinator._reconcile()
        assert bot_a.client.room_put_state.await_args.kwargs["content"]["total"] == 1
        enabled = False
        await coordinator.quiesce()
        assert bot_a.client.room_put_state.await_args.kwargs["content"] == _EMPTY
    finally:
        gate.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_follow_up_cache_invalidation_and_external_change_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged buckets skip hashing; mutations invalidate; open-reply & grant changes still admit on same revision."""
    grants: dict[str, JobGrant] = {"pending-job": "pending"}
    open_replies = {"reply-open"}
    runtime = await tool_job_runtime(tmp_path, grant=lambda j: grants.get(j.job_id, "allowed"))
    rooms = ("!room_a:test", "!room_b:test", "!room_c:test", "!room_d:test")
    owners = {r: replace(job_owner(), room_id=r) for r in rooms}
    ob = owners["!room_b:test"]
    key_b = ConversationKey(ob.recipient, "!room_b:test", ob.resolved_thread_id, str(ob.requester_id), False)
    bot = _bot("parent", set(rooms), open_replies=open_replies)
    coordinator = job_coordinator(tmp_path, runtime, bot)
    sent_notices = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(coord_module, "send_message_result", sent_notices)
    id_calls, notice_calls = [], []
    orig_id, orig_notice = coord_module.next_follow_up_id, coord_module.held_notice
    monkeypatch.setattr(coord_module, "next_follow_up_id", lambda k, g: (id_calls.append(1), orig_id(k, g))[1])
    monkeypatch.setattr(coord_module, "held_notice", lambda k, g: (notice_calls.append(1), orig_notice(k, g))[1])
    try:
        held_src = _follow_up_event_id(key_b, [], _FOLLOW_UP_LIMIT)
        await _seed_jobs(
            runtime,
            [
                _snap("a-1", owners["!room_a:test"]),
                _snap("b-held", ob, source_event_id=held_src),
                _snap("c-reply", owners["!room_c:test"], reply_id="reply-open"),
                _snap("pending-job", owners["!room_d:test"]),
            ],
        )
        await coordinator._reconcile()
        assert [c.args[0].room_id for c in bot.admit_job_follow_up.await_args_list] == ["!room_a:test"]
        assert (sent_notices.await_count, bool(id_calls), bool(notice_calls)) == (1, True, True)

        id_calls.clear()
        notice_calls.clear()
        await coordinator._reconcile()
        assert (id_calls, notice_calls) == ([], [])

        open_replies.clear()
        grants["pending-job"] = "allowed"
        await coordinator._reconcile()
        assert [c.args[0].room_id for c in bot.admit_job_follow_up.await_args_list] == [
            "!room_a:test",
            "!room_c:test",
            "!room_d:test",
        ]

        id_calls.clear()
        await runtime.release_wait("a-2", await _start(runtime, "a-2", owners["!room_a:test"], _fast_ok))
        await wait_for_status(runtime, "a-2", "completed")
        await coordinator._reconcile()
        assert len(id_calls) >= 1
        assert bot.admit_job_follow_up.await_args.args[0].room_id == "!room_a:test"

        for jid in ("a-1", "a-2"):
            await runtime.acknowledge_wait(jid, (await runtime.wait(jid, owner=owners["!room_a:test"], depth=0)).claim)
        backdate_job(runtime, "b-held", datetime.now(UTC) - timedelta(days=31))
        await runtime.expire_ended(now=datetime.now(UTC), source_finished=AsyncMock(return_value=True))
        await coordinator._reconcile()
        assert {"!room_a:test", "!room_b:test"}.isdisjoint({raw[1] for raw in coordinator._follow_up_known})
        assert coordinator._sent_notices == {}
    finally:
        await runtime.shutdown()


class _FullScanOracle:
    """Independent full-scan reference oracle over runtime.retained_jobs()."""

    def __init__(self) -> None:
        self.published: dict[tuple[str, str], dict[str, object]] = {}
        self.admitted: set[str] = set()
        self.sent_notices: dict[str, frozenset[str]] = {}

    def publish(self, runtime: ToolJobRuntime, bots: dict[str, MagicMock]) -> list[tuple[str, str, dict[str, object]]]:
        retained = runtime.retained_jobs()
        awaited = frozenset(jid for jid, e in runtime._entries.items() if e.claim is not None)
        writes: list[tuple[str, str, dict[str, object]]] = []
        groups: dict[tuple[str, str], list[BackgroundJob]] = {pair: [] for pair in self.published}
        for job in retained:
            if job.owner.room_id is not None:
                groups.setdefault((job.owner.recipient, job.owner.room_id), []).append(job)
        for (agent, room_id), group in groups.items():
            pub = self.published.get((agent, room_id))
            listed_before = set() if pub is None else {str(item["job_id"]) for item in pub["jobs"]}  # type: ignore[attr-defined]
            content = _jobs_state_content(group, awaited=awaited, listed_before=listed_before)
            if pub is None and not any(j.status in TERMINAL_STATUSES for j in group):
                pub = _EMPTY
            if pub != content and (bot := bots.get(agent)) is not None and room_id in bot.client.rooms:
                writes.append((agent, room_id, content))
                self.published[(agent, room_id)] = content
        return writes

    async def admit_follow_ups(
        self,
        runtime: ToolJobRuntime,
        bots: dict[str, MagicMock],
    ) -> tuple[list[tuple[ConversationKey, str]], list[tuple[str, str]]]:
        raw_groups = {ConversationKey(*k): tuple(v) for k, v in _scan_candidates(runtime.retained_jobs()).items()}
        due, admitted_now, notices_now = set(), [], []
        for key in sorted(raw_groups, key=_key_order):
            group = raw_groups[key]
            cands, held = follow_up_jobs(group), held_jobs(group)
            known = ({next_follow_up_id(key, cands)} if cands else set()) | (
                {held_notice(key, held)[0]} if held and not key.silent else set()
            )
            if not known or known <= self.admitted | self.sent_notices.keys():
                due |= known
                continue
            if (bot := bots.get(key.recipient)) is None:
                continue
            ended = tuple(
                sorted(
                    (
                        j
                        for j in group
                        if runtime._entries[j.job_id].claim is None
                        and runtime._grant(j) == "allowed"
                        and j.status != "cancelled"
                    ),
                    key=lambda j: (j.created_at, j.job_id),
                ),
            )
            ended = await unheld_jobs(ended, bot.journal_principal().replies)
            if jobs := follow_up_jobs(ended):
                fid = next_follow_up_id(key, jobs)
                if fid not in self.admitted:
                    admitted_now.append((key, fid))
                    self.admitted.add(fid)
                due.add(fid)
            elif not key.silent and (held_now := held_jobs(ended)):
                nid, _ = held_notice(key, held_now)
                if nid not in self.sent_notices:
                    notices_now.append((key.room_id, nid))
                    self.sent_notices[nid] = frozenset(j.job_id for j in held_now)
        self.admitted &= due
        waiting = {j.job_id for g in raw_groups.values() for j in g}
        self.sent_notices = {nid: ids for nid, ids in self.sent_notices.items() if ids <= waiting}
        return admitted_now, notices_now


class _EquivalenceHarness:
    """Drive randomized tool-job transitions and compare indexed reconciliation against _FullScanOracle."""

    agents, rooms, threads, requesters = (
        ("agent_a", "agent_b"),
        ("!r1:test", "!r2:test", "!r3:test"),
        (None, "$t1", "$t2"),
        ("@u1:test", "@u2:test"),
    )

    def __init__(self, storage: Path) -> None:
        self.storage, self.rng, self.next_idx = storage, random.Random(20261125), 0  # noqa: S311
        self.grants: dict[str, JobGrant] = {}
        self.open_replies: set[str] = {"rep-1"}
        self.gates: dict[str, tuple[asyncio.Event, str]] = {}
        self.claims: dict[str, JobClaim | None] = {}
        self.owners: dict[str, tuple[ToolExecutionIdentity, int]] = {}
        self.sent_notices: list[tuple[str, str]] = []
        self.runtime: ToolJobRuntime
        self.bots: dict[str, MagicMock]
        self.coordinator: ToolJobRuntimeCoordinator
        self.oracle: _FullScanOracle

    async def init(self) -> None:
        self.runtime = await tool_job_runtime(self.storage, grant=lambda j: self.grants.get(j.job_id, "allowed"))
        await self.runtime.recover()
        self.bots = {a: _bot(a, set(self.rooms), open_replies=self.open_replies) for a in self.agents}
        self.coordinator = _coord(self.storage, self.runtime, self.bots.get)
        self.oracle = _FullScanOracle()

    async def start_random(self) -> None:
        self.next_idx += 1
        jid = f"job-{self.next_idx:04d}"
        a, r, t, u = (
            self.rng.choice(self.agents),
            self.rng.choice(self.rooms),
            self.rng.choice(self.threads),
            self.rng.choice(self.requesters),
        )
        silent, wf_node, depth = self.rng.random() < 0.25, self.rng.random() < 0.15, int(self.rng.random() < 0.15)
        streak = self.rng.choice((0, 0, _FOLLOW_UP_LIMIT - 1, _FOLLOW_UP_LIMIT))
        owner = replace(job_owner(), agent_name=a, room_id=r, thread_id=t, resolved_thread_id=t, requester_id=u)
        src = _follow_up_event_id(ConversationKey(a, r, t, u, silent), [], streak) if streak else "$human"
        ev, status = asyncio.Event(), self.rng.choice(("completed", "failed", "cancelled"))
        self.gates[jid], self.grants[jid] = (ev, status), "allowed"

        async def run_job(event: asyncio.Event = ev, out: str = status) -> BackgroundOutcome:
            await event.wait()
            return BackgroundOutcome(out, "res")  # type: ignore[arg-type]

        claim = await _start(
            self.runtime,
            jid,
            owner,
            run_job,
            depth=depth,
            kind="delegation" if wf_node else "tool",
            source_event_id=src,
            source_kind=SILENT_SCHEDULE_SOURCE_KIND if silent else None,
            adapter={"child": {"workflow_run_id": "wf-1", "subagent_id": "s1"}} if wf_node else {},
            reply_id="rep-1" if self.rng.random() < 0.2 else None,
        )
        self.claims[jid], self.owners[jid] = claim, (owner, depth)
        if self.rng.random() < 0.5:
            await self.runtime.release_wait(jid, claim)
            self.claims[jid] = None

    async def _mutate_live(self, op: str, live_ids: list[str]) -> None:
        jid = self.rng.choice(live_ids)
        if op == "finish":
            ev, expected_status = self.gates[jid]
            ev.set()
            if self.runtime._entries[jid].job.status in {"running", "awaiting_approval"}:
                await wait_for_status(self.runtime, jid, expected_status)
        else:
            await self.runtime.set_awaiting_approval(jid, awaiting=self.rng.choice((True, False)))

    async def mutate_ended(self, op: str, ended_ids: list[str]) -> None:
        if op == "claim":
            jid = self.rng.choice(ended_ids)
            if self.claims.get(jid) is not None:
                await self.runtime.release_wait(jid, self.claims[jid])
                self.claims[jid] = None
            elif jid in self.owners and self.grants.get(jid, "allowed") == "allowed":
                self.claims[jid] = (
                    await self.runtime.wait(jid, owner=self.owners[jid][0], depth=self.owners[jid][1])
                ).claim
        elif op == "ack":
            if unconsumed := [
                jid for jid in ended_ids if not self.runtime._entries[jid].job.consumed and jid in self.owners
            ]:
                jid = self.rng.choice(unconsumed)
                if self.claims.get(jid) is None and self.grants.get(jid, "allowed") == "allowed":
                    self.claims[jid] = (
                        await self.runtime.wait(jid, owner=self.owners[jid][0], depth=self.owners[jid][1])
                    ).claim
                if self.claims.get(jid) is not None:
                    await self.runtime.acknowledge_wait(jid, self.claims[jid])
                    self.claims[jid] = None
        elif op == "expire":
            if unclaimed := [jid for jid in ended_ids if self.runtime._entries[jid].claim is None]:
                jid = self.rng.choice(unclaimed)
                backdate_job(self.runtime, jid, datetime.now(UTC) - timedelta(days=31))
                await self.runtime.expire_ended(now=datetime.now(UTC), source_finished=AsyncMock(return_value=True))

    async def verify_reconcile(self) -> None:
        expected_revoked = {
            j.job_id
            for j in self.runtime.retained_jobs()
            if j.status not in TERMINAL_STATUSES and self.grants.get(j.job_id, "allowed") == "denied"
        }
        await self.runtime.cancel_revoked()
        for jid in expected_revoked:
            assert self.runtime._entries[jid].job.status in {"cancel_requested", "cancelled"}
        if drains := [e.drain for e in self.runtime._entries.values() if e.drain is not None]:
            await asyncio.gather(*drains, return_exceptions=True)

        for bot in self.bots.values():
            bot.client.room_put_state.reset_mock()
            bot.admit_job_follow_up.reset_mock()
        self.sent_notices.clear()
        await self.coordinator._reconcile()
        _assert_indexes(self.runtime)

        actual_writes = [
            (a, c.kwargs["room_id"], c.kwargs["content"])
            for a, b in self.bots.items()
            for c in b.client.room_put_state.await_args_list
        ]
        actual_admitted = [
            (c.args[0], c.args[1]) for b in self.bots.values() for c in b.admit_job_follow_up.await_args_list
        ]
        oracle_writes = self.oracle.publish(self.runtime, self.bots)
        oracle_admitted, oracle_notices = await self.oracle.admit_follow_ups(self.runtime, self.bots)
        assert sorted(actual_writes, key=lambda x: (x[0], x[1])) == sorted(oracle_writes, key=lambda x: (x[0], x[1]))
        assert sorted(actual_admitted, key=lambda x: (_key_order(x[0]), x[1])) == sorted(
            oracle_admitted,
            key=lambda x: (_key_order(x[0]), x[1]),
        )
        assert self.sent_notices == oracle_notices

        for raw_key, group in _scan_candidates(self.runtime.retained_jobs()).items():
            key = ConversationKey(*raw_key)
            expected = tuple(
                sorted(
                    j.job_id
                    for j in group
                    if self.runtime._entries[j.job_id].claim is None
                    and self.grants.get(j.job_id, "allowed") == "allowed"
                ),
            )
            assert tuple(sorted(j.job_id for j in await self.runtime.ended_jobs(key))) == expected
        assert await self.runtime.ended_jobs(ConversationKey("agent_a", "!empty:test", None, "@u1:test", False)) == ()

    async def step(self, idx: int) -> None:
        op = self.rng.choices(
            ["start", "finish", "awaiting", "claim", "ack", "grant", "reply", "expire", "restart", "reconcile"],
            [30, 22, 8, 12, 12, 10, 6, 8, 4, 18],
            k=1,
        )[0]
        live_ids = [
            jid
            for jid in self.gates
            if jid in self.runtime._entries and self.runtime._entries[jid].job.status not in TERMINAL_STATUSES
        ]
        ended_ids = [j.job_id for j in self.runtime.retained_jobs() if j.status in TERMINAL_STATUSES]
        if op == "start":
            await self.start_random()
        elif op in {"finish", "awaiting"} and live_ids:
            await self._mutate_live(op, live_ids)
        elif op in {"claim", "ack", "expire"} and ended_ids:
            await self.mutate_ended(op, ended_ids)
        elif op == "grant" and (live_ids or ended_ids):
            self.grants[self.rng.choice(live_ids or ended_ids)] = self.rng.choice(("allowed", "pending", "denied"))
        elif op == "reply":
            self.open_replies.discard("rep-1") if "rep-1" in self.open_replies else self.open_replies.add("rep-1")
        elif op == "restart":
            for ev, _ in self.gates.values():
                ev.set()
            await self.runtime.shutdown()
            self.gates.clear()
            self.claims.clear()
            await self.init()
            await self.verify_reconcile()
            return
        elif op == "reconcile" or idx % 15 == 0:
            await self.verify_reconcile()
            return
        _assert_indexes(self.runtime)


@pytest.mark.asyncio
async def test_randomized_reconcile_equivalence_against_full_scan_oracle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """220 deterministic operations produce identical reconcile effects and queries to a full-scan oracle."""
    monkeypatch.setattr(publication, "_LISTED_AFTER", timedelta(seconds=0))
    harness = _EquivalenceHarness(tmp_path)
    monkeypatch.setattr(
        coord_module,
        "send_message_result",
        AsyncMock(
            side_effect=lambda _c, room_id, _b, *, transaction_id, **_kw: (
                harness.sent_notices.append((room_id, transaction_id)),
                MagicMock(),
            )[1],
        ),
    )
    await harness.init()
    try:
        for step in range(220):
            await harness.step(step)
        await harness.verify_reconcile()
    finally:
        for ev, _ in harness.gates.values():
            ev.set()
        await harness.runtime.shutdown()
