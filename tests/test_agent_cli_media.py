"""Actual Bash results retain hidden tool media and bounded, truthful receipts."""

# ruff: noqa: ANN001, ANN002, ANN003, ANN202, ARG001, D103
from __future__ import annotations

import asyncio
import json
from contextvars import Context, ContextVar
from dataclasses import replace
from uuid import uuid4

import pytest
from agno.media import Audio, File, Image
from agno.tools.function import Function, ToolResult
from agno.tools.toolkit import Toolkit

from mindroom.agent_cli import turn
from mindroom.agent_cli.bash import MinimalBashTools
from mindroom.agent_cli.events import stream_cli_events
from mindroom.agent_cli.json_io import MAX_ENVELOPE_BYTES, canonical_json
from mindroom.agent_cli.lifetime import response_cli_lifetime
from mindroom.agent_cli.projection import project_cli_result
from mindroom.agent_cli.protocol import ContextReadOperation, ToolCallOperation, ToolCallReceipt, ToolDescribeOperation
from mindroom.agent_cli.session import CliTurnOwner
from mindroom.agent_cli.shell_contract import AgentCliShellEnv
from mindroom.agent_cli.turn import LiveTurnTools
from mindroom.agent_loop.model_loop import LoopHooks
from mindroom.agent_loop.run import run_agent
from mindroom.agent_loop.spec import AgentSpec
from mindroom.ai import _process_stream_events, _StreamingAttemptState, collect_streamed_response_content
from mindroom.attachments.media import resolve_scoped_attachments
from mindroom.llm.events import ToolCompleted, ToolStarted
from mindroom.llm.messages import Message
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.events import CollectedStreamPresentation, deserialize_tool_trace, serialize_tool_trace
from mindroom.tool_system.output_files import ToolOutputFilePolicy
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context
from mindroom.tool_system.tool_access import ToolKey
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.minimal_agent_fixtures import cli_window
from tests.test_agent_tool_calls import _catalog, _toolkit


async def _run(tmp_path, result, *, policy=None):  # noqa: C901

    hooks = []

    async def produce() -> ToolResult:
        return result

    async def hook(call, call_next):
        hooks.append((call.function.name, "before"))
        value = await call_next(call.arguments)
        hooks.append((call.function.name, "after"))
        return value

    async def run_shell_command(args: str, timeout: int = 30, tail: int = 100) -> str:  # noqa: ASYNC109
        # HTTP arrives in a separate task without the provider stream's context.
        receipts.append(
            await asyncio.create_task(
                owner.operation(
                    window=cli_window(),
                    operation=ToolCallOperation(
                        operation="tools.call",
                        call_id=uuid4(),
                        toolkit="media",
                        function="produce",
                    ),
                ),
                context=Context(),
            ),
        )
        return "shell done"

    catalog = await _catalog(
        tmp_path,
        [_toolkit("shell", run_shell_command), _toolkit("media", produce)],
        middleware=(hook,),
    )
    catalog.runtime_context = replace(catalog.runtime_context, storage_path=tmp_path / "storage")
    catalog.store = create_state_storage("helper", tmp_path)
    receipts = []

    async def authorize(key, arguments):
        pass

    (tmp_path / "workspace").mkdir(exist_ok=True)
    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run"),
        catalog=catalog,
        authorize=authorize,
        output_file_policy=policy or ToolOutputFilePolicy(tmp_path / "workspace"),
    )
    owner.shell_env = AgentCliShellEnv("http://127.0.0.1:9", "test-grant")
    spec = AgentSpec(
        id="helper",
        name="Helper",
        model=ScriptedModel([calls(tool_call("bash", "outer", command="generate")), answer("done")]),
        system_message=None,
        toolkits=(adapt_toolkit(MinimalBashTools(execute=owner.execute_bash)),),
    )
    finished = False

    async def source():
        nonlocal finished
        async with response_cli_lifetime():
            run = run_agent(
                spec,
                [Message(role="user", content="generate")],
                ctx=catalog.run,
                hooks=LoopHooks(before_dispatch=owner.checkpoint.capture),
            )
            async for event in run:
                if isinstance(event, ToolCompleted):
                    finished = True
                yield event

    async def watched():
        async for event in stream_cli_events(source()):
            if isinstance(event, ToolStarted) and event.tool.tool_name == "produce":
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
    record = state.terminal.record
    assert record is not None
    return owner, record.messages, receipt


@pytest.mark.asyncio
async def test_real_bash_carries_media_and_scoped_attachment_references(tmp_path) -> None:
    generated = ToolResult(
        content="generated",
        images=[Image(content=b"image", mime_type="image/png")],
        audios=[Audio(content=b"audio", mime_type="audio/wav", format="wav")],
        files=[File(content=b"document", mime_type="application/pdf", filename="result.pdf")],
    )
    owner, messages, receipt = await _run(tmp_path, generated)
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
        # The media reach the model once, on the message after the Bash result.
        assert [bool(message.images and message.audio and message.files) for message in messages[-3:]] == [
            False,
            True,
            False,
        ]
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
    owner, messages, receipt = await _run(
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
        assert any(
            message.images and message.images[0].url == "https://example.test/generated.png" for message in messages
        )
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
    catalog = await _catalog(tmp_path, [adapt_toolkit(Toolkit(name="big", tools=[function]))])

    async def authorize(key, arguments):
        pass

    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run"),
        catalog=catalog,
        authorize=authorize,
    )
    try:
        async with owner._window("outer"):
            response = await owner.operation(
                window="outer",
                operation=ToolDescribeOperation(operation="tools.describe", toolkit="big", function="huge"),
            )
        assert len(json.dumps(response).encode()) < 32768
        name = response["context"]["name"]
        parts = []
        offset = 0
        while offset is not None:
            page = await owner.operation(
                window=None,
                operation=ContextReadOperation(operation="context.read", name=name, offset=offset),
            )
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
    owner, messages, receipt = await _run(
        tmp_path,
        ToolResult(
            content="completed effect",
            images=[Image(url="https://example.test/output.png")],
        ),
    )
    try:
        assert receipt["status"] == "completed"
        assert "projection" in str(receipt["outcome"])
        assert any(message.images for message in messages)
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
        return await _catalog(tmp_path, [_toolkit("state", inspect_scope)])

    catalog = await catalog_for_window()
    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run"),
        catalog=catalog,
        authorize=authorize,
    )

    async def submit(window: str):
        return await asyncio.create_task(
            owner.operation(
                window=window,
                operation=ToolCallOperation(
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
                    admitted = await submit(label)
            finally:
                tag.reset(token)
            assert (await owner.get_call(admitted["call_id"]))["outcome"] == label
        assert tag.get() == "unbound"
    finally:
        await owner.close()


@pytest.mark.parametrize(
    "result",
    [
        # Quotes double when JSON-escaped, so this alone would overflow the 64 KiB receipt envelope.
        '"' * 40_000,
        # Halving stops just under the budget, so the notice must count toward it too.
        "x" * 32_700,
    ],
    ids=["escaped", "notice-at-budget"],
)
def test_large_result_without_workspace_is_shortened_to_fit_its_receipt(result: str) -> None:
    """With nowhere to save the full output, an oversized result still reaches the caller, shortened."""
    projected = project_cli_result(result, None, "run_shell_command")

    assert isinstance(projected, str)
    assert projected.startswith(result[:1000])
    assert projected.endswith("Call the tool directly for all of it.]")
    assert len(json.dumps(projected, ensure_ascii=False).encode()) <= 16 * 1024
    receipt = ToolCallReceipt(
        call_id=uuid4(),
        toolkit="shell",
        function="run_shell_command",
        status="completed",
        outcome=projected,
    )
    assert len(canonical_json(receipt.model_dump(mode="json")).encode()) <= MAX_ENVELOPE_BYTES
    assert project_cli_result("small", None, "run_shell_command") == "small"
