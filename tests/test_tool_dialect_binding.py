"""Seam tests for binding a tool dialect to a provider model inside a real Agno agent run."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from agno.agent import Agent
from agno.db.base import SessionType
from agno.db.sqlite import SqliteDb
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.base import RunStatus
from agno.tools.toolkit import Toolkit
from openai import AsyncOpenAI

from mindroom.agents import set_toolkit_owner
from mindroom.openai_models import MindRoomOpenAIChat, MindRoomOpenAIResponses
from mindroom.tool_dialects.agno_compat_model import install_tool_dialect
from mindroom.tool_dialects.types import MINDROOM_WIRE_KEY, DialectArgumentError, ToolDialect, WireFunction
from mindroom.tool_system.tool_access import ToolKey

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from agno.run.agent import RunOutput


def _require(arguments: dict[str, Any], name: str, tool: str) -> Any:  # noqa: ANN401
    if name not in arguments:
        msg = f"{tool} requires {name}"
        raise DialectArgumentError(msg)
    return arguments[name]


_CLAUDE_TOY = ToolDialect(
    name="claude",
    functions=(
        WireFunction(
            key=ToolKey("shell", "run_shell_command"),
            wire_name="Run",
            description="Run a command.",
            parameters={"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
            to_canonical=lambda arguments: {"args": _require(arguments, "cmd", "Run")},
            to_wire=lambda arguments: {"cmd": arguments.get("args")},
            render_result=lambda text: text.replace("ran ", "Run finished: "),
        ),
        WireFunction(
            key=ToolKey("coding", "read_file"),
            wire_name="Read",
            description="Read a file.",
            parameters={"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]},
            to_canonical=lambda arguments: {"path": _require(arguments, "file_path", "Read")},
            to_wire=lambda arguments: {"file_path": arguments.get("path")},
        ),
    ),
)
_CODEX_TOY = ToolDialect(
    name="codex",
    functions=(
        WireFunction(
            key=ToolKey("shell", "run_shell_command"),
            wire_name="Exec",
            description="Execute a command.",
            parameters={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
            to_canonical=lambda arguments: {"args": _require(arguments, "command", "Exec")},
            to_wire=lambda arguments: {"command": arguments.get("args")},
        ),
    ),
)
_ANSWER = {
    "type": "message",
    "id": "msg_answer",
    "role": "assistant",
    "status": "completed",
    "content": [{"type": "output_text", "text": "Done", "annotations": []}],
}


def _function_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
        "status": "completed",
    }


def _response(output: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": f"resp_{len(output)}",
        "object": "response",
        "created_at": 1,
        "model": "gpt-6-astra",
        "status": "completed",
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 1,
            "total_tokens": 11,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
        "error": None,
        "incomplete_details": None,
    }


class _Provider:
    """Responses endpoint that answers each request with the next scripted output."""

    def __init__(self, *outputs: list[dict[str, Any]]) -> None:
        self.outputs = list(outputs)
        self.requests: list[dict[str, Any]] = []

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        output = self.outputs.pop(0) if self.outputs else [_ANSWER]
        return httpx.Response(200, json=_response(output))

    def tool_names(self, index: int = 0) -> list[str]:
        return [tool["name"] for tool in self.requests[index]["tools"]]

    def items(self, index: int, kind: str) -> list[dict[str, Any]]:
        return [item for item in self.requests[index]["input"] if item.get("type") == kind]


def _toolkit(name: str, *functions: Callable[..., str], confirm: tuple[str, ...] = ()) -> Toolkit:
    toolkit = Toolkit(name=name, tools=list(functions), requires_confirmation_tools=list(confirm))
    set_toolkit_owner(toolkit, name)
    return toolkit


def _shell(executions: list[str], *, confirm: bool = False) -> Toolkit:
    def run_shell_command(args: str) -> str:
        """Run a shell command."""
        executions.append(args)
        return f"ran {args}"

    return _toolkit("shell", run_shell_command, confirm=("run_shell_command",) if confirm else ())


def _coding(executions: list[str]) -> Toolkit:
    def read_file(path: str) -> str:
        """Read a file."""
        executions.append(f"read {path}")
        return f"contents of {path}"

    def ls() -> str:
        """List files."""
        executions.append("ls")
        return "a.txt"

    return _toolkit("coding", read_file, ls)


async def _run(
    provider: _Provider,
    toolkits: list[Toolkit],
    dialect: ToolDialect | None,
    *,
    db: SqliteDb | None = None,
    history: bool = False,
) -> RunOutput:
    async with AsyncOpenAI(
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(provider.respond)),
    ) as client:
        model = MindRoomOpenAIResponses(id="gpt-6-astra", async_client=client, store=False)
        if dialect is not None:
            install_tool_dialect(model, dialect)
        agent = Agent(
            model=model,
            tools=toolkits,
            telemetry=False,
            db=db,
            session_id="session",
            add_history_to_context=history,
        )
        return await agent.arun("Go.")


@pytest.mark.asyncio
async def test_request_carries_wire_definitions() -> None:
    """The provider sees the dialect's names, descriptions, and schemas."""
    provider = _Provider([_ANSWER])

    await _run(provider, [_shell([]), _coding([])], _CLAUDE_TOY)

    assert sorted(provider.tool_names()) == ["Read", "Run", "ls"]
    run = next(tool for tool in provider.requests[0]["tools"] if tool["name"] == "Run")
    assert run["description"] == "Run a command."
    assert run["parameters"]["required"] == ["cmd"]


@pytest.mark.asyncio
async def test_wire_call_dispatches_canonical_function() -> None:
    """A wire call runs the canonical function with canonical arguments, and its result renders for the wire."""
    executions: list[str] = []
    provider = _Provider([_function_call("c1", "Run", '{"cmd": "ls"}')])

    result = await _run(provider, [_shell(executions)], _CLAUDE_TOY)

    assert executions == ["ls"]
    assert [(tool.tool_name, tool.tool_args) for tool in result.tools or []] == [("run_shell_command", {"args": "ls"})]
    assert provider.items(1, "function_call_output") == [
        {"type": "function_call_output", "call_id": "c1", "output": "Run finished: ls"},
    ]


@pytest.mark.asyncio
async def test_second_request_replays_same_dialect_call_verbatim() -> None:
    """The follow-up request repeats the model's call exactly as it was sent."""
    provider = _Provider([_function_call("c1", "Run", '{"cmd":   "ls", "note": "kept"}')])

    await _run(provider, [_shell([])], _CLAUDE_TOY)

    [call] = provider.items(1, "function_call")
    assert (call["name"], call["arguments"]) == ("Run", '{"cmd":   "ls", "note": "kept"}')
    assert MINDROOM_WIRE_KEY not in json.dumps(provider.requests[1])


@pytest.mark.asyncio
async def test_session_reload_keeps_mindroom_wire(tmp_path: Path) -> None:
    """Stored history keeps the canonical call and its wire record across a database reload."""
    db = SqliteDb(db_file=str(tmp_path / "sessions.db"))
    provider = _Provider([_function_call("c1", "Run", '{"cmd": "ls", "note": "kept"}')])

    await _run(provider, [_shell([])], _CLAUDE_TOY, db=db)

    session = SqliteDb(db_file=str(tmp_path / "sessions.db")).get_session(
        session_id="session",
        session_type=SessionType.AGENT,
    )
    assert session is not None
    [call] = [call for message in session.runs[0].messages or [] for call in message.tool_calls or []]
    assert call["function"] == {"name": "run_shell_command", "arguments": json.dumps({"args": "ls"})}
    assert call[MINDROOM_WIRE_KEY]["dialect"] == "claude"
    assert call[MINDROOM_WIRE_KEY]["name"] == "Run"


@pytest.mark.asyncio
async def test_switching_dialect_rerenders_history(tmp_path: Path) -> None:
    """A thread that moves to another model family sees its earlier calls in the new family's shape."""
    db = SqliteDb(db_file=str(tmp_path / "sessions.db"))
    await _run(_Provider([_function_call("c1", "Run", '{"cmd": "ls"}')]), [_shell([])], _CLAUDE_TOY, db=db)
    provider = _Provider([_ANSWER])

    await _run(provider, [_shell([])], _CODEX_TOY, db=db, history=True)

    assert provider.tool_names() == ["Exec"]
    [call] = provider.items(0, "function_call")
    assert (call["name"], json.loads(call["arguments"])) == ("Exec", {"command": "ls"})


@pytest.mark.asyncio
async def test_mixed_batch_translates_each_call_and_keeps_mcp_call() -> None:
    """Each call in one batch translates on its own, ids keep their order, and foreign functions stay untouched."""
    executions: list[str] = []

    def run_shell_command(args: str) -> str:
        """An MCP server's own command tool."""
        executions.append(f"mcp {args}")
        return "mcp ran"

    provider = _Provider(
        [
            _function_call("c1", "Read", '{"file_path": "a.txt"}'),
            _function_call("c2", "ls", "{}"),
            _function_call("c3", "run_shell_command", '{"args": "x"}'),
        ],
    )

    await _run(provider, [_coding(executions), _toolkit("mcp_server", run_shell_command)], _CLAUDE_TOY)

    assert sorted(provider.tool_names()) == ["Read", "ls", "run_shell_command"]
    assert sorted(executions) == ["ls", "mcp x", "read a.txt"]
    assert [(item["call_id"], item["name"]) for item in provider.items(1, "function_call")] == [
        ("c1", "Read"),
        ("c2", "ls"),
        ("c3", "run_shell_command"),
    ]
    assert [item["call_id"] for item in provider.items(1, "function_call_output")] == ["c1", "c2", "c3"]


@pytest.mark.asyncio
async def test_untranslatable_call_is_answered_with_error() -> None:
    """A wire call with unusable arguments gets a tool error and runs nothing."""
    executions: list[str] = []
    provider = _Provider([_function_call("c1", "Run", '{"command": "ls"}')])

    await _run(provider, [_shell(executions)], _CLAUDE_TOY)

    assert executions == []
    assert provider.items(1, "function_call_output") == [
        {"type": "function_call_output", "call_id": "c1", "output": "Error: Run requires cmd"},
    ]


@pytest.mark.asyncio
async def test_approval_pause_shows_canonical_call_and_resume_runs_it(tmp_path: Path) -> None:
    """An approval pause holds the canonical call, which still runs after the thread changes models."""
    db = SqliteDb(db_file=str(tmp_path / "sessions.db"))
    executions: list[str] = []
    paused = await _run(
        _Provider([_function_call("c1", "Run", '{"cmd": "ls"}')]),
        [_shell(executions, confirm=True)],
        _CLAUDE_TOY,
        db=db,
    )
    assert paused.status == RunStatus.paused
    [requirement] = paused.requirements or []
    assert requirement.tool_execution is not None
    assert (requirement.tool_execution.tool_name, requirement.tool_execution.tool_args) == (
        "run_shell_command",
        {"args": "ls"},
    )
    requirement.confirm()

    provider = _Provider([_ANSWER])
    async with AsyncOpenAI(
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(provider.respond)),
    ) as client:
        model = MindRoomOpenAIResponses(id="gpt-6-astra", async_client=client, store=False)
        agent = Agent(
            model=model,
            tools=[_shell(executions, confirm=True)],
            telemetry=False,
            db=db,
            session_id="session",
        )
        resumed = await agent.acontinue_run(run_response=paused, requirements=paused.requirements)

    assert resumed.status == RunStatus.completed
    assert executions == ["ls"]
    [call] = provider.items(0, "function_call")
    assert call["name"] == "run_shell_command"


@pytest.mark.asyncio
async def test_mindroom_dialect_request_is_unchanged() -> None:
    """An empty dialect sends exactly what an unbound model sends."""
    bound, unbound = (
        _Provider([_function_call("c1", "run_shell_command", '{"args": "ls"}')]),
        _Provider(
            [_function_call("c1", "run_shell_command", '{"args": "ls"}')],
        ),
    )

    await _run(bound, [_shell([]), _coding([])], ToolDialect(name="mindroom"))
    await _run(unbound, [_shell([]), _coding([])], None)

    assert bound.requests == unbound.requests


def test_overrides_bind_to_deepcopied_model() -> None:
    """Agno deepcopies models, and each copy's overrides must act on that copy."""
    model = MindRoomOpenAIResponses(id="gpt-6-astra", api_key="test")
    install_tool_dialect(model, _CLAUDE_TOY)

    copied = deepcopy(model)

    assert copied._format_tools.__self__ is copied
    assert copied.get_function_calls_to_run.__self__ is copied


@pytest.mark.asyncio
async def test_deepcopied_model_invokes_on_itself() -> None:
    """A deepcopy's provider call runs on the copy, with its own client, not on the original."""
    provider, original_provider = _Provider([_ANSWER]), _Provider([_ANSWER])
    async with (
        AsyncOpenAI(
            api_key="test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(original_provider.respond)),
        ) as original_client,
        AsyncOpenAI(
            api_key="test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(provider.respond)),
        ) as client,
    ):
        model = MindRoomOpenAIResponses(id="gpt-6-astra", async_client=original_client, store=False)
        install_tool_dialect(model, _CLAUDE_TOY)
        copied = deepcopy(model)
        copied.async_client = client

        await copied.ainvoke(
            messages=[Message(role="user", content="Hi.")],
            assistant_message=Message(role="assistant"),
        )

    assert (len(provider.requests), len(original_provider.requests)) == (1, 0)


@pytest.mark.asyncio
async def test_closing_the_stream_closes_the_provider_stream() -> None:
    """A consumer that stops a dialect stream early closes the provider stream before the close returns."""
    closed: list[bool] = []

    async def provider_stream(*_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        try:
            yield ModelResponse(content="a")
            yield ModelResponse(content="b")
        finally:
            closed.append(True)

    model = MindRoomOpenAIResponses(id="gpt-6-astra", api_key="test")
    vars(model)["ainvoke_stream"] = provider_stream
    install_tool_dialect(model, _CLAUDE_TOY)
    stream = model.ainvoke_stream(
        messages=[Message(role="user", content="Hi.")],
        assistant_message=Message(role="assistant"),
    )

    await anext(stream)
    await stream.aclose()

    assert closed == [True]


@pytest.mark.asyncio
async def test_chat_completions_payload_carries_no_wire_record() -> None:
    """Chat Completions sends stored tool calls verbatim, so the wire record must be stripped first."""
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        message: dict[str, Any] = {"role": "assistant", "content": "Done"}
        if len(requests) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "Run", "arguments": '{"cmd": "ls", "note": "kept"}'},
                    },
                ],
            }
        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl-{len(requests)}",
                "object": "chat.completion",
                "created": 1,
                "model": "claude-sonnet-5.5",
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    async with AsyncOpenAI(
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as client:
        model = MindRoomOpenAIChat(id="anthropic/claude-sonnet-5.5", async_client=client)
        install_tool_dialect(model, _CLAUDE_TOY)
        await Agent(model=model, tools=[_shell([])], telemetry=False).arun("Go.")

    [assistant] = [message for message in requests[1]["messages"] if message.get("tool_calls")]
    assert assistant["tool_calls"][0]["function"] == {"name": "Run", "arguments": '{"cmd": "ls", "note": "kept"}'}
    assert MINDROOM_WIRE_KEY not in json.dumps(requests[1])
