"""Returned outcomes and shutdown cleanup survive cancellation and storage failures."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

from mindroom.agent_loop.model_loop import LoopState
from mindroom.agent_loop.spec import RunContext
from mindroom.agent_loop.tools import PreparedTools
from mindroom.config.main import Config
from mindroom.event_journal import ToolJobStore
from mindroom.history.run_checkpoint import checkpoint_hooks
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord, ToolExecution
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.run_storage import RunStorage, TerminalRecord, persist_into
from mindroom.session_storage.agent_storage import create_agent_state_storage
from mindroom.session_storage.store import ConversationStore, persist_to
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_jobs.consumption import ConsumptionOwner, acknowledge_saved_run, consumption_context
from mindroom.tool_jobs.resources import current_execution_resources
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobClaim, JobRecoveryBlockedError
from tests.bot_helpers import _runtime_bound_config
from tests.conftest import runtime_paths_for
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    cancel_settled,
    intercept_job_saves,
    job_owner,
    lookup,
    saved_jobs,
    saved_payload,
    start_job,
    tool_job_runtime,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from pathlib import Path

    from mindroom.event_journal import ToolJobSnapshotUpdate
    from mindroom.tool_jobs import runtime as runtime_module


@pytest.mark.asyncio
async def test_shutdown_save_failure_does_not_abandon_orchestrator_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job write failure must remain visible while later services and the shared journal close."""
    config = _runtime_bound_config(Config(), tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config), config=config)
    orchestrator.event_journal_store()
    runtime = await tool_job_runtime(orchestrator.runtime_paths.storage_root)
    orchestrator._tool_job_runtime._runtime = runtime
    started = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError

    def fail_save(_job: dict[str, object], _payload: object) -> None:
        msg = "job storage unavailable"
        raise OSError(msg)

    try:
        await start_job(
            runtime,
            "shutdown",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
        )
        await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
        with monkeypatch.context() as patch, capture_logs() as logs:
            intercept_job_saves(patch, before=fail_save)
            await orchestrator.stop()
        assert orchestrator._open_journal is None
        assert any(entry["event"] == "Background tool job runtime shutdown failed" for entry in logs)
        assert any(entry["event"] == "All agent bots stopped" for entry in logs)
    finally:
        await orchestrator.stop()


@pytest.mark.asyncio
async def test_outcome_write_failure_preserves_returned_value_until_storage_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed snapshot write cannot replace a completed side effect's output with the storage error."""
    runtime = await tool_job_runtime(tmp_path)
    release = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        await release.wait()
        with (tmp_path / "effect.txt").open("a") as effect:
            effect.write("once")
        return BackgroundOutcome("completed", "retained output", result_payload={"artifact": [1, 2]})

    def fail_outcome(job: dict[str, object], _payload: object) -> None:
        # The outcome and the payload saved with it fail together.
        if job["status"] == "completed":
            msg = "outcome storage unavailable"
            raise OSError(msg)

    try:
        await start_job(
            runtime,
            "write-failure",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
        )
        with monkeypatch.context() as patch:
            intercept_job_saves(patch, before=fail_outcome)
            release.set()
            waited = await asyncio.wait_for(runtime.wait("write-failure", owner=job_owner(), depth=0), JOB_TEST_TIMEOUT)
            assert waited.job.status == "completed"
            assert waited.job.result == "retained output"
            assert await runtime.read_payload(waited.job) == {"artifact": [1, 2]}
            assert await saved_payload(tmp_path, "write-failure") is None
            assert (tmp_path / "effect.txt").read_text() == "once"
        await runtime.acknowledge_wait("write-failure", waited.claim)
        await runtime.shutdown()
        restored = await tool_job_runtime(tmp_path)
        try:
            await restored.recover()
            saved = await lookup(restored, "write-failure", owner=job_owner(), depth=0)
            assert saved.status == "completed"
            assert saved.result == "retained output"
            assert await restored.read_payload(saved) == {"artifact": [1, 2]}
            assert saved.consumed
        finally:
            await restored.shutdown()
    finally:
        release.set()
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True])
async def test_returned_result_survives_stop_during_resource_cleanup(tmp_path: Path, shutdown: bool) -> None:
    """A completed side effect retains its output, but only after its resource cleanup drains."""
    runtime = await tool_job_runtime(tmp_path)
    cleaning, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def cleanup() -> None:
        cleaning.set()
        await release.wait()
        cleaned.set()

    async def operation() -> BackgroundOutcome:
        resources = current_execution_resources()
        assert resources is not None
        reference = resources.acquire()
        assert resources.defer(cleanup)
        await reference.release()
        with (tmp_path / "effect.txt").open("a") as effect:
            effect.write("completed once")
        return BackgroundOutcome("completed", "exact result", result_payload={"retained": [1, 2]})

    stopping = None
    try:
        await start_job(
            runtime,
            "returned",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
        )
        await asyncio.wait_for(cleaning.wait(), JOB_TEST_TIMEOUT)
        stopping = asyncio.create_task(
            runtime.shutdown() if shutdown else cancel_settled(runtime, "returned", owner=job_owner(), depth=0),
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(stopping), 0.02)
        assert not cleaned.is_set()
        assert (await saved_jobs(tmp_path))["returned"].status in {"running", "cancel_requested"}
        release.set()
        await asyncio.wait_for(stopping, JOB_TEST_TIMEOUT)
        assert cleaned.is_set()
        assert (tmp_path / "effect.txt").read_text() == "completed once"
        snapshot = (await saved_jobs(tmp_path))["returned"]
        assert snapshot.status == "completed"
        assert snapshot.result == "exact result"
        assert await saved_payload(tmp_path, "returned") == {"retained": [1, 2]}
    finally:
        release.set()
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_returned_result_survives_cancel_admission_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancellation cannot overwrite output returned while its durable admission owns the lock."""
    runtime = await tool_job_runtime(tmp_path)
    finish, returned, saving, release = (asyncio.Event() for _ in range(4))
    publish = runtime._publish

    async def operation() -> BackgroundOutcome:
        await finish.wait()
        with (tmp_path / "effect.txt").open("a") as effect:
            effect.write("completed once")
        returned.set()
        return BackgroundOutcome("completed", "exact result")

    async def delayed_publish(
        entry: runtime_module._Entry,
        job: runtime_module.BackgroundJob,
        payload: runtime_module.EncodedResultPayload | None = None,
    ) -> None:
        if job.status == "cancel_requested":
            saving.set()
            await release.wait()
        await publish(entry, job, payload)

    stopping = None
    try:
        await start_job(
            runtime,
            "returned",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
        )
        monkeypatch.setattr(runtime, "_publish", delayed_publish)
        stopping = asyncio.create_task(cancel_settled(runtime, "returned", owner=job_owner(), depth=0))
        await asyncio.wait_for(saving.wait(), JOB_TEST_TIMEOUT)
        finish.set()
        await asyncio.wait_for(returned.wait(), JOB_TEST_TIMEOUT)
        release.set()
        settled = await asyncio.wait_for(stopping, JOB_TEST_TIMEOUT)
        assert settled.status == "completed"
        assert settled.result == "exact result"
        snapshot = (await saved_jobs(tmp_path))["returned"]
        assert snapshot.status == "completed"
        assert snapshot.result == "exact result"
        assert (tmp_path / "effect.txt").read_text() == "completed once"
    finally:
        finish.set()
        release.set()
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_completed", [False, True])
async def test_shutdown_save_failure_still_drains_every_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_completed: bool,
) -> None:
    """A failed snapshot write remains visible without abandoning other work or blocking restart."""
    runtime = await tool_job_runtime(tmp_path)
    started, stopped = asyncio.Event(), asyncio.Event()
    cleaned: list[str] = []

    async def completed() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved")

    async def running() -> BackgroundOutcome:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        raise AssertionError

    async def cleanup(job: runtime_module.BackgroundJob) -> None:
        cleaned.append(job.job_id)

    await start_job(
        runtime,
        "first",
        tool_name="tool",
        depth=0,
        adapter={},
        owner=job_owner(),
        operation=completed if first_completed else running,
        cancel=cleanup,
    )
    if first_completed:
        waited = await runtime.wait("first", owner=job_owner(), depth=0)
        await runtime.release_wait("first", waited.claim)
    await start_job(
        runtime,
        "second",
        tool_name="tool",
        depth=0,
        adapter={},
        owner=job_owner(),
        operation=running,
        cancel=cleanup,
    )
    await started.wait()

    def fail_first_save(job: dict[str, object], _payload: object) -> None:
        if job["job_id"] == "first":
            msg = "injected shutdown save failure"
            raise OSError(msg)

    try:
        with monkeypatch.context() as patch:
            intercept_job_saves(patch, before=fail_first_save)
            expectation = nullcontext() if first_completed else pytest.raises(ExceptionGroup, match="shutdown")
            with expectation as failure:
                await runtime.shutdown()
        assert stopped.is_set()
        assert cleaned == (["second"] if first_completed else ["first", "second"])
        assert (await saved_jobs(tmp_path))["second"].status == "interrupted"
        restored = await tool_job_runtime(tmp_path)
        try:
            await restored.recover()
            assert (await lookup(restored, "second", owner=job_owner(), depth=0)).status == "interrupted"
        finally:
            await restored.shutdown()
        if failure is not None:
            assert len(failure.value.exceptions) == 1
            assert isinstance(failure.value.exceptions[0], OSError)
    finally:
        await asyncio.gather(runtime.shutdown(), return_exceptions=True)


@pytest.mark.asyncio
async def test_blocked_shutdown_cleanup_still_settles_other_jobs(tmp_path: Path) -> None:
    """A child still owned elsewhere stays unsettled without stranding later jobs, and shutdown reports it."""
    started: set[str] = set()
    both_started = asyncio.Event()

    async def cleanup(job: runtime_module.BackgroundJob) -> None:
        if job.job_id == "blocked":
            msg = "Native child is still executing; recovery cannot settle it."
            raise JobRecoveryBlockedError(msg)

    runtime = await tool_job_runtime(tmp_path, cancel=cleanup)

    def running(name: str) -> Callable[[], Awaitable[BackgroundOutcome]]:
        async def operation() -> BackgroundOutcome:
            started.add(name)
            if started == {"blocked", "later"}:
                both_started.set()
            await asyncio.Event().wait()
            raise AssertionError

        return operation

    for name in ("blocked", "later"):
        await start_job(
            runtime,
            name,
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=running(name),
        )
    await both_started.wait()
    with pytest.raises(ExceptionGroup, match="shutdown") as failure:
        await runtime.shutdown()
    assert [type(error) for error in failure.value.exceptions] == [JobRecoveryBlockedError]
    saved = {name: job.status for name, job in (await saved_jobs(tmp_path)).items()}
    assert saved == {"blocked": "running", "later": "interrupted"}


async def _start_completed_job(runtime: runtime_module.ToolJobRuntime, job_id: str) -> runtime_module.BackgroundJob:
    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", f"done-{job_id}")

    await start_job(runtime, job_id, tool_name="tool", depth=0, adapter={}, owner=job_owner(), operation=operation)
    waited = await runtime.wait(job_id, owner=job_owner(), depth=0)
    await runtime.release_wait(job_id, waited.claim)
    return waited.job


def _save_finished_run(store: ConversationStore, run_id: str, call_id: str) -> None:
    session = ConversationSession(session_id="session-1", kind="agent", owner_id="leader")
    store.save_run(
        session,
        RunRecord(
            run_id=run_id,
            session_id="session-1",
            status=RunStatus.completed,
            tools=[ToolExecution(tool_call_id=call_id, tool_name="job", result="ok")],
        ),
        record_usage=False,
    )


async def _claim_and_register(
    runtime: runtime_module.ToolJobRuntime,
    owner: ConsumptionOwner,
    job_id: str,
    *,
    run_id: str,
    call_id: str,
    team: bool = False,
    returned_inline: bool = False,
    claim_override: JobClaim | None = None,
) -> JobClaim:
    waited = await runtime.wait(job_id, owner=job_owner(), depth=0, timeout=0)
    assert waited.claim is not None
    owner.register(
        runtime,
        waited.job,
        waited.claim if claim_override is None else claim_override,
        run_id=run_id,
        team=team,
        call_id=call_id,
        returned_inline=returned_inline,
    )
    return waited.claim


@pytest.mark.asyncio
async def test_finalize_consumption_batches_run_reads_and_journal_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finalizing N claims for one run reads that run once and writes all acknowledgements in one transaction."""
    runtime = await tool_job_runtime(tmp_path)
    store = create_agent_state_storage("leader", tmp_path)
    _save_finished_run(store, "run-1", "collect-call")
    get_run_calls: list[str] = []
    original_get_run = ConversationStore.get_run

    def spy_get_run(target: ConversationStore, run_id: str) -> object:
        get_run_calls.append(run_id)
        return original_get_run(target, run_id)

    save_many_batches: list[int] = []
    original_save_many = ToolJobStore.save_many

    async def spy_save_many(job_store: ToolJobStore, updates: Sequence[ToolJobSnapshotUpdate]) -> None:
        save_many_batches.append(len(updates))
        await original_save_many(job_store, updates)

    monkeypatch.setattr(ConversationStore, "get_run", spy_get_run)
    monkeypatch.setattr(ToolJobStore, "save_many", spy_save_many)
    owner = ConsumptionOwner(storage_factory=lambda: store)
    try:
        await owner.finalize()
        assert get_run_calls == []
        assert save_many_batches == []

        job_ids = [f"job-{index}" for index in range(5)]
        for job_id in job_ids:
            await _start_completed_job(runtime, job_id)
            await _claim_and_register(runtime, owner, job_id, run_id="run-1", call_id="collect-call")
        await owner.finalize()
        assert get_run_calls == ["run-1"]
        assert save_many_batches == [5]
        persisted = await saved_jobs(tmp_path)
        for job_id in job_ids:
            assert (await lookup(runtime, job_id, owner=job_owner(), depth=0)).consumed
            assert persisted[job_id].consumed
            reclaimed = await runtime.wait(job_id, owner=job_owner(), depth=0, timeout=0)
            assert reclaimed.claim is not None
            await runtime.release_wait(job_id, reclaimed.claim)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_finalize_consumption_isolates_invalid_claims_and_storage_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid claims and per-run read errors do not block valid claims; journal failures leave all jobs unconsumed."""
    runtime = await tool_job_runtime(tmp_path)
    store = create_agent_state_storage("leader", tmp_path)
    _save_finished_run(store, "run-1", "call-1")
    original_get_run = ConversationStore.get_run

    def failing_get_run(target: ConversationStore, run_id: str) -> object:
        if run_id == "bad-run":
            msg = "corrupt run row"
            raise RuntimeError(msg)
        return original_get_run(target, run_id)

    monkeypatch.setattr(ConversationStore, "get_run", failing_get_run)
    owner = ConsumptionOwner(storage_factory=lambda: store)
    try:
        for job_id in ("valid-a", "valid-b", "stale", "bad-run-job"):
            await _start_completed_job(runtime, job_id)
        await _claim_and_register(runtime, owner, "valid-a", run_id="run-1", call_id="call-1")
        stale_claim = await _claim_and_register(
            runtime,
            owner,
            "stale",
            run_id="run-1",
            call_id="call-1",
            claim_override=JobClaim("wrong"),
        )
        await _claim_and_register(runtime, owner, "valid-b", run_id="run-1", call_id="call-1")
        await _claim_and_register(runtime, owner, "bad-run-job", run_id="bad-run", call_id="call-1")
        with capture_logs() as logs:
            await owner.finalize()
        persisted = await saved_jobs(tmp_path)
        for job_id in ("valid-a", "valid-b"):
            assert (await lookup(runtime, job_id, owner=job_owner(), depth=0)).consumed
            assert persisted[job_id].consumed
        for job_id in ("stale", "bad-run-job"):
            assert not (await lookup(runtime, job_id, owner=job_owner(), depth=0)).consumed
        assert {e["job_id"] for e in logs if e["event"] == "Tool result persistence was not confirmed"} == {
            "stale",
            "bad-run-job",
        }
        # Release stale's real claim (the registered one was wrong); bad-run-job was already released by finalize.
        await runtime.release_wait("stale", stale_claim)

        def fail_save(_job: dict[str, object], _payload: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        for job_id in ("stale", "bad-run-job"):
            await _claim_and_register(runtime, owner, job_id, run_id="run-1", call_id="call-1")
        with monkeypatch.context() as patch, capture_logs() as failure_logs:
            intercept_job_saves(patch, before=fail_save)
            await owner.finalize()
        persisted_after = await saved_jobs(tmp_path)
        for job_id in ("stale", "bad-run-job"):
            assert not (await lookup(runtime, job_id, owner=job_owner(), depth=0)).consumed
            assert not persisted_after[job_id].consumed
            reclaimed = await runtime.wait(job_id, owner=job_owner(), depth=0, timeout=0)
            assert reclaimed.claim is not None
            await runtime.release_wait(job_id, reclaimed.claim)
        assert {e["job_id"] for e in failure_logs if e["event"] == "Tool result persistence was not confirmed"} == {
            "stale",
            "bad-run-job",
        }
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_acknowledge_saved_run_matches_only_exact_finished_non_history_calls(tmp_path: Path) -> None:
    """Mid-turn acknowledgement ignores from_history messages and leaves unrelated or paused claims pending."""
    runtime = await tool_job_runtime(tmp_path)
    owner = ConsumptionOwner()
    try:
        for job_id in ("matched-tool", "matched-msg", "from-history", "wrong-team", "other-run", "paused"):
            await _start_completed_job(runtime, job_id)
        await _claim_and_register(runtime, owner, "matched-tool", run_id="run-1", call_id="call-tool")
        await _claim_and_register(runtime, owner, "matched-msg", run_id="run-1", call_id="call-msg")
        await _claim_and_register(runtime, owner, "from-history", run_id="run-1", call_id="call-history")
        await _claim_and_register(runtime, owner, "wrong-team", run_id="run-1", team=True, call_id="call-tool")
        await _claim_and_register(runtime, owner, "other-run", run_id="run-2", call_id="call-tool")
        await _claim_and_register(runtime, owner, "paused", run_id="run-1", call_id="call-paused")

        saved_run = RunRecord(
            run_id="run-1",
            session_id="session-1",
            status=RunStatus.cancelled,
            tools=[
                ToolExecution(tool_call_id="call-tool", tool_name="job", result="ok"),
                ToolExecution(
                    tool_call_id="call-paused",
                    tool_name="job",
                    requires_confirmation=True,
                    result="waiting",
                ),
            ],
            messages=[
                Message(role="tool", tool_call_id="call-msg", content="done"),
                Message(role="tool", tool_call_id="call-history", content="old", from_history=True),
            ],
        )
        with consumption_context(owner):
            await acknowledge_saved_run(saved_run)

        persisted = await saved_jobs(tmp_path)
        for job_id in ("matched-tool", "matched-msg"):
            assert (await lookup(runtime, job_id, owner=job_owner(), depth=0)).consumed
            assert persisted[job_id].consumed
        for job_id in ("from-history", "wrong-team", "other-run", "paused"):
            assert not (await lookup(runtime, job_id, owner=job_owner(), depth=0)).consumed
            assert not persisted[job_id].consumed
        assert {claim.job.job_id for claim in owner._claims} == {"from-history", "wrong-team", "other-run", "paused"}
        await owner.finalize()
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_non_written_saves_do_not_acknowledge_claims(tmp_path: Path) -> None:
    """Removed, final, and usage-only saves never invoke on_saved or acknowledge job claims."""
    runtime = await tool_job_runtime(tmp_path)
    store = create_agent_state_storage("leader", tmp_path)
    session = ConversationSession(session_id="session-1", kind="agent", owner_id="leader")
    _save_finished_run(store, "run-final", "call-1")
    with store.transaction(write=True) as connection:
        connection.execute(
            f'INSERT INTO "{store.names.log_removed}" (run_id, session_id) VALUES (?, ?)',  # noqa: S608
            ("run-removed", "session-1"),
        )
    owner = ConsumptionOwner()
    called: list[str] = []

    async def on_saved(record: RunRecord | TeamRunRecord) -> None:
        called.append(record.run_id)
        await acknowledge_saved_run(record)

    try:
        for job_id, run_id in (("job-final", "run-final"), ("job-removed", "run-removed"), ("job-usage", "run-usage")):
            await _start_completed_job(runtime, job_id)
            await _claim_and_register(runtime, owner, job_id, run_id=run_id, call_id="call-1")

        hooks = checkpoint_hooks(RunStorage(store=store, session=session, scope=None), on_saved=on_saved)
        assert hooks is not None
        persist_usage_only = persist_to(store, session, replays=lambda _record: False, on_saved=on_saved)
        persist_checked = persist_to(store, session, on_saved=on_saved)

        with consumption_context(owner):
            for run_id in ("run-final", "run-removed"):
                record = RunRecord(
                    run_id=run_id,
                    session_id="session-1",
                    status=RunStatus.running,
                    tools=[ToolExecution(tool_call_id="call-1", tool_name="job", result="ok")],
                )
                state = LoopState(
                    messages=[],
                    offered=PreparedTools(functions={}),
                    tools=(),
                    run=RunContext(run_id=run_id, session=session),
                    record=record,
                    request_index=1,
                    request_template=ModelRequest(messages=()),
                )
                await hooks.before_request(state)
                await persist_checked(record)
            await persist_usage_only(
                RunRecord(
                    run_id="run-usage",
                    session_id="session-1",
                    status=RunStatus.completed,
                    tools=[ToolExecution(tool_call_id="call-1", tool_name="job", result="ok")],
                ),
            )

        assert called == []
        persisted = await saved_jobs(tmp_path)
        for job_id in ("job-final", "job-removed", "job-usage"):
            assert not (await lookup(runtime, job_id, owner=job_owner(), depth=0)).consumed
            assert not persisted[job_id].consumed
        await owner.finalize()
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_acknowledge_saved_run_retries_transient_errors_and_drops_per_claim_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient journal errors keep claims and inline holds for retry; per-claim errors drop without releasing."""
    runtime = await tool_job_runtime(tmp_path)
    owner = ConsumptionOwner()
    try:
        for job_id in ("transient-inline", "valid-ok", "other-waiter"):
            await _start_completed_job(runtime, job_id)
        await _claim_and_register(
            runtime,
            owner,
            "transient-inline",
            run_id="run-1",
            call_id="call-1",
            returned_inline=True,
        )
        run1 = RunRecord(
            run_id="run-1",
            session_id="session-1",
            status=RunStatus.cancelled,
            tools=[ToolExecution(tool_call_id="call-1", tool_name="tool", result="ok")],
        )

        def fail_save(_job: dict[str, object], _payload: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        with monkeypatch.context() as patch, capture_logs() as fail_logs:
            intercept_job_saves(patch, before=fail_save)
            await owner._acknowledge_saved_run(run1)
        assert {e["job_id"] for e in fail_logs if e["event"] == "Tool result persistence was not confirmed"} == {
            "transient-inline",
        }
        assert not (await lookup(runtime, "transient-inline", owner=job_owner(), depth=0)).consumed
        assert len(owner._claims) == 1
        assert "transient-inline" not in {job.job_id for job in await runtime.list_jobs(owner=job_owner(), depth=0)}
        assert (await runtime.wait("transient-inline", owner=job_owner(), depth=0, timeout=0)).claim is None

        await owner._acknowledge_saved_run(run1)
        persisted = await saved_jobs(tmp_path)
        assert persisted["transient-inline"].consumed
        assert persisted["transient-inline"].returned_inline
        assert owner._claims == []

        await _claim_and_register(runtime, owner, "valid-ok", run_id="run-2", call_id="call-2")
        other_claim = await _claim_and_register(
            runtime,
            owner,
            "other-waiter",
            run_id="run-2",
            call_id="call-2",
            claim_override=JobClaim("wrong"),
        )
        run2 = RunRecord(
            run_id="run-2",
            session_id="session-1",
            status=RunStatus.cancelled,
            tools=[ToolExecution(tool_call_id="call-2", tool_name="job", result="ok")],
        )
        with capture_logs() as error_logs:
            await owner._acknowledge_saved_run(run2)
        assert {e["job_id"] for e in error_logs if e["event"] == "Tool result persistence was not confirmed"} == {
            "other-waiter",
        }
        assert (await saved_jobs(tmp_path))["valid-ok"].consumed
        assert not (await saved_jobs(tmp_path))["other-waiter"].consumed
        assert owner._claims == []
        # Another waiter's real claim on "other-waiter" stayed intact.
        assert (await runtime.wait("other-waiter", owner=job_owner(), depth=0, timeout=0)).claim is None
        await runtime.acknowledge_wait("other-waiter", other_claim)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_team_member_save_via_persist_into_acknowledges_member_before_leader(tmp_path: Path) -> None:
    """Persisting a finished team member acknowledges its claims immediately while keeping the leader's pending."""
    runtime = await tool_job_runtime(tmp_path)
    store = create_agent_state_storage("squad", tmp_path)
    session = ConversationSession(session_id="session-1", kind="team", owner_id="squad")
    storage = RunStorage(store=store, session=session, scope=None)
    terminal = TerminalRecord(TeamRunRecord)
    persist = persist_into(terminal, storage, on_saved=acknowledge_saved_run)
    owner = ConsumptionOwner()
    try:
        for job_id in ("member-job", "leader-job"):
            await _start_completed_job(runtime, job_id)
        await _claim_and_register(
            runtime,
            owner,
            "member-job",
            run_id="member-1",
            team=False,
            call_id="member-call",
            returned_inline=True,
        )
        await _claim_and_register(
            runtime,
            owner,
            "leader-job",
            run_id="leader-1",
            team=True,
            call_id="leader-call",
        )
        with consumption_context(owner):
            await persist(
                RunRecord(
                    run_id="member-1",
                    parent_run_id="leader-1",
                    session_id="session-1",
                    agent_id="worker",
                    status=RunStatus.completed,
                    tools=[ToolExecution(tool_call_id="member-call", tool_name="tool", result="done")],
                ),
            )
            mid_persisted = await saved_jobs(tmp_path)
            assert mid_persisted["member-job"].consumed
            assert mid_persisted["member-job"].returned_inline
            assert not mid_persisted["leader-job"].consumed
            assert [claim.job.job_id for claim in owner._claims] == ["leader-job"]
            assert terminal.record is None

            await persist(
                TeamRunRecord(
                    run_id="leader-1",
                    session_id="session-1",
                    team_id="squad",
                    status=RunStatus.completed,
                    tools=[ToolExecution(tool_call_id="leader-call", tool_name="job", result="waited")],
                ),
            )
        final_persisted = await saved_jobs(tmp_path)
        assert final_persisted["leader-job"].consumed
        assert owner._claims == []
        assert terminal.record is not None
        assert terminal.record.run_id == "leader-1"
    finally:
        await runtime.shutdown()
