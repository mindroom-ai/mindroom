"""Explicit Stop cancels owned work without manufacturing result-consumption receipts."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from mindroom.config.main import Config
from mindroom.event_journal import DeliveryStage
from mindroom.message_target import MessageTarget
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.response_sources import ResponseAttempt, ResponseSources
from mindroom.tool_jobs import runtime as runtime_module
from mindroom.tool_jobs.completion import completion_event
from mindroom.tool_jobs.instances import pin_background_tool_jobs, release_background_tool_jobs
from mindroom.tool_jobs.runtime import BackgroundOutcome, register_background_runtime
from mindroom.tool_jobs.user_stop import response_was_stopped, stop_conversation_jobs
from mindroom.turn_record import TurnRecord
from mindroom.user_stop_reconciliation import UserStopReconciler, UserStopReconcilerDeps
from tests.conftest import test_runtime_paths, unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot
from tests.test_event_journal_store import ROOM, admit
from tests.test_user_stop_convergence import _CountingGateway
from tests.tool_job_helpers import backdate_job, job_owner, start_job, tool_job_runtime

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.event_journal import EventJournalStore, PrincipalStore
    from mindroom.tool_jobs.runtime import BackgroundJob
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


async def _bind_reply(
    store: PrincipalStore,
    source: str,
    response: str,
    *,
    edit_order: int | None = None,
    entity_name: str = "agent",
) -> None:
    """Bind a visible reply through the real delivery journal."""
    await store.enqueue_matrix_delivery(
        delivery_id=source,
        stage=DeliveryStage.INITIAL,
        room_id=ROOM,
        thread_id="$thread",
        payload={"body": "Working"},
        edits_event_id=response,
        response_attempt=ResponseAttempt(
            entity_name,
            ResponseSources((source,), (source,), edit_receipt_order=edit_order),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bound_reply", [False, True])
async def test_stop_scopes_prior_work_to_clicked_reply_and_requester(
    tmp_path: Path,
    journal_store: EventJournalStore,
    *,
    bound_reply: bool,
) -> None:
    """A late Stop cancels both earlier turns, including team work, but no unrelated/newer work."""
    store = journal_store.principal("agent@alice")
    for source in ("$first", "$follow-up", "$newer"):
        await admit(store, source)
    if bound_reply:
        await _bind_reply(store, "$follow-up", "$reply")
    await _bind_reply(store, "$newer", "$newer-reply")
    target = MessageTarget.resolve(ROOM, "$thread", "$thread")
    owner = replace(
        job_owner(),
        agent_name="agent",
        room_id=ROOM,
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=target.session_id,
    )
    stopped = TurnRecord.create(
        ("$follow-up",),
        response_event_id="$reply",
        conversation_target=target,
        requester_id=owner.requester_id,
        response_owner="agent",
        user_stop_receipt_order=100,
    )
    jobs = {
        "earlier": (owner, "$first"),
        "current": (owner, "$follow-up"),
        "member": (replace(owner, agent_name="member", transport_agent_name="agent"), "$first"),
        "newer": (owner, "$newer"),
        "other-user": (replace(owner, requester_id="@other:example.org"), "$first"),
        "other-agent": (replace(owner, agent_name="other"), "$first"),
        "other-room": (replace(owner, room_id="!elsewhere:example.org"), "$first"),
        "other-thread": (replace(owner, resolved_thread_id="$other", session_id="other"), "$first"),
    }
    runtime = tool_job_runtime(tmp_path)

    async def operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        return BackgroundOutcome("completed", "unreachable")

    try:
        for job_id, (source_owner, source) in jobs.items():
            await start_job(
                runtime,
                job_id,
                tool_name="tool",
                depth=0,
                source_event_id=source,
                adapter={},
                owner=source_owner,
                operation=operation,
            )
        # Merely sending a follow-up has not cancelled any accepted work.
        for job_id, (source_owner, _) in jobs.items():
            assert (await runtime.lookup(job_id, owner=source_owner, depth=0)).status == "running"
        await stop_conversation_jobs(runtime, store, stopped, stop_receipt_order=100)
        for job_id, (source_owner, _) in jobs.items():
            job = await runtime.lookup(job_id, owner=source_owner, depth=0)
            if job_id in {"earlier", "current", "member"}:
                assert job.user_stop_receipt_order == 100
                await runtime.cancel(job_id, owner=source_owner, depth=0)
            else:
                assert job.user_stop_receipt_order is None
                assert job.status == "running"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("retention_pass", [False, True])
async def test_stop_follows_ancestry_through_an_answered_completion(
    tmp_path: Path,
    journal_store: EventJournalStore,
    *,
    retention_pass: bool,
) -> None:
    """Work started by an already answered completion turn still belongs to the human turn behind that completion.

    Retention keeps the delivered job, however old and consumed, while that work exists, so the ancestry stays intact.
    """
    store = journal_store.principal("agent@alice")
    for source in ("$first", "$follow-up"):
        await admit(store, source)
    await _bind_reply(store, "$follow-up", "$reply")
    target = MessageTarget.resolve(ROOM, "$thread", "$thread")
    owner = replace(
        job_owner(),
        agent_name="agent",
        room_id=ROOM,
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=target.session_id,
    )
    stopped = TurnRecord.create(
        ("$follow-up",),
        response_event_id="$reply",
        conversation_target=target,
        requester_id=owner.requester_id,
        response_owner="agent",
        user_stop_receipt_order=100,
    )
    runtime = tool_job_runtime(tmp_path)

    async def finished() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "prior result")

    async def running() -> BackgroundOutcome:
        await asyncio.Event().wait()
        return BackgroundOutcome("completed", "unreachable")

    try:
        await start_job(
            runtime,
            "prior",
            tool_name="tool",
            depth=0,
            source_event_id="$first",
            adapter={},
            owner=owner,
            operation=finished,
        )
        ready = await runtime.wait("prior", owner=owner, depth=0)
        completion = completion_event(ready.job, sender_id="@mindroom_agent:example.org")
        await store.admit(completion)
        await start_job(
            runtime,
            "descendant",
            tool_name="tool",
            depth=0,
            source_event_id=completion.event_id,
            adapter={},
            owner=owner,
            operation=running,
        )
        # Answering the completion turn settles its journal event, which releases the event's saved payload.
        await store.settle(completion.event_id)
        if retention_pass:
            await runtime.acknowledge_wait("prior", ready.claim)
            backdate_job(runtime, "prior", datetime.now(UTC) - timedelta(days=31))

            async def source_finished(_job: BackgroundJob) -> bool:
                return True

            await runtime.expire_consumed(
                before=datetime.now(UTC) - timedelta(days=30),
                source_finished=source_finished,
            )
        else:
            await runtime.release_wait("prior", ready.claim)
        await stop_conversation_jobs(runtime, store, stopped, stop_receipt_order=100)
        assert await runtime.is_user_stopped("descendant")
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_stop_includes_reserved_waits_and_preserves_honest_cancellation(tmp_path: Path) -> None:
    """Stop returns after cancellation admission while uncooperative cleanup still owns the job."""
    runtime = tool_job_runtime(tmp_path)
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
        return BackgroundOutcome("completed", "unreachable")

    async def selected(job: BackgroundJob) -> bool:
        return job.job_id == "active"

    try:
        _, claim = await runtime.start(
            "active",
            tool_name="slow",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
        )
        await started.wait()
        await asyncio.wait_for(runtime.stop_jobs(receipt_order=7, matches=selected), 2)
        await asyncio.wait_for(cleaning.wait(), 2)
        saved = await runtime.lookup("active", owner=job_owner(), depth=0)
        assert saved.status == "cancel_requested"
        assert saved.user_stop_receipt_order == 7
        assert not saved.consumed
        assert await runtime.pending_outcomes() == []
        release.set()
        await runtime.release_wait("active", claim)
        waited = await runtime.wait("active", owner=job_owner(), depth=0)
        assert waited.job.status == "cancelled"
        await runtime.release_wait("active", waited.claim)
        assert await runtime.pending_outcomes() == []
    finally:
        release.set()
        await runtime.shutdown()

    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        saved = await restored.lookup("active", owner=job_owner(), depth=0)
        assert saved.user_stop_receipt_order == 7
        assert not saved.consumed
        assert await restored.pending_outcomes() == []
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_replayed_stop_preserves_newer_edit_but_cancels_older_edit_work(
    tmp_path: Path,
    journal_store: EventJournalStore,
) -> None:
    """One coalesced reply can have several revisions; the durable STOP cutoff selects the older one."""
    store = journal_store.principal("agent@alice")
    for source in ("$first", "$coalesced", "$old-edit", "$new-edit"):
        await admit(store, source)
    old = await store.load_event("$old-edit")
    new = await store.load_event("$new-edit")
    assert old is not None
    assert new is not None
    await _bind_reply(store, "$old-edit", "$reply", edit_order=old.receipt_order)
    await _bind_reply(store, "$new-edit", "$reply", edit_order=new.receipt_order)
    target = MessageTarget.resolve(ROOM, "$thread", "$thread")
    owner = replace(
        job_owner(),
        agent_name="agent",
        room_id=ROOM,
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=target.session_id,
    )
    stopped = TurnRecord.create(
        ("$first", "$coalesced"),
        response_event_id="$reply",
        conversation_target=target,
        requester_id=owner.requester_id,
        response_owner="agent",
        latest_edit_receipt_order=new.receipt_order,
        user_stop_receipt_order=old.receipt_order,
    )
    runtime = tool_job_runtime(tmp_path)

    async def operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        return BackgroundOutcome("completed")

    try:
        for job_id, source in (("older-edit", "$old-edit"), ("newer-edit", "$new-edit")):
            await start_job(
                runtime,
                job_id,
                tool_name="tool",
                depth=0,
                source_event_id=source,
                adapter={},
                owner=owner,
                operation=operation,
            )
        await stop_conversation_jobs(runtime, store, stopped, stop_receipt_order=old.receipt_order)
        assert await runtime.is_user_stopped("older-edit")
        assert not await runtime.is_user_stopped("newer-edit")
        assert (await runtime.lookup("newer-edit", owner=owner, depth=0)).status == "running"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("restart_gap", [False, True])
async def test_stop_is_applied_live_and_after_crash_before_job_markers(
    tmp_path: Path,
    *,
    enabled: bool,
    restart_gap: bool,
) -> None:
    """Real Stop ownership and coordinator startup both apply the saved intent before completion delivery."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    config = runner.deps.runtime.config
    config.background_tool_jobs.enabled = enabled
    paths = runner.deps.runtime_paths
    store = runner.deps.approval_store
    await bot._turn_store.warm()
    await admit(store, "$source")
    await _bind_reply(store, "$source", "$reply", entity_name="general")
    target = MessageTarget.resolve(ROOM, "$thread", "$thread")
    owner = replace(
        job_owner(),
        agent_name="general",
        room_id=ROOM,
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=target.session_id,
    )
    await bot._turn_store.record_turn(
        TurnRecord.create(
            ("$source",),
            response_event_id="$reply",
            conversation_target=target,
            requester_id=owner.requester_id,
            response_owner="general",
            completed=False,
        ),
    )
    runtime = tool_job_runtime(paths.storage_root)
    instance = pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    coordinator = None

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "Keep the saved answer")

    try:
        await start_job(
            runtime,
            "ready",
            tool_name="tool",
            depth=0,
            source_event_id="$source",
            adapter={},
            owner=owner,
            operation=operation,
        )
        waited = await runtime.wait("ready", owner=owner, depth=0)
        await runtime.release_wait("ready", waited.claim)
        if restart_gap:
            await bot._turn_store.record_user_stopped_response("$reply", 100, delivery_settled=True)
            await runtime.shutdown()
            release_background_tool_jobs(paths, instance)
            coordinator = ToolJobRuntimeCoordinator(
                paths,
                lambda: config,
                lambda name: bot if name == "general" else None,
                runner.deps.runtime.agent_reply_memberships,
            )
            await coordinator.initialize(bot._journal_store)
            await coordinator.sync()
            if enabled:
                runtime = coordinator.runtime
        else:
            reconciler = UserStopReconciler(UserStopReconcilerDeps(bot._turn_store, runner, _CountingGateway()))
            assert await reconciler.finalize("$reply", 100, AsyncMock())
        assert await runtime.is_user_stopped("ready") is enabled
        if enabled:
            assert await runtime.pending_outcomes() == []
            assert await runtime.outcome("ready", 0) is None
    finally:
        if coordinator is not None:
            await coordinator.stop()
        await runtime.shutdown()


async def _every_job(_job: BackgroundJob) -> bool:
    return True


async def _completed() -> BackgroundOutcome:
    return BackgroundOutcome("completed", "Keep the saved answer")


@pytest.mark.asyncio
async def test_replayed_stop_saves_its_mark_once_across_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Applying the same Stop again, or an older one, at every startup neither rewrites the job nor moves its update time."""
    writes: list[str] = []
    writer = runtime_module.write_json_file_durable

    def counting_writer(path: Path, payload: object, *, strict_atomic_replace: bool) -> None:
        writes.append(path.name)
        writer(path, payload, strict_atomic_replace=strict_atomic_replace)

    runtime = tool_job_runtime(tmp_path)
    try:
        await start_job(
            runtime,
            "ready",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=_completed,
        )
        waited = await runtime.wait("ready", owner=job_owner(), depth=0)
        await runtime.release_wait("ready", waited.claim)
        monkeypatch.setattr(runtime_module, "write_json_file_durable", counting_writer)
        await runtime.stop_jobs(receipt_order=100, matches=_every_job)
        stopped = await runtime.lookup("ready", owner=job_owner(), depth=0)
        await runtime.stop_jobs(receipt_order=100, matches=_every_job)
        await runtime.stop_jobs(receipt_order=50, matches=_every_job)
    finally:
        await runtime.shutdown()
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        await restored.stop_jobs(receipt_order=100, matches=_every_job)
        replayed = await restored.lookup("ready", owner=job_owner(), depth=0)
    finally:
        await restored.shutdown()
    assert writes == ["ready.json"]
    assert replayed.user_stop_receipt_order == 100
    assert replayed.updated_at == stopped.updated_at


@pytest.mark.asyncio
async def test_slow_stop_matching_leaves_jobs_accessible(tmp_path: Path) -> None:
    """Stop judges jobs outside the runtime lock, so its journal reads cannot stall other job access."""
    runtime = tool_job_runtime(tmp_path)
    judging, release = asyncio.Event(), asyncio.Event()

    async def slow_match(_job: BackgroundJob) -> bool:
        judging.set()
        await release.wait()
        return True

    stop = None
    try:
        await start_job(runtime, "held", tool_name="tool", depth=0, adapter={}, owner=job_owner(), operation=_completed)
        stop = asyncio.create_task(runtime.stop_jobs(receipt_order=100, matches=slow_match))
        await judging.wait()
        seen = await asyncio.wait_for(runtime.lookup("held", owner=job_owner(), depth=0), 30)
        assert seen.user_stop_receipt_order is None
        release.set()
        await stop
        assert (await runtime.lookup("held", owner=job_owner(), depth=0)).user_stop_receipt_order == 100
    finally:
        release.set()
        if stop is not None:
            await asyncio.gather(stop, return_exceptions=True)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_stop_reaches_an_approval_continued_while_it_was_judged(tmp_path: Path) -> None:
    """An approval continued while Stop was judging its paused generation still gets stopped in its new generation."""
    runtime = tool_job_runtime(tmp_path)
    judging, release, continued = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def awaiting() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval")

    async def resumed() -> BackgroundOutcome:
        continued.set()
        await asyncio.Event().wait()
        raise AssertionError

    async def held_match(_job: BackgroundJob) -> bool:
        judging.set()
        await release.wait()
        return True

    stop = None
    try:
        await start_job(runtime, "paused", tool_name="tool", depth=0, adapter={}, owner=job_owner(), operation=awaiting)
        waited = await runtime.wait("paused", owner=job_owner(), depth=0)
        await runtime.release_wait("paused", waited.claim)
        stop = asyncio.create_task(runtime.stop_jobs(receipt_order=100, matches=held_match))
        await judging.wait()
        await asyncio.wait_for(
            runtime.continue_job("paused", owner=job_owner(), depth=0, expected_generation=0, operation=resumed),
            30,
        )
        await continued.wait()
        release.set()
        await stop
        settled = await runtime.wait("paused", owner=job_owner(), depth=0)
        await runtime.release_wait("paused", settled.claim)
        assert settled.job.user_stop_receipt_order == 100
        assert settled.job.status == "cancelled"
    finally:
        release.set()
        if stop is not None:
            await asyncio.gather(stop, return_exceptions=True)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_approval_stopped_during_shutdown_is_cancelled_on_restart(tmp_path: Path) -> None:
    """Shutdown leaves a Stopped approval paused, so recovery cancels it with its cleanup as live cancellation would.

    Like live cancellation of an approval, the cancelled outcome takes a fresh generation that stale claims cannot own.
    """
    cleaned: list[str] = []

    async def awaiting() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval")

    async def cleanup(job: BackgroundJob) -> None:
        cleaned.append(job.job_id)

    runtime = tool_job_runtime(tmp_path)
    try:
        await start_job(runtime, "paused", tool_name="tool", depth=0, adapter={}, owner=job_owner(), operation=awaiting)
        waited = await runtime.wait("paused", owner=job_owner(), depth=0)
        await runtime.release_wait("paused", waited.claim)
        await runtime.quiesce()
        await runtime.stop_jobs(receipt_order=100, matches=_every_job)
        assert (await runtime.lookup("paused", owner=job_owner(), depth=0)).status == "awaiting_approval"
    finally:
        await runtime.shutdown()
    restored = tool_job_runtime(tmp_path, cancel=cleanup)
    try:
        await restored.recover()
        job = await restored.lookup("paused", owner=job_owner(), depth=0)
        assert await restored.stoppable_jobs() == []
    finally:
        await restored.shutdown()
    assert (job.status, job.generation, job.user_stop_receipt_order, cleaned) == ("cancelled", 1, 100, ["paused"])


@pytest.mark.asyncio
async def test_stop_on_a_closed_runtime_raises_without_saving_a_mark(tmp_path: Path) -> None:
    """A closed runtime refuses a Stop, so its unsettled reaction replays after restart instead of being lost."""
    runtime = tool_job_runtime(tmp_path)
    try:
        await start_job(runtime, "done", tool_name="tool", depth=0, adapter={}, owner=job_owner(), operation=_completed)
    finally:
        await runtime.shutdown()
    with pytest.raises(runtime_module.JobAccessError, match="closed"):
        await runtime.stop_jobs(receipt_order=100, matches=_every_job)
    assert runtime_module.read_job_snapshot(tmp_path / "tool_jobs" / "done.json").user_stop_receipt_order is None


def _conversation_owner(target: MessageTarget) -> ToolExecutionIdentity:
    return replace(
        job_owner(),
        agent_name="general",
        room_id=ROOM,
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=target.session_id,
    )


async def _stoppable_reply(bot: AgentBot, store: PrincipalStore, target: MessageTarget) -> None:
    """Admit a human turn and bind its unfinished visible reply, which a Stop can target."""
    await bot._turn_store.warm()
    await admit(store, "$source")
    await _bind_reply(store, "$source", "$reply", entity_name="general")
    await bot._turn_store.record_turn(
        TurnRecord.create(
            ("$source",),
            response_event_id="$reply",
            conversation_target=target,
            requester_id=job_owner().requester_id,
            response_owner="general",
            completed=False,
        ),
    )


@pytest.mark.asyncio
async def test_stop_during_shutdown_marks_jobs_without_failing(tmp_path: Path) -> None:
    """A Stop while shutdown drains execution still settles; the job keeps its mark and shutdown interrupts it."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    runner.deps.runtime.config.background_tool_jobs.enabled = True
    paths = runner.deps.runtime_paths
    target = MessageTarget.resolve(ROOM, "$thread", "$thread")
    owner = _conversation_owner(target)
    await _stoppable_reply(bot, runner.deps.approval_store, target)
    runtime = tool_job_runtime(paths.storage_root)
    pin_background_tool_jobs(runner.deps.runtime.config, paths)
    register_background_runtime(paths, runtime)
    started, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()
        raise AssertionError

    quiesce = None
    try:
        await start_job(
            runtime,
            "active",
            tool_name="tool",
            depth=0,
            source_event_id="$source",
            adapter={},
            owner=owner,
            operation=operation,
        )
        await started.wait()
        quiesce = asyncio.create_task(runtime.quiesce())
        await draining.wait()
        reconciler = UserStopReconciler(UserStopReconcilerDeps(bot._turn_store, runner, _CountingGateway()))
        assert await reconciler.finalize("$reply", 100, AsyncMock())
        release.set()
        await quiesce
        job = await runtime.lookup("active", owner=owner, depth=0)
        assert job.status == "interrupted"
        assert job.user_stop_receipt_order == 100
    finally:
        release.set()
        if quiesce is not None:
            await asyncio.gather(quiesce, return_exceptions=True)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["sync", "worker_pass"])
async def test_saved_stop_reaches_jobs_of_a_bot_that_appears_after_startup(tmp_path: Path, trigger: str) -> None:
    """Startup restores a saved Stop for a recipient whose bot appears only after the first pass."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    config = runner.deps.runtime.config
    config.background_tool_jobs.enabled = True
    paths = runner.deps.runtime_paths
    target = MessageTarget.resolve(ROOM, "$thread", "$thread")
    owner = _conversation_owner(target)
    await _stoppable_reply(bot, runner.deps.approval_store, target)
    runtime = tool_job_runtime(paths.storage_root)
    try:
        await start_job(
            runtime,
            "ready",
            tool_name="tool",
            depth=0,
            source_event_id="$source",
            adapter={},
            owner=owner,
            operation=_completed,
        )
        waited = await runtime.wait("ready", owner=owner, depth=0)
        await runtime.release_wait("ready", waited.claim)
    finally:
        await runtime.shutdown()
    # A crash after the Stop intent landed and before any job marker did.
    await bot._turn_store.record_user_stopped_response("$reply", 100, delivery_settled=True)
    bots: dict[str, AgentBot] = {}
    coordinator = ToolJobRuntimeCoordinator(
        paths,
        lambda: config,
        bots.get,
        runner.deps.runtime.agent_reply_memberships,
    )
    try:
        await coordinator.initialize(bot._journal_store)
        await coordinator.sync()
        assert not await coordinator.runtime.is_user_stopped("ready")
        bots["general"] = bot
        if trigger == "sync":
            await coordinator.sync()
        else:
            await coordinator.deliver_pending()
        assert await coordinator.runtime.is_user_stopped("ready")
        assert await coordinator.runtime.outcome("ready", 0) is None
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_stopped_job_fences_its_settled_completion(tmp_path: Path, journal_store: EventJournalStore) -> None:
    """A settled completion event keeps only its identity, which still fences the response delivering a Stopped job."""
    paths = test_runtime_paths(tmp_path)
    store = journal_store.principal("parent@alice")
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(Config(), paths)
    register_background_runtime(paths, runtime)
    try:
        await start_job(
            runtime,
            "ready",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=_completed,
        )
        waited = await runtime.wait("ready", owner=job_owner(), depth=0)
        await runtime.release_wait("ready", waited.claim)
        completion = completion_event(waited.job, sender_id="@mindroom_parent:test")
        await store.admit(completion)
        await store.settle(completion.event_id)
        assert not await response_was_stopped(completion.event_id, paths, "parent")
        await runtime.stop_jobs(receipt_order=100, matches=_every_job)
        assert await response_was_stopped(completion.event_id, paths, "parent")
    finally:
        await runtime.shutdown()
