"""Actual Bash results retain hidden tool media and bounded, truthful receipts."""

# ruff: noqa: ANN001, ANN002, ANN003, ANN202, ARG001, ARG002, D103, PLR0915
from __future__ import annotations

import asyncio
import json
from contextvars import Context, ContextVar
from dataclasses import replace
from uuid import uuid4

import pytest
from agno.media import Audio, File, Image
from agno.models.message import Message
from agno.run.agent import ToolCallCompletedEvent, ToolCallStartedEvent
from agno.tools.function import Function, ToolResult
from agno.tools.toolkit import Toolkit

from mindroom.agent_cli import turn
from mindroom.agent_cli.bash import MinimalBashTools
from mindroom.agent_cli.events import stream_cli_events
from mindroom.agent_cli.json_io import MAX_ENVELOPE_BYTES, canonical_json
from mindroom.agent_cli.lifetime import response_cli_lifetime
from mindroom.agent_cli.protocol import ContextReadOperation, ToolCallOperation, ToolDescribeOperation
from mindroom.agent_cli.session import CliTurnOwner
from mindroom.agent_cli.turn import LiveTurnTools
from mindroom.agent_storage import create_state_storage
from mindroom.agno_compat_prepared_tools import prepare_agent_tools
from mindroom.ai import _process_stream_events, _StreamingAttemptState, collect_streamed_response_content
from mindroom.attachment_media import resolve_scoped_attachments
from mindroom.tool_system.events import CollectedStreamPresentation, deserialize_tool_trace, serialize_tool_trace
from mindroom.tool_system.output_files import ToolOutputFilePolicy
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context
from mindroom.tool_system.tool_access import ToolKey
from tests.test_agent_tool_calls import _catalog


async def _run(tmp_path, result, *, policy=None):  # noqa: C901

    hooks = []

    async def produce() -> ToolResult:
        return result

    async def hook(name, function_call, arguments):
        hooks.append((name, "before"))
        value = await function_call(**arguments)
        hooks.append((name, "after"))
        return value

    async def run_shell_command(args: str, timeout: int = 30, tail: int = 100) -> str:  # noqa: ASYNC109
        pytest.fail("ordinary worker")

    media = Toolkit(name="media", tools=[produce])
    media.get_async_functions()["produce"].tool_hooks = [hook]
    shell = Toolkit(name="shell", tools=[run_shell_command])
    shell.get_async_functions()["run_shell_command"].tool_hooks = [hook]
    catalog = await _catalog(tmp_path, [shell, media])
    catalog.runtime_context = replace(catalog.runtime_context, storage_path=tmp_path / "storage")
    catalog.run_response.agent_id = "helper"
    catalog.agent.db = create_state_storage("helper", tmp_path, subdir="sessions", session_table="sessions")
    receipts = []

    async def authorize(key, arguments):
        pass

    class Worker:
        async def invoke_shell(self, name, arguments):
            # HTTP arrives in a separate task without the provider stream's context.
            receipts.append(
                await asyncio.create_task(
                    owner.operation(
                        ToolCallOperation(operation="tools.call", call_id=uuid4(), toolkit="media", function="produce"),
                    ),
                    context=Context(),
                ),
            )
            return "worker done"

    (tmp_path / "workspace").mkdir(exist_ok=True)
    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run", "worker"),
        catalog=catalog,
        worker=Worker(),
        authorize=authorize,
        output_file_policy=policy or ToolOutputFilePolicy(tmp_path / "workspace"),
    )
    facade = MinimalBashTools(execute=owner.execute_bash)
    function = prepare_agent_tools(
        catalog.agent,
        processed_tools=[facade],
        run_response=catalog.run_response,
        run_context=catalog.run_context,
        session=catalog.session,
    )[0]
    message = Message(
        role="assistant",
        tool_calls=[
            {
                "id": "outer",
                "type": "function",
                "function": {"name": "bash", "arguments": '{"command":"generate"}'},
            },
        ],
    )
    messages = [message]
    events = []
    finished = False

    async def source():
        nonlocal finished
        async with response_cli_lifetime() as lifetime:
            lifetime.bind_provider(owner.checkpoint, function)
            calls = catalog.agent.model.get_function_calls_to_run(message, messages, {"bash": function})
            async for event in catalog.agent.model.arun_function_calls(calls, messages):
                events.append(event)
                for tool in event.tool_executions or []:
                    if event.event == "ToolCallStarted":
                        yield ToolCallStartedEvent(tool=tool)
                    elif event.event == "ToolCallCompleted":
                        finished = True
                        yield ToolCallCompletedEvent(tool=tool)

    async def watched():
        async for event in stream_cli_events(source()):
            if isinstance(event, ToolCallStartedEvent) and event.tool.tool_name == "produce":
                assert not finished
            yield event

    state = _StreamingAttemptState()
    async with asyncio.timeout(5):
        text, trace = await collect_streamed_response_content(
            _process_stream_events(watched(), state=state, show_tool_calls=True, agent_name="helper"),
            presentation=CollectedStreamPresentation(show_tool_calls=True),
        )
    receipt = await owner.get_call(receipts[0]["call_id"])
    assert hooks == [
        ("run_shell_command", "before"),
        ("produce", "before"),
        ("produce", "after"),
        ("run_shell_command", "after"),
    ]
    assert [entry.tool_name for entry in trace] == ["bash", "produce"]
    assert all(entry.type == "tool_call_completed" for entry in trace)
    assert "⏳" not in text
    assert trace[1].parent_bash_call_id == "outer"
    assert trace[1].toolkit_name == "media"
    assert [tool.tool_name for tool in state.completed_tool_executions] == ["bash"]

    stored = serialize_tool_trace(trace, include_internal=True)
    assert deserialize_tool_trace(stored)[1].parent_bash_call_id == "outer"
    assert "parent_bash_call_id" not in serialize_tool_trace(trace)[1]
    return owner, messages, events, receipt


@pytest.mark.asyncio
async def test_real_bash_carries_media_and_scoped_attachment_references(tmp_path) -> None:
    generated = ToolResult(
        content="generated",
        images=[Image(content=b"image", mime_type="image/png")],
        audios=[Audio(content=b"audio", mime_type="audio/wav", format="wav")],
        files=[File(content=b"document", mime_type="application/pdf", filename="result.pdf")],
    )
    owner, messages, events, receipt = await _run(tmp_path, generated)
    try:
        assert receipt["status"] == "completed"
        refs = receipt["attachments"]
        records = resolve_scoped_attachments(
            tmp_path / "storage",
            [item["attachment_id"] for item in refs],
            room_id="!room:test",
            thread_id="$thread",
        )
        assert {record.mime_type for record in records} == {"image/png", "audio/wav", "application/pdf"}
        assert all(record.sender == "@alice:test" for record in records)
        assert all(record.source_event_id == receipt["call_id"] for record in records)
        assert (
            len(
                resolve_scoped_attachments(
                    tmp_path / "storage",
                    [item["attachment_id"] for item in refs],
                    room_id="!other:test",
                    thread_id="$thread",
                ),
            )
            == 0
        )
        assert any(event.images and event.audios and event.files for event in events)
        assert any(message.images and message.audio and message.files for message in messages)
        assert [message.tool_call_id for message in messages if message.role == "tool"] == ["outer"]
        assert all(tool["function"]["name"] == "bash" for message in messages for tool in message.tool_calls or [])
    finally:
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("content", ["complete output\n" * 10000, "\x01" * 12000], ids=["large-text", "json-escapes"])
async def test_completed_large_media_result_uses_workspace_artifact(tmp_path, failure, content) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    policy = ToolOutputFilePolicy(root, max_bytes=8 if failure else 1000000, auto_save_threshold_bytes=1000000)
    owner, _messages, events, receipt = await _run(
        tmp_path,
        ToolResult(
            content=content,
            images=[Image(url="https://example.test/generated.png", mime_type="image/png")],
        ),
        policy=policy,
    )
    try:
        assert receipt["status"] == "completed"
        assert len(canonical_json(receipt).encode()) <= MAX_ENVELOPE_BYTES
        if failure:
            assert "error" in str(receipt["outcome"]).lower()
        else:
            outcome = receipt["outcome"]["mindroom_tool_output"]
            # Saved-file previews must also fit the CLI's serialized output budget.
            assert len(canonical_json(receipt["outcome"]).encode()) <= 16 * 1024
            assert not outcome["path"].startswith("/")
            assert (root / outcome["path"]).read_text() == content
            consumer = await asyncio.create_subprocess_exec(
                "cat",
                outcome["path"],
                cwd=root,
                stdout=asyncio.subprocess.PIPE,
            )
            stdout, _ = await consumer.communicate()
            assert consumer.returncode == 0
            assert stdout == content.encode()
        assert any(event.images and event.images[0].url == "https://example.test/generated.png" for event in events)
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_cli_event_stream_close_cancels_producer() -> None:

    stopped = asyncio.Event()

    async def source():
        try:
            yield "started"
            await asyncio.Event().wait()
        finally:
            stopped.set()

    stream = stream_cli_events(source())
    assert await anext(stream) == "started"
    await stream.aclose()
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_single_oversized_schema_is_retrievable_through_scoped_context(tmp_path) -> None:

    async def huge(**kwargs) -> str:
        return "unused"

    properties = {f"property_{index}": {"type": "string", "description": "value " * 60} for index in range(400)}
    function = Function(
        name="huge",
        entrypoint=huge,
        skip_entrypoint_processing=True,
        parameters={"type": "object", "properties": properties},
    )
    catalog = await _catalog(tmp_path, [Toolkit(name="big", tools=[function])])

    async def authorize(key, arguments):
        pass

    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run", "worker"),
        catalog=catalog,
        worker=None,
        authorize=authorize,
    )
    try:
        async with owner._window("outer"):
            response = await owner.operation(
                ToolDescribeOperation(operation="tools.describe", toolkit="big", function="huge"),
            )
        assert len(json.dumps(response).encode()) < 32768
        name = response["context"]["name"]
        parts = []
        offset = 0
        while offset is not None:
            page = await owner.operation(ContextReadOperation(operation="context.read", name=name, offset=offset))
            parts.append(page["text"])
            offset = page["next_offset"]
        descriptor = json.loads("".join(parts))
        assert descriptor["input_schema"]["properties"]["property_399"] == properties["property_399"]
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_projection_exception_keeps_completed_status_and_media(tmp_path, monkeypatch) -> None:

    def fail_projection(*args):
        msg = "artifact unavailable"
        raise OSError(msg)

    monkeypatch.setattr(turn, "project_cli_result", fail_projection)
    owner, _messages, events, receipt = await _run(
        tmp_path,
        ToolResult(
            content="completed effect",
            images=[Image(url="https://example.test/output.png")],
        ),
    )
    try:
        assert receipt["status"] == "completed"
        assert "projection" in str(receipt["outcome"])
        assert any(event.images for event in events)
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_admission_uses_current_window_context_after_rebuild(tmp_path) -> None:

    tag = ContextVar("window-tag", default="unbound")

    async def inspect_scope() -> str:
        return tag.get()

    async def authorize(key, arguments):
        assert key == ToolKey("state", "inspect_scope")

    async def catalog_for_window():
        return await _catalog(tmp_path, [Toolkit(name="state", tools=[inspect_scope])])

    catalog = await catalog_for_window()
    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run", "worker"),
        catalog=catalog,
        worker=None,
        authorize=authorize,
    )

    async def submit():
        return await asyncio.create_task(
            owner.operation(
                ToolCallOperation(
                    operation="tools.call",
                    call_id=uuid4(),
                    toolkit="state",
                    function="inspect_scope",
                ),
            ),
            context=Context(),
        )

    try:
        for index, label in enumerate(("first", "rebuilt")):
            if index:
                await owner.retire_binding()
                owner.bind_catalog(await catalog_for_window())
            token = tag.set(label)
            try:
                async with owner._window(label):
                    admitted = await submit()
            finally:
                tag.reset(token)
            assert (await owner.get_call(admitted["call_id"]))["outcome"] == label
        assert tag.get() == "unbound"
    finally:
        await owner.close()
