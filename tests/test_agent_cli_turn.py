"""Queue admission and canonical nested shell progress in one real live turn."""

# ruff: noqa: D103, ANN001, ANN003, ANN202, ARG001, ARG002, PLR0915, ASYNC110
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Never
from uuid import uuid4

import pytest
from agno.models.message import Message
from agno.tools.function import Function
from agno.tools.toolkit import Toolkit

from mindroom.agent_cli import turn
from mindroom.agent_cli.json_io import canonical_json
from mindroom.agent_cli.lifetime import current_cli_lifetime, response_cli_lifetime
from mindroom.agent_cli.protocol import (
    ContextReadOperation,
    ToolCallOperation,
    ToolCallReceipt,
    ToolDescribeOperation,
    ToolListOperation,
    ToolSearchOperation,
)
from mindroom.agent_cli.session import (
    CliAuthenticationError,
    CliBashWindowRequiredError,
    CliOperationError,
    CliTurnOwner,
    TurnToolRegistry,
)
from mindroom.agent_cli.turn import LiveTurnTools
from mindroom.agent_storage import create_state_storage
from mindroom.api.agent_cli import bind_agent_cli_registry, router
from mindroom.cancellation import request_task_cancel
from mindroom.tool_system.agent_tool_calls import DeferredAgentToolkit
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context
from mindroom.tool_system.tool_access import ToolKey
from tests.test_agent_tool_calls import _catalog

if TYPE_CHECKING:
    from pathlib import Path

    from agno.run import RunContext


@pytest.mark.asyncio
async def test_outer_shell_cli_nested_shell_cli_mutation_and_late_rejection(
    tmp_path: Path,
) -> None:

    hooks = []

    async def run_shell_command(args: str) -> str:
        pytest.fail("ordinary worker fallback")

    async def mutate(run_context: RunContext) -> str:
        run_context.session_state["count"] = run_context.session_state.get("count", 0) + 1
        return "mutated"

    async def hook(name, function_call, arguments):
        hooks.append(("before", arguments["args"]))
        result = await function_call(**arguments)
        hooks.append(("after", arguments["args"]))
        return result

    shell = Toolkit(name="shell", tools=[run_shell_command])
    shell.get_async_functions()["run_shell_command"].tool_hooks = [hook]
    catalog = await _catalog(tmp_path, [shell, Toolkit(name="state", tools=[mutate])])
    catalog.run_response.agent_id = "helper"
    catalog.agent.db = create_state_storage("helper", tmp_path, subdir="sessions", session_table="sessions")

    async def authorize(key, arguments) -> None:
        return None

    async def wait(call_id):
        while (receipt := await owner.get_call(call_id))["status"] in {"queued", "running"}:
            await asyncio.sleep(0.001)
        assert receipt["status"] == "completed", receipt
        return receipt

    class Worker:
        async def invoke_shell(self, name, arguments) -> str:
            assert name == "run_shell_command"
            if arguments["args"] == "outer":
                queued = await owner.operation(
                    ToolCallOperation(
                        operation="tools.call",
                        call_id=uuid4(),
                        toolkit="shell",
                        function="run_shell_command",
                        arguments={"args": "nested"},
                    ),
                )
            else:
                queued = await owner.operation(
                    ToolCallOperation(operation="tools.call", call_id=uuid4(), toolkit="state", function="mutate"),
                )
            assert queued["status"] == "queued"
            settled = await wait(queued["call_id"])
            assert settled["parent_bash_call_id"] == "outer-1"
            return "worker done"

    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run", "worker"),
        catalog=catalog,
        worker=Worker(),
        authorize=authorize,
    )

    async def bash(command: str) -> str:
        return command

    function = Function.from_callable(bash)
    message = Message(
        role="assistant",
        tool_calls=[
            {"id": "outer-1", "type": "function", "function": {"name": "bash", "arguments": '{"command":"outer"}'}},
        ],
    )
    async with response_cli_lifetime() as lifetime:
        lifetime.bind_provider(owner.checkpoint, function)
        calls = catalog.agent.model.get_function_calls_to_run(message, [message], {"bash": function})
        async with asyncio.timeout(3):
            assert (
                await owner.execute_bash(ToolKey("shell", "run_shell_command"), {"args": "outer"}, calls[0])
                == "worker done"
            )
    assert hooks == [("before", "outer"), ("before", "nested"), ("after", "nested"), ("after", "outer")]
    assert catalog.run_context.session_state["count"] == 1
    late = ToolCallOperation(operation="tools.call", call_id=uuid4(), toolkit="state", function="mutate")
    with pytest.raises(CliBashWindowRequiredError, match="active Bash"):
        await owner.operation(late)
    with pytest.raises(CliBashWindowRequiredError, match="active Bash"):
        await owner.operation(ToolDescribeOperation(operation="tools.describe", toolkit="state", function="mutate"))
    with pytest.raises(CliAuthenticationError):
        await owner.get_call(str(late.call_id))
    await owner.close()
    assert catalog.run_context.session_state["count"] == 1


@pytest.mark.asyncio
async def test_response_lifetime_keeps_owner_across_attempts_and_revokes_at_end() -> None:

    events = []

    class Owner:
        def bind_response_task(self, task):
            pass

        def revoke(self) -> None:
            events.append("revoke")

        async def retire_binding(self) -> None:
            events.append("retire")

        async def close(self) -> None:
            events.append("close")

    registry = TurnToolRegistry()
    owner = Owner()
    async with response_cli_lifetime() as lifetime:
        assert current_cli_lifetime() is lifetime
        lifetime.register(owner, registry)
        await lifetime.retire_attempt()
        assert lifetime.owner is owner
        assert events == ["retire"]
    assert current_cli_lifetime() is None
    assert events == ["retire", "close"]


@pytest.mark.asyncio
async def test_write_failure_prevents_worker_and_inner_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    effects = []

    async def run_shell_command(args: str) -> str:
        effects.append("ordinary")
        return args

    catalog = await _catalog(tmp_path, [Toolkit(name="shell", tools=[run_shell_command])])
    catalog.run_response.agent_id = "helper"
    catalog.agent.db = create_state_storage("helper", tmp_path, subdir="sessions", session_table="sessions")

    async def authorize(key, arguments) -> None:
        effects.append("authorize")

    class Worker:
        async def invoke_shell(self, name, arguments) -> str:
            effects.append("worker")
            return "bad"

    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run", "worker"),
        catalog=catalog,
        worker=Worker(),
        authorize=authorize,
    )

    async def bash(command: str) -> str:
        return command

    function = Function.from_callable(bash)
    message = Message(
        role="assistant",
        tool_calls=[
            {"id": "bash-1", "type": "function", "function": {"name": "bash", "arguments": '{"command":"touch"}'}},
        ],
    )

    def fail(**kwargs) -> Never:
        msg = "database unavailable"
        raise OSError(msg)

    monkeypatch.setattr(catalog.agent.db, "upsert_run", fail)
    async with response_cli_lifetime() as lifetime:
        lifetime.bind_provider(owner.checkpoint, function)
        calls = catalog.agent.model.get_function_calls_to_run(message, [message], {"bash": function})
        with pytest.raises(OSError, match="database unavailable"):
            await owner.execute_bash(ToolKey("shell", "run_shell_command"), {"args": "touch"}, calls[0])
    assert effects == []
    await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("approval", [False, True])
async def test_real_cli_call_wait_completes_inside_outer_bash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    approval: bool,
) -> None:

    import uvicorn  # noqa: PLC0415 - keep optional provider/server imports deferred
    from fastapi import FastAPI  # noqa: PLC0415 - keep optional provider/server imports deferred

    async def run_shell_command(args: str) -> str:
        pytest.fail("ordinary worker must remain unused")

    async def mutate(run_context: RunContext) -> str:
        run_context.session_state["http"] = "inside-bash"
        return "completed-via-http"

    catalog = await _catalog(
        tmp_path,
        [Toolkit(name="shell", tools=[run_shell_command]), Toolkit(name="state", tools=[mutate])],
    )
    waiting = asyncio.Event()
    decision = asyncio.Event()

    async def pause(paused):
        waiting.set()
        await decision.wait()
        paused.requirements[0].confirm()
        return paused.requirements

    if approval:
        binding = await catalog.bind(ToolKey("state", "mutate"))
        binding.function.requires_confirmation = True
        catalog.runtime_context = replace(catalog.runtime_context, cli_approval_handler=pause)
    catalog.run_response.agent_id = "helper"
    catalog.agent.db = create_state_storage("helper", tmp_path, subdir="sessions", session_table="sessions")

    async def authorize(key, arguments) -> None:
        return None

    env = os.environ.copy()

    class Worker:
        async def invoke_shell(self, name, arguments) -> str:
            first = await asyncio.to_thread(
                subprocess.run,
                ["mindroom-agent", "tools", "call", "state", "mutate"],
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            assert first.returncode == 3, first.stderr

            receipt = json.loads(first.stdout)
            second = await asyncio.to_thread(
                subprocess.run,
                ["mindroom-agent", "calls", "wait", receipt["call_id"]],
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            assert second.returncode == 0, second.stderr
            assert json.loads(second.stdout)["outcome"] == "completed-via-http"
            return "same-window"

    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run", "worker"),
        catalog=catalog,
        worker=Worker(),
        authorize=authorize,
    )
    registry = TurnToolRegistry()
    registry.register(owner)
    grant = owner.issue(now_ns=time.time_ns(), expires_at_ns=time.time_ns() + 10**12)
    token = tmp_path / "token"
    token.write_text(grant.raw_token)
    app = FastAPI()
    app.include_router(router)
    bind_agent_cli_registry(app, registry)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    env.update(MINDROOM_AGENT_CLI_GATEWAY_URL=f"http://127.0.0.1:{port}", MINDROOM_AGENT_CLI_TOKEN_PATH=str(token))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off", ws="none"))
    server_task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            await asyncio.sleep(0.01)

        async def bash(command: str) -> str:
            return command

        function = Function.from_callable(bash)
        message = Message(
            role="assistant",
            tool_calls=[
                {
                    "id": "bash-http",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"http"}'},
                },
            ],
        )
        async with response_cli_lifetime() as lifetime:
            lifetime.bind_provider(owner.checkpoint, function)
            calls = catalog.agent.model.get_function_calls_to_run(message, [message], {"bash": function})
            async with asyncio.timeout(10):
                bash_task = asyncio.create_task(
                    owner.execute_bash(
                        ToolKey("shell", "run_shell_command"),
                        {"args": "http"},
                        calls[0],
                    ),
                )
                if approval:
                    await waiting.wait()
                    assert not bash_task.done()
                    assert catalog.run_context.session_state == {}
                    decision.set()
                assert await bash_task == "same-window"
        assert catalog.run_context.session_state == {"http": "inside-bash"}
        assert catalog.run_response.messages == []
    finally:
        server.should_exit = True
        await server_task
        sock.close()
        await owner.close()


@pytest.mark.asyncio
async def test_provider_batch_serializes_bash_but_independent_turns_progress(
    tmp_path: Path,
) -> None:

    entered: dict[str, int] = {}
    maximum: dict[str, int] = {}
    both_turns = asyncio.Event()
    workers_started = set()
    order = []
    owners = []

    async def run_shell_command(args: str) -> str:
        pytest.fail("ordinary transport")

    async def authorize(key, arguments):
        return None

    async def bash(command: str) -> str:
        return command

    function = Function.from_callable(bash)

    for name in ("one", "two"):
        catalog = await _catalog(tmp_path / name, [Toolkit(name="shell", tools=[run_shell_command])])
        catalog.run_response.agent_id = "helper"
        catalog.agent.db = create_state_storage("helper", tmp_path / name, subdir="sessions", session_table="sessions")

        class Worker:
            def __init__(self, name) -> None:
                self.name = name

            async def invoke_shell(self, operation, arguments):
                name = self.name
                entered[name] = entered.get(name, 0) + 1
                maximum[name] = max(maximum.get(name, 0), entered[name])
                order.append((name, arguments["args"]))
                workers_started.add(name)
                if len(workers_started) == 2:
                    both_turns.set()
                await both_turns.wait()
                await asyncio.sleep(0)
                entered[name] -= 1
                return arguments["args"]

        owners.append(
            LiveTurnTools(
                CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), name, "run", name),
                catalog=catalog,
                worker=Worker(name),
                authorize=authorize,
            ),
        )

    async def drive(owner):
        message = Message(
            role="assistant",
            tool_calls=[
                {
                    "id": f"{owner.owner.turn_id}-{index}",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"batch"}'},
                }
                for index in range(2)
            ],
        )
        async with response_cli_lifetime() as lifetime:
            lifetime.bind_provider(owner.checkpoint, function)
            calls = owner.catalog.agent.model.get_function_calls_to_run(message, [message], {"bash": function})
            return await asyncio.gather(
                *(
                    owner.execute_bash(ToolKey("shell", "run_shell_command"), {"args": str(index)}, call)
                    for index, call in enumerate(calls)
                ),
            )

    try:
        async with asyncio.timeout(3):
            assert await asyncio.gather(*(drive(owner) for owner in owners)) == [["0", "1"], ["0", "1"]]
        assert maximum == {"one": 1, "two": 1}
        assert [args for name, args in order if name == "one"] == ["0", "1"]
    finally:
        for owner in owners:
            await owner.close()


@pytest.mark.asyncio
async def test_catalog_rebind_retains_worker_and_rejects_between_attempt_calls(
    tmp_path: Path,
) -> None:

    calls = []
    receipts = []

    async def run_shell_command(args: str) -> str:
        pytest.fail("ordinary transport")

    async def change() -> str:
        calls.append("new binding")
        return "new binding"

    async def authorize(key, arguments):
        return None

    class Worker:
        async def invoke_shell(self, name, arguments):
            receipt = await owner.operation(
                ToolCallOperation(operation="tools.call", call_id=uuid4(), toolkit="state", function="change"),
            )
            receipts.append(receipt)
            while (await owner.get_call(receipt["call_id"]))["status"] in {"queued", "running"}:
                await asyncio.sleep(0.001)
            return "drained"

    old = await _catalog(tmp_path, [])
    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(old.runtime_context), "turn", "run", "worker"),
        catalog=old,
        worker=Worker(),
        authorize=authorize,
    )
    await owner.retire_binding()
    with pytest.raises(CliOperationError, match="being rebuilt"):
        await owner.operation(ToolListOperation(operation="tools.list"))
    with pytest.raises(CliBashWindowRequiredError, match="active Bash"):
        await owner.operation(
            ToolCallOperation(operation="tools.call", call_id=uuid4(), toolkit="state", function="change"),
        )
    new = await _catalog(
        tmp_path,
        [Toolkit(name="shell", tools=[run_shell_command]), Toolkit(name="state", tools=[change])],
    )
    new.run_response.agent_id = "helper"
    new.agent.db = create_state_storage("helper", tmp_path, subdir="sessions", session_table="sessions")
    owner.bind_catalog(new)

    async def bash(command: str) -> str:
        return command

    function = Function.from_callable(bash)
    message = Message(
        role="assistant",
        tool_calls=[
            {"id": "new-bash", "type": "function", "function": {"name": "bash", "arguments": '{"command":"new"}'}},
        ],
    )
    async with response_cli_lifetime() as lifetime:
        lifetime.bind_provider(owner.checkpoint, function)
        provider_calls = new.agent.model.get_function_calls_to_run(message, [message], {"bash": function})
        async with asyncio.timeout(3):
            assert (
                await owner.execute_bash(ToolKey("shell", "run_shell_command"), {"args": "new"}, provider_calls[0])
                == "drained"
            )
    assert calls == ["new binding"]
    assert (await owner.get_call(receipts[0]["call_id"]))["parent_bash_call_id"] == "new-bash"
    await owner.close()


@pytest.mark.asyncio
async def test_cancelling_outer_io_cancels_and_joins_admitted_work(
    tmp_path: Path,
) -> None:

    started = asyncio.Event()
    cleaned = asyncio.Event()
    shutdown_seen = []

    async def run_shell_command(args: str) -> str:
        pytest.fail("ordinary transport")

    async def wait() -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            shutdown_seen.append(owner.close_for_shutdown)
            cleaned.set()
        return "unreachable"

    async def authorize(key, arguments):
        return None

    class Worker:
        async def invoke_shell(self, name, arguments):
            await owner.operation(
                ToolCallOperation(operation="tools.call", call_id=uuid4(), toolkit="state", function="wait"),
            )
            await asyncio.Event().wait()
            return "unreachable"

    catalog = await _catalog(
        tmp_path,
        [Toolkit(name="shell", tools=[run_shell_command]), Toolkit(name="state", tools=[wait])],
    )
    catalog.run_response.agent_id = "helper"
    catalog.agent.db = create_state_storage("helper", tmp_path, subdir="sessions", session_table="sessions")
    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run", "worker"),
        catalog=catalog,
        worker=Worker(),
        authorize=authorize,
    )

    async def bash(command: str) -> str:
        return command

    function = Function.from_callable(bash)
    message = Message(
        role="assistant",
        tool_calls=[
            {"id": "cancel-bash", "type": "function", "function": {"name": "bash", "arguments": '{"command":"wait"}'}},
        ],
    )
    async with response_cli_lifetime() as lifetime:
        lifetime.bind_provider(owner.checkpoint, function)
        calls = catalog.agent.model.get_function_calls_to_run(message, [message], {"bash": function})
        task = asyncio.create_task(
            owner.execute_bash(ToolKey("shell", "run_shell_command"), {"args": "wait"}, calls[0]),
        )
        await started.wait()
        request_task_cancel(task, process_shutdown=True)
        async with asyncio.timeout(1):
            with pytest.raises(asyncio.CancelledError):
                await task
    assert cleaned.is_set()
    assert shutdown_seen == [True]
    await owner.close()


@pytest.mark.asyncio
async def test_admission_closes_at_quiescence_and_rejects_later_submission(
    tmp_path: Path,
) -> None:

    running = asyncio.Event()
    release = asyncio.Event()
    called = []

    async def change(value: str) -> str:
        called.append(value)
        if value == "old":
            running.set()
            await release.wait()
        return value

    async def authorize(key, arguments):
        return None

    catalog = await _catalog(tmp_path, [Toolkit(name="state", tools=[change])])
    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run", "worker"),
        catalog=catalog,
        worker=None,
        authorize=authorize,
    )
    leave = asyncio.Event()

    async def first_window():
        async with owner._window("old-bash"):
            await leave.wait()

    window = asyncio.create_task(first_window())
    await asyncio.sleep(0)
    old = await owner.operation(
        ToolCallOperation(
            operation="tools.call",
            call_id=uuid4(),
            toolkit="state",
            function="change",
            arguments={"value": "old"},
        ),
    )
    await running.wait()
    leave.set()
    await asyncio.sleep(0)
    late = await owner.operation(
        ToolCallOperation(
            operation="tools.call",
            call_id=uuid4(),
            toolkit="state",
            function="change",
            arguments={"value": "later"},
        ),
    )
    assert not window.done()
    assert (await owner.get_call(late["call_id"]))["parent_bash_call_id"] == "old-bash"
    release.set()
    await window
    assert called == ["old", "later"]
    assert (await owner.get_call(old["call_id"]))["parent_bash_call_id"] == "old-bash"
    with pytest.raises(CliBashWindowRequiredError, match="active Bash"):
        await owner.operation(
            ToolCallOperation(
                operation="tools.call",
                call_id=uuid4(),
                toolkit="state",
                function="change",
                arguments={"value": "after"},
            ),
        )
    async with owner._window("next-bash"):
        pass
    assert called == ["old", "later"]
    await owner.close()


@pytest.mark.asyncio
async def test_cursor_discovery_and_deferred_describe_require_window(
    tmp_path: Path,
) -> None:

    loaded = []
    authorized = []

    async def authorize(key, arguments):
        authorized.append(key)

    catalog = await _catalog(tmp_path, [])
    for index in range(15):

        async def factory(index=index):
            loaded.append(index)

            async def selected() -> str:
                return "selected"

            return Toolkit(name=f"tool{index}", tools=[selected])

        catalog.add_deferred(DeferredAgentToolkit(f"tool{index}", f"Metadata {index}", factory))
    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run", "worker"),
        catalog=catalog,
        worker=None,
        authorize=authorize,
    )
    names = []
    cursor = None
    while True:
        page = await owner.operation(ToolListOperation(operation="tools.list", cursor=cursor, limit=4))
        names.extend(item["toolkit"] for item in page["items"])
        assert all("input_schema" not in item for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert names == [f"tool{index}" for index in range(15)]
    assert loaded == []
    search = await owner.operation(
        ToolSearchOperation(operation="tools.search", query="Metadata", toolkit="tool12", limit=1),
    )
    assert [item["toolkit"] for item in search["items"]] == ["tool12"]
    operation = ToolDescribeOperation(operation="tools.describe", toolkit="tool12", function="selected")
    with pytest.raises(CliBashWindowRequiredError, match="active Bash"):
        await owner.operation(operation)
    assert loaded == []
    async with owner._window("describe-bash"):
        descriptor = await owner.operation(operation)
        assert descriptor["input_schema"]["properties"] == {}
        with pytest.raises(ValueError, match="unavailable"):
            await owner.operation(
                ToolDescribeOperation(operation="tools.describe", toolkit="unassigned", function="selected"),
            )
    assert loaded == [12]
    assert ToolKey("tool12", "selected") in authorized
    await owner.close()


@pytest.mark.asyncio
async def test_refused_describe_is_a_caller_safe_rejection(tmp_path: Path) -> None:
    """Revoked authority on describe reads like a refused call, not an internal error."""

    def selected() -> str:
        return "selected"

    async def authorize(key, arguments):
        msg = "Current configuration no longer permits this CLI catalog"
        raise PermissionError(msg)

    catalog = await _catalog(tmp_path, [Toolkit(name="state", tools=[selected])])
    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run", "worker"),
        catalog=catalog,
        worker=None,
        authorize=authorize,
    )
    async with owner._window("bash"):
        with pytest.raises(CliOperationError, match="Tool is unavailable"):
            await owner.operation(
                ToolDescribeOperation(operation="tools.describe", toolkit="state", function="selected"),
            )
    await owner.close()


@pytest.mark.asyncio
async def test_live_turn_bounds_receipts_and_concurrent_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runaway Bash loop cannot grow one response's orchestrator-side CLI state without limit."""
    release = asyncio.Event()

    async def slow() -> str:
        await release.wait()
        return "done"

    async def authorize(key, arguments):
        return None

    catalog = await _catalog(tmp_path, [Toolkit(name="state", tools=[slow])])
    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run", "worker"),
        catalog=catalog,
        worker=None,
        authorize=authorize,
    )
    monkeypatch.setattr(turn, "_MAX_ACTIVE_OPERATIONS", 1)
    monkeypatch.setattr(turn, "_MAX_CALL_RECEIPTS", 2)

    def call() -> ToolCallOperation:
        return ToolCallOperation(operation="tools.call", call_id=uuid4(), toolkit="state", function="slow")

    async with owner._window("bash"):
        first = await owner.operation(call())
        with pytest.raises(CliOperationError, match="at once"):
            await owner.operation(call())
        release.set()
        while (await owner.get_call(first["call_id"]))["status"] != "completed":
            await asyncio.sleep(0.001)
        await owner.operation(call())
        with pytest.raises(CliOperationError, match="already holds 2"):
            await owner.operation(call())
    await owner.close()


@pytest.mark.asyncio
async def test_owner_rejects_catalog_bound_to_another_execution_identity(
    tmp_path: Path,
) -> None:

    catalog = await _catalog(tmp_path, [])

    async def authorize(key, arguments):
        return None

    with pytest.raises(ValueError, match="identity"):
        LiveTurnTools(
            CliTurnOwner(
                replace(
                    build_execution_identity_from_runtime_context(catalog.runtime_context),
                    requester_id="@other:test",
                ),
                "turn",
                "run",
                "worker",
            ),
            catalog=catalog,
            worker=None,
            authorize=authorize,
        )


@pytest.mark.asyncio
async def test_context_page_caps_complete_unicode_envelope(tmp_path: Path) -> None:

    catalog = await _catalog(tmp_path, [])

    async def authorize(key, arguments):
        return None

    context = "😀" * 20000
    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run", "worker"),
        catalog=catalog,
        worker=None,
        authorize=authorize,
        context={"instructions": context},
    )
    first = await owner.operation(ContextReadOperation(operation="context.read", name="instructions", limit=65536))
    assert len(canonical_json(first).encode()) <= 65536
    assert first["next_offset"] is not None
    second = await owner.operation(
        ContextReadOperation(operation="context.read", name="instructions", offset=first["next_offset"], limit=65536),
    )
    assert first["text"] + second["text"] == context
    assert second["next_offset"] is None
    await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("large_result", [False, True])
async def test_live_call_retains_frozen_arguments_and_bounds_terminal_output(
    tmp_path: Path,
    *,
    large_result: bool,
) -> None:

    called = []

    async def change(value: str) -> str:
        called.append(value)
        return "x" * 65536 if large_result else value

    async def authorize(key, arguments):
        return None

    catalog = await _catalog(tmp_path, [Toolkit(name="state", tools=[change])])
    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run", "worker"),
        catalog=catalog,
        worker=None,
        authorize=authorize,
    )
    operation = ToolCallOperation(
        operation="tools.call",
        call_id=uuid4(),
        toolkit="state",
        function="change",
        arguments={"value": "original"},
    )
    original = operation.model_copy(deep=True)
    async with owner._window("bash"):
        await owner.operation(operation)
        operation.arguments["value"] = "mutated"
    receipt = await owner.operation(original)
    assert called == ["original"]
    assert receipt["status"] == "completed"
    assert ToolCallReceipt.model_validate(receipt).outcome == (
        {"error": "Tool result projection failed; execution status is retained"} if large_result else "original"
    )
    await owner.close()


@pytest.mark.asyncio
async def test_nested_shell_submits_child_after_outer_worker_returns(tmp_path: Path) -> None:

    nested_entered = asyncio.Event()
    outer_returning = asyncio.Event()
    hooks = []

    async def run_shell_command(args: str) -> str:
        pytest.fail("ordinary worker must remain unused")

    async def change() -> str:
        catalog.run_context.session_state["changed"] = True
        return "child complete"

    async def hook(name, function_call, arguments):
        hooks.append(("before", arguments["args"]))
        result = await function_call(**arguments)
        hooks.append(("after", arguments["args"]))
        return result

    shell = Toolkit(name="shell", tools=[run_shell_command])
    shell.get_async_functions()["run_shell_command"].tool_hooks = [hook]
    catalog = await _catalog(tmp_path, [shell, Toolkit(name="state", tools=[change])])
    catalog.run_response.agent_id = "helper"
    catalog.agent.db = create_state_storage("helper", tmp_path, subdir="sessions", session_table="sessions")

    async def authorize(key, arguments):
        return None

    class Worker:
        async def invoke_shell(self, name, arguments):
            if arguments["args"] == "outer":
                await owner.operation(
                    ToolCallOperation(
                        operation="tools.call",
                        call_id=uuid4(),
                        toolkit="shell",
                        function="run_shell_command",
                        arguments={"args": "nested"},
                    ),
                )
                await nested_entered.wait()
                outer_returning.set()
                return "outer complete"
            nested_entered.set()
            await outer_returning.wait()
            child = await owner.operation(
                ToolCallOperation(
                    operation="tools.call",
                    call_id=uuid4(),
                    toolkit="state",
                    function="change",
                ),
            )
            while (receipt := await owner.get_call(child["call_id"]))["status"] in {"queued", "running"}:
                await asyncio.sleep(0)
            assert receipt["status"] == "completed"
            assert receipt["parent_bash_call_id"] == "bash-drain"
            return "nested complete"

    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run", "worker"),
        catalog=catalog,
        worker=Worker(),
        authorize=authorize,
    )

    async def bash(command: str) -> str:
        return command

    function = Function.from_callable(bash)
    message = Message(
        role="assistant",
        tool_calls=[
            {
                "id": "bash-drain",
                "type": "function",
                "function": {"name": "bash", "arguments": '{"command":"outer"}'},
            },
        ],
    )
    try:
        async with response_cli_lifetime() as lifetime:
            lifetime.bind_provider(owner.checkpoint, function)
            calls = catalog.agent.model.get_function_calls_to_run(message, [message], {"bash": function})
            async with asyncio.timeout(2):
                assert (
                    await owner.execute_bash(ToolKey("shell", "run_shell_command"), {"args": "outer"}, calls[0])
                    == "outer complete"
                )
        assert catalog.run_context.session_state["changed"] is True
        assert hooks == [("before", "outer"), ("before", "nested"), ("after", "nested"), ("after", "outer")]
    finally:
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("before_start", [False, True])
async def test_admitted_describe_waiter_settles_when_its_task_is_cancelled(
    tmp_path: Path,
    *,
    before_start: bool,
) -> None:

    factory_entered = asyncio.Event()
    catalog = await _catalog(tmp_path, [])

    async def factory():
        factory_entered.set()
        await asyncio.Event().wait()
        pytest.fail("cancelled factory must not finish")

    catalog.add_deferred(DeferredAgentToolkit("deferred", "Deferred tools", factory))

    async def authorize(key, arguments):
        return None

    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run", "worker"),
        catalog=catalog,
        worker=None,
        authorize=authorize,
    )
    try:
        async with owner._window("bash"):
            describe = asyncio.create_task(
                owner.operation(
                    ToolDescribeOperation(
                        operation="tools.describe",
                        toolkit="deferred",
                        function="selected",
                    ),
                ),
            )
            await asyncio.sleep(0)
            if before_start:
                # Exactly the same cancellation used by owner teardown, before
                # the admitted coroutine can execute its first instruction.
                for task in owner._active:
                    task.cancel()
            else:
                await factory_entered.wait()
            await owner.close()
        assert factory_entered.is_set() is not before_start
        assert describe.done(), "owner teardown abandoned its admitted describe HTTP waiter"
        with pytest.raises(CliAuthenticationError):
            await describe
    finally:
        describe.cancel()
        await asyncio.gather(describe, return_exceptions=True)
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_bash_waits_for_exact_native_approval_without_holding_catalog(tmp_path: Path, approved: bool) -> None:

    effects = []
    waiting = asyncio.Event()
    decide = asyncio.Event()

    async def run_shell_command(args: str) -> str:
        pytest.fail("ordinary shell fallback")

    def action(value: str) -> str:
        effects.append(value)
        return value

    function = Function.from_callable(action)
    function.owning_toolkit = "actions"
    function.requires_confirmation = True
    catalog = await _catalog(tmp_path, [Toolkit(name="shell", tools=[run_shell_command]), function])
    catalog.run_response.agent_id = "helper"
    catalog.agent.db = create_state_storage("helper", tmp_path, subdir="sessions", session_table="sessions")

    async def pause(paused):
        assert not catalog._lock.locked()
        assert paused.cli_call["arguments"] == {"value": "exact\nvalue"}
        assert paused.cli_call["parent_bash_call_id"] == "outer"
        assert paused.tool_trace[0].parent_bash_call_id == "outer"
        assert paused.tool_trace[0].toolkit_name == "actions"
        saved = catalog.agent.db.get_session("session")
        assert saved.session_data["session_state"]["before_pause"] is True
        waiting.set()
        await decide.wait()
        requirement = paused.requirements[0]
        if approved:
            requirement.confirm()
        else:
            requirement.reject("Denied by requester")
        return (requirement,)

    catalog.runtime_context = replace(catalog.runtime_context, cli_approval_handler=pause)

    async def authorize(key, arguments):
        pass

    class Worker:
        async def invoke_shell(self, name, arguments):
            catalog.run_context.session_state["before_pause"] = True
            receipt = await owner.operation(
                ToolCallOperation(
                    operation="tools.call",
                    call_id=uuid4(),
                    toolkit="actions",
                    function="action",
                    arguments={"value": "exact\nvalue"},
                ),
            )
            while (receipt := await owner.get_call(receipt["call_id"]))["status"] in {"queued", "running", "waiting"}:
                await asyncio.sleep(0)
            assert receipt["status"] == ("completed" if approved else "failed")
            return "Bash finished after decision"

    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run", "worker"),
        catalog=catalog,
        worker=Worker(),
        authorize=authorize,
    )

    async def bash(command: str) -> str:
        return command

    provider = Function.from_callable(bash)
    message = Message(
        role="assistant",
        tool_calls=[
            {"id": "outer", "type": "function", "function": {"name": "bash", "arguments": '{"command":"work"}'}},
        ],
    )
    async with response_cli_lifetime() as lifetime:
        lifetime.bind_provider(owner.checkpoint, provider)
        calls = catalog.agent.model.get_function_calls_to_run(message, [message], {"bash": provider})
        task = asyncio.create_task(
            owner.execute_bash(ToolKey("shell", "run_shell_command"), {"args": "work"}, calls[0]),
        )
        try:
            async with asyncio.timeout(3):
                await waiting.wait()
                assert not task.done()
                assert effects == []
                decide.set()
                assert await task == "Bash finished after decision"
        finally:
            await owner.close()
            await asyncio.gather(task, return_exceptions=True)
    assert effects == (["exact\nvalue"] if approved else [])
