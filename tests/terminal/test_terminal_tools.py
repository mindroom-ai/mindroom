"""The terminal toolkit: an agent's persistent terminals beside its worker's shell, typed into as the shell's rules allow."""

from __future__ import annotations

import asyncio
import contextlib
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import aiohttp
import pytest
from aiohttp import web

from mindroom.agent_loop.tools import plan_tool_batch
from mindroom.approval.tool_approval import evaluate_tool_approval
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools import terminal as terminal_tools
from mindroom.terminal import client as client_module
from mindroom.terminal.client import TerminalDetachedError
from mindroom.terminal.resolution import agent_terminals
from mindroom.terminal.session import TerminalEvent
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.tool_function import ToolGate
from mindroom.tool_system.worker_routing import agent_workspace_root_path
from tests.conftest import bind_runtime_paths
from tests.terminal.conftest import IDENTITY, agent_toolkit, primary_paths
from tests.tool_approval_helpers import gated_functions

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.terminal.client import WorkerTerminals
    from mindroom.tool_system.tool_function import ToolFunction

pytestmark = pytest.mark.asyncio

# A review script that asks before a shell command that runs rm.
_RM_REVIEW = "def check(tool_name, arguments, agent_name):\n    return 'rm' in arguments['args'].split()\n"
_LINE_CHANGED = "Error: the terminal's line changed after these keys were checked"


def _config(paths: RuntimePaths, tools: list[Any], *, ask_for_rm: bool = False) -> Config:
    rules = [{"match": "run_shell_command", "script": "rm_review.py"}] if ask_for_rm else []
    paths.config_path.with_name("rm_review.py").write_text(_RM_REVIEW, encoding="utf-8")
    config = Config(
        agents={"code": AgentConfig(display_name="Code", tools=tools)},
        tool_approval={"rules": rules},
    )
    return bind_runtime_paths(config, paths)


def _functions(config: Config, paths: RuntimePaths) -> dict[str, ToolFunction]:
    """Return the terminal functions the agent's turn is offered, with the gates the approval rules set."""
    return gated_functions(agent_toolkit("terminal", config, paths), config)


def _terminals(config: Config, paths: RuntimePaths) -> WorkerTerminals:
    """Return the same terminals as a person in the room reaches them."""
    terminals = agent_terminals("code", config, paths, IDENTITY, create=False)
    assert terminals is not None
    return terminals


def _planned(function: ToolFunction, arguments: dict[str, object]) -> dict[str, Any]:
    """Return the arguments the model loop stores for one call of ``function``, which its approval judges and it runs."""
    call = {"id": "call", "function": {"name": function.name, "arguments": json.dumps(arguments)}}
    batch = plan_tool_batch(
        [call],
        {function.name: function},
        calls_so_far=0,
        max_tool_calls_per_turn=None,
        entity="code",
        failing_tools={},
    )
    [planned] = batch.planned
    return planned.execution.tool_args or {}


async def _call(function: ToolFunction, **arguments: object) -> str:
    """Run one call as the agent's turn runs it: with the arguments its approval judged."""
    result = await function.entrypoint(**_planned(function, arguments))
    assert isinstance(result, str)
    return result


async def _asks(config: Config, paths: RuntimePaths, function: ToolFunction, arguments: dict[str, object]) -> bool:
    """Return whether the shell's rules ask before this call, judging the arguments the turn stores for it."""
    return await _asks_for(config, paths, function.name, _planned(function, arguments))


async def _asks_for(config: Config, paths: RuntimePaths, function_name: str, arguments: dict[str, Any]) -> bool:
    alias = builtin_tool_registry().entries["terminal"].approval_aliases[function_name]
    asks, _ = await evaluate_tool_approval(config, paths, function_name, arguments, "code", alias)
    return asks


async def _open(functions: dict[str, ToolFunction]) -> str:
    opened = await _call(functions["terminal_open"])
    assert opened.startswith("Opened terminal ")
    return opened.removeprefix("Opened terminal ").removesuffix(".")


async def test_an_agent_runs_commands_types_keys_reads_and_lists_its_terminals(tmp_path: Path, runner: str) -> None:
    """A run waits for the shell's prompt and reports the exit status; keys reach whatever runs in the terminal."""
    paths = primary_paths(tmp_path, runner)
    functions = _functions(_config(paths, ["shell", "terminal"]), paths)
    session_id = await _open(functions)

    assert await _call(functions["terminal_run"], session_id=session_id, command="echo hi; false") == (
        "hi\n[Exit status 1.]"
    )
    running = await _call(functions["terminal_run"], session_id=session_id, command="sleep 30", timeout_s=0.5)
    assert running == "[Still running after 0.5 s.]"
    assert (await _call(functions["terminal_send"], session_id=session_id, keys="<ctrl-c>")).startswith(
        "Sent. New output:\n^C\n",
    )
    assert await _call(functions["terminal_run"], session_id=session_id, command="echo $((6*7))") == (
        "42\n[Exit status 0.]"
    )
    assert "42" in await _call(functions["terminal_read"], session_id=session_id)
    # With the terminal's echo off, readline still ends the line before the output, which stays whole.
    assert await _call(functions["terminal_run"], session_id=session_id, command="stty -echo") == "[Exit status 0.]"
    assert await _call(functions["terminal_run"], session_id=session_id, command="echo $((6*7))") == (
        "42\n[Exit status 0.]"
    )
    await _call(functions["terminal_run"], session_id=session_id, command="stty echo")
    # The echo of a command wider than the terminal is redrawn across rows, and is no output either.
    wide = "echo " + "a" * 150 + " $((2+3))"
    assert await _call(functions["terminal_run"], session_id=session_id, command=wide) == (
        "a" * 150 + " 5\n[Exit status 0.]"
    )
    assert await _call(functions["terminal_list"]) == f"- {session_id}: running"
    refused = await _call(functions["terminal_send"], session_id=session_id, keys="<ctrl-c>echo hi<enter>")
    assert refused.startswith("Error: send <ctrl-c> on its own")
    too_long = await _call(functions["terminal_send"], session_id=session_id, keys="x" * 16385)
    assert too_long == "Error: Type at most 16384 characters at once; send a long text in parts."
    for command in ("ls\x01", "\x03ls", "cd /tmp\nls"):
        refused = await _call(functions["terminal_run"], session_id=session_id, command=command)
        assert refused.startswith("Error: a command is one line of plain text;")
    # A line longer than any answer keeps its newest part.
    long_line = await _call(functions["terminal_run"], session_id=session_id, command="printf 'x%.0s' $(seq 300000)")
    assert long_line == "x" * 16_000 + "\n[Exit status 0.]"
    assert await _call(functions["terminal_read"], session_id="nope") == (
        "Error: there is no terminal 'nope'; terminal_list shows yours."
    )


@pytest.mark.parametrize(
    "tools",
    [["terminal"], [{"shell": {"exclude_tools": ["run_shell_command"]}}, "terminal"]],
)
async def test_an_agent_without_a_shell_command_gets_no_terminal_function(
    tmp_path: Path,
    runner: str,
    tools: list[Any],
) -> None:
    """Without the shell, or with its command function excluded, there is nothing to type terminal keys into."""
    paths = primary_paths(tmp_path, runner)
    assert _functions(_config(paths, tools), paths) == {}


async def test_an_agent_whose_shell_runs_in_the_primary_gets_no_terminal_function(tmp_path: Path) -> None:
    """No terminal runs in the primary process, so its agents have none."""
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SANDBOX_EXECUTION_MODE": "off"},
    )
    assert _functions(_config(paths, ["shell", "terminal"]), paths) == {}


async def test_the_shell_s_rules_judge_what_the_agent_types_as_the_command_it_runs(tmp_path: Path, runner: str) -> None:
    """A run is judged as its command; keys as what their Enter runs, joined to the line anyone typed before."""
    paths = primary_paths(tmp_path, runner)
    config = _config(paths, ["shell", "terminal"], ask_for_rm=True)
    functions = _functions(config, paths)
    run, send = functions["terminal_run"], functions["terminal_send"]
    assert (functions["terminal_open"].gate, run.gate, send.gate) == (ToolGate.POLICY,) * 3
    # Only the preparation before approval fills the line: the model can't send one.
    assert ("line" in send.parameters["properties"], send.parameters["additionalProperties"]) == (False, False)
    assert functions["terminal_read"].gate is None
    session_id = await _open(functions)

    assert await _asks(config, paths, run, {"session_id": session_id, "command": "rm x"})
    assert not await _asks(config, paths, run, {"session_id": session_id, "command": "ls"})
    assert await _asks(config, paths, functions["terminal_open"], {})

    assert not await _asks(config, paths, send, {"session_id": session_id, "keys": "rm -rf "})
    assert (await _call(send, session_id=session_id, keys="rm -rf ")).endswith("rm -rf")
    assert await _asks(config, paths, send, {"session_id": session_id, "keys": "tmp<enter>"})
    assert not await _asks(config, paths, send, {"session_id": session_id, "keys": "<ctrl-c>"})
    await _call(send, session_id=session_id, keys="<ctrl-c>")

    # A person types into the same terminal from the room.
    async with _terminals(config, paths).attach(session_id) as person:
        await person.send_input("rm -rf tmp")
    assert await _asks(config, paths, send, {"session_id": session_id, "keys": "<enter>"})
    await _call(send, session_id=session_id, keys="<ctrl-c>")
    assert not await _asks(config, paths, send, {"session_id": session_id, "keys": "ls<enter>"})
    assert await _asks(config, paths, send, {"session_id": session_id, "keys": "<up><enter>"})


async def test_keys_judged_before_someone_typed_on_the_line_are_not_typed(tmp_path: Path, runner: str) -> None:
    """The approved keys go in only onto the line they were judged against; the agent reads it and tries again."""
    paths = primary_paths(tmp_path, runner)
    config = _config(paths, ["shell", "terminal"], ask_for_rm=True)
    functions = _functions(config, paths)
    send = functions["terminal_send"]
    session_id = await _open(functions)
    kept = agent_workspace_root_path(tmp_path / "storage", "code") / "tmp"
    kept.mkdir()
    judged = _planned(send, {"session_id": session_id, "keys": "<enter>"})
    assert not await _asks_for(config, paths, "terminal_send", judged)

    async with _terminals(config, paths).attach(session_id) as person:
        await person.send_input("rm -rf tmp")
    assert (await send.entrypoint(**judged)).startswith(_LINE_CHANGED)
    # A run types only at an empty prompt, so the command never joins the person's line.
    ran = await _call(functions["terminal_run"], session_id=session_id, command="ls")
    assert ran.startswith("Error: the terminal changed after this command was checked, or its shell is not waiting")
    assert (await _call(functions["terminal_read"], session_id=session_id)).endswith("rm -rf tmp")
    assert kept.is_dir()


async def test_a_command_judged_before_someone_ran_another_is_not_run(tmp_path: Path, runner: str) -> None:
    """A command approved for a terminal runs only if nobody typed in it since, as a `cd` would change what it does."""
    paths = primary_paths(tmp_path, runner)
    config = _config(paths, ["shell", "terminal"], ask_for_rm=True)
    functions = _functions(config, paths)
    run = functions["terminal_run"]
    session_id = await _open(functions)
    judged = _planned(run, {"session_id": session_id, "command": "pwd"})
    async with _terminals(config, paths).attach(session_id) as person:
        await person.send_input("cd /\r")

    refused = await run.entrypoint(**judged)
    assert refused.startswith("Error: the terminal changed after this command was checked")
    assert "\n/\n" not in await _call(functions["terminal_read"], session_id=session_id) + "\n"


async def test_a_line_from_outside_the_turn_s_preparation_is_checked_and_without_one_the_rules_ask(
    tmp_path: Path,
    runner: str,
) -> None:
    """A path that skips the preparation, such as a script run or a scheduled call, passes the arguments as sent.

    A ``line`` it claims is typed against only while the terminal's line is exactly that one; without one, the shell's
    rules ask whenever they may, since the keys alone cannot tell what their Enter runs.
    """
    paths = primary_paths(tmp_path, runner)
    config = _config(paths, ["shell", "terminal"], ask_for_rm=True)
    functions = _functions(config, paths)
    send = functions["terminal_send"]
    session_id = await _open(functions)
    async with _terminals(config, paths).attach(session_id) as person:
        await person.send_input("rm -rf tmp")
        revision = person.line.revision

    forged = {"session_id": session_id, "keys": "<enter>", "line": {"revision": revision, "typed": "ls"}}
    assert not await _asks_for(config, paths, "terminal_send", forged)
    assert (await send.entrypoint(**forged)).startswith(_LINE_CHANGED)
    assert await _asks_for(config, paths, "terminal_send", {"session_id": session_id, "keys": "ls<enter>"})


async def test_a_judged_line_runs_only_as_written(tmp_path: Path, runner: str) -> None:
    """The shell expands no ``!`` history and reads neither key bindings nor history that worker code wrote."""
    paths = primary_paths(tmp_path, runner)
    functions = _functions(_config(paths, ["shell", "terminal"]), paths)
    home = agent_workspace_root_path(tmp_path / "storage", "code")
    home.mkdir(parents=True, exist_ok=True)
    (home / ".inputrc").write_text('"q": "echo planted-$((3*3))\\n"\n', encoding="utf-8")
    (home / ".bash_history").write_text("echo from-history-$((4*4))\n", encoding="utf-8")
    session_id = await _open(functions)
    run, send = functions["terminal_run"], functions["terminal_send"]

    # The shell's first command, so no command of its own is the last one in its history.
    assert "from-history-16" not in await _call(run, session_id=session_id, command="fc -s")
    assert await _call(run, session_id=session_id, command="echo $HOME") == f"{home.resolve()}\n[Exit status 0.]"
    assert await _call(run, session_id=session_id, command="echo a!!b") == "a!!b\n[Exit status 0.]"
    # Typed alone: Ctrl-C in the same write would make the terminal drop the key before the shell reads it.
    await _call(send, session_id=session_id, keys="q")
    assert "planted-9" not in await _call(functions["terminal_read"], session_id=session_id)


class _Chatty:
    """A viewer of a terminal printing far more than a call keeps, then exiting."""

    def __init__(self, chunks: int) -> None:
        self._chunks = chunks

    async def receive(self) -> TerminalEvent:
        self._chunks -= 1
        return TerminalEvent("output", data=b"x" * 65536) if self._chunks >= 0 else TerminalEvent("exit", code=0)


async def test_a_call_keeps_only_the_newest_output_however_much_the_terminal_prints() -> None:
    """Output a worker prints for minutes never grows the primary past what one answer can use."""
    output, cut, ended, _ = await terminal_tools._output(_Chatty(64), 10)  # type: ignore[arg-type]

    assert (len(output), cut, ended) == (256 * 1024, True, True)


class _Gone:
    """A viewer whose connection to the runner ended."""

    async def receive(self) -> TerminalEvent:
        raise TerminalDetachedError


async def test_a_call_whose_terminal_connection_ends_says_so_rather_than_still_running() -> None:
    """A lost connection is no timeout, so a run never answers "still running" for it."""
    with pytest.raises(TerminalDetachedError):
        await terminal_tools._output(_Gone(), 10, after=0)  # type: ignore[arg-type]


@asynccontextmanager
async def _late_first_keys(runner: str) -> AsyncIterator[str]:
    """Serve a proxy to ``runner`` that holds the first input of the first connection to send one.

    It releases it once another connection sends input, or after half a second, as a network may deliver it late.
    """
    sent_elsewhere = asyncio.Event()
    first: list[web.WebSocketResponse] = []

    async def relay(request: web.Request) -> web.WebSocketResponse:
        viewer = web.WebSocketResponse()
        await viewer.prepare(request)
        async with (
            aiohttp.ClientSession() as client,
            client.ws_connect(f"{runner}{request.path_qs}", headers=request.headers) as upstream,
        ):

            async def output() -> None:
                async for message in upstream:
                    await viewer.send_bytes(message.data) if message.type == aiohttp.WSMsgType.BINARY else None

            shown = asyncio.create_task(output())
            async for message in viewer:
                if not first:
                    first.append(viewer)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(sent_elsewhere.wait(), 0.5)
                elif first[0] is not viewer:
                    sent_elsewhere.set()
                await upstream.send_str(message.data)
            shown.cancel()
        return viewer

    app = web.Application()
    app.router.add_get("/{path:.*}", relay)
    server = web.AppRunner(app, access_log=None)
    await server.setup()
    try:
        site = web.TCPSite(server, "127.0.0.1", 0)
        await site.start()
        yield f"http://127.0.0.1:{server.addresses[0][1]}"
    finally:
        await server.cleanup()


async def test_typing_reaches_the_shell_in_the_order_it_was_recorded(
    tmp_path: Path,
    runner: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A person's keystroke never lands on the line an agent's later call was judged against, late or kept by the shell.

    The person types `r`, the agent clears the line with Ctrl-C and runs `m -rf tmp`, judged as just that.
    """
    paths = primary_paths(tmp_path, runner)
    config = _config(paths, ["shell", "terminal"], ask_for_rm=True)
    functions = _functions(config, paths)
    session_id = await _open(functions)
    kept = agent_workspace_root_path(tmp_path / "storage", "code") / "tmp"
    kept.mkdir()
    real_worker_call = client_module.worker_call
    async with _late_first_keys(runner) as proxy:

        def through_proxy(*args: Any, **kwargs: Any) -> object:  # noqa: ANN401
            call = real_worker_call(*args, **kwargs)
            return replace(call, api_root=call.api_root.replace(runner, proxy))

        monkeypatch.setattr(client_module, "worker_call", through_proxy)
        async with _terminals(config, paths).attach(session_id) as person:
            await person.send_input("r")
            # The line is empty only once the shell prompts after a Ctrl-C, which a Ctrl-C right after typing can
            # leave out, keeping the typing; the agent then sends another, as the run's refusal says.
            for _ in range(5):
                await _call(functions["terminal_send"], session_id=session_id, keys="<ctrl-c>")
                ran = await _call(functions["terminal_run"], session_id=session_id, command="m -rf tmp")
                if not ran.startswith(
                    "Error: the terminal changed after this command was checked, or its shell is not waiting",
                ):
                    break

    assert kept.is_dir()
    assert ran.endswith("[Exit status 127.]")


async def test_an_agent_is_told_when_a_terminal_s_shell_has_exited(tmp_path: Path, runner: str) -> None:
    """Keys and commands for a shell that has exited are refused as such, not answered as sent or still running."""
    paths = primary_paths(tmp_path, runner)
    functions = _functions(_config(paths, ["shell", "terminal"]), paths)
    session_id = await _open(functions)

    assert (await _call(functions["terminal_send"], session_id=session_id, keys="exit<enter>")).endswith(
        "[The terminal's shell has exited.]",
    )
    for function, arguments in (("terminal_send", {"keys": "<ctrl-c>"}), ("terminal_run", {"command": "ls"})):
        answer = await _call(functions[function], session_id=session_id, **arguments)
        assert answer == "Error: the terminal's shell has exited; open another with terminal_open."


class _TwoPrompts:
    """A viewer to whom a late scrollback's prompt and those after the run and after a queued command come at once."""

    prompt = 3

    async def receive(self) -> TerminalEvent:
        marks = (
            b"\x1b]133;D;0;mindroom=1\x07bash$ echo hi; false\r\n",
            b"hi\r\n\x1b]133;D;1;mindroom=2\x07bash$ true\r\n",
            b"\x1b]133;D;0;mindroom=3\x07bash$ ",
        )
        return TerminalEvent("output", data=b"".join(marks))


async def test_a_run_ends_at_its_own_prompt_even_when_a_later_one_arrives_with_it() -> None:
    """Its output and exit status are the run's, never those of a command someone typed meanwhile."""
    output = await terminal_tools._output(_TwoPrompts(), 10, after=1)  # type: ignore[arg-type]

    assert output == (b"\x1b]133;D;0;mindroom=1\x07bash$ echo hi; false\r\nhi\r\n", False, True, 1)
