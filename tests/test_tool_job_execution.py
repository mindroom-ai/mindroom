"""Managed tool calls on the owned loop retain one execution after the parent releases its wait."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from collections.abc import AsyncIterator  # noqa: TC003 - Agno resolves tool return annotations at runtime.
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Never

import pytest
from agno.run import RunContext  # noqa: TC002 - Agno resolves tool parameter annotations at runtime.

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.custom_tools.job import JobTools
from mindroom.llm.errors import ProviderError
from mindroom.llm.runs import RunStatus
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_jobs import execution, results
from mindroom.tool_jobs.consumption import (
    ConsumptionOwner,
    consume_tool_job,
    consumption_context,
    set_consumption_storage,
)
from mindroom.tool_jobs.control import pending_human_messages_context
from mindroom.tool_jobs.conversation import ConversationKey
from mindroom.tool_jobs.execution_authority import JobCall
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import current_execution_resources, defer_execution_cleanup, execution_resources
from mindroom.tool_jobs.results import (
    ToolResultPayload,
    _decode_result_payload,
    encode_result_payload,
    encode_tool_result,
    read_result_payload,
)
from mindroom.tool_jobs.runtime import JobAccessError, job_summary, register_background_runtime
from mindroom.tool_system.agno_toolkits import AgnoImage, Toolkit, ToolResult
from mindroom.tool_system.call_record import recording_tool_calls
from mindroom.tool_system.invocation import ToolCallContext, ToolOutcome
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.tool_system.tool_function import ToolValue
from mindroom.tool_system.tool_hooks import run_async_entrypoint_blocking_call
from tests.delegation_helpers import _delegate_runtime_context, _runtime_paths
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    ProcessRuntime,
    SpanRecorder,
    call_job_tool,
    cancel_settled,
    job_actor,
    job_tool,
    lookup,
    pending_outcomes,
    run_job_turn,
    saved_jobs,
    saved_payload,
    tool_job_runtime,
    toolkit_job_tools,
    wait_for_status,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.llm.runs import RunRecord
    from mindroom.session_storage.store import ConversationStore
    from mindroom.tool_jobs.execution import JobActor
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


def _hold_job_outcomes(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Hold every job's outcome until the returned event is set, so a released call returns its handle first.

    A job that finished before its caller's wait looked would return its result inline instead.
    """
    reply_returned = threading.Event()
    encode = execution.encode_result_payload

    def held(payload: ToolResultPayload) -> dict[str, object]:
        assert reply_returned.wait(JOB_TEST_TIMEOUT)
        return encode(payload)

    monkeypatch.setattr(execution, "encode_result_payload", held)
    return reply_returned


async def _managed_context(tmp_path: Path, config: Config | None = None) -> tuple[ToolRuntimeContext, ProcessRuntime]:
    """A reply's context in a conversation whose calls can become jobs, with that runtime registered."""
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(config or Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    return context, runtime


def _job_controls(context: ToolRuntimeContext) -> JobTools:
    return JobTools(context.runtime_paths, build_execution_identity_from_runtime_context(context))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["state", "stream"])
async def test_large_outcome_encoding_leaves_event_loop_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """State changes and a drained stream are encoded with the tool value off the event loop."""
    text = "payload" * 16_384
    loop_thread = threading.get_ident()
    encoding_threads: list[int] = []

    def encode(payload: ToolResultPayload) -> dict[str, object]:
        encoding_threads.append(threading.get_ident())
        return encode_result_payload(payload)

    async def tool(run_context: RunContext) -> str:
        run_context.session_state["large"] = text
        return "saved"

    async def streamed() -> AsyncIterator[str]:
        yield text
        yield text

    context, runtime = await _managed_context(tmp_path)
    actor = job_actor(job_tool(streamed if kind == "stream" else tool))
    monkeypatch.setattr(execution, "encode_result_payload", encode)
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                outcome = await call_job_tool(
                    actor,
                    "streamed" if kind == "stream" else "tool",
                    call_id="payload-call",
                    run_id="payload-run",
                )
        assert len(encoding_threads) == 1
        assert encoding_threads[0] != loop_thread
        owner = build_execution_identity_from_runtime_context(context)
        listed = (await runtime.list_jobs(owner=owner, depth=0))[0]
        payload = await read_result_payload(runtime, await lookup(runtime, listed.job_id, owner=owner, depth=0))
        if kind == "stream":
            assert payload.value == text * 2
        else:
            assert payload.state_delta["large"]["value"] == text
        assert not outcome.is_error
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True])
async def test_cancellation_during_encoding_drains_resources_and_keeps_returned_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shutdown: bool,
) -> None:
    """An operation that returned before cancellation still owns its completed encoded outcome."""
    started, release, closed = threading.Event(), threading.Event(), asyncio.Event()
    context, runtime = await _managed_context(tmp_path)
    owner = build_execution_identity_from_runtime_context(context)

    async def cleanup() -> None:
        closed.set()

    async def tool(run_context: RunContext) -> str:
        resources = current_execution_resources()
        assert resources is not None
        reference = resources.acquire()
        assert defer_execution_cleanup(cleanup)
        await reference.release()
        run_context.session_state["changed"] = "encoded state"
        return "completed before cancellation"

    def encode(payload: ToolResultPayload) -> dict[str, object]:
        started.set()
        assert release.wait(5)
        return encode_result_payload(payload)

    actor = job_actor(job_tool(tool))
    monkeypatch.setattr(execution, "encode_result_payload", encode)
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                outcome = await call_job_tool(actor, "tool", {"background_after": 0}, run_id="encoding")
                job_id = json.loads(outcome.content)["job_id"]
                assert await asyncio.to_thread(started.wait, JOB_TEST_TIMEOUT)
                if shutdown:
                    stopping = asyncio.create_task(runtime.shutdown())
                    await asyncio.sleep(0)
                else:
                    stopping = asyncio.create_task(cancel_settled(runtime, job_id, owner=owner, depth=0))
                    await wait_for_status(runtime, job_id, "cancel_requested")
                assert not closed.is_set()
                release.set()
                await stopping
                assert closed.is_set()
                saved = (await saved_jobs(tmp_path))[job_id]
                assert (saved.status, saved.result) == ("completed", "completed before cancellation")
                # Shutdown has closed the runtime, so decode the saved payload itself.
                assert _decode_result_payload(await saved_payload(tmp_path, job_id)).state_delta == {
                    "changed": {"before_present": False, "before": None, "present": True, "value": "encoded state"},
                }
    finally:
        release.set()
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(("managed", "background_after"), [(False, None), (True, None), (True, 0)])
async def test_registered_sync_tool_completes_through_the_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed: bool,
    background_after: float | None,
) -> None:
    """A registered synchronous tool completes through the loop's dispatch, inline and as its job."""
    reply_returned = _hold_job_outcomes(monkeypatch) if background_after == 0 else threading.Event()
    (tmp_path / "input.txt").write_text("registered sync result\n")
    paths = _runtime_paths(tmp_path)
    config = Config(
        agents={"leader": AgentConfig(display_name="Leader", tools=["coding"])},
        background_tool_jobs=BackgroundToolJobsConfig(enabled=managed),
    )
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)
    if managed:
        pin_background_tool_jobs(context.config, paths)
        register_background_runtime(paths, runtime)
    toolkit = get_tool_by_name(
        "coding",
        paths,
        worker_target=None,
        disable_sandbox_proxy=True,
        tool_init_overrides={"base_dir": str(tmp_path)},
    )
    arguments: dict[str, object] = {"path": "input.txt"}
    if managed:
        arguments["background_after"] = background_after
    model = ScriptedModel([calls(tool_call("read_file", "sync-call", **arguments)), answer("done")])
    actor = job_actor(*toolkit_job_tools(toolkit, authored_name="coding").values())
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                record = await run_job_turn(actor, model, "read the input", session_id=context.session_id)
                assert record.tools is not None
                tool = record.tools[0]
                if managed:
                    jobs = await runtime.list_jobs(owner=owner, depth=0)
                    assert len(jobs) == 1
                    reply_returned.set()
                    waited = await runtime.wait(jobs[0].job_id, owner=owner, depth=0)
                    assert waited.job.status == "completed", waited.job.result
                    assert "registered sync result" in waited.job.result
                if background_after is None:
                    assert not tool.tool_call_error, tool.result
                    assert "registered sync result" in str(tool.result)
                else:
                    assert json.loads(str(tool.result))["job_id"] == jobs[0].job_id
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
async def test_failed_shell_command_states_exit_code_inline_and_as_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed: bool,
) -> None:
    """A non-zero exit is the command's returned result, so its job completes; its exit code leads the summary."""
    reply_returned = _hold_job_outcomes(monkeypatch) if managed else threading.Event()
    paths = _runtime_paths(tmp_path)
    config = Config(
        agents={"leader": AgentConfig(display_name="Leader", tools=["shell"])},
        background_tool_jobs=BackgroundToolJobsConfig(enabled=managed),
    )
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)
    if managed:
        pin_background_tool_jobs(context.config, paths)
        register_background_runtime(paths, runtime)
    toolkit = get_tool_by_name(
        "shell",
        paths,
        worker_target=None,
        disable_sandbox_proxy=True,
        tool_init_overrides={"base_dir": str(tmp_path)},
    )
    arguments: dict[str, object] = {"args": "echo partial; echo broken >&2; exit 7"}
    if managed:
        arguments["background_after"] = 0
    model = ScriptedModel([calls(tool_call("run_shell_command", "shell-call", **arguments)), answer("done")])
    actor = job_actor(*toolkit_job_tools(toolkit, authored_name="shell").values())
    expected = [f"[cwd: {tmp_path}]", "Exit code 7", "Output:", "partial", "Stderr:", "broken"]
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                record = await run_job_turn(actor, model, "run the command", session_id=context.session_id)
                assert record.tools is not None
                tool = record.tools[0]
                if not managed:
                    assert not tool.tool_call_error, tool.result
                    assert str(tool.result).splitlines() == expected
                    return
                job_id = json.loads(str(tool.result))["job_id"]
                reply_returned.set()
                waited = await asyncio.wait_for(runtime.wait(job_id, owner=owner, depth=0), JOB_TEST_TIMEOUT)
                listed = job_summary(waited.job)
                assert listed["status"] == "completed"
                assert listed["summary"].splitlines()[:2] == expected[:2]
                payload = await read_result_payload(runtime, waited.job)
                assert payload.error is None
                assert str(payload.value).splitlines() == expected
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_human_followup_releases_original_call_once(tmp_path: Path) -> None:
    """A follow-up releases the parent while its original tool keeps running."""
    started, release = asyncio.Event(), asyncio.Event()
    invocations = 0

    async def slow_tool() -> str:
        nonlocal invocations
        invocations += 1
        started.set()
        await release.wait()
        return "actual result"

    context, runtime = await _managed_context(tmp_path)
    owner = build_execution_identity_from_runtime_context(context)
    signal = asyncio.Event()
    model = ScriptedModel([calls(tool_call("slow_tool", "exact-call")), answer("done")])
    actor = job_actor(job_tool(slow_tool))
    try:
        async with execution_resources():
            with tool_runtime_context(context), pending_human_messages_context(signal):
                parent = asyncio.create_task(run_job_turn(actor, model, session_id=context.session_id))
                await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
                signal.set()
                done, _ = await asyncio.wait({parent}, timeout=JOB_TEST_TIMEOUT)
                try:
                    assert parent in done, "managed ordinary tool must release its parent on human follow-up"
                    record = parent.result()
                    assert record.tools is not None
                    handle = record.tools[0].result
                    job_id = json.loads(str(handle))["job_id"]
                    signal.clear()
                    release.set()
                    completed = await asyncio.wait_for(runtime.wait(job_id, owner=owner, depth=0), JOB_TEST_TIMEOUT)
                    assert completed.job.status == "completed"
                    assert completed.job.result == "actual result"
                    await runtime.release_wait(job_id, completed.claim)
                    jobs = await runtime.list_jobs(owner=owner, depth=0)
                    assert [job.job_id for job in jobs] == [job_id]
                    assert invocations == 1
                    assert record.tools[0].result == handle
                finally:
                    release.set()
                    await parent
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_streamed_result_saves_its_text_and_media_once(tmp_path: Path) -> None:
    """The saved job keeps full text and media only in its payload value; metadata keeps a bounded summary."""
    text = "".join(f"chunk {index:04d} " for index in range(200))

    async def streamed() -> AsyncIterator[ToolResult | str]:
        yield text[:1000]
        yield ToolResult(content=text[1000:1500], images=[AgnoImage(content=b"image")])
        yield text[1500:]

    context, runtime = await _managed_context(tmp_path)
    owner = build_execution_identity_from_runtime_context(context)
    actor = job_actor(job_tool(streamed))
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                outcome = await call_job_tool(actor, "streamed", call_id="once", run_id="once-run")
        assert outcome.content == text
        assert outcome.images[0].content == b"image"
        job = (await runtime.list_jobs(owner=owner, depth=0))[0]
        assert job.result == text[:500]
        assert job.summary_truncated
        payload = await saved_payload(tmp_path, job.job_id)
        assert payload is not None
        saved = json.dumps(asdict((await saved_jobs(tmp_path))[job.job_id])) + json.dumps(payload)
        for marker in ("chunk 0060 ", "chunk 0100 ", "chunk 0180 "):
            assert saved.count(marker) == 1
        assert saved.count(base64.b64encode(b"image").decode()) == 1
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_oversized_result_becomes_a_failed_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A result beyond the encoded limit fails its job with the size-limit error instead of being saved."""

    async def large() -> str:
        return "x" * 200

    context, runtime = await _managed_context(tmp_path)
    owner = build_execution_identity_from_runtime_context(context)
    actor = job_actor(job_tool(large))
    monkeypatch.setattr(results, "_MAX_ENCODED_RESULT_BYTES", 150)
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                outcome = await call_job_tool(actor, "large", call_id="large", run_id="large-run")
        assert outcome.is_error
        assert "encoded JSON limit" in outcome.content
        job = (await runtime.list_jobs(owner=owner, depth=0))[0]
        assert job.status == "failed"
        assert not job.has_result_payload
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_resource_owner_releases_only_after_last_child() -> None:
    """Parent cleanup remains deferred until both accepted operations settle."""
    closed = []

    async def cleanup() -> None:
        closed.append("closed")

    async with execution_resources() as resources:
        first = resources.acquire()
        second = resources.acquire()
        assert defer_execution_cleanup(cleanup)
    assert closed == []
    await first.release()
    assert closed == []
    await second.release()
    assert closed == ["closed"]


@pytest.mark.asyncio
async def test_run_connected_toolkit_call_stays_inline_through_human_followup(tmp_path: Path) -> None:
    """A human follow-up cannot detach a call whose SDK connection lasts only for its run."""
    started, release = asyncio.Event(), asyncio.Event()

    class ConnectionTools(Toolkit):
        _requires_connect = True

        def __init__(self) -> None:
            super().__init__(name="connection", tools=[self.read])

        async def read(self) -> str:
            started.set()
            await release.wait()
            return "connected result"

    context, runtime = await _managed_context(tmp_path)
    owner = build_execution_identity_from_runtime_context(context)
    signal = asyncio.Event()
    model = ScriptedModel([calls(tool_call("read", "read-call")), answer("done")])
    actor = job_actor(*toolkit_job_tools(ConnectionTools(), authored_name="connection").values())

    @owned_tool_execution()
    async def parent_run() -> object:
        return await run_job_turn(actor, model, session_id=context.session_id)

    try:
        with tool_runtime_context(context), pending_human_messages_context(signal):
            parent = asyncio.create_task(parent_run())
            await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
            signal.set()
            release.set()
            record = await asyncio.wait_for(parent, JOB_TEST_TIMEOUT)
        assert record.tools[0].result == "connected result"
        assert "background_after" not in model.requests[0].tools[0].parameters["properties"]
        assert await runtime.list_jobs(owner=owner, depth=0) == []
    finally:
        release.set()
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_generator_result_finishes_inside_owned_operation(tmp_path: Path, fails: bool) -> None:
    """Generator output and failures settle before their execution resources close."""
    started, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def generated() -> AsyncIterator[ToolResult | str]:
        try:
            yield "event text"
            started.set()
            await release.wait()
            if fails:
                msg = "iteration failed"
                raise ValueError(msg)
            yield ToolResult(content="picture", images=[AgnoImage(content=b"image")])
        finally:
            closed.set()

    context, runtime = await _managed_context(tmp_path)
    owner = build_execution_identity_from_runtime_context(context)
    signal = asyncio.Event()
    model = ScriptedModel([calls(tool_call("generated", "generator-call")), answer("done")])
    actor = job_actor(job_tool(generated))

    @owned_tool_execution()
    async def parent_run() -> object:
        return await run_job_turn(actor, model, session_id=context.session_id)

    try:
        with tool_runtime_context(context), pending_human_messages_context(signal):
            parent = asyncio.create_task(parent_run())
            await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
            signal.set()
            record = await asyncio.wait_for(parent, JOB_TEST_TIMEOUT)
            assert not closed.is_set()
            job_id = json.loads(str(record.tools[0].result))["job_id"]
            release.set()
            signal.clear()
            result = await runtime.wait(job_id, owner=owner, depth=0)
            assert closed.is_set()
            assert result.job.status == ("failed" if fails else "completed")
            payload = await read_result_payload(runtime, result.job)
            if fails:
                assert payload.error == "iteration failed"
                assert result.job.result == "iteration failed"
            else:
                assert isinstance(payload.value, ToolValue)
                assert payload.value.content == "event textpicture"
                assert payload.value.images[0].content == b"image"
    finally:
        release.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_exact_call_reattachment_rejects_changed_arguments(tmp_path: Path) -> None:
    """One call identity cannot launch a second invocation with changed arguments."""
    invocations = []

    async def record(value: str) -> str:
        invocations.append(value)
        return value

    context, runtime = await _managed_context(tmp_path)
    actor = job_actor(job_tool(record))
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                first = await call_job_tool(actor, "record", {"value": "first"}, call_id="same-call", run_id="same-run")
                again = await call_job_tool(actor, "record", {"value": "first"}, call_id="same-call", run_id="same-run")
                assert first.content == again.content == "first"
                # The refused call fails as a call; the reply's run goes on.
                changed = await call_job_tool(
                    actor,
                    "record",
                    {"value": "changed"},
                    call_id="same-call",
                    run_id="same-run",
                )
                assert changed.is_error
                assert "not available" in changed.content
        assert invocations == ["first"]
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("save_fails", [False, True])
async def test_fast_result_acknowledges_exact_saved_run(  # noqa: PLR0915 - Receipt, save, and restart boundaries.
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    save_fails: bool,
) -> None:
    """A fast foreground result suppresses delivery only after database readback."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader")})
    context = replace(_delegate_runtime_context(config, paths), membership_turn_id="$original-request")
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)

    def storage_factory() -> ConversationStore:
        return create_session_storage("leader", config, paths, owner)

    async def fast_tool() -> str:
        return "actual result"

    model = ScriptedModel([calls(tool_call("fast_tool", "exact-call", background_after=None)), answer("done")])
    storage = storage_factory()
    actor = job_actor(job_tool(fast_tool))
    if save_fails:

        def fail_save(*_args: object, **_kwargs: object) -> Never:
            msg = "storage unavailable"
            raise RuntimeError(msg)

        monkeypatch.setattr(type(storage), "_save_encoded", fail_save)

    @owned_tool_execution()
    async def parent_run() -> object:
        set_consumption_storage(storage_factory)
        try:
            return await run_job_turn(actor, model, session_id=context.session_id, store=storage)
        finally:
            await runtime.quiesce()

    try:
        with tool_runtime_context(context):
            if save_fails:
                with pytest.raises(RuntimeError, match="storage unavailable"):
                    await parent_run()
            else:
                record = await parent_run()
                assert record.tools[0].result == "actual result"
        jobs = runtime.retained_jobs()
        assert len(jobs) == 1
        assert jobs[0].adapter["arguments"] == encode_tool_result({"background_after": None})
        assert jobs[0].source_event_id == "$original-request"
        assert jobs[0].owner == owner
        assert jobs[0].consumed is not save_fails
        assert len(pending_outcomes(runtime)) == int(save_fails)
        await runtime.shutdown()
        restored = await tool_job_runtime(tmp_path)
        try:
            await restored.recover()
            saved = await lookup(restored, jobs[0].job_id, owner=owner, depth=0)
            assert saved.source_event_id == "$original-request"
            assert saved.owner == owner
            assert saved.consumed is not save_fails
            assert len(pending_outcomes(restored)) == int(save_fails)
            assert saved.adapter["arguments"] == encode_tool_result({"background_after": None})
        finally:
            await restored.shutdown()
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_cancel_returns_while_a_sync_job_waits_for_its_thread(tmp_path: Path) -> None:
    """A cancel returns at once, and cannot settle or release resources before the actual sync worker exits."""
    started, release = threading.Event(), threading.Event()
    finished = threading.Event()

    def slow_tool() -> str:
        started.set()
        release.wait(5)
        finished.set()
        return "finished"

    context, runtime = await _managed_context(tmp_path)
    owner = build_execution_identity_from_runtime_context(context)
    signal = asyncio.Event()
    model = ScriptedModel([calls(tool_call("slow_tool", "sync-call")), answer("done")])
    actor = job_actor(job_tool(slow_tool, authored_name="slow"))

    @owned_tool_execution()
    async def parent_run() -> object:
        return await run_job_turn(actor, model, session_id=context.session_id)

    try:
        with tool_runtime_context(context), pending_human_messages_context(signal):
            parent = asyncio.create_task(parent_run())
            assert await asyncio.to_thread(started.wait, JOB_TEST_TIMEOUT)
            signal.set()
            record = await asyncio.wait_for(parent, JOB_TEST_TIMEOUT)
            job_id = json.loads(str(record.tools[0].result))["job_id"]
            # The model's cancel returns at once; the job settles once the actual thread exits.
            requested = await asyncio.wait_for(runtime.cancel(job_id, owner=owner, depth=0), JOB_TEST_TIMEOUT)
            assert requested.status == "cancel_requested"
            assert not finished.is_set()
            drain = runtime._entries[job_id].drain
            assert drain is not None
            done, _ = await asyncio.wait({drain}, timeout=0.05)
            assert not done, "cancellation settled while synchronous side effects were still running"
            release.set()
            await wait_for_status(runtime, job_id, "cancelled")
            assert finished.is_set()
    finally:
        release.set()
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
async def test_a_jobs_blocking_call_runs_on_its_own_thread_not_the_shared_pool(tmp_path: Path, managed: bool) -> None:
    """A job's call may block for hours, so what its entrypoint offloads, like a worker request, gets its own thread."""
    shared = ThreadPoolExecutor(thread_name_prefix="shared-pool")
    threads: list[str] = []

    def blocking() -> str:
        threads.append(threading.current_thread().name)
        return "done"

    async def offloading() -> str:
        return await run_async_entrypoint_blocking_call(blocking, executor=shared)

    paths = _runtime_paths(tmp_path)
    config = Config(
        agents={"leader": AgentConfig(display_name="Leader")},
        background_tool_jobs=BackgroundToolJobsConfig(enabled=managed),
    )
    context = _delegate_runtime_context(config, paths)
    runtime = await tool_job_runtime(tmp_path)
    if managed:
        pin_background_tool_jobs(context.config, paths)
        register_background_runtime(paths, runtime)
    model = ScriptedModel([calls(tool_call("offloading", "offloading-call")), answer("done")])
    actor = job_actor(job_tool(offloading, authored_name="threads"))

    @owned_tool_execution()
    async def parent_run() -> object:
        return await run_job_turn(actor, model, session_id=context.session_id)

    try:
        with tool_runtime_context(context):
            await asyncio.wait_for(parent_run(), JOB_TEST_TIMEOUT)
        [thread] = threads
        assert thread.startswith("mindroom-job-call" if managed else "shared-pool")
    finally:
        shared.shutdown(wait=False)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "failed"])
async def test_a_cancel_of_an_ended_job_does_not_replay_its_outcome(tmp_path: Path, outcome: str) -> None:
    """A cancel of an ended job neither replays its outcome's errors or state changes nor acknowledges it."""
    config = Config(agents={"leader": AgentConfig(display_name="Leader")})
    context, runtime = await _managed_context(tmp_path, config)
    owner = build_execution_identity_from_runtime_context(context)
    paths = context.runtime_paths

    async def original(run_context: RunContext) -> str:
        run_context.session_state["counter"] = 1
        if outcome == "failed":
            message = "original execution failed"
            raise RuntimeError(message)
        return "original output"

    def storage_factory() -> ConversationStore:
        return create_session_storage("leader", config, paths, owner)

    storage = storage_factory()
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                await call_job_tool(
                    job_actor(job_tool(original)),
                    "original",
                    call_id="original-call",
                    run_id="original-run",
                    session_state={"counter": 0},
                )
        job = (await runtime.list_jobs(owner=owner, depth=0))[0]
        model = ScriptedModel(
            [calls(tool_call("job", "cancel-call", action="cancel", job_id=job.job_id)), answer("Cancelled.")],
        )
        controls = job_actor(*toolkit_job_tools(_job_controls(context), authored_name="job").values())

        @owned_tool_execution()
        async def cancel() -> object:
            set_consumption_storage(storage_factory)
            with tool_runtime_context(context):
                return await run_job_turn(
                    controls,
                    model,
                    "cancel",
                    session_id=context.session_id,
                    session_state={"counter": 0},
                    store=storage,
                )

        record = await cancel()
        assert not record.tools[0].tool_call_error
        assert json.loads(str(record.tools[0].result))["job_id"] == job.job_id
        session = storage.load_session(context.session_id, "agent")
        assert session is not None
        assert session.session_state.get("counter", 0) == 0
        # The job had already ended, so its outcome stays for the next notice or collect to report.
        assert [pending.job_id for pending in pending_outcomes(runtime)] == [job.job_id]
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("later_counter", [0, 1, 9])
async def test_saved_result_reread_preserves_later_session_state(
    tmp_path: Path,
    restart: bool,
    later_counter: int,
) -> None:
    """Old output is readable across turns and restart without repeating mutations or conflict warnings."""
    config = Config(agents={"leader": AgentConfig(display_name="Leader")})
    context, runtime = await _managed_context(tmp_path, config)
    owner = build_execution_identity_from_runtime_context(context)
    paths = context.runtime_paths

    def storage_factory() -> ConversationStore:
        return create_session_storage("leader", config, paths, owner)

    async def change_state(run_context: RunContext) -> str:
        run_context.session_state["counter"] = 1
        return "original output"

    storage = storage_factory()

    @owned_tool_execution()
    async def run(actor: JobActor, model: ScriptedModel, state: dict[str, object], run_id: str) -> RunRecord:
        set_consumption_storage(storage_factory)
        with tool_runtime_context(context):
            return await run_job_turn(
                actor,
                model,
                "continue",
                run_id=run_id,
                session_id=context.session_id,
                session_state=state,
                store=storage,
            )

    try:
        first_state: dict[str, object] = {"counter": 0}
        model = ScriptedModel([calls(tool_call("change_state", "change")), answer("done")])
        await run(job_actor(job_tool(change_state)), model, first_state, "first")
        assert first_state["counter"] == 1
        (job,) = runtime.retained_jobs()
        assert job.consumed
        if restart:
            await runtime.shutdown()
            runtime = await tool_job_runtime(tmp_path)
            await runtime.recover()
            register_background_runtime(paths, runtime)
        later_state: dict[str, object] = {"counter": later_counter}
        model = ScriptedModel([calls(tool_call("job", "reread", action="wait", job_id=job.job_id)), answer("again")])
        controls = job_actor(*toolkit_job_tools(_job_controls(context), authored_name="job").values())
        later = await run(controls, model, later_state, "later")
        assert later_state["counter"] == later_counter
        assert later.tools[0].result == "original output"
        assert pending_outcomes(runtime) == []
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_job_list_skips_a_call_that_returned_inline_earlier_in_the_same_turn(tmp_path: Path) -> None:
    """A call that returned its result inline is no job, even before the reply that got it is saved."""
    config = Config(agents={"leader": AgentConfig(display_name="Leader")})
    context, runtime = await _managed_context(tmp_path, config)
    owner = build_execution_identity_from_runtime_context(context)

    def storage_factory() -> ConversationStore:
        return create_session_storage("leader", config, context.runtime_paths, owner)

    async def report(topic: str) -> str:
        return f"report on {topic}"

    model = ScriptedModel(
        [
            calls(tool_call("report", "report-call", topic="alpha", background_after=None)),
            calls(tool_call("job", "list-call", action="list")),
            answer("done"),
        ],
    )
    controls = toolkit_job_tools(_job_controls(context), authored_name="job").values()
    actor = job_actor(job_tool(report), *controls)
    storage = storage_factory()

    @owned_tool_execution()
    async def run() -> object:
        set_consumption_storage(storage_factory)
        with tool_runtime_context(context):
            return await run_job_turn(actor, model, "Report on alpha", session_id=context.session_id, store=storage)

    try:
        record = await run()
        assert [tool.result for tool in record.tools if tool.tool_call_id == "list-call"] == ["[]"]
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_a_key_a_sibling_writes_as_the_job_starts_survives_its_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key a sibling adds right after the job copies the live state is neither deleted nor a conflict."""

    async def change_state(run_context: RunContext) -> str:
        run_context.session_state["changed"] = 1
        return "result"

    detached = execution._detached

    def detached_then_a_sibling_writes(call: JobCall) -> JobCall:
        frozen = detached(call)
        # An inline sync sibling of the same batch writes between the job's copy and anything read after it.
        call.context.session_state["sibling"] = "kept"
        return frozen

    monkeypatch.setattr(execution, "_detached", detached_then_a_sibling_writes)
    context, runtime = await _managed_context(tmp_path)
    state: dict[str, object] = {"changed": 0}

    @owned_tool_execution()
    async def reply() -> ToolOutcome:
        async with execution_resources():
            with tool_runtime_context(context):
                return await call_job_tool(
                    job_actor(job_tool(change_state)),
                    "change_state",
                    call_id="state-call",
                    run_id="origin",
                    session_state=state,
                )

    try:
        outcome = await reply()
        assert outcome.content == "result"
        assert state == {"changed": 1, "sibling": "kept"}
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_later_consumption_merges_only_changed_state_and_reports_conflicts(tmp_path: Path) -> None:
    """Background mutation stays isolated, and later consumption preserves newer keys."""
    started, release = asyncio.Event(), asyncio.Event()

    async def change_state(run_context: RunContext) -> str:
        started.set()
        await release.wait()
        run_context.session_state.update({"changed": 1, "conflict": "tool"})
        return "result"

    context, runtime = await _managed_context(tmp_path)
    owner = build_execution_identity_from_runtime_context(context)
    signal = asyncio.Event()
    tool = job_tool(change_state)
    state: dict[str, object] = {"changed": 0, "conflict": "old", "unrelated": "old"}
    try:
        async with execution_resources():
            with tool_runtime_context(context), pending_human_messages_context(signal):
                parent = asyncio.create_task(
                    call_job_tool(
                        job_actor(tool),
                        "change_state",
                        call_id="state-call",
                        run_id="origin",
                        session_state=state,
                    ),
                )
                await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
                signal.set()
                returned = await parent
                job_id = json.loads(returned.content)["job_id"]
                release.set()
                signal.clear()
                waited = await runtime.wait(job_id, owner=owner, depth=0)
                assert state == {"changed": 0, "conflict": "old", "unrelated": "old"}
                state.update({"conflict": "newer", "unrelated": "newer"})
                claims = ConsumptionOwner()
                call = JobCall(
                    tool=tool,
                    arguments={},
                    context=ToolCallContext(
                        run_id="origin",
                        session_id="session",
                        call_id="state-call",
                        session_state=state,
                    ),
                    team=False,
                )
                with consumption_context(claims):
                    value, _ = await consume_tool_job(runtime, waited.job, waited.claim, job_call=call)
                    await claims.finalize()
                assert state["changed"] == 1
                assert state["conflict"] == state["unrelated"] == "newer"
                assert value == ToolValue(content="result\nSession state conflicts: conflict")
    finally:
        release.set()
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("rich", [False, True])
async def test_streamed_state_conflict_notice_reaches_the_tool_message(tmp_path: Path, *, rich: bool) -> None:
    """A drained stream's result retains the consumption warning and newer parent state, and its media."""
    started, release = asyncio.Event(), asyncio.Event()

    async def generated(run_context: RunContext) -> AsyncIterator[str | ToolResult]:
        started.set()
        await release.wait()
        run_context.session_state.update({"changed": 1, "conflict": "tool"})
        yield "progress "
        yield ToolResult(content="Result.", images=[AgnoImage(content=b"image")]) if rich else "Result."

    context, runtime = await _managed_context(tmp_path)
    state: dict[str, object] = {"changed": 0, "conflict": "old"}
    model = ScriptedModel([calls(tool_call("generated", "stream-state")), answer("done")])

    @owned_tool_execution()
    async def reply() -> object:
        with tool_runtime_context(context):
            return await run_job_turn(job_actor(job_tool(generated)), model, session_state=state)

    pending = asyncio.create_task(reply())
    try:
        await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
        state["conflict"] = "newer"
        release.set()
        record = await pending
        assert state["changed"] == 1
        assert state["conflict"] == "newer"
        result = str(record.tools[0].result)
        assert result.startswith("progress Result.")
        assert result.count("Session state conflicts: conflict") == 1
        follow_up = model.requests[1].messages[-1]
        assert (follow_up.role == "user" and [image.content for image in follow_up.images or ()] == [b"image"]) is rich
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["driver_type", "oversized", "missing_file", "cyclic"])
async def test_a_call_that_ran_never_reads_as_one_that_did_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: str,
) -> None:
    """A result the job cannot keep as returned still tells the model the call ran, so it is not repeated.

    A value with no wire tag keeps its text, as inline; a result over the size limit, whose media file is gone, or
    that refers to itself says the call took effect.
    """
    ran: list[str] = []

    async def create_event() -> dict[str, object] | ToolResult:
        ran.append("created")
        if result == "missing_file":
            return ToolResult(content="event card", images=[AgnoImage(filepath=tmp_path / "gone.png")])
        if result == "cyclic":
            cycle: dict[str, object] = {}
            cycle["self"] = cycle
            return cycle
        return {"at": datetime(2026, 10, 10, tzinfo=UTC)}

    if result == "oversized":
        monkeypatch.setattr(results, "_MAX_ENCODED_RESULT_BYTES", 64)
    context, runtime = await _managed_context(tmp_path)
    model = ScriptedModel([calls(tool_call("create_event", "event-call")), answer("done")])
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                record = await run_job_turn(job_actor(job_tool(create_event)), model, "Create")
        [tool] = record.tools
        assert ran == ["created"]
        if result == "driver_type":
            assert not tool.tool_call_error
            assert "2026-10-10 00:00:00+00:00" in str(tool.result)
        else:
            assert tool.tool_call_error
            assert "The call ran" in str(tool.result)
            assert "do not call it again" in str(tool.result)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["waited", "released", "stopped"])
async def test_reply_records_a_managed_call_before_its_job_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """A restart's account names a job's call as it names an inline one, and a Stop committed first refuses it."""
    reply_returned = _hold_job_outcomes(monkeypatch) if case == "released" else threading.Event()
    ran: list[str] = []

    async def write_note(text: str) -> str:
        ran.append(text)
        return f"noted {text}"

    context, runtime = await _managed_context(tmp_path)
    arguments: dict[str, object] = {"text": "hello"}
    if case == "released":
        arguments["background_after"] = 0
    model = ScriptedModel([calls(tool_call("write_note", "note-call", **arguments)), answer("done")])
    actor = job_actor(job_tool(write_note))
    recorder = SpanRecorder(stopped=case == "stopped")
    owner = build_execution_identity_from_runtime_context(context)
    try:
        async with execution_resources():
            with tool_runtime_context(context), recording_tool_calls(recorder):
                if case == "stopped":
                    with pytest.raises(asyncio.CancelledError):
                        await run_job_turn(actor, model, "Note")
                    assert (ran, recorder.records, await runtime.list_jobs(owner=owner, depth=0)) == ([], [], [])
                    return
                record = await run_job_turn(actor, model, "Note")
        result = record.tools[0].result
        # The record keeps the call's application arguments, never the reserved wait budget.
        assert recorder.records[0] == ("started", "write_note", {"text": "hello"}, None)
        assert recorder.records[1] == ("finished", "write_note", {"text": "hello"}, result)
        if case == "released":
            handle = json.loads(str(result))
            assert handle["tool"] == "write_note"
            reply_returned.set()
            await wait_for_status(runtime, handle["job_id"], "completed")
        else:
            assert result == "noted hello"
        assert ran == ["hello"]
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_a_call_whose_access_resolves_during_its_wait_returns_its_handle(tmp_path: Path) -> None:
    """A call that ran while its requester's access re-resolved returns its job handle, so the model never repeats it.

    The job stays held, so the reply waits for it and a wake delivers its result once the access resolves.
    """
    grant = {"access": "allowed"}
    ran: list[str] = []

    async def pay() -> str:
        ran.append("paid")
        # A membership refresh starts while the call runs.
        grant["access"] = "pending"
        return "paid"

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = await tool_job_runtime(
        tmp_path,
        authorize=lambda _job: grant["access"] == "allowed",
        denied=lambda _job: False,
    )
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    model = ScriptedModel([calls(tool_call("pay", "pay-call")), answer("done")])
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                record = await run_job_turn(job_actor(job_tool(pay)), model, "Pay")
        [tool] = record.tools
        assert ran == ["paid"]
        assert not tool.tool_call_error
        handle = json.loads(str(tool.result))
        assert handle["tool"] == "pay"
        await wait_for_status(runtime, handle["job_id"], "completed")
        # Its outcome stays unconsumed for the wake.
        assert not runtime._entries[handle["job_id"]].job.consumed
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["start", "authority"])
async def test_a_refused_job_fails_its_call_not_the_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    refusal: str,
) -> None:
    """A call the job runtime refuses fails as that call; the run goes on.

    The runtime refuses the job while room membership re-resolves, or the call's authority before admission, as after
    a reload removed its toolkit while the model was answering.
    """
    ran: list[str] = []

    async def write_note() -> str:
        ran.append("note")
        return "noted"

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = await tool_job_runtime(tmp_path, authorize=lambda _job: refusal != "start")
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    if refusal == "authority":

        def refuse(*_args: object) -> None:
            msg = "Tool job is not available in this conversation."
            raise JobAccessError(msg)

        monkeypatch.setattr(runtime, "authorize_execution", refuse)
    model = ScriptedModel([calls(tool_call("write_note", "note-call")), answer("done")])
    recorder = SpanRecorder(stopped=False)

    async def cleanup() -> None:
        return None

    try:
        async with execution_resources():
            with tool_runtime_context(context), recording_tool_calls(recorder):
                record = await run_job_turn(job_actor(job_tool(write_note)), model, "Note")
            # The refused call gave its resource reference back, so the reply's own cleanup is not deferred.
            assert defer_execution_cleanup(cleanup) is False
        assert record.status is RunStatus.completed
        assert record.content == "done"
        assert record.tools[0].tool_call_error
        assert ran == []
        # A job the runtime refused finishes the record its call opened; a call refused before admission opens none.
        assert [entry[0] for entry in recorder.records] == (["started", "finished"] if refusal == "start" else [])
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_a_call_cancelled_while_its_reply_waits_tells_the_model_not_to_run_it_again(tmp_path: Path) -> None:
    """A person's cancel of a call the reply still waits on reaches the model as a reason, never an empty failure."""
    started = asyncio.Event()

    async def slow_tool() -> str:
        started.set()
        await asyncio.Event().wait()
        return "finished"

    context, runtime = await _managed_context(tmp_path)
    owner = build_execution_identity_from_runtime_context(context)
    model = ScriptedModel([calls(tool_call("slow_tool", "call")), answer("done")])

    def storage_factory() -> ConversationStore:
        return create_session_storage("leader", context.config, context.runtime_paths, owner)

    storage = storage_factory()

    @owned_tool_execution()
    async def reply() -> object:
        set_consumption_storage(storage_factory)
        return await run_job_turn(
            job_actor(job_tool(slow_tool, authored_name="slow")),
            model,
            session_id=context.session_id,
            store=storage,
        )

    try:
        with tool_runtime_context(context), pending_human_messages_context(asyncio.Event()):
            running = asyncio.create_task(reply())
            await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
            (job,) = runtime.retained_jobs()
            await runtime.request_cancel(job.job_id)
            await asyncio.wait_for(running, JOB_TEST_TIMEOUT)
        [message] = [message for message in model.requests[1].messages if message.role == "tool"]
        assert message.tool_call_error
        assert "cancelled" in str(message.content)
        assert "do not call it again" in str(message.content)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_a_finished_call_counts_as_read_when_its_reply_fails_on_the_next_step(tmp_path: Path) -> None:
    """A call that finished inside its reply is read once the failed run is saved."""

    async def report(topic: str) -> str:
        return f"report on {topic}"

    async def overloaded() -> None:
        msg = "provider overloaded"
        raise ProviderError(msg, provider="Synthetic")

    context, runtime = await _managed_context(tmp_path)
    owner = build_execution_identity_from_runtime_context(context)
    model = ScriptedModel([calls(tool_call("report", "call", topic="alpha")), [Await(overloaded)]])

    def storage_factory() -> ConversationStore:
        return create_session_storage("leader", context.config, context.runtime_paths, owner)

    storage = storage_factory()

    @owned_tool_execution()
    async def reply() -> None:
        set_consumption_storage(storage_factory)
        await run_job_turn(job_actor(job_tool(report)), model, "Go", session_id=context.session_id, store=storage)

    try:
        with tool_runtime_context(context), suppress(ProviderError):
            await reply()
        (job,) = runtime.retained_jobs()
        assert job.status == "completed"
        assert job.consumed
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_a_call_whose_wait_is_refused_leaves_its_job_to_be_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call whose wait the runtime refuses fails, and gives its job back, so the job's outcome still gets reported."""
    gate = asyncio.Event()

    async def slow_tool() -> str:
        await gate.wait()
        return "finished"

    context, runtime = await _managed_context(tmp_path)

    async def refuse_wait(*_args: object, **_kwargs: object) -> Never:
        msg = "Tool job is not available in this conversation."
        raise JobAccessError(msg)

    monkeypatch.setattr(runtime, "wait", refuse_wait)
    model = ScriptedModel([calls(tool_call("slow_tool", "call")), answer("done")])
    key = ConversationKey(
        recipient=context.recipient,
        room_id=context.room_id,
        thread_id=context.resolved_thread_id,
        requester_id=context.requester_id,
        silent=False,
    )
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                record = await run_job_turn(job_actor(job_tool(slow_tool)), model, "Go")
        assert record.tools[0].tool_call_error
        (job,) = runtime.retained_jobs()
        gate.set()
        await wait_for_status(runtime, job.job_id, "completed")
        assert [ended.job_id for ended in await runtime.ended_jobs(key)] == [job.job_id]
    finally:
        gate.set()
        await runtime.shutdown()
