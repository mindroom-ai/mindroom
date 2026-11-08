"""The terminal toolkit: an agent's persistent terminals beside its worker's shell, typed into as the shell's rules allow."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from mindroom.agents import build_agent_toolkit
from mindroom.approval.tool_approval import evaluate_tool_approval
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.terminal.resolution import agent_terminals
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.sandbox_proxy import tool_placement
from mindroom.tool_system.tool_function import ToolGate
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, agent_workspace_root_path
from tests.conftest import bind_runtime_paths
from tests.terminal.conftest import primary_paths
from tests.tool_approval_helpers import gated_functions

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.terminal.client import WorkerTerminals
    from mindroom.tool_system.tool_function import ToolFunction

pytestmark = pytest.mark.asyncio

_IDENTITY = ToolExecutionIdentity("matrix", "code", "@alice:localhost", "!room:localhost", None, None, "session")
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
    registry = builtin_tool_registry()
    toolkit = build_agent_toolkit(
        "terminal",
        agent_tools=resolve_agent_tools(config, "code", registry),
        placement=tool_placement(paths, registry),
        runtime_paths=paths,
        execution_identity=_IDENTITY,
    )
    assert toolkit is not None
    return gated_functions(toolkit, config)


def _terminals(config: Config, paths: RuntimePaths) -> WorkerTerminals:
    """Return the same terminals as a person in the room reaches them."""
    terminals = agent_terminals("code", config, paths, _IDENTITY, create=False)
    assert terminals is not None
    return terminals


async def _call(function: ToolFunction, **arguments: object) -> str:
    """Run one call as the agent's turn runs it: with the arguments its approval judged."""
    execution = function.planned_execution("call", dict(arguments))
    result = await function.entrypoint(**(execution.tool_args or {}))
    assert isinstance(result, str)
    return result


async def _asks(config: Config, paths: RuntimePaths, function: ToolFunction, arguments: dict[str, object]) -> bool:
    """Return whether the shell's rules ask before this call, judging the arguments the turn stores for it."""
    stored = function.planned_execution("call", arguments).tool_args or {}
    return await _asks_for(config, paths, function.name, stored)


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
    assert await _call(functions["terminal_list"]) == f"- {session_id}: running"
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
    assert functions["terminal_read"].gate is None
    session_id = await _open(functions)

    assert await _asks(config, paths, run, {"session_id": session_id, "command": "rm x"})
    assert not await _asks(config, paths, run, {"session_id": session_id, "command": "ls"})
    assert await _asks(config, paths, functions["terminal_open"], {})

    assert not await _asks(config, paths, send, {"session_id": session_id, "keys": "rm -rf "})
    assert await _call(send, session_id=session_id, keys="rm -rf ") == "Sent. New output:\nrm -rf"
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
    judged = send.planned_execution("call", {"session_id": session_id, "keys": "<enter>"}).tool_args or {}
    assert not await _asks_for(config, paths, "terminal_send", judged)

    async with _terminals(config, paths).attach(session_id) as person:
        await person.send_input("rm -rf tmp")
    assert (await send.entrypoint(**judged)).startswith(_LINE_CHANGED)
    assert (await _call(functions["terminal_read"], session_id=session_id)).endswith("rm -rf tmp")
    assert kept.is_dir()


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
