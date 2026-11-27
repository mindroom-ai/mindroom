"""Scaling, transition, and full-scan equivalence tests for indexed tool-job reconciliation."""

from __future__ import annotations

import asyncio
import json
import random
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
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
    FollowUpKey,
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
    saved_jobs,
    start_job,
    tool_job_journal,
    tool_job_runtime,
    wait_for_status,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from pathlib import Path

    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

type _Op = Callable[[], Awaitable[BackgroundOutcome]]


def _snap(jid: str, owner: ToolExecutionIdentity, status: str = "completed", **kw: Any) -> BackgroundJob:  # noqa: ANN401
    base: dict[str, Any] = {"tool_name": "t", "depth": 0, "adapter": {}, "status": status}
    return BackgroundJob(job_id=jid, owner=owner, **{**base, **kw})


async def _launch(rt: ToolJobRuntime, jid: str, owner: ToolExecutionIdentity, op: _Op, **kw: Any) -> None:  # noqa: ANN401
    await start_job(rt, jid, owner=owner, operation=op, **{"tool_name": "t", "depth": 0, "adapter": {}, **kw})


async def _fast_ok() -> BackgroundOutcome:
    return BackgroundOutcome("completed", "ok")


def _bot(agent: str, rooms: set[str], *, open_replies: set[str] | None = None) -> MagicMock:
    bot = MagicMock()
    bot.matrix_id = MatrixID.parse(f"@mindroom_{agent}:test")
    bot.running = bot.first_sync_complete = True
    bot.client.rooms = {r: MagicMock() for r in rooms}
    ok_put = lambda **kw: nio.RoomPutStateResponse(event_id="$s", room_id=kw["room_id"])  # noqa: E731
    bot.client.room_put_state = AsyncMock(side_effect=ok_put)
    bot.admit_job_follow_up = AsyncMock()
    active = set() if open_replies is None else open_replies
    reply_obj = MagicMock(state=ReplyState.ACTIVE, current_span_id="s1")
    load_reply = lambda rid: reply_obj if rid in active else None  # noqa: E731
    bot.journal_principal.return_value.replies.load = AsyncMock(side_effect=load_reply)
    return bot


def _coord(
    storage: Path,
    rt: ToolJobRuntime,
    bots: Callable[[str], Any],
    cfg: Callable[[], Config] = Config,
) -> ToolJobRuntimeCoordinator:
    paths = test_runtime_paths(storage)
    journal_fn = lambda: tool_job_journal(paths.storage_root)  # noqa: E731
    coord = ToolJobRuntimeCoordinator(paths, cfg, bots, MagicMock(), journal_fn)
    coord._runtime = rt
    return coord


async def _seed_jobs(rt: ToolJobRuntime, jobs: Sequence[BackgroundJob]) -> None:
    store, gen = rt._store, rt._store._runtime_generation
    rows = [(j.job_id, json.dumps(asdict(j))) for j in jobs]
    await store._backend.write(lambda tx: [journal_tool_jobs.accept(tx, gen, jid, raw) for jid, raw in rows])
    await rt.recover()


def _scan_candidates(jobs: Sequence[BackgroundJob]) -> dict[FollowUpKey, list[BackgroundJob]]:
    buckets: dict[FollowUpKey, list[BackgroundJob]] = {}
    for j in jobs:
        o = j.owner
        if (
            j.status in TERMINAL_STATUSES
            and j.depth == 0
            and not j.consumed
            and not is_workflow_node_job(j)
            and o.room_id
            and o.requester_id
        ):
            silent = j.source_kind == SILENT_SCHEDULE_SOURCE_KIND
            buckets.setdefault((o.recipient, o.room_id, o.resolved_thread_id, o.requester_id, silent), []).append(j)
    return buckets


async def _expected_ended_rooms(storage: Path, retained: Sequence[BackgroundJob]) -> frozenset[tuple[str, str]]:
    """Derive expected ended rooms independently from saved snapshots plus hot in-memory snapshots."""
    all_jobs = {**(await saved_jobs(storage)), **{j.job_id: j for j in retained}}
    return frozenset(
        (j.owner.recipient, j.owner.room_id)
        for j in all_jobs.values()
        if j.status in TERMINAL_STATUSES and j.owner.room_id is not None
    )


async def _assert_indexes(rt: ToolJobRuntime, storage: Path) -> None:
    retained = rt.retained_jobs()
    live = {j.job_id for j in retained if j.status not in TERMINAL_STATUSES}
    assert {j.job_id for j in rt.live_jobs()} == live == set(rt._live)
    assert rt.awaited_job_ids() == {jid for jid, e in rt._entries.items() if e.claim is not None}
    cont = {e.job.job_id for e in rt._entries.values() if rt._awaits_continuation(e)}
    assert {j.job_id for j in rt.awaiting_continuation()} == cont
    rooms = Counter(
        (j.owner.recipient, j.owner.room_id) for j in retained if j.status in TERMINAL_STATUSES and j.owner.room_id
    )
    assert rt._ended_room_counts == dict(rooms)
    assert rt.ended_rooms() == await _expected_ended_rooms(storage, retained)
    cands = {k: {j.job_id for j in g} for k, g in _scan_candidates(retained).items()}
    revs = rt.follow_up_candidate_revisions()
    assert set(revs) == set(cands)
    assert {k: {j.job_id for j in rt.follow_up_candidate_jobs(k)} for k in revs} == cands
    all_cand = {i for ids in cands.values() for i in ids}
    assert {j.job_id for j in retained if rt.is_follow_up_candidate(j.job_id)} == all_cand


@pytest.mark.asyncio
async def test_reconcile_scales_with_live_jobs_and_skips_idle_cleared_rooms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With 5,000+ ended jobs and 3 live jobs, _reconcile touches only live jobs and skips idle cleared rooms."""
    ended_n, live_n, room_n = 5_000, 3, 20
    grants: list[str] = []
    rt = await tool_job_runtime(tmp_path, grant=lambda j: (grants.append(j.job_id), "allowed")[1])
    gate = asyncio.Event()
    try:
        base = job_owner()
        snaps = [
            _snap(f"e-{i:05d}", replace(base, room_id=f"!r{i % room_n}:t"), consumed=True, returned_inline=i % 2 == 0)
            for i in range(ended_n)
        ]
        await _seed_jobs(rt, snaps)

        async def blocked() -> BackgroundOutcome:
            await gate.wait()
            return BackgroundOutcome("completed", "done")

        for i in range(live_n):
            await _launch(rt, f"live-{i}", replace(base, room_id="!r0:t"), blocked)

        bot = _bot("parent", {f"!r{i}:t" for i in range(room_n)})
        coord = job_coordinator(tmp_path, rt, bot)
        ret_c, st_c, cnt_c = [], [], []
        o_ret, o_st, o_cnt = rt.retained_jobs, publication._job_state, publication._jobs_state_content
        monkeypatch.setattr(rt, "retained_jobs", lambda: (ret_c.append(1), o_ret())[1])
        monkeypatch.setattr(publication, "_job_state", lambda j: (st_c.append(j.job_id), o_st(j))[1])

        def spy_cnt(jobs: Sequence[BackgroundJob], **kw: object) -> dict[str, object]:
            cnt_c.append((len(jobs), all(j.status not in TERMINAL_STATUSES for j in jobs)))
            return o_cnt(jobs, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(publication, "_jobs_state_content", spy_cnt)

        grants.clear()
        await coord._reconcile()
        assert (len(ret_c), len(grants), len(st_c), bot.client.room_put_state.await_count) == (
            0,
            live_n,
            live_n,
            room_n,
        )
        assert sum(n for n, _ in cnt_c) == live_n
        assert all(ok for _, ok in cnt_c)

        grants.clear()
        st_c.clear()
        cnt_c.clear()
        bot.client.room_put_state.reset_mock()
        await coord._reconcile()
        assert (len(ret_c), len(grants), len(st_c), bot.client.room_put_state.await_count) == (0, live_n, live_n, 0)
        assert cnt_c == [(live_n, True)]
    finally:
        gate.set()
        await rt.shutdown()


@pytest.mark.asyncio
async def test_warm_pass_skips_unchanged_candidate_buckets_and_expired_cleared_rooms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Warm passes skip unchanged candidate job fetches, notice scans when empty, and cleared/expired room grouping."""
    rt = await tool_job_runtime(tmp_path)
    owner_a, owner_b = replace(job_owner(), room_id="!ra:t"), replace(job_owner(), room_id="!rb:t")
    bot = _bot("parent", {"!ra:t", "!rb:t"})
    coord = job_coordinator(tmp_path, rt, bot)
    try:
        snaps = [_snap(f"c-{i:03d}", owner_a, status="cancelled") for i in range(200)]
        snaps.append(_snap("admitted-1", owner_b))
        await _seed_jobs(rt, snaps)

        await coord._reconcile()
        assert (bot.admit_job_follow_up.await_count, coord._sent_notices) == (1, {})

        fetches, checks = [], []
        o_fetch, o_cand = rt.follow_up_candidate_jobs, rt.is_follow_up_candidate
        monkeypatch.setattr(rt, "follow_up_candidate_jobs", lambda k: (fetches.append(k), o_fetch(k))[1])
        monkeypatch.setattr(rt, "is_follow_up_candidate", lambda j: (checks.append(j), o_cand(j))[1])

        await coord._reconcile()
        assert (fetches, checks) == ([], [])
        assert coord._publisher._group_jobs(rt.live_jobs(), rt.ended_rooms()) == ({}, set())

        old = datetime.now(UTC) - timedelta(days=31)
        for job in rt.retained_jobs():
            backdate_job(rt, job.job_id, old)
        await rt.expire_ended(now=datetime.now(UTC), source_finished=AsyncMock(return_value=True))
        assert rt.retained_jobs() == ()
        assert coord._publisher._group_jobs(rt.live_jobs(), rt.ended_rooms()) == ({}, set())
        await coord._reconcile()
        assert coord._follow_up_known == {}
    finally:
        await rt.shutdown()


@pytest.mark.asyncio
async def test_staggered_held_notices_skip_unchanged_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Staggered held notices skip revalidation on unchanged passes and prune selectively on ack."""
    rt = await tool_job_runtime(tmp_path)
    owner = replace(job_owner(), room_id="!ra:t")
    key = ConversationKey(owner.recipient, "!ra:t", owner.resolved_thread_id, str(owner.requester_id), False)
    bot = _bot("parent", {"!ra:t"})
    coord = job_coordinator(tmp_path, rt, bot)
    sent_notices = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(coord_module, "send_message_result", sent_notices)
    try:
        held_src = _follow_up_event_id(key, [], _FOLLOW_UP_LIMIT)
        await _launch(rt, "h-1", owner, _fast_ok, source_event_id=held_src)
        await wait_for_status(rt, "h-1", "completed")
        await coord._reconcile()
        [n1] = list(coord._sent_notices)

        await _launch(rt, "h-2", owner, _fast_ok, source_event_id=held_src)
        await wait_for_status(rt, "h-2", "completed")
        await coord._reconcile()
        [n12] = [nid for nid in coord._sent_notices if nid != n1]

        await _launch(rt, "h-3", owner, _fast_ok, source_event_id=held_src)
        await wait_for_status(rt, "h-3", "completed")
        await coord._reconcile()
        assert sent_notices.await_count == 3
        assert len(coord._sent_notices) == 3

        fetches, checks = [], []
        o_fetch, o_cand = rt.follow_up_candidate_jobs, rt.is_follow_up_candidate
        monkeypatch.setattr(rt, "follow_up_candidate_jobs", lambda k: (fetches.append(k), o_fetch(k))[1])
        monkeypatch.setattr(rt, "is_follow_up_candidate", lambda j: (checks.append(j), o_cand(j))[1])

        await coord._reconcile()
        assert (fetches, checks) == ([], [])

        await rt.acknowledge_wait("h-3", (await rt.wait("h-3", owner=owner, depth=0)).claim)
        await coord._reconcile()
        assert set(coord._sent_notices) == {n1, n12}
        assert set(coord._sent_notice_revisions) == {n1, n12}
        assert sent_notices.await_count == 3

        fetches.clear()
        checks.clear()
        await coord._reconcile()
        assert (fetches, checks) == ([], [])

        for jid in ("h-1", "h-2"):
            await rt.acknowledge_wait(jid, (await rt.wait(jid, owner=owner, depth=0)).claim)
        checks.clear()
        await coord._reconcile()
        assert (coord._sent_notices, coord._sent_notice_revisions, checks) == ({}, {}, [])

        coord._sent_notices["manual-stale"] = frozenset({"h-1"})
        await coord._reconcile()
        assert coord._sent_notices == {}
    finally:
        await rt.shutdown()


@pytest.mark.asyncio
async def test_admit_follow_ups_skips_grant_checks_for_cancelled_jobs(tmp_path: Path) -> None:
    """_admit_follow_ups asks ended_jobs to skip cancelled jobs before checking grants."""
    grant_calls: list[str] = []
    rt = await tool_job_runtime(tmp_path, grant=lambda j: (grant_calls.append(j.job_id), "allowed")[1])
    owner = replace(job_owner(), room_id="!rb:t")
    key = ConversationKey(owner.recipient, "!rb:t", owner.resolved_thread_id, str(owner.requester_id), False)
    bot = _bot("parent", {"!rb:t"}, open_replies={"open-reply"})
    coord = job_coordinator(tmp_path, rt, bot)
    try:
        snaps = [_snap(f"canc-{i}", owner, status="cancelled") for i in range(4)]
        snaps.append(_snap("held-reply", owner, reply_id="open-reply"))
        await _seed_jobs(rt, snaps)

        grant_calls.clear()
        await coord._admit_follow_ups(rt)
        assert grant_calls == ["held-reply"]
        assert bot.admit_job_follow_up.await_count == 0

        # Default ended_jobs() retains cancelled jobs for other callers.
        all_ended = await rt.ended_jobs(key)
        assert {job.job_id for job in all_ended} == {"held-reply", *(f"canc-{i}" for i in range(4))}
    finally:
        await rt.shutdown()


@pytest.mark.asyncio
async def test_index_transitions_lifecycle_claims_and_expiry(tmp_path: Path) -> None:
    """start, span_stopped, reattach, waits, save_adapter, claims, cancel, ack, and expiry keep indexes exact."""
    grants: dict[str, JobGrant] = {}
    owner_a, owner_b = replace(job_owner(), room_id="!ra:t"), replace(job_owner(), room_id="!rb:t")
    gate_a, gate_cancel = asyncio.Event(), asyncio.Event()
    rt = await tool_job_runtime(tmp_path, grant=lambda j: grants.get(j.job_id, "allowed"))
    try:

        async def gated(ev: asyncio.Event) -> BackgroundOutcome:
            await ev.wait()
            return BackgroundOutcome("completed", "ok")

        op_a = lambda: gated(gate_a)  # noqa: E731
        _, claim_a = await rt.start("job-a", tool_name="t", depth=0, adapter={"v": 1}, owner=owner_a, operation=op_a)
        await rt.set_awaiting_approval("job-a", awaiting=True)
        await rt.set_awaiting_approval("job-a", awaiting=False)
        async with rt.human_decision_wait("job-a"):
            await _assert_indexes(rt, tmp_path)
        await rt.save_adapter("job-a", v=2)

        grants["job-a"] = "pending"
        assert (await rt.wait("job-a", owner=owner_a, depth=0, claim=claim_a)).claim is None
        await _assert_indexes(rt, tmp_path)
        grants["job-a"] = "allowed"
        _, claim_a = await rt.start(
            "job-a",
            tool_name="t",
            depth=0,
            adapter={"v": 2},
            owner=owner_a,
            operation=op_a,
            reattach=True,
        )
        await rt.release_waits((("job-a", claim_a),))
        gate_a.set()
        await wait_for_status(rt, "job-a", "completed")

        await _launch(rt, "job-stopped", owner_a, op_a, span_stopped=lambda: True)
        await wait_for_status(rt, "job-stopped", "cancelled")
        await _launch(rt, "job-cancel", owner_b, lambda: gated(gate_cancel))
        await rt.cancel("job-cancel", owner=owner_b, depth=0)
        await wait_for_status(rt, "job-cancel", "cancelled")
        claim_ack = (await rt.wait("job-a", owner=owner_a, depth=0)).claim
        await rt.acknowledge_waits((AcknowledgeWait("job-a", claim_ack),))
        await _assert_indexes(rt, tmp_path)

        b_key = (owner_b.recipient, "!rb:t", owner_b.resolved_thread_id, str(owner_b.requester_id), False)
        old_rev = rt.follow_up_candidate_revisions()[b_key]
        backdate_job(rt, "job-cancel", datetime.now(UTC) - timedelta(days=31))
        expired = await rt.expire_ended(now=datetime.now(UTC), source_finished=AsyncMock(return_value=True))
        assert [j.job_id for j in expired] == ["job-cancel"]
        assert (owner_b.recipient, "!rb:t") not in rt._ended_room_counts
        await _assert_indexes(rt, tmp_path)

        await _launch(rt, "job-b2", owner_b, _fast_ok)
        await wait_for_status(rt, "job-b2", "completed")
        assert rt.follow_up_candidate_revisions()[b_key] > old_rev
        await _assert_indexes(rt, tmp_path)
    finally:
        gate_a.set()
        gate_cancel.set()
        await rt.shutdown()


@pytest.mark.asyncio
async def test_index_transitions_save_failure_and_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Failed outcome saves and retry via cancel drain keep all indexes exact without double-counting."""
    owner_a = replace(job_owner(), room_id="!ra:t")
    gate_fail = asyncio.Event()
    rt = await tool_job_runtime(tmp_path)
    try:

        async def op_fail() -> BackgroundOutcome:
            await gate_fail.wait()
            msg = "boom"
            raise RuntimeError(msg)

        await _launch(rt, "job-fail", owner_a, op_fail)
        fail_save = True

        def maybe_fail(snap: dict[str, object], _payload: object | None) -> None:
            if fail_save and snap["job_id"] == "job-fail" and snap["status"] == "failed":
                msg = "disk"
                raise OSError(msg)

        intercept_job_saves(monkeypatch, before=maybe_fail)
        gate_fail.set()
        await wait_for_status(rt, "job-fail", "failed")
        assert not rt._outcome_saved("job-fail")
        await _assert_indexes(rt, tmp_path)
        fail_save = False
        await rt.cancel("job-fail", owner=owner_a, depth=0)
        if (drain := rt._entries["job-fail"].drain) is not None:
            await drain
        assert rt._outcome_saved("job-fail")
        intercept_job_saves(monkeypatch)
        await _assert_indexes(rt, tmp_path)
    finally:
        gate_fail.set()
        await rt.shutdown()


@pytest.mark.asyncio
async def test_index_transitions_recovery_and_continuation(tmp_path: Path) -> None:
    """recover() and continue_stopped(relaunch / outcome / None) keep indexes exact without double-counting."""
    owner_a, owner_b = replace(job_owner(), room_id="!ra:t"), replace(job_owner(), room_id="!rb:t")
    rt2 = await tool_job_runtime(tmp_path / "restart", continues=lambda j: j.kind == "delegation")
    gate = asyncio.Event()
    try:
        snaps = [
            _snap("cont-relaunch", owner_a, "running", kind="delegation", tool_name="w"),
            _snap("cont-outcome", owner_a, "running", kind="delegation", tool_name="w"),
            _snap("cont-none", owner_a, "running", kind="delegation", tool_name="w"),
            _snap("tool-running", owner_a, "running"),
            _snap("ended-unconsumed", owner_a, "completed", consumed=False),
            _snap("ended-consumed", owner_b, "completed", consumed=True),
        ]
        await _seed_jobs(rt2, snaps)
        assert {j.job_id for j in rt2.awaiting_continuation()} == {"cont-relaunch", "cont-outcome", "cont-none"}
        assert (tj := rt2.retained_job("tool-running")) is not None
        assert tj.status == "interrupted"
        await _assert_indexes(rt2, tmp_path / "restart")

        async def resumed() -> BackgroundOutcome:
            await gate.wait()
            return BackgroundOutcome("completed", "resumed")

        spec = JobSpec(tool_name="w", kind="delegation", adapter={"r": 1}, operation=resumed)
        await rt2.continue_stopped("cont-relaunch", spec)
        await rt2.continue_stopped("cont-outcome", BackgroundOutcome("completed", "direct"))
        await rt2.continue_stopped("cont-none", None)
        assert rt2.awaiting_continuation() == ()
        await _assert_indexes(rt2, tmp_path / "restart")
        gate.set()
        await wait_for_status(rt2, "cont-relaunch", "completed")
        await _assert_indexes(rt2, tmp_path / "restart")
    finally:
        gate.set()
        await rt2.shutdown()


@pytest.mark.asyncio
async def test_startup_room_clearing_retries_and_quiesce_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent bots or transient failures retry clearing, stay quiet once cleared, and quiesce(disabled) clears live rooms."""
    rt = await tool_job_runtime(tmp_path, continues=lambda j: j.kind == "delegation")
    gate = asyncio.Event()
    try:
        oa = replace(job_owner(), agent_name="agent_a", room_id="!ra:t")
        ob = replace(job_owner(), agent_name="agent_b", room_id="!rb:t")
        await _seed_jobs(
            rt,
            [_snap("e-a", oa, "interrupted", consumed=True), _snap("e-b", ob, "interrupted", consumed=True)],
        )
        bot_a, bot_b = _bot("agent_a", {"!ra:t", "!rc:t"}), _bot("agent_b", {"!rb:t"})
        a_ready, fail_b, enabled = False, True, True

        def flaky_b(**kw: object) -> object:
            if fail_b:
                return nio.RoomPutStateError(message="tmp", status_code="M_UNKNOWN")
            return nio.RoomPutStateResponse(event_id="$ok", room_id=str(kw["room_id"]))

        bot_b.client.room_put_state = AsyncMock(side_effect=flaky_b)
        cfg = lambda: Config(background_tool_jobs=BackgroundToolJobsConfig(enabled=enabled))  # noqa: E731
        coord = _coord(tmp_path, rt, lambda n: (bot_a if a_ready else None) if n == "agent_a" else bot_b, cfg)

        await coord._reconcile()
        assert (
            bot_a.client.room_put_state.await_count,
            bot_b.client.room_put_state.await_count,
            coord._publisher._published,
        ) == (0, 1, {})

        a_ready, fail_b = True, False
        await coord._reconcile()
        assert bot_a.client.room_put_state.await_args.kwargs["content"] == _EMPTY
        assert bot_b.client.room_put_state.await_args.kwargs["content"] == _EMPTY

        cnt_c: list[int] = []
        o_cnt = publication._jobs_state_content
        monkeypatch.setattr(
            publication,
            "_jobs_state_content",
            lambda jobs, **kw: (cnt_c.append(len(jobs)), o_cnt(jobs, **kw))[1],
        )
        bot_a.client.room_put_state.reset_mock()
        bot_b.client.room_put_state.reset_mock()
        await coord._reconcile()
        assert (cnt_c, bot_a.client.room_put_state.await_count, bot_b.client.room_put_state.await_count) == ([], 0, 0)

        async def blocked() -> BackgroundOutcome:
            await gate.wait()
            return BackgroundOutcome("completed", "ok")

        child = {"child": {"subagent_id": "s1", "delegation_id": "d1"}}
        await _launch(
            rt,
            "kept-live",
            replace(oa, room_id="!rc:t"),
            blocked,
            tool_name="w",
            kind="delegation",
            adapter=child,
        )
        await coord._reconcile()
        assert bot_a.client.room_put_state.await_args.kwargs["content"]["total"] == 1
        enabled = False
        await coord.quiesce()
        assert bot_a.client.room_put_state.await_args.kwargs["content"] == _EMPTY
    finally:
        gate.set()
        await rt.shutdown()


@pytest.mark.asyncio
async def test_follow_up_cache_invalidation_and_external_change_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged buckets skip hashing; mutations invalidate; open-reply & grant changes still admit on same revision."""
    grants: dict[str, JobGrant] = {"pending-job": "pending"}
    open_replies = {"reply-open"}
    rt = await tool_job_runtime(tmp_path, grant=lambda j: grants.get(j.job_id, "allowed"))
    rooms = ("!ra:t", "!rb:t", "!rc:t", "!rd:t")
    owners = {r: replace(job_owner(), room_id=r) for r in rooms}
    ob = owners["!rb:t"]
    key_b = ConversationKey(ob.recipient, "!rb:t", ob.resolved_thread_id, str(ob.requester_id), False)
    bot = _bot("parent", set(rooms), open_replies=open_replies)
    coord = job_coordinator(tmp_path, rt, bot)
    sent_notices = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(coord_module, "send_message_result", sent_notices)
    id_c, notice_c = [], []
    o_id, o_notice = coord_module.next_follow_up_id, coord_module.held_notice
    monkeypatch.setattr(coord_module, "next_follow_up_id", lambda k, g: (id_c.append(1), o_id(k, g))[1])
    monkeypatch.setattr(coord_module, "held_notice", lambda k, g: (notice_c.append(1), o_notice(k, g))[1])
    try:
        held_src = _follow_up_event_id(key_b, [], _FOLLOW_UP_LIMIT)
        snaps = [
            _snap("a-1", owners["!ra:t"]),
            _snap("b-held", ob, source_event_id=held_src),
            _snap("c-reply", owners["!rc:t"], reply_id="reply-open"),
            _snap("pending-job", owners["!rd:t"]),
        ]
        await _seed_jobs(rt, snaps)
        await coord._reconcile()
        assert [c.args[0].room_id for c in bot.admit_job_follow_up.await_args_list] == ["!ra:t"]
        assert (sent_notices.await_count, bool(id_c), bool(notice_c)) == (1, True, True)

        id_c.clear()
        notice_c.clear()
        await coord._reconcile()
        assert (id_c, notice_c) == ([], [])

        open_replies.clear()
        grants["pending-job"] = "allowed"
        await coord._reconcile()
        assert [c.args[0].room_id for c in bot.admit_job_follow_up.await_args_list] == ["!ra:t", "!rc:t", "!rd:t"]

        id_c.clear()
        await _launch(rt, "a-2", owners["!ra:t"], _fast_ok)
        await wait_for_status(rt, "a-2", "completed")
        await coord._reconcile()
        assert len(id_c) >= 1
        assert bot.admit_job_follow_up.await_args.args[0].room_id == "!ra:t"

        for jid in ("a-1", "a-2"):
            await rt.acknowledge_wait(jid, (await rt.wait(jid, owner=owners["!ra:t"], depth=0)).claim)
        backdate_job(rt, "b-held", datetime.now(UTC) - timedelta(days=31))
        await rt.expire_ended(now=datetime.now(UTC), source_finished=AsyncMock(return_value=True))
        await coord._reconcile()
        assert {"!ra:t", "!rb:t"}.isdisjoint({raw[1] for raw in coord._follow_up_known})
        assert coord._sent_notices == {}
    finally:
        await rt.shutdown()


@dataclass
class _FullScanOracle:
    """Independent full-scan reference oracle over runtime.retained_jobs()."""

    published: dict[tuple[str, str], dict[str, object]] = field(default_factory=dict)
    admitted: set[str] = field(default_factory=set)
    sent_notices: dict[str, frozenset[str]] = field(default_factory=dict)

    async def publish(
        self,
        rt: ToolJobRuntime,
        bots: dict[str, MagicMock],
        *,
        storage: Path,
    ) -> list[tuple[str, str, dict[str, object]]]:
        retained = rt.retained_jobs()
        ended_rooms = await _expected_ended_rooms(storage, retained)
        awaited = frozenset(jid for jid, e in rt._entries.items() if e.claim is not None)
        writes: list[tuple[str, str, dict[str, object]]] = []
        groups: dict[tuple[str, str], list[BackgroundJob]] = {pair: [] for pair in self.published}
        for pair in ended_rooms:
            groups.setdefault(pair, [])
        for job in retained:
            if job.owner.room_id is not None:
                groups.setdefault((job.owner.recipient, job.owner.room_id), []).append(job)
        for (agent, room_id), group in groups.items():
            pub = self.published.get((agent, room_id))
            listed = set() if pub is None else {str(item["job_id"]) for item in pub["jobs"]}  # type: ignore[attr-defined]
            content = _jobs_state_content(group, awaited=awaited, listed_before=listed)
            has_terminal = (agent, room_id) in ended_rooms
            if pub is None and not has_terminal:
                pub = _EMPTY
            if pub != content and (bot := bots.get(agent)) is not None and room_id in bot.client.rooms:
                writes.append((agent, room_id, content))
                self.published[(agent, room_id)] = content
        return writes

    async def admit_follow_ups(
        self,
        rt: ToolJobRuntime,
        bots: dict[str, MagicMock],
    ) -> tuple[list[tuple[ConversationKey, str]], list[tuple[str, str]]]:
        raw = {ConversationKey(*k): tuple(v) for k, v in _scan_candidates(rt.retained_jobs()).items()}
        due, adm_now, notices_now = set(), [], []
        for key in sorted(raw, key=_key_order):
            group = raw[key]
            cands, held = follow_up_jobs(group), held_jobs(group)
            known = ({next_follow_up_id(key, cands)} if cands else set()) | (
                {held_notice(key, held)[0]} if held and not key.silent else set()
            )
            if not known or known <= self.admitted | self.sent_notices.keys():
                due |= known
                continue
            if (bot := bots.get(key.recipient)) is None:
                continue
            ok = [
                j
                for j in group
                if rt._entries[j.job_id].claim is None and rt._grant(j) == "allowed" and j.status != "cancelled"
            ]
            unheld = await unheld_jobs(
                tuple(sorted(ok, key=lambda j: (j.created_at, j.job_id))),
                bot.journal_principal().replies,
            )
            if jobs := follow_up_jobs(unheld):
                fid = next_follow_up_id(key, jobs)
                if fid not in self.admitted:
                    adm_now.append((key, fid))
                    self.admitted.add(fid)
                due.add(fid)
            elif not key.silent and (held_now := held_jobs(unheld)):
                nid, _ = held_notice(key, held_now)
                if nid not in self.sent_notices:
                    notices_now.append((key.room_id, nid))
                    self.sent_notices[nid] = frozenset(j.job_id for j in held_now)
        self.admitted &= due
        waiting = {j.job_id for g in raw.values() for j in g}
        self.sent_notices = {nid: ids for nid, ids in self.sent_notices.items() if ids <= waiting}
        return adm_now, notices_now


@dataclass
class _EquivalenceHarness:
    """Drive 220 randomized transitions and compare indexed reconciliation against _FullScanOracle."""

    storage: Path
    rng: random.Random = field(default_factory=lambda: random.Random(20261125))  # noqa: S311
    grants: dict[str, JobGrant] = field(default_factory=dict)
    open_replies: set[str] = field(default_factory=lambda: {"rep-1"})
    gates: dict[str, tuple[asyncio.Event, str]] = field(default_factory=dict)
    claims: dict[str, JobClaim | None] = field(default_factory=dict)
    owners: dict[str, tuple[ToolExecutionIdentity, int]] = field(default_factory=dict)
    sent_notices: list[tuple[str, str]] = field(default_factory=list)
    next_idx: int = 0
    rt: ToolJobRuntime = field(init=False)
    bots: dict[str, MagicMock] = field(init=False)
    coord: ToolJobRuntimeCoordinator = field(init=False)
    oracle: _FullScanOracle = field(init=False)

    async def init(self) -> None:
        self.rt = await tool_job_runtime(self.storage, grant=lambda j: self.grants.get(j.job_id, "allowed"))
        await self.rt.recover()
        self.bots = {a: _bot(a, {"!r1:t", "!r2:t", "!r3:t"}, open_replies=self.open_replies) for a in ("a1", "a2")}
        self.coord = _coord(self.storage, self.rt, self.bots.get)
        self.oracle = _FullScanOracle()

    async def _claim(self, jid: str) -> JobClaim | None:
        if self.claims.get(jid) is None and jid in self.owners and self.grants.get(jid, "allowed") == "allowed":
            owner, depth = self.owners[jid]
            self.claims[jid] = (await self.rt.wait(jid, owner=owner, depth=depth)).claim
        return self.claims.get(jid)

    async def _start_random(self) -> None:
        self.next_idx += 1
        jid = f"j-{self.next_idx:04d}"
        a, r = self.rng.choice(("a1", "a2")), self.rng.choice(("!r1:t", "!r2:t", "!r3:t"))
        t, u = self.rng.choice((None, "$t1", "$t2")), self.rng.choice(("@u1:t", "@u2:t"))
        silent, wf, depth = self.rng.random() < 0.25, self.rng.random() < 0.15, int(self.rng.random() < 0.15)
        streak = self.rng.choice((0, 0, _FOLLOW_UP_LIMIT - 1, _FOLLOW_UP_LIMIT))
        owner = replace(job_owner(), agent_name=a, room_id=r, thread_id=t, resolved_thread_id=t, requester_id=u)
        src = _follow_up_event_id(ConversationKey(a, r, t, u, silent), [], streak) if streak else "$human"
        ev, status = asyncio.Event(), self.rng.choice(("completed", "failed", "cancelled"))
        self.gates[jid], self.grants[jid] = (ev, status), "allowed"

        async def run_job(event: asyncio.Event = ev, out: str = status) -> BackgroundOutcome:
            await event.wait()
            return BackgroundOutcome(out, "res")  # type: ignore[arg-type]

        kw: dict[str, Any] = {
            "tool_name": "t",
            "depth": depth,
            "kind": "delegation" if wf else "tool",
            "source_event_id": src,
            "source_kind": SILENT_SCHEDULE_SOURCE_KIND if silent else None,
            "adapter": {"child": {"workflow_run_id": "wf-1", "subagent_id": "s1"}} if wf else {},
            "reply_id": "rep-1" if self.rng.random() < 0.2 else None,
        }
        _, claim = await self.rt.start(jid, owner=owner, operation=run_job, **kw)
        self.claims[jid], self.owners[jid] = claim, (owner, depth)
        if self.rng.random() < 0.5:
            await self.rt.release_wait(jid, claim)
            self.claims[jid] = None

    async def _mutate_live(self, op: str, live_ids: list[str]) -> None:
        jid = self.rng.choice(live_ids)
        if op == "finish":
            ev, expected = self.gates[jid]
            ev.set()
            if self.rt._entries[jid].job.status in {"running", "awaiting_approval"}:
                await wait_for_status(self.rt, jid, expected)
        else:
            await self.rt.set_awaiting_approval(jid, awaiting=self.rng.choice((True, False)))

    async def _mutate_ended(self, op: str, ended_ids: list[str]) -> None:
        if op == "claim":
            jid = self.rng.choice(ended_ids)
            if self.claims.get(jid) is not None:
                await self.rt.release_wait(jid, self.claims[jid])
                self.claims[jid] = None
            else:
                await self._claim(jid)
        elif op == "ack":
            if unconsumed := [j for j in ended_ids if not self.rt._entries[j].job.consumed and j in self.owners]:
                jid = self.rng.choice(unconsumed)
                if (claim := await self._claim(jid)) is not None:
                    await self.rt.acknowledge_wait(jid, claim)
                    self.claims[jid] = None
        elif unclaimed := [j for j in ended_ids if self.rt._entries[j].claim is None]:
            jid = self.rng.choice(unclaimed)
            backdate_job(self.rt, jid, datetime.now(UTC) - timedelta(days=31))
            await self.rt.expire_ended(now=datetime.now(UTC), source_finished=AsyncMock(return_value=True))

    async def verify_reconcile(self) -> None:
        revoked = {
            j.job_id
            for j in self.rt.retained_jobs()
            if j.status not in TERMINAL_STATUSES and self.grants.get(j.job_id, "allowed") == "denied"
        }
        await self.rt.cancel_revoked()
        for jid in revoked:
            assert self.rt._entries[jid].job.status in {"cancel_requested", "cancelled"}
        if drains := [e.drain for e in self.rt._entries.values() if e.drain is not None]:
            await asyncio.gather(*drains, return_exceptions=True)

        for bot in self.bots.values():
            bot.client.room_put_state.reset_mock()
            bot.admit_job_follow_up.reset_mock()
        self.sent_notices.clear()
        await self.coord._reconcile()
        await _assert_indexes(self.rt, self.storage)

        actual_w = [
            (a, c.kwargs["room_id"], c.kwargs["content"])
            for a, b in self.bots.items()
            for c in b.client.room_put_state.await_args_list
        ]
        actual_a = [(c.args[0], c.args[1]) for b in self.bots.values() for c in b.admit_job_follow_up.await_args_list]
        oracle_w = await self.oracle.publish(self.rt, self.bots, storage=self.storage)
        oracle_a, oracle_n = await self.oracle.admit_follow_ups(self.rt, self.bots)
        wk, ak = (lambda x: (x[0], x[1])), (lambda x: (_key_order(x[0]), x[1]))
        assert sorted(actual_w, key=wk) == sorted(oracle_w, key=wk)
        assert sorted(actual_a, key=ak) == sorted(oracle_a, key=ak)
        assert self.sent_notices == oracle_n
        assert self.coord._sent_notices == self.oracle.sent_notices
        assert set(self.coord._sent_notice_revisions) == set(self.coord._sent_notices)

        for raw_key, group in _scan_candidates(self.rt.retained_jobs()).items():
            key = ConversationKey(*raw_key)
            ok = [
                j.job_id
                for j in group
                if self.rt._entries[j.job_id].claim is None and self.grants.get(j.job_id, "allowed") == "allowed"
            ]
            assert sorted(j.job_id for j in await self.rt.ended_jobs(key)) == sorted(ok)
        assert await self.rt.ended_jobs(ConversationKey("a1", "!empty:t", None, "@u1:t", False)) == ()

    async def step(self, idx: int) -> None:
        ops = ("start", "finish", "awaiting", "claim", "ack", "grant", "reply", "expire", "restart", "reconcile")
        op = self.rng.choices(ops, (30, 22, 8, 12, 12, 10, 6, 8, 4, 18), k=1)[0]
        live_ids = [
            j for j in self.gates if j in self.rt._entries and self.rt._entries[j].job.status not in TERMINAL_STATUSES
        ]
        ended_ids = [j.job_id for j in self.rt.retained_jobs() if j.status in TERMINAL_STATUSES]
        if op == "start":
            await self._start_random()
        elif op in {"finish", "awaiting"} and live_ids:
            await self._mutate_live(op, live_ids)
        elif op in {"claim", "ack", "expire"} and ended_ids:
            await self._mutate_ended(op, ended_ids)
        elif op == "grant" and (live_ids or ended_ids):
            self.grants[self.rng.choice(live_ids or ended_ids)] = self.rng.choice(("allowed", "pending", "denied"))
        elif op == "reply":
            self.open_replies.discard("rep-1") if "rep-1" in self.open_replies else self.open_replies.add("rep-1")
        elif op == "restart":
            for ev, _ in self.gates.values():
                ev.set()
            await self.rt.shutdown()
            self.gates.clear()
            self.claims.clear()
            await self.init()
            await self.verify_reconcile()
            return
        elif op == "reconcile" or idx % 15 == 0:
            await self.verify_reconcile()
            return
        await _assert_indexes(self.rt, self.storage)


@pytest.mark.asyncio
async def test_randomized_reconcile_equivalence_against_full_scan_oracle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """220 deterministic operations produce identical reconcile effects and queries to a full-scan oracle."""
    monkeypatch.setattr(publication, "_LISTED_AFTER", timedelta(seconds=0))
    harness = _EquivalenceHarness(tmp_path)

    async def fake_send(_c: object, room_id: str, _b: object, *, transaction_id: str, **_kw: object) -> MagicMock:
        harness.sent_notices.append((room_id, transaction_id))
        return MagicMock()

    monkeypatch.setattr(coord_module, "send_message_result", fake_send)
    await harness.init()
    try:
        for step in range(220):
            await harness.step(step)
        await harness.verify_reconcile()
    finally:
        for ev, _ in harness.gates.values():
            ev.set()
        await harness.rt.shutdown()
