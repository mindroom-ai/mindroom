"""Tests for full LLM request logging."""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from agno.models.anthropic import Claude
from agno.models.message import Message, MessageMetrics
from agno.models.response import ModelResponse
from structlog.testing import capture_logs

from mindroom import llm_request_logging
from mindroom.claude_prompt_cache import install_claude_deferred_tool_search
from mindroom.config.main import Config
from mindroom.config.models import DebugConfig
from mindroom.llm_request_logging import (
    _RequestLogRef,
    _write_llm_request_log,
    _write_llm_response_log,
    bind_llm_request_log_context,
    current_llm_request_log_context,
    install_llm_request_logging,
    record_llm_request_tools,
    stream_with_llm_request_log_context,
)
from mindroom.openai_tool_search import request_params_with_deferred_tool_search

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path
    from typing import TextIO


@dataclass
class _FakeModel:
    id: str = "test-model"
    provider: str | None = "OpenAI"
    system_prompt: str | None = None
    temperature: float | None = 0.7
    client: object | None = None
    async_client: object | None = None

    response_usage: MessageMetrics | None = None

    async def ainvoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        return ModelResponse(content="ok", response_usage=self.response_usage)

    async def ainvoke_stream(self, *_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        yield ModelResponse(content="ok")
        yield ModelResponse(content="!", response_usage=self.response_usage)


@dataclass
class _CancellableInvokeModel(_FakeModel):
    invocation_started: asyncio.Event = field(default_factory=asyncio.Event)
    provider_cancellation: asyncio.CancelledError | None = None

    async def ainvoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        self.invocation_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            self.provider_cancellation = exc
            raise


@dataclass
class _CancellableStreamModel(_FakeModel):
    invocation_started: asyncio.Event = field(default_factory=asyncio.Event)
    provider_cancellation: asyncio.CancelledError | None = None

    async def ainvoke_stream(self, *_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        self.invocation_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            self.provider_cancellation = exc
            raise
        yield ModelResponse(content="unreachable")


async def _assert_cancellation_is_deferred(task: asyncio.Task[Any]) -> None:
    """Observe one requested cancellation without releasing blocked work."""
    assert task.cancelling() > 0
    ready_callbacks_processed = asyncio.Event()
    asyncio.get_running_loop().call_soon(ready_callbacks_processed.set)
    await ready_callbacks_processed.wait()
    assert not task.done()


@dataclass
class _AppendBoundaryProbe:
    first_writer_started: threading.Event = field(default_factory=threading.Event)
    release_first_writer: threading.Event = field(default_factory=threading.Event)
    guard: threading.Lock = field(default_factory=threading.Lock)
    write_calls: int = 0
    active_writers: int = 0
    overlap_detected: bool = False


class _ReleaseFirstWriterOnContention:
    def __init__(self, probe: _AppendBoundaryProbe) -> None:
        self._probe = probe
        self._lock = threading.Lock()

    def __enter__(self) -> _ReleaseFirstWriterOnContention:
        if not self._lock.acquire(blocking=False):
            self._probe.release_first_writer.set()
            assert self._lock.acquire(timeout=5)
        return self

    def __exit__(self, *_args: object) -> None:
        self._lock.release()


class _ObservedAppendHandle:
    def __init__(
        self,
        handle: TextIO,
        probe: _AppendBoundaryProbe,
    ) -> None:
        self._handle = handle
        self._probe = probe

    def __enter__(self) -> _ObservedAppendHandle:
        self._handle.__enter__()
        return self

    def __exit__(self, *args: object) -> None:
        self._handle.__exit__(*args)  # type: ignore[arg-type]

    def write(self, value: str) -> int:
        with self._probe.guard:
            is_first_writer = self._probe.write_calls == 0
            self._probe.write_calls += 1
            self._probe.active_writers += 1
            if self._probe.active_writers > 1:
                self._probe.overlap_detected = True
                self._probe.release_first_writer.set()
        if is_first_writer:
            self._probe.first_writer_started.set()
            assert self._probe.release_first_writer.wait(timeout=5)
        try:
            written = self._handle.write(value)
            self._handle.flush()
            return written
        finally:
            with self._probe.guard:
                self._probe.active_writers -= 1


def _mutate_borrowed_request_values(
    model: _FakeModel,
    messages: list[Message],
    tools: list[dict[str, str]],
) -> None:
    model.id = "mutated-model"
    model.temperature = 1.0
    messages[0].content = "mutated message"
    tools[0]["name"] = "mutated_tool"


@pytest.mark.asyncio
async def test_llm_request_payload_is_built_on_writer_thread(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Message, tool, and model serialization should stay off the event loop."""
    event_loop_thread = threading.get_ident()
    observed_threads: dict[str, int] = {}
    written_lines: list[str] = []

    def _record(name: str, value: object) -> object:
        observed_threads[name] = threading.get_ident()
        return value

    monkeypatch.setattr(
        llm_request_logging,
        "_system_prompt",
        lambda *_args: _record("system_prompt", "system"),
    )
    monkeypatch.setattr(
        llm_request_logging,
        "_request_message_payloads",
        lambda *_args: _record("messages", [{"role": "user", "content": "hello"}]),
    )
    monkeypatch.setattr(
        llm_request_logging,
        "_json_safe",
        lambda value: _record("tools", value),
    )
    monkeypatch.setattr(
        llm_request_logging,
        "model_params_payload",
        lambda *_args: _record("model_params", {"temperature": 0.7}),
    )

    def _write(_path: Path, line: str) -> None:
        observed_threads["write"] = threading.get_ident()
        written_lines.append(line)

    monkeypatch.setattr(llm_request_logging, "_write_serialized_jsonl_line", _write)

    await _write_llm_request_log(
        model=_FakeModel(),  # type: ignore[arg-type]
        agent_name="assistant",
        messages=[Message(role="user", content="hello")],
        tools=[{"name": "search"}],
        log_path=tmp_path / "requests.jsonl",
        request_context={"correlation_id": "request-1"},
        request_log_id="log-1",
    )

    assert set(observed_threads) == {"system_prompt", "messages", "tools", "model_params", "write"}
    assert event_loop_thread not in observed_threads.values()
    payload = json.loads(written_lines[0])
    assert payload["messages"] == [{"role": "user", "content": "hello"}]
    assert payload["tools"] == [{"name": "search"}]


def test_concurrent_jsonl_appends_serialize_writer_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Concurrent appends must not overlap at the real-file writer boundary."""
    probe = _AppendBoundaryProbe()
    path_type = type(tmp_path)
    real_open = path_type.open

    def observed_open(path: Path, *args: object, **kwargs: object) -> _ObservedAppendHandle:
        handle = real_open(path, *args, **kwargs)  # type: ignore[arg-type]
        return _ObservedAppendHandle(handle, probe)

    monkeypatch.setattr(llm_request_logging, "_JSONL_APPEND_LOCK", _ReleaseFirstWriterOnContention(probe))
    monkeypatch.setattr(path_type, "open", observed_open)
    log_path = tmp_path / "requests.jsonl"
    lines = [
        json.dumps({"record": "first"}),
        json.dumps({"record": "second"}),
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_append = executor.submit(llm_request_logging._write_serialized_jsonl_line, log_path, lines[0])
        assert probe.first_writer_started.wait(timeout=5)
        second_append = executor.submit(llm_request_logging._write_serialized_jsonl_line, log_path, lines[1])
        first_append.result(timeout=5)
        second_append.result(timeout=5)
    with real_open(log_path, encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle]
    assert not probe.overlap_detected
    assert len(records) == 2
    assert {record["record"] for record in records} == {"first", "second"}


@pytest.mark.asyncio
async def test_llm_request_log_uses_serialized_snapshot_when_file_write_is_delayed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Mutation after serialization must not rewrite a queued immutable line."""
    writer_started = threading.Event()
    release_writer = threading.Event()
    written_lines: list[str] = []

    def delayed_write(_path: Path, line: str) -> None:
        writer_started.set()
        assert release_writer.wait(timeout=5)
        written_lines.append(line)

    monkeypatch.setattr(llm_request_logging, "_write_serialized_jsonl_line", delayed_write)
    model = _FakeModel(id="submitted-model", temperature=0.7)
    messages = [Message(role="user", content="submitted message")]
    tools = [{"name": "submitted_tool"}]

    write_task = asyncio.create_task(
        _write_llm_request_log(
            model=model,  # type: ignore[arg-type]
            agent_name="assistant",
            messages=messages,
            tools=tools,
            log_path=tmp_path / "requests.jsonl",
            request_log_id="log-1",
        ),
    )
    try:
        assert await asyncio.to_thread(writer_started.wait, 5)
        model.id = "mutated-model"
        model.temperature = 1.0
        messages[0].content = "mutated message"
        tools[0]["name"] = "mutated_tool"
        release_writer.set()
        await write_task
    finally:
        release_writer.set()
        await asyncio.gather(write_task, return_exceptions=True)

    assert len(written_lines) == 1
    payload = json.loads(written_lines[0])
    assert payload["model_id"] == "submitted-model"
    assert payload["messages"][0]["content"] == "submitted message"  # type: ignore[index]
    assert payload["tools"] == [{"name": "submitted_tool"}]
    assert payload["model_params"] == {"temperature": 0.7}


@pytest.mark.asyncio
async def test_request_log_pins_container_membership_before_worker_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Queued serialization borrows elements from shallow-copied containers."""
    worker_queued = asyncio.Event()
    release_worker = asyncio.Event()
    original_to_thread = asyncio.to_thread

    async def delayed_to_thread(function: object, /, *args: object, **kwargs: object) -> object:
        worker_queued.set()
        await release_worker.wait()
        return await original_to_thread(function, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(llm_request_logging.asyncio, "to_thread", delayed_to_thread)
    submitted_message = Message(role="user", content="submitted message")
    submitted_tool = {"name": "submitted_tool"}
    messages = [submitted_message]
    tools = [submitted_tool]
    log_path = tmp_path / "requests.jsonl"
    write_task = asyncio.create_task(
        _write_llm_request_log(
            model=_FakeModel(),  # type: ignore[arg-type]
            agent_name="assistant",
            messages=messages,
            tools=tools,
            log_path=log_path,
            request_log_id="log-1",
        ),
    )
    try:
        await worker_queued.wait()
        messages[:] = [Message(role="user", content="replacement message")]
        tools[:] = [{"name": "replacement_tool"}]
        release_worker.set()
        await write_task
    finally:
        release_worker.set()
        await asyncio.gather(write_task, return_exceptions=True)

    payload = json.loads(log_path.read_text(encoding="utf-8"))
    assert payload["messages"][0]["content"] == "submitted message"
    assert payload["tools"] == [{"name": "submitted_tool"}]


@pytest.mark.asyncio
async def test_pending_cancellation_waits_for_accepted_worker() -> None:
    """Cancellation requested before helper entry must wait for accepted work."""
    worker_started = asyncio.Event()
    release_worker = asyncio.Event()
    cancellation_requested = asyncio.Event()

    async def accepted_worker() -> None:
        worker_started.set()
        await release_worker.wait()

    worker = asyncio.create_task(accepted_worker())
    await worker_started.wait()

    async def cancel_before_helper_entry() -> tuple[bool, tuple[object, ...], int]:
        current_task = asyncio.current_task()
        assert current_task is not None
        current_task.cancel("pending cancellation")
        cancellation_requested.set()
        try:
            await llm_request_logging._await_before_cancelling(worker)
        except asyncio.CancelledError as exc:
            return worker.done(), exc.args, current_task.cancelling()
        pytest.fail("pending cancellation was not delivered")

    caller = asyncio.create_task(cancel_before_helper_entry())
    try:
        await cancellation_requested.wait()
        await _assert_cancellation_is_deferred(caller)
        release_worker.set()
        worker_done, cancel_args, cancel_count = await caller
    finally:
        release_worker.set()
        await asyncio.gather(caller, worker, return_exceptions=True)

    assert worker_done
    assert cancel_args == ("pending cancellation",)
    assert cancel_count == 1


@pytest.mark.asyncio
async def test_llm_request_log_defers_cancellation_until_borrowed_values_are_persisted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cancellation must not release borrowed values before their record is durable."""
    serialization_started, release_serialization, serialization_finished = (threading.Event() for _ in range(3))
    original_serialize = llm_request_logging._serialize_llm_request_log_line

    def blocked_serialize(*args: object, **kwargs: object) -> str:
        serialization_started.set()
        assert release_serialization.wait(timeout=5)
        try:
            return original_serialize(*args, **kwargs)  # type: ignore[arg-type]
        finally:
            serialization_finished.set()

    monkeypatch.setattr(llm_request_logging, "_serialize_llm_request_log_line", blocked_serialize)
    model = _FakeModel(id="submitted-model", temperature=0.7)
    messages = [Message(role="user", content="submitted message")]
    tools = [{"name": "submitted_tool"}]
    log_path = tmp_path / "requests.jsonl"
    write_task = asyncio.create_task(
        _write_llm_request_log(
            model=model,  # type: ignore[arg-type]
            agent_name="assistant",
            messages=messages,
            tools=tools,
            log_path=log_path,
            request_log_id="log-1",
        ),
    )
    cancellation_observed = asyncio.Event()
    caught_cancellations: list[asyncio.CancelledError] = []

    async def observe_cancellation() -> None:
        try:
            await write_task
        except asyncio.CancelledError as exc:
            caught_cancellations.append(exc)
            _mutate_borrowed_request_values(model, messages, tools)
            cancellation_observed.set()
            raise

    observer = asyncio.create_task(observe_cancellation())
    try:
        assert await asyncio.to_thread(serialization_started.wait, 5)
        write_task.cancel("first cancellation")
        await _assert_cancellation_is_deferred(write_task)
        write_task.cancel("second cancellation")
        await _assert_cancellation_is_deferred(write_task)
        assert not cancellation_observed.is_set()
        release_serialization.set()
        with pytest.raises(asyncio.CancelledError):
            await observer
        assert await asyncio.to_thread(serialization_finished.wait, 5)
    finally:
        release_serialization.set()
        await asyncio.gather(write_task, observer, return_exceptions=True)

    assert len(caught_cancellations) == 1
    assert caught_cancellations[0].args == ("first cancellation",)
    assert write_task.cancelling() == 2
    payload = json.loads(log_path.read_text(encoding="utf-8"))
    assert payload["model_id"] == "submitted-model"
    assert payload["messages"][0]["content"] == "submitted message"
    assert payload["tools"] == [{"name": "submitted_tool"}]
    assert payload["model_params"] == {"temperature": 0.7}


@pytest.mark.parametrize("worker_fails", [False, True])
@pytest.mark.asyncio
async def test_invoke_cancellation_is_delivered_once_before_caller_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    worker_fails: bool,
) -> None:
    """Deferred cancellation must not fire again after the caller catches it."""
    serializer_started, release_serializer = (threading.Event() for _ in range(2))
    continuation_started, release_continuation = (asyncio.Event() for _ in range(2))
    serialization_error = OSError("serialization failed")
    original_serialize = llm_request_logging._serialize_llm_request_log_line
    caught_cancellations: list[asyncio.CancelledError] = []
    cancellation_counts: list[int] = []

    def controlled_serialization(*args: object, **kwargs: object) -> str:
        serializer_started.set()
        assert release_serializer.wait(timeout=5)
        if worker_fails:
            raise serialization_error
        return original_serialize(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(llm_request_logging, "_serialize_llm_request_log_line", controlled_serialization)
    model = _FakeModel()
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    async def invoke_and_continue() -> str:
        try:
            await model.ainvoke(
                messages=[Message(role="user", content="hello")],
                assistant_message=Message(role="assistant"),
                tools=[],
            )
        except asyncio.CancelledError as exc:
            caught_cancellations.append(exc)
            current_task = asyncio.current_task()
            assert current_task is not None
            cancellation_counts.append(current_task.cancelling())
        continuation_started.set()
        await release_continuation.wait()
        return "continued"

    invoke_task = asyncio.create_task(invoke_and_continue())
    try:
        assert await asyncio.to_thread(serializer_started.wait, 5)
        invoke_task.cancel("cancel request")
        await _assert_cancellation_is_deferred(invoke_task)
        release_serializer.set()
        await continuation_started.wait()
        assert not invoke_task.done()
        release_continuation.set()
        result = await invoke_task
    finally:
        release_serializer.set()
        release_continuation.set()
        await asyncio.gather(invoke_task, return_exceptions=True)

    assert result == "continued"
    assert len(caught_cancellations) == 1
    assert caught_cancellations[0].args == ("cancel request",)
    assert cancellation_counts == [1]
    assert invoke_task.cancelling() == 1
    if worker_fails:
        assert caught_cancellations[0].__cause__ is serialization_error
    else:
        assert caught_cancellations[0].__cause__ is None


@pytest.mark.asyncio
async def test_provider_cancellation_remains_primary_during_request_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A later cancellation during request persistence must not replace the provider one."""
    writer_started = threading.Event()
    release_writer = threading.Event()
    original_write = llm_request_logging._write_serialized_jsonl_line

    def blocked_write(path: Path, line: str) -> None:
        writer_started.set()
        assert release_writer.wait(timeout=5)
        original_write(path, line)

    monkeypatch.setattr(llm_request_logging, "_write_serialized_jsonl_line", blocked_write)
    model = _CancellableInvokeModel()
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    async def invoke_and_capture() -> tuple[asyncio.CancelledError, int]:
        try:
            await model.ainvoke(
                messages=[Message(role="user", content="hello")],
                assistant_message=Message(role="assistant"),
                tools=[],
            )
        except asyncio.CancelledError as exc:
            current_task = asyncio.current_task()
            assert current_task is not None
            return exc, current_task.cancelling()
        pytest.fail("cancellation was not delivered")

    invoke_task = asyncio.create_task(invoke_and_capture())
    try:
        await model.invocation_started.wait()
        invoke_task.cancel("provider cancellation")
        assert await asyncio.to_thread(writer_started.wait, 5)
        invoke_task.cancel("later cancellation")
        await _assert_cancellation_is_deferred(invoke_task)
        release_writer.set()
        cancelled, cancel_count = await invoke_task
    finally:
        release_writer.set()
        await asyncio.gather(invoke_task, return_exceptions=True)

    assert model.provider_cancellation is not None
    assert cancelled is model.provider_cancellation
    assert cancelled.args == ("provider cancellation",)
    assert cancel_count == 2
    assert invoke_task.cancelling() == 2
    assert len(_read_log_entries(tmp_path)) == 1


@pytest.mark.asyncio
async def test_provider_cancellation_chains_request_worker_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Provider cancellation stays primary when accepted request persistence fails."""
    serializer_started = threading.Event()
    release_serializer = threading.Event()
    worker_error = OSError("serialization failed")

    def failing_serialization(*_args: object, **_kwargs: object) -> str:
        serializer_started.set()
        assert release_serializer.wait(timeout=5)
        raise worker_error

    monkeypatch.setattr(llm_request_logging, "_serialize_llm_request_log_line", failing_serialization)
    model = _CancellableInvokeModel()
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )
    invoke_task = asyncio.create_task(
        model.ainvoke(
            messages=[Message(role="user", content="hello")],
            assistant_message=Message(role="assistant"),
            tools=[],
        ),
    )
    try:
        await model.invocation_started.wait()
        invoke_task.cancel("provider cancellation")
        assert await asyncio.to_thread(serializer_started.wait, 5)
        release_serializer.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await invoke_task
    finally:
        release_serializer.set()
        await asyncio.gather(invoke_task, return_exceptions=True)

    assert model.provider_cancellation is not None
    assert cancelled.value is model.provider_cancellation
    assert cancelled.value.args == ("provider cancellation",)
    assert cancelled.value.__cause__ is worker_error
    assert invoke_task.cancelling() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_record", ["request", "response"])
async def test_invoke_cancellation_waits_for_linked_success_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    blocked_record: str,
) -> None:
    """Successful-call cancellation must wait for its linked request and response."""
    writer_started = threading.Event()
    release_writer = threading.Event()
    continuation_started = asyncio.Event()
    release_continuation = asyncio.Event()
    original_write = llm_request_logging._write_serialized_jsonl_line

    def blocked_write(path: Path, line: str) -> None:
        record_type = json.loads(line).get("record", "request")
        if record_type == blocked_record:
            writer_started.set()
            assert release_writer.wait(timeout=5)
        original_write(path, line)

    monkeypatch.setattr(llm_request_logging, "_write_serialized_jsonl_line", blocked_write)
    model = _FakeModel(response_usage=MessageMetrics(input_tokens=5, output_tokens=7))
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    async def invoke_and_continue() -> tuple[tuple[object, ...], int]:
        try:
            await model.ainvoke(
                messages=[Message(role="user", content="hello")],
                assistant_message=Message(role="assistant"),
                tools=[],
            )
        except asyncio.CancelledError as exc:
            current_task = asyncio.current_task()
            assert current_task is not None
            continuation_started.set()
            await release_continuation.wait()
            return exc.args, current_task.cancelling()
        pytest.fail("cancellation was not delivered")

    with capture_logs() as logs:
        invoke_task = asyncio.create_task(invoke_and_continue())
        try:
            assert await asyncio.to_thread(writer_started.wait, 5)
            invoke_task.cancel("first cancellation")
            await _assert_cancellation_is_deferred(invoke_task)
            invoke_task.cancel("second cancellation")
            await _assert_cancellation_is_deferred(invoke_task)
            release_writer.set()
            await continuation_started.wait()
            release_continuation.set()
            cancel_args, cancel_count = await invoke_task
        finally:
            release_writer.set()
            release_continuation.set()
            await asyncio.gather(invoke_task, return_exceptions=True)

    request_entry, response_entry = _read_log_entries(tmp_path)
    assert response_entry["record"] == "response"
    assert response_entry["request_log_id"] == request_entry["request_log_id"]
    assert response_entry["usage"]["input_tokens"] == 5
    assert cancel_args == ("first cancellation",)
    assert cancel_count == 2
    assert invoke_task.cancelling() == 2
    assert [entry["usage_available"] for entry in logs] == [True]


@pytest.mark.asyncio
async def test_inner_worker_cancellation_is_not_counted_as_caller_cancellation() -> None:
    """A cancelled worker must not masquerade as a new outer request."""

    async def run_after_prior_cancellation() -> None:
        current_task = asyncio.current_task()
        assert current_task is not None
        current_task.cancel("prior cancellation")
        with pytest.raises(asyncio.CancelledError, match="prior cancellation"):
            await asyncio.Event().wait()

        worker = asyncio.create_task(asyncio.Event().wait())
        worker.cancel("worker cancelled")
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await llm_request_logging._await_before_cancelling(worker)

        assert current_task.cancelling() == 1
        assert cancelled.value.args == ("worker cancelled",)
        assert cancelled.value.__cause__ is None

    completed = asyncio.create_task(run_after_prior_cancellation())
    await completed
    assert completed.cancelling() == 1


@pytest.mark.asyncio
async def test_stream_cancellation_persists_one_request_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Generator cleanup must not retry an accepted request append."""
    writer_started = threading.Event()
    release_writer = threading.Event()
    original_write = llm_request_logging._write_serialized_jsonl_line

    def blocked_write(path: Path, line: str) -> None:
        writer_started.set()
        assert release_writer.wait(timeout=5)
        original_write(path, line)

    monkeypatch.setattr(llm_request_logging, "_write_serialized_jsonl_line", blocked_write)
    model = _FakeModel()
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )
    stream = model.ainvoke_stream(
        messages=[Message(role="user", content="hello")],
        assistant_message=Message(role="assistant"),
        tools=[],
    )
    next_chunk = asyncio.create_task(anext(stream))
    try:
        assert await asyncio.to_thread(writer_started.wait, 5)
        next_chunk.cancel("cancel stream")
        await _assert_cancellation_is_deferred(next_chunk)
        release_writer.set()
        with pytest.raises(asyncio.CancelledError):
            await next_chunk
    finally:
        release_writer.set()
        await asyncio.gather(next_chunk, return_exceptions=True)
        await stream.aclose()

    entries = _read_log_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0]["messages"][0]["role"] == "user"
    assert entries[0]["messages"][0]["content"] == "hello"


@pytest.mark.asyncio
async def test_stream_cancellation_finalizes_received_chunk_usage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cancellation after a usage chunk must persist one linked response first."""

    @dataclass
    class _UsageOnFirstChunkModel(_FakeModel):
        async def ainvoke_stream(self, *_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
            yield ModelResponse(content="ok", response_usage=MessageMetrics(input_tokens=3, output_tokens=2))

    request_writer_started = threading.Event()
    release_request_writer = threading.Event()
    response_writer_started = threading.Event()
    release_response_writer = threading.Event()
    original_write = llm_request_logging._write_serialized_jsonl_line

    def blocked_write(path: Path, line: str) -> None:
        if json.loads(line).get("record") == "response":
            response_writer_started.set()
            assert release_response_writer.wait(timeout=5)
        else:
            request_writer_started.set()
            assert release_request_writer.wait(timeout=5)
        original_write(path, line)

    monkeypatch.setattr(llm_request_logging, "_write_serialized_jsonl_line", blocked_write)
    model = _UsageOnFirstChunkModel()
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )
    stream = model.ainvoke_stream(
        messages=[Message(role="user", content="hello")],
        assistant_message=Message(role="assistant"),
        tools=[],
    )

    with capture_logs() as logs:
        next_chunk = asyncio.create_task(anext(stream))
        try:
            assert await asyncio.to_thread(request_writer_started.wait, 5)
            next_chunk.cancel("first cancellation")
            await _assert_cancellation_is_deferred(next_chunk)
            release_request_writer.set()
            assert await asyncio.to_thread(response_writer_started.wait, 5)
            next_chunk.cancel("second cancellation")
            await _assert_cancellation_is_deferred(next_chunk)
            release_response_writer.set()
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await next_chunk
        finally:
            release_request_writer.set()
            release_response_writer.set()
            await asyncio.gather(next_chunk, return_exceptions=True)
            await stream.aclose()

    request_entry, response_entry = _read_log_entries(tmp_path)
    assert response_entry["record"] == "response"
    assert response_entry["request_log_id"] == request_entry["request_log_id"]
    assert response_entry["usage"]["input_tokens"] == 3
    assert response_entry["usage"]["output_tokens"] == 2
    assert cancelled.value.args == ("first cancellation",)
    assert next_chunk.cancelling() == 2
    assert [entry["usage_available"] for entry in logs] == [True]


@pytest.mark.asyncio
async def test_stream_cancellation_chains_request_worker_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Stream cancellation must retain the accepted request worker's error."""
    serializer_started = threading.Event()
    release_serializer = threading.Event()
    worker_error = OSError("serialization failed")

    def failing_serialization(*_args: object, **_kwargs: object) -> str:
        serializer_started.set()
        assert release_serializer.wait(timeout=5)
        raise worker_error

    monkeypatch.setattr(llm_request_logging, "_serialize_llm_request_log_line", failing_serialization)
    model = _FakeModel()
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )
    stream = model.ainvoke_stream(
        messages=[Message(role="user", content="hello")],
        assistant_message=Message(role="assistant"),
        tools=[],
    )
    next_chunk = asyncio.create_task(anext(stream))
    try:
        assert await asyncio.to_thread(serializer_started.wait, 5)
        next_chunk.cancel("stream cancellation")
        await _assert_cancellation_is_deferred(next_chunk)
        release_serializer.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await next_chunk
    finally:
        release_serializer.set()
        await asyncio.gather(next_chunk, return_exceptions=True)
        await stream.aclose()

    assert cancelled.value.args == ("stream cancellation",)
    assert cancelled.value.__cause__ is worker_error
    assert next_chunk.cancelling() == 1


@pytest.mark.asyncio
async def test_stream_provider_cancellation_chains_final_request_worker_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cleanup must retain a failed request task when no chunk was received."""
    serializer_started = threading.Event()
    release_serializer = threading.Event()
    worker_error = OSError("serialization failed")

    def failing_serialization(*_args: object, **_kwargs: object) -> str:
        serializer_started.set()
        assert release_serializer.wait(timeout=5)
        raise worker_error

    monkeypatch.setattr(llm_request_logging, "_serialize_llm_request_log_line", failing_serialization)
    model = _CancellableStreamModel()
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )
    stream = model.ainvoke_stream(
        messages=[Message(role="user", content="hello")],
        assistant_message=Message(role="assistant"),
        tools=[],
    )
    next_chunk = asyncio.create_task(anext(stream))
    try:
        await model.invocation_started.wait()
        next_chunk.cancel("provider stream cancellation")
        assert await asyncio.to_thread(serializer_started.wait, 5)
        await _assert_cancellation_is_deferred(next_chunk)
        next_chunk.cancel("later cancellation")
        await _assert_cancellation_is_deferred(next_chunk)
        release_serializer.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await next_chunk
    finally:
        release_serializer.set()
        await asyncio.gather(next_chunk, return_exceptions=True)
        await stream.aclose()

    assert model.provider_cancellation is not None
    assert cancelled.value is model.provider_cancellation
    assert cancelled.value.args == ("provider stream cancellation",)
    assert cancelled.value.__cause__ is worker_error
    assert next_chunk.cancelling() == 2


@pytest.mark.asyncio
async def test_stream_request_worker_failure_does_not_mask_provider_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Ordinary provider failure remains primary over cleanup logging failure."""
    provider_error = RuntimeError("provider failed")
    worker_error = OSError("serialization failed")

    @dataclass
    class _FailingStreamModel(_FakeModel):
        async def ainvoke_stream(self, *_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
            raise provider_error
            yield ModelResponse(content="unreachable")

    def failing_serialization(*_args: object, **_kwargs: object) -> str:
        raise worker_error

    monkeypatch.setattr(llm_request_logging, "_serialize_llm_request_log_line", failing_serialization)
    model = _FailingStreamModel()
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )
    stream = model.ainvoke_stream(
        messages=[Message(role="user", content="hello")],
        assistant_message=Message(role="assistant"),
        tools=[],
    )

    with capture_logs() as logs, pytest.raises(RuntimeError) as raised:
        await anext(stream)

    assert raised.value is provider_error
    assert [entry["event"] for entry in logs] == [
        "Failed to persist LLM request log",
        "LLM usage",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_response_worker_failure_is_chained_to_deferred_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    streaming: bool,
) -> None:
    """An accepted response append must report failure to its cancellation owner."""
    response_writer_started = threading.Event()
    release_response_writer = threading.Event()
    worker_error = OSError("response append failed")
    original_write = llm_request_logging._write_serialized_jsonl_line

    def failing_response_write(path: Path, line: str) -> None:
        if json.loads(line).get("record") == "response":
            response_writer_started.set()
            assert release_response_writer.wait(timeout=5)
            raise worker_error
        original_write(path, line)

    monkeypatch.setattr(llm_request_logging, "_write_serialized_jsonl_line", failing_response_write)
    model = _FakeModel(response_usage=MessageMetrics(input_tokens=3, output_tokens=2))
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    async def call_model() -> None:
        if streaming:
            async for _chunk in model.ainvoke_stream(
                messages=[Message(role="user", content="hello")],
                assistant_message=Message(role="assistant"),
                tools=[],
            ):
                pass
        else:
            await model.ainvoke(
                messages=[Message(role="user", content="hello")],
                assistant_message=Message(role="assistant"),
                tools=[],
            )

    async def call_and_capture() -> tuple[asyncio.CancelledError, int]:
        try:
            await call_model()
        except asyncio.CancelledError as exc:
            current_task = asyncio.current_task()
            assert current_task is not None
            return exc, current_task.cancelling()
        pytest.fail("cancellation was not delivered")

    call_task = asyncio.create_task(call_and_capture())
    try:
        assert await asyncio.to_thread(response_writer_started.wait, 5)
        call_task.cancel("first cancellation")
        await _assert_cancellation_is_deferred(call_task)
        call_task.cancel("second cancellation")
        await _assert_cancellation_is_deferred(call_task)
        release_response_writer.set()
        cancelled, cancel_count = await call_task
    finally:
        release_response_writer.set()
        await asyncio.gather(call_task, return_exceptions=True)

    assert cancelled.args == ("first cancellation",)
    assert cancelled.__cause__ is worker_error
    assert cancel_count == 2
    assert call_task.cancelling() == 2
    entries = _read_log_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0].get("record") is None


@pytest.mark.asyncio
async def test_settled_stream_request_task_does_not_reenter_cancellation_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Later chunks and cleanup must use the settled request result directly."""
    original_await = llm_request_logging._await_before_cancelling
    settled_request_task: asyncio.Task[_RequestLogRef | None] | None = None
    settled_request_reentered_boundary = False

    async def observe_boundary[Result](task: asyncio.Task[Result]) -> Result:
        nonlocal settled_request_reentered_boundary, settled_request_task
        if task is settled_request_task:
            settled_request_reentered_boundary = True
        result = await original_await(task)
        if isinstance(result, _RequestLogRef):
            settled_request_task = task
        return result

    monkeypatch.setattr(llm_request_logging, "_await_before_cancelling", observe_boundary)
    model = _FakeModel(response_usage=MessageMetrics(input_tokens=3, output_tokens=2))
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    contents = [
        chunk.content
        async for chunk in model.ainvoke_stream(
            messages=[Message(role="user", content="hello")],
            assistant_message=Message(role="assistant"),
            tools=[],
        )
    ]

    assert contents == ["ok", "!"]
    assert settled_request_task is not None
    assert not settled_request_reentered_boundary
    assert [entry.get("record") for entry in _read_log_entries(tmp_path)] == [None, "response"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_uncancelled_request_worker_failure_remains_best_effort(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    streaming: bool,
) -> None:
    """An ordinary request worker failure must not change uncancelled provider behavior."""
    worker_error = OSError("serialization failed")

    def failing_serialization(*_args: object, **_kwargs: object) -> str:
        raise worker_error

    monkeypatch.setattr(llm_request_logging, "_serialize_llm_request_log_line", failing_serialization)
    model = _FakeModel(response_usage=MessageMetrics(input_tokens=3, output_tokens=2))
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    with capture_logs() as logs:
        if streaming:
            contents = [
                chunk.content
                async for chunk in model.ainvoke_stream(
                    messages=[Message(role="user", content="hello")],
                    assistant_message=Message(role="assistant"),
                    tools=[],
                )
            ]
        else:
            response = await model.ainvoke(
                messages=[Message(role="user", content="hello")],
                assistant_message=Message(role="assistant"),
                tools=[],
            )
            contents = [response.content]

    assert contents == (["ok", "!"] if streaming else ["ok"])
    assert [entry["event"] for entry in logs] == [
        "Failed to persist LLM request log",
        "LLM usage",
    ]


@pytest.mark.asyncio
async def test_wire_tool_normalization_runs_on_request_serializer_thread(tmp_path: Path) -> None:
    """Capture pins membership while element traversal stays off-loop."""
    event_loop_thread = threading.get_ident()
    traversal_threads: list[int] = []

    class TraversalProbe(dict[str, object]):
        def items(self):  # noqa: ANN202
            traversal_threads.append(threading.get_ident())
            return super().items()

    capture = llm_request_logging._WireToolsCapture(enabled=True)
    token = llm_request_logging._WIRE_TOOLS_CAPTURE.set(capture)
    wire_tools = [TraversalProbe({"name": "search"})]
    try:
        record_llm_request_tools(wire_tools)
    finally:
        llm_request_logging._WIRE_TOOLS_CAPTURE.reset(token)

    assert traversal_threads == []
    wire_tools.clear()
    await _write_llm_request_log(
        model=_FakeModel(),  # type: ignore[arg-type]
        agent_name="assistant",
        messages=[Message(role="user", content="hello")],
        tools=capture.tools,
        log_path=tmp_path / "requests.jsonl",
        request_log_id="log-1",
    )

    assert traversal_threads
    assert event_loop_thread not in traversal_threads
    payload = json.loads((tmp_path / "requests.jsonl").read_text(encoding="utf-8"))
    assert payload["tools"] == [{"name": "search"}]


@pytest.mark.asyncio
async def test_llm_request_logging_preserves_serializable_values_that_reject_deepcopy(tmp_path: Path) -> None:
    """Offloading must not narrow the logger's accepted JSON-compatible values."""

    class CopyDisabledError(RuntimeError):
        pass

    class SerializableButNotCopyable(dict[str, object]):
        def __deepcopy__(self, _memo: dict[int, object]) -> object:
            raise CopyDisabledError

    content = SerializableButNotCopyable({"nested": ["kept"]})
    model = _FakeModel()
    model.temperature = content  # type: ignore[assignment]

    await _write_llm_request_log(
        model=model,  # type: ignore[arg-type]
        agent_name="assistant",
        messages=[Message.model_construct(role="user", content=[content])],
        tools=[{"payload": content}],
        log_path=tmp_path / "requests.jsonl",
        request_log_id="log-1",
    )

    payload = json.loads((tmp_path / "requests.jsonl").read_text(encoding="utf-8"))
    assert payload["messages"][0]["content"] == [{"nested": ["kept"]}]
    assert payload["tools"] == [{"payload": {"nested": ["kept"]}}]
    assert payload["model_params"] == {"temperature": {"nested": ["kept"]}}


@pytest.mark.asyncio
async def test_llm_request_timestamp_and_daily_file_share_submission_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A writer delayed past midnight must stay in its submission day's file."""
    submission_time = datetime(2026, 8, 25, 12).astimezone()
    worker_time = datetime(2026, 8, 26, 12).astimezone()
    observed_times = iter((submission_time, worker_time))

    class _Clock:
        @classmethod
        def now(cls) -> datetime:
            return next(observed_times)

    monkeypatch.setattr(llm_request_logging, "datetime", _Clock)

    request_log_ref = await llm_request_logging._write_llm_request_log_if_present(
        model=_FakeModel(),  # type: ignore[arg-type]
        agent_name="assistant",
        kwargs={"messages": [Message(role="user", content="hello")], "tools": []},
        log_dir=str(tmp_path),
        default_log_dir=tmp_path / "unused",
        request_context={},
        wire_tools_capture=llm_request_logging._WireToolsCapture(enabled=True),
    )

    assert request_log_ref is not None
    assert request_log_ref.log_path.name == "llm-requests-2026-08-25.jsonl"
    entry = json.loads(request_log_ref.log_path.read_text(encoding="utf-8"))
    assert entry["timestamp"] == submission_time.astimezone().isoformat()


class _PlainAsyncIterator:
    """Async iterator without aclose(), valid under the AsyncIterator contract."""

    def __init__(self, values: list[str]) -> None:
        self._values = values
        self.contexts: list[dict[str, object]] = []

    def __aiter__(self) -> _PlainAsyncIterator:
        return self

    async def __anext__(self) -> str:
        if not self._values:
            raise StopAsyncIteration
        self.contexts.append(current_llm_request_log_context())
        return self._values.pop(0)


@dataclass
class _DeferredWireModel(_FakeModel):
    """Fake adapter that filters one deferred schema until load_tool."""

    deferred_tool_loaded: bool = False

    def load_tool(self) -> None:
        self.deferred_tool_loaded = True

    def _record_wire_tools(self, kwargs: dict[str, object]) -> None:
        tools = kwargs.get("tools")
        assert isinstance(tools, list)
        wire_tools = [
            tool
            for tool in tools
            if self.deferred_tool_loaded or not isinstance(tool, dict) or tool.get("name") != "deferred_search"
        ]
        record_llm_request_tools(wire_tools)

    async def ainvoke(self, *_args: object, **kwargs: object) -> ModelResponse:
        self._record_wire_tools(kwargs)
        return ModelResponse(content="ok")

    async def ainvoke_stream(self, *_args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
        self._record_wire_tools(kwargs)
        yield ModelResponse(content="ok")


@dataclass
class _OpenAIDeferredWireModel(_FakeModel):
    """Fake OpenAI adapter that runs MindRoom's real wire-tool preparation."""

    async def ainvoke(self, *_args: object, **kwargs: object) -> ModelResponse:
        request_params_with_deferred_tool_search(
            {"tools": kwargs.get("tools")},
            frozenset({"deferred_search"}),
        )
        return ModelResponse(content="ok")


def _read_log_entries(log_dir: Path) -> list[dict[str, Any]]:
    log_files = list(log_dir.glob("llm-requests-*.jsonl"))
    assert len(log_files) == 1
    return [json.loads(line) for line in log_files[0].read_text(encoding="utf-8").splitlines()]


def test_debug_config_parses() -> None:
    """Debug config should parse both explicit and default request logging settings."""
    config = Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "debug": {"log_llm_requests": True, "llm_request_log_dir": "custom-logs"},
        },
    )
    assert config.debug == DebugConfig(log_llm_requests=True, llm_request_log_dir="custom-logs")
    assert (
        Config.model_validate({"models": {"default": {"provider": "openai", "id": "test-model"}}}).debug
        == DebugConfig()
    )


@pytest.mark.asyncio
async def test_llm_request_logging_writes_jsonl(tmp_path: Path) -> None:  # noqa: PLR0915
    """Enabled request logging should emit one full JSONL entry per invoke path."""
    model = _FakeModel()
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )
    messages = [
        Message(role="system", content="s" * 600, created_at=111),
        Message(
            role="user",
            content="hello",
            created_at=222,
            metrics=MessageMetrics(input_tokens=2, total_tokens=2, duration=1.5),
        ),
    ]
    assistant_message = Message(role="assistant")

    with bind_llm_request_log_context(
        agent_id="assistant",
        session_id="session-123",
        room_id="!room:example.com",
        thread_id="$thread:example.com",
        reply_to_event_id="$reply:example.com",
        requester_id="@user:example.com",
        correlation_id="$reply:example.com",
        current_turn_prompt="try now",
        model_prompt="try now\n\nbe explicit",
        full_prompt="system\n\nuser: try now",
        source_event_ids=["$reply:example.com", "$coalesced:example.com"],
        source_event_prompts={"$coalesced:example.com": "older prompt"},
    ):
        result = await model.ainvoke(
            messages=messages,
            assistant_message=assistant_message,
            tools=[{"name": "search"}],
        )
    assert result.content == "ok"

    with bind_llm_request_log_context(
        agent_id="assistant",
        session_id="session-123",
        room_id="!room:example.com",
        thread_id="$thread:example.com",
        reply_to_event_id="$reply:example.com",
        requester_id="@user:example.com",
        correlation_id="$reply:example.com",
        current_turn_prompt="try now",
        model_prompt="try now\n\nbe explicit",
        full_prompt="system\n\nuser: try now",
        source_event_ids=["$reply:example.com", "$coalesced:example.com"],
        source_event_prompts={"$coalesced:example.com": "older prompt"},
    ):
        stream = model.ainvoke_stream(
            messages=messages,
            assistant_message=assistant_message,
            tools=[],
        )
    streamed = [chunk async for chunk in stream]
    assert [chunk.content for chunk in streamed] == ["ok", "!"]

    entries = _read_log_entries(tmp_path)
    assert len(entries) == 2
    assert entries[0]["agent_id"] == "assistant"
    assert entries[0]["model_id"] == "test-model"
    assert entries[0]["session_id"] == "session-123"
    assert entries[0]["room_id"] == "!room:example.com"
    assert entries[0]["thread_id"] == "$thread:example.com"
    assert entries[0]["reply_to_event_id"] == "$reply:example.com"
    assert entries[0]["requester_id"] == "@user:example.com"
    assert entries[0]["correlation_id"] == "$reply:example.com"
    assert entries[0]["current_turn_prompt"] == "try now"
    assert entries[0]["model_prompt"] == "try now\n\nbe explicit"
    assert entries[0]["full_prompt"] == "system\n\nuser: try now"
    assert entries[0]["source_event_ids"] == ["$reply:example.com", "$coalesced:example.com"]
    assert entries[0]["source_event_prompts"] == {"$coalesced:example.com": "older prompt"}
    assert entries[0]["system_prompt"] == "s" * 600
    assert entries[0]["messages"][0]["role"] == "system"
    assert entries[0]["messages"][0]["content"] == "s" * 600
    assert entries[0]["messages"][0]["created_at"] == 111
    assert entries[0]["messages"][1]["role"] == "user"
    assert entries[0]["messages"][1]["content"] == "hello"
    assert entries[0]["messages"][1]["created_at"] == 222
    assert entries[0]["messages"][1]["metrics"]["input_tokens"] == 2
    assert entries[0]["messages"][1]["metrics"]["total_tokens"] == 2
    assert entries[0]["messages"][1]["metrics"]["duration"] == 1.5
    assert entries[0]["message_count"] == 2
    assert entries[0]["tools"] == [{"name": "search"}]
    assert entries[0]["tool_count"] == 1
    assert entries[0]["model_params"] == {"temperature": 0.7}
    assert "timestamp" in entries[0]
    assert entries[1]["messages"][0]["created_at"] == 111
    assert entries[1]["messages"][1]["created_at"] == 222
    assert entries[1]["messages"][1]["metrics"]["input_tokens"] == 2
    assert entries[1]["thread_id"] == "$thread:example.com"
    assert entries[1]["reply_to_event_id"] == "$reply:example.com"
    assert entries[1]["requester_id"] == "@user:example.com"
    assert entries[1]["correlation_id"] == "$reply:example.com"
    assert entries[1]["tools"] == []
    assert entries[1]["tool_count"] == 0


@pytest.mark.asyncio
async def test_llm_request_logging_uses_post_defer_wire_tools(tmp_path: Path) -> None:
    """Logs should exclude an unloaded deferred schema and include it after load_tool."""
    model = _DeferredWireModel()
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )
    catalog_tools = [{"name": "always_available"}, {"name": "deferred_search"}]
    request_kwargs = {
        "messages": [Message(role="user", content="search")],
        "assistant_message": Message(role="assistant"),
        "tools": catalog_tools,
    }

    await model.ainvoke(**request_kwargs)
    model.load_tool()
    assert [chunk.content async for chunk in model.ainvoke_stream(**request_kwargs)] == ["ok"]

    before_load, after_load = _read_log_entries(tmp_path)
    assert before_load["tools"] == [{"name": "always_available"}]
    assert before_load["tool_count"] == 1
    assert after_load["tools"] == catalog_tools
    assert after_load["tool_count"] == 2


@pytest.mark.asyncio
async def test_llm_request_logging_records_claude_native_wire_tools(tmp_path: Path) -> None:
    """Claude logs should match the final native-search array passed to its SDK."""
    model = Claude(id="claude-opus-5", api_key="test-key", cache_system_prompt=False)

    class _FakeMessagesAPI:
        async def create(self, **_kwargs: object) -> object:
            return object()

    class _FakeClient:
        def __init__(self) -> None:
            self.messages = _FakeMessagesAPI()

    vars(model)["get_async_client"] = lambda: _FakeClient()
    vars(model)["_has_beta_features"] = lambda **_kwargs: False
    vars(model)["_parse_provider_response"] = lambda *_args, **_kwargs: ModelResponse(content="ok")
    install_llm_request_logging(
        model,
        agent_name="claude",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )
    install_claude_deferred_tool_search(model, deferred_tool_names=frozenset({"deferred_search"}))
    tools = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} description",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in ("always_available", "deferred_search")
    ]

    await model.ainvoke(
        messages=[Message(role="user", content="search")],
        assistant_message=Message(role="assistant"),
        tools=tools,
    )

    entry = _read_log_entries(tmp_path)[0]
    assert [tool["name"] for tool in entry["tools"]] == [
        "tool_search_tool_regex",
        "always_available",
        "deferred_search",
    ]
    assert entry["tools"][0]["type"] == "tool_search_tool_regex_20251119"
    assert entry["tools"][2]["defer_loading"] is True
    assert entry["tool_count"] == 3


@pytest.mark.asyncio
async def test_llm_request_logging_records_openai_native_wire_tools(tmp_path: Path) -> None:
    """OpenAI logs should match the final server-side-search tools array."""
    model = _OpenAIDeferredWireModel()
    install_llm_request_logging(
        model,
        agent_name="openai",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )
    tools = [
        {"type": "function", "name": "always_available"},
        {"type": "function", "name": "deferred_search"},
    ]

    await model.ainvoke(
        messages=[Message(role="user", content="search")],
        assistant_message=Message(role="assistant"),
        tools=tools,
    )

    entry = _read_log_entries(tmp_path)[0]
    assert entry["tools"] == [
        {"type": "tool_search"},
        {"type": "function", "name": "always_available"},
        {"type": "function", "name": "deferred_search", "defer_loading": True},
    ]
    assert entry["tool_count"] == 3


@pytest.mark.asyncio
async def test_llm_request_logging_redacts_sensitive_values_before_jsonl_write(tmp_path: Path) -> None:
    """Durable request logs should preserve structure while masking credentials."""
    model = _FakeModel()
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    with bind_llm_request_log_context(
        agent_id="assistant",
        session_id="session-123",
        correlation_id="corr-1",
        callback_url="https://example.test/oauth/callback?code=code-secret&state=state-secret&keep=1",
    ):
        await model.ainvoke(
            messages=[
                Message(
                    role="user",
                    content="call failed with Authorization: Bearer auth-secret and api_key=api-secret",
                ),
            ],
            assistant_message=Message(role="assistant"),
            tools=[
                {
                    "name": "custom_api",
                    "headers": {
                        "Authorization": "Bearer auth-secret",
                        "set-cookie": "session=secret",
                    },
                    "nested": [{"refresh_token": "refresh-secret"}],
                },
            ],
        )

    entry = _read_log_entries(tmp_path)[0]
    serialized = json.dumps(entry)

    assert "auth-secret" not in serialized
    assert "api-secret" not in serialized
    assert "refresh-secret" not in serialized
    assert "code-secret" not in serialized
    assert "state-secret" not in serialized
    assert entry["messages"][0]["content"] == (
        "call failed with Authorization: Bearer ***redacted*** and api_key=***redacted***"
    )
    assert entry["tools"][0]["headers"] == {
        "Authorization": "***redacted***",
        "set-cookie": "***redacted***",
    }
    assert entry["tools"][0]["nested"] == [{"refresh_token": "***redacted***"}]
    assert entry["callback_url"] == (
        "https://example.test/oauth/callback?code=***redacted***&state=***redacted***&keep=1"
    )


@pytest.mark.asyncio
async def test_stream_with_llm_request_log_context_accepts_plain_async_iterator() -> None:
    """Request-log stream binding should not require an aclose method."""
    source = _PlainAsyncIterator(["one", "two"])

    async def collect() -> list[str]:
        return [
            item
            async for item in stream_with_llm_request_log_context(
                source,
                request_context={"correlation_id": "corr-1"},
            )
        ]

    assert await collect() == ["one", "two"]
    assert source.contexts == [
        {"correlation_id": "corr-1"},
        {"correlation_id": "corr-1"},
    ]


@pytest.mark.asyncio
async def test_llm_request_logging_uses_model_name_when_context_is_unbound(tmp_path: Path) -> None:
    """Unbound model calls should still keep their configured model-owner attribution."""
    model = _FakeModel()
    install_llm_request_logging(
        model,
        agent_name="router",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    await model.ainvoke(
        messages=[Message(role="user", content="route this")],
        assistant_message=Message(role="assistant"),
        tools=[],
    )

    entries = _read_log_entries(tmp_path)
    assert entries[0]["agent_id"] == "router"
    assert entries[0]["model_id"] == "test-model"


@pytest.mark.asyncio
async def test_llm_request_logging_disabled_still_emits_usage_telemetry(tmp_path: Path) -> None:
    """Usage telemetry should stay enabled without writing full request logs."""
    model = _FakeModel(
        response_usage=MessageMetrics(
            input_tokens=1_000,
            output_tokens=50,
            reasoning_tokens=20,
            cache_read_tokens=800,
            cache_write_tokens=100,
        ),
    )
    with capture_logs() as logs:
        install_llm_request_logging(
            model,
            agent_name="default",
            debug_config=DebugConfig(),
            default_log_dir=tmp_path,
            configured_provider="openai",
        )
        with bind_llm_request_log_context(correlation_id="corr-1", full_prompt="private prompt"):
            await model.ainvoke(
                messages=[Message(role="user", content="hello")],
                assistant_message=Message(role="assistant"),
                tools=[],
            )

    assert list(tmp_path.iterdir()) == []
    assert logs == [
        {
            "event": "LLM usage",
            "log_level": "info",
            "model_name": "default",
            "model_id": "test-model",
            "provider": "OpenAI",
            "usage_available": True,
            "input_tokens": 1_000,
            "context_input_tokens": 1_000,
            "output_tokens": 50,
            "reasoning_tokens": 20,
            "cache_read_tokens": 800,
            "cache_write_tokens": 100,
            "uncached_input_tokens": 200,
            "cache_read_ratio": 0.8,
            "correlation_id": "corr-1",
        },
    ]


@pytest.mark.asyncio
async def test_llm_request_log_failure_does_not_fail_invoke_or_drop_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional request persistence must not change response or usage behavior."""
    log_error = OSError("disk full")

    async def fail_request_log(**_kwargs: object) -> None:
        raise log_error

    monkeypatch.setattr(
        "mindroom.llm_request_logging._write_llm_request_log_if_enabled",
        fail_request_log,
    )
    model = _FakeModel(response_usage=MessageMetrics(input_tokens=12, output_tokens=3))
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    with capture_logs() as logs:
        response = await model.ainvoke(
            messages=[Message(role="user", content="hello")],
            assistant_message=Message(role="assistant"),
            tools=[],
        )

    assert response.content == "ok"
    assert [entry["event"] for entry in logs] == [
        "Failed to persist LLM request log",
        "LLM usage",
    ]


@pytest.mark.asyncio
async def test_llm_request_log_failure_does_not_mask_provider_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request-log failure in finally must preserve the provider exception."""
    provider_error = RuntimeError("provider failed")
    log_error = OSError("disk full")

    @dataclass
    class _FailingModel(_FakeModel):
        async def ainvoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
            raise provider_error

    async def fail_request_log(**_kwargs: object) -> None:
        raise log_error

    monkeypatch.setattr(
        "mindroom.llm_request_logging._write_llm_request_log_if_enabled",
        fail_request_log,
    )
    model = _FailingModel()
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    with capture_logs(), pytest.raises(RuntimeError, match="provider failed"):
        await model.ainvoke(
            messages=[Message(role="user", content="hello")],
            assistant_message=Message(role="assistant"),
            tools=[],
        )


@pytest.mark.asyncio
async def test_llm_request_log_failure_does_not_terminate_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streaming must remain usable when optional request persistence fails."""
    log_error = OSError("disk full")

    async def fail_request_log(**_kwargs: object) -> None:
        raise log_error

    monkeypatch.setattr(
        "mindroom.llm_request_logging._write_llm_request_log_if_enabled",
        fail_request_log,
    )
    model = _FakeModel(response_usage=MessageMetrics(input_tokens=12, output_tokens=3))
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    with capture_logs() as logs:
        chunks = [
            chunk
            async for chunk in model.ainvoke_stream(
                messages=[Message(role="user", content="hello")],
                assistant_message=Message(role="assistant"),
                tools=[],
            )
        ]

    assert [chunk.content for chunk in chunks] == ["ok", "!"]
    assert [entry["event"] for entry in logs] == [
        "Failed to persist LLM request log",
        "LLM usage",
    ]


@pytest.mark.asyncio
async def test_llm_response_log_failure_does_not_fail_invoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional response telemetry must not replace a successful response."""
    log_error = OSError("disk full")

    async def fail_response_log(**_kwargs: object) -> None:
        raise log_error

    monkeypatch.setattr(
        "mindroom.llm_request_logging._write_llm_response_log",
        fail_response_log,
    )
    model = _FakeModel(response_usage=MessageMetrics(input_tokens=12, output_tokens=3))
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(),
        default_log_dir=tmp_path,
    )

    with capture_logs() as logs:
        response = await model.ainvoke(
            messages=[Message(role="user", content="hello")],
            assistant_message=Message(role="assistant"),
            tools=[],
        )

    assert response.content == "ok"
    assert [entry["event"] for entry in logs] == ["Failed to emit LLM response telemetry"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_uncancelled_response_worker_failure_remains_best_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    streaming: bool,
) -> None:
    """A response append failure must not replace normal provider behavior."""
    worker_error = OSError("response append failed")
    original_write = llm_request_logging._write_serialized_jsonl_line

    def failing_response_write(path: Path, line: str) -> None:
        if json.loads(line).get("record") == "response":
            raise worker_error
        original_write(path, line)

    monkeypatch.setattr(llm_request_logging, "_write_serialized_jsonl_line", failing_response_write)
    model = _FakeModel(response_usage=MessageMetrics(input_tokens=3, output_tokens=2))
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    with capture_logs() as logs:
        if streaming:
            contents = [
                chunk.content
                async for chunk in model.ainvoke_stream(
                    messages=[Message(role="user", content="hello")],
                    assistant_message=Message(role="assistant"),
                    tools=[],
                )
            ]
        else:
            response = await model.ainvoke(
                messages=[Message(role="user", content="hello")],
                assistant_message=Message(role="assistant"),
                tools=[],
            )
            contents = [response.content]

    assert contents == (["ok", "!"] if streaming else ["ok"])
    assert [entry["event"] for entry in logs] == [
        "LLM usage",
        "Failed to emit LLM response telemetry",
    ]


@pytest.mark.asyncio
async def test_llm_usage_telemetry_normalizes_anthropic_cache_tokens(tmp_path: Path) -> None:
    """Anthropic cache tokens should be added to raw input before calculating cache ratios."""
    model = _FakeModel(
        provider="Anthropic",
        response_usage=MessageMetrics(input_tokens=200, cache_read_tokens=800, cache_write_tokens=100),
    )
    with capture_logs() as logs:
        install_llm_request_logging(
            model,
            agent_name="claude",
            debug_config=DebugConfig(),
            default_log_dir=tmp_path,
            configured_provider="anthropic",
        )
        await model.ainvoke(
            messages=[Message(role="user", content="hello")],
            assistant_message=Message(role="assistant"),
            tools=[],
        )

    usage_log = logs[0]
    assert usage_log["context_input_tokens"] == 1_100
    assert usage_log["uncached_input_tokens"] == 300
    assert usage_log["cache_read_ratio"] == 0.727273


@pytest.mark.asyncio
async def test_llm_usage_telemetry_reports_missing_provider_metrics(tmp_path: Path) -> None:
    """Completed calls without provider metrics should remain visible in telemetry."""
    model = _FakeModel()
    with capture_logs() as logs:
        install_llm_request_logging(
            model,
            agent_name="default",
            debug_config=DebugConfig(),
            default_log_dir=tmp_path,
        )
        with bind_llm_request_log_context(correlation_id="corr-no-usage"):
            await model.ainvoke(
                messages=[Message(role="user", content="hello")],
                assistant_message=Message(role="assistant"),
                tools=[],
            )

    assert logs == [
        {
            "event": "LLM usage",
            "log_level": "info",
            "model_name": "default",
            "model_id": "test-model",
            "provider": "OpenAI",
            "usage_available": False,
            "correlation_id": "corr-no-usage",
        },
    ]


@pytest.mark.asyncio
async def test_llm_usage_telemetry_does_not_double_count_invoke_via_stream(tmp_path: Path) -> None:
    """A model whose invoke method consumes its stream should emit one usage event."""

    @dataclass
    class _InvokeViaStreamModel(_FakeModel):
        async def ainvoke(self, *args: object, **kwargs: object) -> ModelResponse:
            response = ModelResponse(content="")
            async for chunk in self.ainvoke_stream(*args, **kwargs):
                response.content = f"{response.content or ''}{chunk.content or ''}"
                if chunk.response_usage is not None:
                    response.response_usage = chunk.response_usage
            return response

    model = _InvokeViaStreamModel(response_usage=MessageMetrics(input_tokens=100, cache_read_tokens=80))
    with capture_logs() as logs:
        install_llm_request_logging(
            model,
            agent_name="default",
            debug_config=DebugConfig(),
            default_log_dir=tmp_path,
        )
        response = await model.ainvoke(
            messages=[Message(role="user", content="hello")],
            assistant_message=Message(role="assistant"),
            tools=[],
        )

    assert response.content == "ok!"
    assert [entry["event"] for entry in logs] == ["LLM usage"]
    assert logs[0]["cache_read_ratio"] == 0.8


@pytest.mark.asyncio
async def test_llm_usage_telemetry_counts_call_while_stream_is_paused(tmp_path: Path) -> None:
    """A real same-model call between stream pulls should emit its own usage event."""
    model = _FakeModel(response_usage=MessageMetrics(input_tokens=100, cache_read_tokens=80))
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(),
        default_log_dir=tmp_path,
    )

    with capture_logs() as logs:
        stream = model.ainvoke_stream(
            messages=[Message(role="user", content="stream")],
            assistant_message=Message(role="assistant"),
            tools=[],
        )
        first_chunk = await anext(stream)
        nested_response = await model.ainvoke(
            messages=[Message(role="user", content="nested")],
            assistant_message=Message(role="assistant"),
            tools=[],
        )
        remaining_chunks = [chunk async for chunk in stream]

    assert first_chunk.content == "ok"
    assert nested_response.content == "ok"
    assert [chunk.content for chunk in remaining_chunks] == ["!"]
    assert [entry["event"] for entry in logs] == ["LLM usage", "LLM usage"]


@pytest.mark.asyncio
async def test_llm_response_usage_record_written_and_linked(tmp_path: Path) -> None:
    """A provider response with usage should append a response record joined by request_log_id."""
    model = _FakeModel(
        response_usage=MessageMetrics(input_tokens=5, output_tokens=7, cache_read_tokens=100, cache_write_tokens=20),
    )
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    with bind_llm_request_log_context(agent_id="assistant", session_id="session-1", correlation_id="corr-9"):
        await model.ainvoke(
            messages=[Message(role="user", content="hello")],
            assistant_message=Message(role="assistant"),
            tools=[],
        )

    request_entry, response_entry = _read_log_entries(tmp_path)
    assert request_entry["request_log_id"]
    assert "record" not in request_entry
    assert response_entry["record"] == "response"
    assert response_entry["request_log_id"] == request_entry["request_log_id"]
    assert response_entry["agent_id"] == "default"
    assert response_entry["model_id"] == "test-model"
    assert response_entry["correlation_id"] == "corr-9"
    assert response_entry["usage"] == {
        "input_tokens": 5,
        "output_tokens": 7,
        "cache_read_tokens": 100,
        "cache_write_tokens": 20,
    }


@pytest.mark.asyncio
async def test_llm_response_usage_record_uses_final_stream_chunk(tmp_path: Path) -> None:
    """Streaming should record the usage carried by the last usage-bearing chunk."""
    model = _FakeModel(response_usage=MessageMetrics(input_tokens=3, cache_read_tokens=42))
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    stream = model.ainvoke_stream(
        messages=[Message(role="user", content="hello")],
        assistant_message=Message(role="assistant"),
        tools=[],
    )
    assert [chunk.content async for chunk in stream] == ["ok", "!"]

    request_entry, response_entry = _read_log_entries(tmp_path)
    assert response_entry["record"] == "response"
    assert response_entry["request_log_id"] == request_entry["request_log_id"]
    assert response_entry["usage"]["cache_read_tokens"] == 42
    assert response_entry["usage"]["output_tokens"] == 0


@pytest.mark.asyncio
async def test_llm_response_record_skipped_without_usage(tmp_path: Path) -> None:
    """Responses without usage metrics should not produce response records."""
    model = _FakeModel()
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    await model.ainvoke(
        messages=[Message(role="user", content="hello")],
        assistant_message=Message(role="assistant"),
        tools=[],
    )

    entries = _read_log_entries(tmp_path)
    assert len(entries) == 1
    assert "record" not in entries[0]


@pytest.mark.asyncio
async def test_llm_response_usage_recorded_when_stream_is_abandoned(tmp_path: Path) -> None:
    """Usage seen before an early aclose() must still produce a response record."""

    @dataclass
    class _EarlyUsageModel(_FakeModel):
        async def ainvoke_stream(self, *_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
            yield ModelResponse(content="ok", response_usage=MessageMetrics(input_tokens=3, cache_read_tokens=42))
            yield ModelResponse(content="never consumed")

    model = _EarlyUsageModel()
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    stream = model.ainvoke_stream(
        messages=[Message(role="user", content="hello")],
        assistant_message=Message(role="assistant"),
        tools=[],
    )
    first_chunk = await anext(stream)
    assert first_chunk.content == "ok"
    await stream.aclose()

    request_entry, response_entry = _read_log_entries(tmp_path)
    assert response_entry["record"] == "response"
    assert response_entry["request_log_id"] == request_entry["request_log_id"]
    assert response_entry["usage"]["cache_read_tokens"] == 42


@pytest.mark.asyncio
async def test_llm_response_record_reuses_the_request_records_file(tmp_path: Path) -> None:
    """The response record must land in the request record's daily file, not the current day's."""
    request_day_file = tmp_path / "llm-requests-2026-01-01.jsonl"
    await _write_llm_response_log(
        model=_FakeModel(),
        agent_name="default",
        request_log_ref=_RequestLogRef(request_log_id="req-1", log_path=request_day_file),
        usage=MessageMetrics(input_tokens=1, cache_read_tokens=5),
        request_context={},
    )

    entries = [json.loads(line) for line in request_day_file.read_text(encoding="utf-8").splitlines()]
    assert entries[0]["record"] == "response"
    assert entries[0]["request_log_id"] == "req-1"
