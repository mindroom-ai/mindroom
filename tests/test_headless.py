"""Headless runs: one configured agent's turn from another Python program, without Matrix or `mindroom run`."""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import nio
import pytest
from aiohttp import web

from mindroom.constants import resolve_runtime_paths
from mindroom.entity_resolution import persist_offline_entity_accounts
from mindroom.headless import HeadlessConfigError, run_turn, validate_config
from mindroom.matrix.state import matrix_state_for_runtime
from mindroom.providers import model_http_client
from mindroom.shell.output_capture import ShellOutputDestination
from tests.conftest import install_model_providers
from tests.minimal_agent_fixtures import ScriptedProvider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.llm.provider import ModelRequest, StreamEvent


def _config(**agent: object) -> dict[str, Any]:
    return {
        "administrators": ["@alice:localhost"],
        "models": {"default": {"provider": "openai", "id": "scripted"}},
        "memory": "none",
        "defaults": {"allow_shell_without_worker": True},
        "agents": {"coder": {"display_name": "Coder", "tools": ["shell", "file"], **agent}},
    }


def _install(monkeypatch: pytest.MonkeyPatch, *steps: object) -> ScriptedProvider:
    provider = ScriptedProvider(steps=list(steps))
    install_model_providers(monkeypatch, {"default": provider})
    return provider


def _last_tool_result(request: ModelRequest) -> str:
    return str([message for message in request.messages if message.role == "tool"][-1].content)


def _files(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def _processes_running(marker: str) -> list[int]:
    """Return the processes whose command line holds ``marker``."""
    pids = []
    for proc in Path("/proc").iterdir():
        try:
            command = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        except OSError:
            continue
        if marker in command:
            pids.append(int(proc.name))
    return pids


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """Return an empty directory for the tools to work in."""
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.mark.asyncio
async def test_run_turn_runs_tools_and_returns_text(
    tmp_path: Path,
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scripted shell call runs in the workdir, and the result holds the reply and the agent's tool calls."""
    provider = _install(monkeypatch, [("run_shell_command", {"args": "echo hi > out.txt"})], "finished")
    result = await run_turn(_config(), agent="coder", prompt="do it", storage=tmp_path / "storage", workdir=workdir)

    assert (result.text, result.completed, result.failed, result.tool_calls) == ("finished", True, False, 1)
    assert result.run_metadata["io.mindroom.ai_run"]["status"] == "completed"
    assert (workdir / "out.txt").read_text() == "hi\n"
    # A headless turn makes no request of its own, such as routing or a thread title.
    assert len(provider.requests) == 2
    # No Matrix room: the prompt neither describes one nor attributes the prompt to the requester's Matrix ID.
    sent = str([message.content for message in provider.requests[0].messages])
    assert "Matrix Reply Targeting" not in sent
    assert "@alice:localhost" not in sent


@pytest.mark.asyncio
async def test_run_turn_closes_pooled_model_connections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The event loop's pooled model connections are closed when the turn ends, since the caller owns the loop."""
    model_http_client._loop_client()
    _install(monkeypatch, "hello")
    await run_turn(_config(), agent="coder", prompt="hi", storage=tmp_path / "storage")

    assert asyncio.get_running_loop() not in model_http_client._LOOP_CLIENTS


@pytest.mark.asyncio
async def test_run_turn_needs_no_matrix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No account is prepared beforehand and no Matrix client is ever built; the accounts it records hold no secret."""

    def no_client(*_args: object, **_kwargs: object) -> None:
        msg = "a headless run built a Matrix client"
        raise AssertionError(msg)

    monkeypatch.setattr(nio.AsyncClient, "__init__", no_client)
    _install(monkeypatch, "hello")
    storage = tmp_path / "storage"
    result = await run_turn(_config(), agent="coder", prompt="hi", storage=storage)

    assert (result.text, result.completed) == ("hello", True)
    runtime_paths = resolve_runtime_paths(config_path=storage / "config.yaml", storage_path=storage)
    accounts = matrix_state_for_runtime(runtime_paths).accounts
    assert {"agent_router", "agent_coder"} <= set(accounts)
    assert all(account.password is None and account.access_token is None for account in accounts.values())


@pytest.mark.asyncio
async def test_run_turn_delegates_to_itself(tmp_path: Path, workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With itself in `delegate_to`, the agent's `run_subagent` runs a copy in the same workdir and returns its answer."""
    provider = _install(
        monkeypatch,
        [("run_subagent", {"agent_name": "coder", "task": "print the working directory"})],
        [("run_shell_command", {"args": "pwd"})],
        "the child's answer",
        "done",
    )
    storage = tmp_path / "storage"
    result = await run_turn(
        _config(delegate_to=["coder"]),
        agent="coder",
        prompt="go",
        storage=storage,
        workdir=workdir,
    )

    assert (result.text, result.tool_calls) == ("done", 1)
    assert len(provider.requests) == 4
    assert _last_tool_result(provider.requests[2]).splitlines()[-1] == str(workdir)
    assert _last_tool_result(provider.requests[3]).startswith("the child's answer")
    assert _files(workdir) == []
    assert any(name.endswith("/run.json") for name in _files(storage))


@pytest.mark.asyncio
async def test_delegation_stops_at_depth_three(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The third nested copy is offered no `run_subagent`; its call fails as a tool error, and every turn completes."""
    deeper = [("run_subagent", {"agent_name": "coder", "task": "go deeper"})]
    provider = _install(monkeypatch, deeper, deeper, deeper, deeper, "c3", "c2", "c1", "lead")
    result = await run_turn(_config(delegate_to=["coder"]), agent="coder", prompt="go", storage=tmp_path / "storage")

    assert (result.text, result.completed) == ("lead", True)
    offered = ["run_subagent" in [tool.name for tool in request.tools] for request in provider.requests]
    assert offered == [True, True, True, False, False, True, True, True]
    assert _last_tool_result(provider.requests[4]) == "Error: The requested tool does not exist or is not available."


@pytest.mark.asyncio
@pytest.mark.parametrize("in_subagent", [False, True])
async def test_cancellation_stops_shell_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    in_subagent: bool,
) -> None:
    """Cancelling the call during a long shell command raises promptly and leaves no process running."""
    marker = f"sleep 613.{int(in_subagent)}25"
    shell = [("run_shell_command", {"args": marker, "timeout": 900})]
    steps = [[("run_subagent", {"agent_name": "coder", "task": "wait"})], shell] if in_subagent else [shell]
    _install(monkeypatch, *steps)
    turn = asyncio.create_task(
        run_turn(_config(delegate_to=["coder"]), agent="coder", prompt="wait", storage=tmp_path / "storage"),
    )
    async with asyncio.timeout(30):
        while not _processes_running(marker):  # noqa: ASYNC110 - polls a process another program starts
            await asyncio.sleep(0.01)

    started = time.monotonic()
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    assert time.monotonic() - started < 5
    assert _processes_running(marker) == []


def _plugin(tmp_path: Path) -> str:
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "mindroom.plugin.json").write_text(json.dumps({"name": "demo"}))
    return str(plugin)


def _credentialed_storage(tmp_path: Path, config: dict[str, Any]) -> None:
    storage = tmp_path / "storage"
    runtime_paths = resolve_runtime_paths(config_path=storage / "config.yaml", storage_path=storage)
    validated = validate_config(config, agent="coder", storage=storage)
    persist_offline_entity_accounts(validated, runtime_paths, password="pw")  # noqa: S106 - a fixture account


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (
            lambda c, _p: c.update(teams={"crew": {"display_name": "Crew", "role": "r", "agents": ["coder"]}}),
            "'crew' is a team",
        ),
        (lambda c, _p: c["agents"]["coder"].update(worker_tools=["shell"]), "'coder': shell run in a worker"),
        (lambda c, _p: c.pop("defaults"), "shell runs in this process; set defaults.allow_shell_without_worker: true"),
        (
            lambda c, _p: (
                c.update(mcp_servers={"docs": {"transport": "stdio", "command": "true"}})
                or c["agents"]["coder"]["tools"].append("mcp_docs")
            ),
            "MCP tools need a running MindRoom: mcp_docs",
        ),
        (lambda c, _p: c.update(administrators=[]), "administrators is empty"),
        (lambda c, _p: c.update(requester_id="@bob:localhost"), "'@bob:localhost' is not in administrators"),
        (
            lambda c, p: (
                c.update(knowledge_bases={"docs": {"path": str(p)}})
                or c["agents"]["coder"].update(knowledge_bases=["docs"])
            ),
            "knowledge bases need a running MindRoom",
        ),
        (
            lambda c, _p: c["agents"]["coder"].update(memory_backend="file", memory_search={"mode": "semantic"}),
            "semantic memory_search needs a running MindRoom",
        ),
        (lambda c, p: c.update(plugins=[_plugin(p)]), "plugins need a running MindRoom"),
        (lambda c, p: _credentialed_storage(p, c), "give headless runs a storage of their own"),
    ],
    ids=[
        "team",
        "worker-tools",
        "shell-without-worker",
        "mcp",
        "no-administrators",
        "non-administrator",
        "knowledge",
        "semantic-memory",
        "plugins",
        "storage",
    ],
)
@pytest.mark.asyncio
async def test_run_turn_refuses_what_needs_a_running_mindroom(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: Any,  # noqa: ANN401
    problem: str,
) -> None:
    """Each refusal names its setting, and the turn never reaches the model."""
    provider = _install(monkeypatch)
    config = _config(delegate_to=["helper"])
    config["agents"]["helper"] = {"display_name": "Helper"}
    change(config, tmp_path)
    requester_id = config.pop("requester_id", None)
    agent = "crew" if "teams" in config else "coder"
    with pytest.raises(HeadlessConfigError) as raised:
        await run_turn(config, agent=agent, prompt="hi", storage=tmp_path / "storage", requester_id=requester_id)

    assert any(problem in item for item in raised.value.problems), raised.value.problems
    assert provider.requests == []


@pytest.mark.asyncio
async def test_refusals_cover_the_subagents_an_agent_may_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A subagent's worker tools are refused too, since its calls would run in the same turn."""
    _install(monkeypatch)
    config = _config(delegate_to=["helper"])
    config["agents"]["helper"] = {"display_name": "Helper", "tools": ["shell"], "worker_tools": ["shell"]}
    with pytest.raises(HeadlessConfigError, match="agent 'helper': shell run in a worker"):
        await run_turn(config, agent="coder", prompt="hi", storage=tmp_path / "storage")


@pytest.mark.asyncio
async def test_mem0_memory_runs_headless(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mem0 memories are recalled into the turn as they are in Matrix."""
    memory = AsyncMock()
    memory.search.return_value = {"results": [{"id": "m1", "memory": "The user likes PINEAPPLE", "score": 0.9}]}
    provider = _install(monkeypatch, "noted")
    config = _config()
    config.pop("memory")
    with patch("mindroom.memory._backend.create_memory_instance", return_value=memory):
        result = await run_turn(config, agent="coder", prompt="what do I like?", storage=tmp_path / "storage")

    assert result.completed
    assert "PINEAPPLE" in str([message.content for message in provider.requests[0].messages])


@pytest.mark.asyncio
async def test_workdir_is_shell_cwd_and_file_base(
    tmp_path: Path,
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`shell` runs in the workdir and `file` resolves relative names against it; MindRoom's state stays in storage."""
    provider = _install(
        monkeypatch,
        [("run_shell_command", {"args": "pwd"}), ("save_file", {"contents": "a", "file_name": "a.txt"})],
        "done",
    )
    storage = tmp_path / "storage"
    monkeypatch.chdir(tmp_path)
    await run_turn(_config(), agent="coder", prompt="go", storage=storage, workdir=Path(workdir.name))

    shell_result = next(message for message in provider.requests[1].messages if message.role == "tool")
    assert str(shell_result.content).splitlines()[-1] == str(workdir)
    assert _files(workdir) == ["a.txt"]
    assert (storage / "agents" / "coder" / "sessions" / "coder.db").is_file()


@pytest.mark.asyncio
async def test_default_base_dir_is_the_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a workdir, the tools work in the agent's workspace, as they do in Matrix."""
    provider = _install(monkeypatch, [("run_shell_command", {"args": "pwd"})], "done")
    storage = tmp_path / "storage"
    await run_turn(_config(), agent="coder", prompt="go", storage=storage)

    assert _last_tool_result(provider.requests[1]).splitlines()[-1] == str(storage / "agents" / "coder" / "workspace")


class _ReadsSavedOutput(ScriptedProvider):
    """Read a large file, then read back the start of the saved output by the path its receipt names."""

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        if len(self.requests) == 2:
            receipt = ast.literal_eval(_last_tool_result(request))
            path = receipt["mindroom_tool_output"]["path"]
            self.steps.insert(0, [("read_file_chunk", {"file_name": path, "start_line": 0, "end_line": 1})])
        async for event in super().stream(request):
            yield event


@pytest.mark.asyncio
@pytest.mark.parametrize("file_access", ["workspace", "unrestricted"])
async def test_saved_output_is_read_back_through_file(
    tmp_path: Path,
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_access: str,
) -> None:
    """A tool output over the save threshold stays in storage, and `file` reads it back by its receipt's path."""
    provider = _ReadsSavedOutput(
        steps=[
            [("run_shell_command", {"args": "seq 1 20000 > big.txt"})],
            [("read_file", {"file_name": "big.txt"})],
            "done",
        ],
    )
    install_model_providers(monkeypatch, {"default": provider})
    storage = tmp_path / "storage"
    await run_turn(_config(file_access=file_access), agent="coder", prompt="go", storage=storage, workdir=workdir)

    assert _last_tool_result(provider.requests[3]).splitlines() == ["1", "2"]
    assert _files(workdir) == ["big.txt"]
    (saved,) = (storage / "agents" / "coder" / "workspace" / "mindroom_tool_outputs").iterdir()
    assert saved.read_text().startswith("1\n2\n")


@pytest.mark.asyncio
async def test_shell_redirect_receipt_names_the_saved_file(
    tmp_path: Path,
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shell command redirected with `mindroom_output_path` in workdir mode names the file it wrote by absolute path."""
    provider = _install(
        monkeypatch,
        [("run_shell_command", {"args": "seq 1 3", "mindroom_output_path": "out.txt"})],
        "done",
    )
    storage = tmp_path / "storage"
    await run_turn(_config(), agent="coder", prompt="go", storage=storage, workdir=workdir)

    receipt = json.loads(_last_tool_result(provider.requests[1]))["mindroom_tool_output"]
    saved = storage / "agents" / "coder" / "workspace" / "mindroom_tool_outputs" / "out.txt"
    assert receipt["path"] == str(saved.resolve())
    assert saved.read_text().endswith("1\n2\n3\n")
    assert _files(workdir) == []


def test_shell_output_destination_keeps_the_workdir_flag_across_the_supervisor() -> None:
    """A worker's shell supervisor receives the destination as a payload, so the flag must survive it."""
    destination = ShellOutputDestination(workspace_root="/w", path="out.txt", max_bytes=1, tools_use_workdir=True)

    assert ShellOutputDestination.from_payload(dataclasses.asdict(destination)) == destination


def test_validate_config_returns_the_config_or_names_every_problem(tmp_path: Path) -> None:
    """A config headless runs can serve validates to its `Config`; a refusal lists each problem, not only the first."""
    assert set(validate_config(_config(), agent="coder", storage=tmp_path / "storage").agents) == {"coder"}
    with pytest.raises(HeadlessConfigError) as raised:
        validate_config(_config(), agent="ghost", storage=tmp_path / "storage", requester_id="@bob:localhost")

    assert raised.value.problems == [
        "no agent named 'ghost' is configured",
        "requester '@bob:localhost' is not in administrators",
    ]


@pytest.mark.asyncio
async def test_workdir_may_be_a_link_under_home(tmp_path: Path, workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A workdir given as `~/<link to a directory>` is that directory, for `file` as for `shell`."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "link").symlink_to(workdir)
    provider = _install(
        monkeypatch,
        [("save_file", {"contents": "a", "file_name": "a.txt"}), ("run_shell_command", {"args": "pwd"})],
        "done",
    )
    await run_turn(_config(), agent="coder", prompt="go", storage=tmp_path / "storage", workdir=Path("~/link"))

    assert _files(workdir) == ["a.txt"]
    assert _last_tool_result(provider.requests[1]).splitlines()[-1] == str(workdir.resolve())


def _chat_chunk(delta: dict[str, object], finish: str | None = None) -> bytes:
    choice = {"index": 0, "delta": delta, "finish_reason": finish}
    body = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m", "choices": [choice]}
    return f"data: {json.dumps(body)}\n\n".encode()


@pytest.mark.asyncio
async def test_concurrent_turns_on_one_loop_keep_their_model_connections(tmp_path: Path) -> None:
    """A turn that ends while another still streams from its model leaves the other's connection open."""

    async def completions(request: web.Request) -> web.StreamResponse:
        body = await request.json()
        slow = "slow" in json.dumps(body["messages"])
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for part in ("one ", "two ", "three"):
            await response.write(_chat_chunk({"content": part}))
            if slow:
                await asyncio.sleep(0.3)
        await response.write(_chat_chunk({}, "stop"))
        await response.write(b"data: [DONE]\n\n")
        return response

    app = web.Application()
    app.router.add_post("/v1/chat/completions", completions)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    config = _config()
    config["models"]["default"] = {
        "provider": "openai",
        "id": "m",
        "api": "chat_completions",
        "extra_kwargs": {"base_url": f"http://127.0.0.1:{port}/v1", "api_key": "unused"},
    }
    try:
        fast, slow = await asyncio.gather(
            run_turn(config, agent="coder", prompt="fast", storage=tmp_path / "fast"),
            run_turn(config, agent="coder", prompt="slow", storage=tmp_path / "slow"),
        )
    finally:
        await runner.cleanup()

    assert (fast.text, fast.completed) == ("one two three", True)
    assert (slow.text, slow.completed) == ("one two three", True)
