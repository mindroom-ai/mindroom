"""The terminal toolkit: an agent's terminals beside its worker's shell, typed in only while its commands never ask."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import aiohttp
import pytest
from aiohttp import web

from mindroom.agent_loop.spec import RunContext
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools import terminal as terminal_tools
from mindroom.message_target import MessageTarget
from mindroom.session_storage.types import ConversationSession
from mindroom.terminal import client as client_module
from mindroom.terminal.client import TerminalDetachedError
from mindroom.terminal.resolution import agent_terminals
from mindroom.terminal.session import TerminalEvent
from mindroom.tool_system import agent_tool_calls
from mindroom.tool_system.tool_access import ToolKey, UnknownToolError
from mindroom.tool_system.tool_function import AdaptedToolkit
from mindroom.tool_system.worker_routing import agent_workspace_root_path
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import bind_runtime_paths, make_conversation_reader_mock, make_relation_lookup
from tests.terminal.conftest import IDENTITY, agent_toolkit, primary_paths
from tests.tool_approval_helpers import gated_functions

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.terminal.client import WorkerTerminals
    from mindroom.tool_system.tool_function import ToolFunction

pytestmark = pytest.mark.asyncio

_NOT_AT_PROMPT = "Error: the terminal's shell is not waiting at an empty prompt"


def _config(paths: RuntimePaths, tools: list[Any], approval: dict[str, Any] | None = None) -> Config:
    config = Config(agents={"code": AgentConfig(display_name="Code", tools=tools)}, tool_approval=approval or {})
    return bind_runtime_paths(config, paths)


def _functions(config: Config, paths: RuntimePaths) -> dict[str, ToolFunction]:
    """Return the terminal functions the agent's turn is offered, with the gates the approval rules set."""
    return gated_functions(agent_toolkit("terminal", config, paths), config)


def _terminals(config: Config, paths: RuntimePaths) -> WorkerTerminals:
    """Return the same terminals as a person in the room reaches them."""
    terminals = agent_terminals("code", config, paths, IDENTITY, create=False)
    assert terminals is not None
    return terminals


async def _call(function: ToolFunction, **arguments: object) -> str:
    result = await function.entrypoint(**arguments)
    assert isinstance(result, str)
    return result


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
    # A command that ends with a newline is still one line.
    assert await _call(functions["terminal_run"], session_id=session_id, command="echo $((6*7))\n") == (
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
    for command in ("ls\x01", "\x03ls", "cd /tmp\nls", "ls\x7f", "ls\x9b"):
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


@pytest.mark.parametrize(
    ("approval", "restricted"),
    [
        ({"rules": [{"match": "run_shell_command", "action": "require_approval"}]}, True),
        ({"default": "require_approval"}, True),
        # A review script decides per call, and what a shell runs can't be read from the text typed into it.
        ({"rules": [{"match": "run_*", "script": "rm_review.py"}]}, True),
        ({}, False),
        ({"default": "require_approval", "rules": [{"match": "run_shell_command", "action": "auto_approve"}]}, False),
    ],
)
async def test_an_agent_types_in_terminals_only_while_its_shell_commands_never_ask(
    tmp_path: Path,
    runner: str,
    approval: dict[str, Any],
    restricted: bool,
) -> None:
    """Any rule that may ask about ``run_shell_command`` leaves the agent only reading and listing, and says why."""
    paths = primary_paths(tmp_path, runner)
    config = _config(paths, ["shell", "terminal"], approval)
    functions = _functions(config, paths)
    terminals = _terminals(config, paths)
    session_id = (await terminals.open()).id

    assert {"terminal_open", "terminal_run", "terminal_send"}.isdisjoint(functions) == restricted
    instructions = agent_toolkit("terminal", config, paths).instructions or ""
    assert ("run commands with run_shell_command" in instructions) == restricted
    if restricted:
        # People still type in the agent's terminals.
        async with terminals.attach(session_id) as person:
            await person.send_input("echo ran-$((6*7))\r")
    else:
        await _call(functions["terminal_run"], session_id=session_id, command="echo ran-$((6*7))")
    assert "ran-42" in await _call(functions["terminal_read"], session_id=session_id)
    assert f"- {session_id}: running" in await _call(functions["terminal_list"])


async def test_under_a_default_that_asks_no_path_offers_a_typing_call_to_approve(tmp_path: Path, runner: str) -> None:
    """Each path calls only what the toolkit offers, so no approval card can ask for typing the agent may not do."""
    paths = primary_paths(tmp_path, runner)
    config = _config(paths, ["shell", "terminal"], {"default": "require_approval"})
    functions = _functions(config, paths)
    assert sorted(functions) == ["terminal_list", "terminal_read"]

    # The agent CLI, through the catalog of the response it belongs to.
    catalog = agent_tool_calls.PreparedAgentToolCatalog(
        run=RunContext(
            run_id="run",
            user_id="@alice:localhost",
            session=ConversationSession(session_id="session", kind="agent", owner_id="code"),
        ),
        runtime_context=make_test_tool_runtime_context(
            agent_name="code",
            target=MessageTarget("!room:localhost", None, None, "$event", "session"),
            requester_id="@alice:localhost",
            client=MagicMock(),
            config=config,
            runtime_paths=paths,
            relations=make_relation_lookup(),
            conversation_reader=make_conversation_reader_mock(),
        ),
        store=None,
        middleware=(),
        max_tool_calls_per_turn=None,
    )
    await catalog.prepare([AdaptedToolkit(name="terminal", functions=tuple(functions.values()), instructions=None)])
    with pytest.raises(UnknownToolError, match="its functions: terminal_list, terminal_read"):
        await catalog.bind(ToolKey("terminal", "terminal_send"))


async def test_a_shell_started_in_a_terminal_keeps_runs_out_until_it_exits(tmp_path: Path, runner: str) -> None:
    """A shell started inside prints no prompts of its own, so a run never types into it or loses count after it."""
    paths = primary_paths(tmp_path, runner)
    functions = _functions(_config(paths, ["shell", "terminal"]), paths)
    session_id = await _open(functions)
    run, send = functions["terminal_run"], functions["terminal_send"]

    await _call(send, session_id=session_id, keys="bash --norc -i<enter>")
    for _ in range(2):
        await _call(send, session_id=session_id, keys="true<enter>")
    assert (await _call(run, session_id=session_id, command="echo inside")).startswith(_NOT_AT_PROMPT)
    await _call(send, session_id=session_id, keys="exit<enter>")
    await _call(send, session_id=session_id, keys="<ctrl-c>")
    assert await _call(run, session_id=session_id, command="echo back-$((2*3))") == "back-6\n[Exit status 0.]"


async def test_named_keys_are_keys_and_other_text_is_typed_as_written() -> None:
    """A key name it does not know, or a key without its brackets, is text."""
    assert terminal_tools._keys_to_text("y<Enter><ctrl-d><ctrl-->< up><left>") == "y\r\x04<ctrl-->< up>\x1b[D"


async def test_output_reads_as_the_screen_shows_it() -> None:
    """Escapes go, and backspaces take effect, as readline's own erasing of a typed character shows."""
    assert terminal_tools._plain_text(b"abc\x08\x1b[K\r\nxy\x08\x08z\r\n\x08w") == "ab\nz\nw"


@pytest.mark.timeout(10)
async def test_output_full_of_backspaces_reads_in_one_pass() -> None:
    """Worker code writes the output, so erasing it costs one pass however many backspaces it holds."""
    assert terminal_tools._plain_text(b"x" * 100_000 + b"\x08" * 100_000 + b"done") == "done"


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


@contextlib.asynccontextmanager
async def _late_first_keys(runner: str) -> AsyncIterator[str]:
    """Serve a proxy to ``runner`` that holds the first input of the first connection to send one.

    It releases it once another connection sends input, or after half a second, as a network may deliver it late.
    """
    sent_elsewhere = asyncio.Event()
    first: web.WebSocketResponse | None = None

    async def relay(request: web.Request) -> web.WebSocketResponse:
        nonlocal first
        viewer = web.WebSocketResponse()
        await viewer.prepare(request)
        async with (
            aiohttp.ClientSession() as client,
            client.ws_connect(f"{runner}{request.path_qs}", headers=request.headers) as upstream,
        ):

            async def output() -> None:
                async for message in upstream:
                    if message.type == aiohttp.WSMsgType.BINARY:
                        await viewer.send_bytes(message.data)

            shown = asyncio.create_task(output())
            async for message in viewer:
                if first is None:
                    first = viewer
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(sent_elsewhere.wait(), 0.5)
                elif first is not viewer:
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
    """A person's keystroke never joins an agent's later command, late or kept by the shell.

    The person types `r`, the agent clears the line with Ctrl-C and runs `m -rf tmp`, which must run as just that.
    """
    paths = primary_paths(tmp_path, runner)
    config = _config(paths, ["shell", "terminal"])
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
                if not ran.startswith(_NOT_AT_PROMPT):
                    break

    assert kept.is_dir()
    assert ran.endswith("[Exit status 127.]"), ran


async def test_an_agent_is_told_when_a_terminal_s_shell_has_exited(tmp_path: Path, runner: str) -> None:
    """Keys and commands for a shell that has exited are refused as such, not answered as sent or still running."""
    paths = primary_paths(tmp_path, runner)
    functions = _functions(_config(paths, ["shell", "terminal"]), paths)
    session_id = await _open(functions)
    ran_exit = await _open(functions)
    exited = "[The terminal's shell has exited.]"

    assert (await _call(functions["terminal_send"], session_id=session_id, keys="exit<enter>")).endswith(exited)
    # Bash says `exit` as it leaves, after the echo of the command.
    assert await _call(functions["terminal_run"], session_id=ran_exit, command="exit") == f"exit\n{exited}"
    for terminal in (session_id, ran_exit):
        assert (await _call(functions["terminal_read"], session_id=terminal)).endswith(f"\n{exited}")
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
