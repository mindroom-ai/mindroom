"""Headless runs: one configured agent's turn from another Python program, without Matrix or `mindroom run`."""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import json
import pickle
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import httpx
import nio
import pytest

from mindroom import headless, logging_config
from mindroom.constants import resolve_runtime_paths
from mindroom.headless import HeadlessConfigError, HeadlessRun, run_turn, validate_config
from mindroom.llm.media import File
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, complete
from mindroom.llm.request_context import bind_request_headers
from mindroom.matrix.state import matrix_state_for_runtime
from mindroom.providers import model_http_client
from mindroom.providers.errors import missing_api_key_error
from mindroom.shell.output_capture import ShellOutputDestination
from tests.conftest import install_model_providers, requires_linux
from tests.fakes.scripted_model import Await, ScriptedModel, answer, tool_call
from tests.identity_helpers import persist_entity_accounts
from tests.minimal_agent_fixtures import ScriptedProvider, offered_tools
from tests.providers.anthropic_family_support import claude_message
from tests.providers.family_support import FAMILIES
from tests.providers.golden_support import Wire, pipeline_provider, streamed_reply

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    import httpx2

    from mindroom.llm.provider import StreamEvent


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
    provider.install(monkeypatch)
    return provider


def _last_tool_result(request: ModelRequest) -> str:
    return str([message for message in request.messages if message.role == "tool"][-1].content)


def _sent(request: ModelRequest, role: str | None = None) -> str:
    """Return the content of a request's messages, or only of those with ``role``, as one string."""
    return str([message.content for message in request.messages if role is None or message.role == role])


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


@pytest.fixture(autouse=True)
def _structlog_logging_after_the_test(monkeypatch: pytest.MonkeyPatch) -> None:
    """A headless call switches the process to library logging; later tests on this worker log through structlog."""
    monkeypatch.setattr(logging_config, "_library_logging", False)


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
    sent = _sent(provider.requests[0])
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
    config = _config()
    config["mindroom_user"] = {"username": "mindroom_owner"}
    result = await run_turn(config, agent="coder", prompt="hi", storage=storage)

    assert (result.text, result.completed) == ("hello", True)
    runtime_paths = resolve_runtime_paths(config_path=storage / "config.yaml", storage_path=storage)
    accounts = matrix_state_for_runtime(runtime_paths).accounts
    assert {"agent_router", "agent_coder", "agent_user"} <= set(accounts)
    assert accounts["agent_user"].username == "mindroom_owner"
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
    offered = ["run_subagent" in offered_tools(request) for request in provider.requests]
    assert offered == [True, True, True, False, False, True, True, True]
    assert _last_tool_result(provider.requests[4]) == "Error: The requested tool does not exist or is not available."


@requires_linux(reason="finds the shell's child process through /proc")
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
    persist_entity_accounts(validate_config(config, agent="coder", storage=storage), runtime_paths)


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        pytest.param(
            lambda c, _p: c.update(teams={"crew": {"display_name": "Crew", "role": "r", "agents": ["coder"]}}),
            "'crew' is a team",
            id="team",
        ),
        pytest.param(
            lambda c, _p: c["agents"]["coder"].update(worker_tools=["shell"]),
            "agent 'coder' runs shell in a worker",
            id="worker-tools",
        ),
        pytest.param(
            lambda c, _p: c["agents"]["helper"].update(tools=["shell"], worker_tools=["shell"]),
            "agent 'helper' runs shell in a worker",
            id="subagent-worker-tools",
        ),
        pytest.param(
            lambda c, _p: c.pop("defaults"),
            "shell runs in this process; set defaults.allow_shell_without_worker: true",
            id="shell-without-worker",
        ),
        pytest.param(
            lambda c, _p: c.pop("defaults") and c["agents"]["coder"].update(worker_tools=["shell"]),
            "shell runs in this process; set defaults.allow_shell_without_worker: true",
            id="worker-shell-without-worker",
        ),
        pytest.param(
            lambda c, _p: (
                c.update(mcp_servers={"docs": {"transport": "stdio", "command": "true"}})
                or c["agents"]["coder"]["tools"].append("mcp_docs")
            ),
            "MCP tools need a running MindRoom: mcp_docs",
            id="mcp",
        ),
        pytest.param(lambda c, _p: c.update(administrators=[]), "administrators is empty", id="no-administrators"),
        pytest.param(
            lambda c, p: (
                c.update(knowledge_bases={"docs": {"path": str(p)}})
                or c["agents"]["coder"].update(knowledge_bases=["docs"])
            ),
            "knowledge bases need a running MindRoom",
            id="knowledge",
        ),
        pytest.param(
            lambda c, _p: c["agents"]["coder"].update(memory_backend="file", memory_search={"mode": "semantic"}),
            "semantic memory_search needs a running MindRoom",
            id="semantic-memory",
        ),
        pytest.param(lambda c, p: c.update(plugins=[_plugin(p)]), "plugins need a running MindRoom", id="plugins"),
        pytest.param(
            lambda c, _p: (
                c.update(knowledge_graph={"model": "default"}) or c["agents"]["coder"].update(knowledge_graph=True)
            ),
            "the knowledge graph needs a running MindRoom",
            id="knowledge-graph",
        ),
        pytest.param(
            lambda c, p: _credentialed_storage(p, c),
            "give headless runs a storage of their own",
            id="storage",
        ),
    ],
)
@pytest.mark.asyncio
async def test_run_turn_refuses_what_needs_a_running_mindroom(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: Callable[[dict[str, Any], Path], object],
    problem: str,
) -> None:
    """Each refusal names its setting, and the turn never reaches the model; one case refuses a subagent's tools."""
    provider = _install(monkeypatch)
    config = _config(delegate_to=["helper"])
    config["agents"]["helper"] = {"display_name": "Helper"}
    change(config, tmp_path)
    agent = "crew" if "teams" in config else "coder"
    with pytest.raises(HeadlessConfigError) as raised:
        await run_turn(config, agent=agent, prompt="hi", storage=tmp_path / "storage")

    assert any(problem in item for item in raised.value.problems), raised.value.problems
    assert provider.requests == []


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
    assert "PINEAPPLE" in _sent(provider.requests[0])


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
    # The notes on writing into the workspace are left out, since the tools resolve relative paths in the workdir.
    system = _sent(provider.requests[0], "system")
    assert "MINDROOM_AGENT_WORKSPACE" not in system
    assert "skills/<skill-name>/" not in system


@pytest.mark.asyncio
async def test_run_turn_refuses_a_missing_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A workdir that is not an existing directory is refused with the config's own problems, also by validation."""
    provider = _install(monkeypatch)
    config = _config()
    config.pop("defaults")
    with pytest.raises(HeadlessConfigError) as raised:
        await run_turn(config, agent="coder", prompt="go", storage=tmp_path / "storage", workdir=tmp_path / "nope")

    assert raised.value.problems == [
        "agent 'coder': shell runs in this process; set defaults.allow_shell_without_worker: true",
        f"workdir {tmp_path / 'nope'} is not an existing directory",
    ]
    assert provider.requests == []
    with pytest.raises(HeadlessConfigError, match="is not an existing directory"):
        validate_config(_config(), agent="coder", storage=tmp_path / "storage", workdir=tmp_path / "nope")


@pytest.mark.asyncio
async def test_a_disabled_plugin_is_not_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A plugin entry with `enabled: false` never loads, so it needs no running MindRoom."""
    _install(monkeypatch, "hello")
    config = _config()
    config["plugins"] = [{"path": _plugin(tmp_path), "enabled": False}]
    result = await run_turn(config, agent="coder", prompt="hi", storage=tmp_path / "storage")
    assert result.completed


@pytest.mark.asyncio
async def test_default_base_dir_is_the_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a workdir, the tools work in the agent's workspace, as they do in Matrix."""
    provider = _install(monkeypatch, [("run_shell_command", {"args": "pwd"})], "done")
    storage = tmp_path / "storage"
    await run_turn(_config(), agent="coder", prompt="go", storage=storage)

    assert _last_tool_result(provider.requests[1]).splitlines()[-1] == str(storage / "agents" / "coder" / "workspace")
    system = _sent(provider.requests[0], "system")
    assert "MINDROOM_AGENT_WORKSPACE" in system
    assert "skills/<skill-name>/" in system


@dataclasses.dataclass
class _ReadsSavedOutput(ScriptedProvider):
    """Read a large file, then read back the start of the saved output by the path its receipt names, or write it."""

    write_back: bool = False

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        if len(self.requests) == 2:
            receipt = ast.literal_eval(_last_tool_result(request))
            path = receipt["mindroom_tool_output"]["path"]
            call = (
                ("save_file", {"contents": "x", "file_name": path})
                if self.write_back
                else ("read_file_chunk", {"file_name": path, "start_line": 0, "end_line": 1})
            )
            self.steps.insert(0, [call])
        async for event in super().stream(request):
            yield event


def _reads_saved_output(*, write_back: bool = False) -> _ReadsSavedOutput:
    return _ReadsSavedOutput(
        steps=[
            [("run_shell_command", {"args": "seq 1 20000 > big.txt"})],
            [("read_file", {"file_name": "big.txt"})],
            "done",
        ],
        write_back=write_back,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("file_access", ["workspace", "unrestricted"])
async def test_saved_output_is_read_back_through_file(
    tmp_path: Path,
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_access: str,
) -> None:
    """A tool output over the save threshold stays in storage, and `file` reads it back by its receipt's path."""
    provider = _reads_saved_output()
    provider.install(monkeypatch)
    storage = tmp_path / "storage"
    await run_turn(_config(file_access=file_access), agent="coder", prompt="go", storage=storage, workdir=workdir)

    assert _last_tool_result(provider.requests[3]).splitlines() == ["1", "2"]
    assert _files(workdir) == ["big.txt"]
    (saved,) = (storage / "agents" / "coder" / "workspace" / "mindroom_tool_outputs").iterdir()
    assert saved.read_text().startswith("1\n2\n")


@pytest.mark.asyncio
async def test_saved_outputs_are_read_only_under_workspace_access(
    tmp_path: Path,
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under `file_access: workspace`, `file` may read the saved outputs beside a workdir but not write them."""
    provider = _reads_saved_output(write_back=True)
    provider.install(monkeypatch)
    storage = tmp_path / "storage"
    await run_turn(_config(), agent="coder", prompt="go", storage=storage, workdir=workdir)

    assert "is in the saved tool outputs directory you can only read." in _last_tool_result(provider.requests[3])
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
    with pytest.raises(HeadlessConfigError, match="requester '' is not in administrators"):
        validate_config(_config(), agent="coder", storage=tmp_path / "storage", requester_id="")
    with pytest.raises(HeadlessConfigError) as raised:
        validate_config(_config(), agent="ghost", storage=tmp_path / "storage", requester_id="@bob:localhost")

    assert raised.value.problems == [
        "no agent named 'ghost' is configured",
        "requester '@bob:localhost' is not in administrators",
    ]


# A program that imports MindRoom as a library: argv is its structlog setup, a plugin directory, and a storage.
_LIBRARY_PROGRAM = """
import sys
from pathlib import Path

import structlog

if sys.argv[1] == "structlog":
    structlog.configure(wrapper_class=structlog.stdlib.BoundLogger, logger_factory=structlog.stdlib.LoggerFactory())

from mindroom.headless import HeadlessConfigError, validate_config
from mindroom.logging_config import get_logger

config = {
    "administrators": ["@alice:localhost"],
    "models": {"default": {"provider": "openai", "id": "scripted"}},
    "agents": {"coder": {"display_name": "Coder"}},
    "plugins": [sys.argv[2]],
}
try:
    validate_config(config, agent="coder", storage=Path(sys.argv[3]))
except HeadlessConfigError:
    pass


def fail(extra_header):
    git_env = {"GIT_CONFIG_VALUE_0": extra_header}
    raise ValueError(f"api_key=api-secret, git failed with {', '.join(git_env)} configured")


# Only `fail`'s frame locals hold this value, which no redaction rule matches; the source lines show its name.
header = "frame-local-" + "header"
try:
    fail(header)
except ValueError:
    get_logger("mindroom.example").exception("Request failed")
get_logger("mindroom.example").info("Prompt sent")
"""


@pytest.mark.parametrize("structlog_setup", ["none", "structlog"])
def test_a_library_program_gets_warnings_on_stderr_and_nothing_on_stdout(tmp_path: Path, structlog_setup: str) -> None:
    """MindRoom's records reach stderr at WARNING and above, redacted and with one traceback, whatever structlog setup."""
    completed = subprocess.run(
        [sys.executable, "-c", _LIBRARY_PROGRAM, structlog_setup, _plugin(tmp_path), str(tmp_path / "storage")],
        capture_output=True,
        check=True,
        text=True,
        timeout=120,
    )

    assert completed.stdout == ""
    assert completed.stderr.count("Loading non-bundled plugin") == 1
    assert completed.stderr.count("Traceback (most recent call last):") == 1
    assert "git failed with GIT_CONFIG_VALUE_0 configured" in completed.stderr
    assert "api-secret" not in completed.stderr
    assert "frame-local-header" not in completed.stderr
    assert "Prompt sent" not in completed.stderr


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


@pytest.mark.asyncio
async def test_concurrent_turns_close_model_connections_once_after_the_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn that ends while another still runs leaves the loop's pooled model connections to the last one to end."""
    slow_streaming, fast_done = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        slow_streaming.set()
        await fast_done.wait()

    install_model_providers(monkeypatch, {"default": ScriptedModel([[Await(hold), *answer("slow")], answer("fast")])})
    closes: list[bool] = []

    async def close() -> None:
        closes.append(fast_done.is_set())

    monkeypatch.setattr(headless, "aclose_model_http_client", close)
    slow_turn = asyncio.create_task(run_turn(_config(), agent="coder", prompt="slow", storage=tmp_path / "slow"))
    await asyncio.wait_for(slow_streaming.wait(), 10)
    fast = await run_turn(_config(), agent="coder", prompt="fast", storage=tmp_path / "fast")
    fast_done.set()
    slow = await slow_turn

    assert (fast.text, slow.text) == ("fast", "slow")
    assert closes == [True]


def test_a_refusal_survives_pickling() -> None:
    """A refusal pickled, as a process pool sends it from its worker, keeps its message and problems."""
    error = pickle.loads(pickle.dumps(HeadlessConfigError(["first problem", "second problem"])))  # noqa: S301

    assert (str(error), error.problems) == ("first problem; second problem", ["first problem", "second problem"])


@pytest.mark.asyncio
async def test_file_memory_runs_headless(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent's file memory reaches its prompt as it does in Matrix."""
    provider = _install(monkeypatch, "noted")
    storage = tmp_path / "storage"
    memory = storage / "agents" / "coder" / "workspace" / "MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("The user likes PINEAPPLE.\n")
    config = _config(memory_backend="file")
    config.pop("memory")
    result = await run_turn(config, agent="coder", prompt="what do I like?", storage=storage)

    assert result.completed
    assert "PINEAPPLE" in _sent(provider.requests[0])


@pytest.mark.asyncio
async def test_subagent_redirect_receipt_names_the_saved_file(
    tmp_path: Path,
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `run_subagent` answer redirected with `mindroom_output_path` beside a workdir is saved with the saved outputs."""
    call = {"agent_name": "coder", "task": "answer", "mindroom_output_path": "child.txt"}
    provider = _install(monkeypatch, [("run_subagent", call)], "the child's answer", "done")
    storage = tmp_path / "storage"
    await run_turn(_config(delegate_to=["coder"]), agent="coder", prompt="go", storage=storage, workdir=workdir)

    saved = storage / "agents" / "coder" / "workspace" / "mindroom_tool_outputs" / "child.txt"
    receipt = ast.literal_eval(_last_tool_result(provider.requests[2]))["mindroom_tool_output"]
    assert receipt["path"] == str(saved.resolve())
    assert saved.read_text().startswith("the child's answer")
    assert _files(workdir) == []


_PROFILE = "---\ndescription: A helper\n---\nYou are HELPER-PROFILE.\n"


@dataclasses.dataclass
class _FollowsTheWorkspacePath(ScriptedProvider):
    """Save a profile at the workspace the prompt names, run it, and read the child's record the receipt names.

    Without a workspace path in the prompt, the profile path stays relative, as the profile description reads.
    """

    root: str = ""

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        if not self.requests:
            found = re.search(r"Your workspace, [^`]* is `([^`]+)`", _sent(request, "system"))
            self.root = f"{found.group(1)}/" if found else ""
            path = f"{self.root}subagents/helper.md"
            self.steps = [[("save_file", {"contents": _PROFILE, "file_name": path})], *self.steps]
        if len(self.requests) == 3:
            record = re.search(r"record: (?:\w+:)?(\S+)", _last_tool_result(request))
            self.steps.insert(0, [("read_file", {"file_name": f"{record.group(1) if record else ''}/run.json"})])
        async for event in super().stream(request):
            yield event


@pytest.mark.asyncio
async def test_a_profile_and_a_record_are_found_where_the_prompt_says(
    tmp_path: Path,
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Beside a workdir the prompt names the workspace, so a profile `file` saves there runs and its record reads."""
    provider = _FollowsTheWorkspacePath(steps=[[("run_subagent", {"task": "go", "profile": "helper"})], "ran", "done"])
    provider.install(monkeypatch)
    storage = tmp_path / "storage"
    config = _config(delegate_to=["coder"], file_access="unrestricted")
    await run_turn(config, agent="coder", prompt="go", storage=storage, workdir=workdir)

    assert provider.root == f"{storage.resolve() / 'agents' / 'coder' / 'workspace'}/"
    assert "You are HELPER-PROFILE." in _sent(provider.requests[2], "system")
    assert _last_tool_result(provider.requests[3]).startswith("ran")
    assert json.loads(_last_tool_result(provider.requests[4]))["status"] == "completed"
    assert _files(workdir) == []


@dataclasses.dataclass
class _ReadsTheReceiptRecord(ScriptedProvider):
    """Delegate, then read the child's record where the receipt names it, as a model would."""

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        if len(self.requests) == 2:
            record = re.search(r"record: (?:\w+:)?(\S+)", _last_tool_result(request))
            self.steps.insert(0, [("read_file", {"file_name": f"{record.group(1) if record else ''}/run.json"})])
        async for event in super().stream(request):
            yield event


@pytest.mark.asyncio
async def test_another_agents_record_is_found_where_the_receipt_says(
    tmp_path: Path,
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Beside a workdir, a receipt names another agent's record by its path in that agent's workspace."""
    provider = _ReadsTheReceiptRecord(
        steps=[[("run_subagent", {"agent_name": "helper", "task": "go"})], "helped", "done"],
    )
    provider.install(monkeypatch)
    config = _config(delegate_to=["helper"], file_access="unrestricted")
    config["agents"]["helper"] = {"display_name": "Helper"}
    await run_turn(config, agent="coder", prompt="go", storage=tmp_path / "storage", workdir=workdir)

    assert json.loads(_last_tool_result(provider.requests[3]))["status"] == "completed"
    assert _files(workdir) == []


@pytest.mark.asyncio
async def test_the_prompt_says_where_the_tools_work_beside_a_workdir(
    tmp_path: Path,
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """File access names the workdir for `file` and `coding`, and output paths are relative to the saved outputs."""
    provider = _install(monkeypatch, "done")
    await run_turn(_config(), agent="coder", prompt="go", storage=tmp_path / "storage", workdir=workdir)

    system = _sent(provider.requests[0], "system")
    assert "(`file` and `coding`: the workdir and your saved tool outputs, read-only outside the workdir;" in system
    assert f"- Workdir: `{workdir}`, where `shell` runs and `file` and `coding` resolve relative paths; " in system
    assert "code that `python` runs uses this process's current directory, so give it absolute paths." in system
    (save_file,) = (tool for tool in provider.requests[0].tools if tool.name == "save_file")
    description = save_file.parameters["properties"]["mindroom_output_path"]["description"]
    assert description.startswith("Optional MindRoom-managed path relative to your saved tool outputs directory.")


@pytest.mark.asyncio
async def test_the_identity_names_no_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A headless agent is told it answers a program, not that the OpenAI-compatible API exposes it."""
    provider = _install(monkeypatch, "done")
    await run_turn(_config(), agent="coder", prompt="go", storage=tmp_path / "storage")

    system = _sent(provider.requests[0], "system")
    assert "You are Coder, the MindRoom agent `coder`, answering a prompt from another program." in system
    assert "OpenAI-compatible" not in system


@pytest.mark.asyncio
async def test_an_authored_matrix_identity_still_opens_the_headless_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator's own Matrix identity prompt still applies headless, without a Matrix ID or an API to name."""
    provider = _install(monkeypatch, "done")
    config = _config()
    template = "I am {display_name} ({matrix_id}).\n{openai_compat_history_guidance}"
    config["prompts"] = {"AGENT_IDENTITY_CONTEXT_TEMPLATE": template}
    await run_turn(config, agent="coder", prompt="go", storage=tmp_path / "storage")

    system = _sent(provider.requests[0], "system")
    assert "I am Coder (not available in a headless run)." in system
    assert "You are Coder, the MindRoom agent `coder`, answering a prompt from another program." in system
    assert "OpenAI-compatible" not in system


@pytest.mark.asyncio
async def test_a_missing_provider_key_names_the_variable_and_the_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no dashboard to open, the reply names the key's variable and the `.env` file that may hold it."""

    async def no_key() -> None:
        error = missing_api_key_error("OPENAI_API_KEY", provider="OpenAI")
        raise error

    install_model_providers(monkeypatch, {"default": ScriptedModel([[Await(no_key)]])})
    storage = tmp_path / "storage"
    result = await run_turn(_config(), agent="coder", prompt="go", storage=storage)

    assert result.failed
    assert result.text == (
        "[coder] 🔑 No AI provider key is set up yet, so I can't reply. "
        "OPENAI_API_KEY not set. Please set the OPENAI_API_KEY environment variable. "
        f"A `.env` file at {storage.resolve() / '.env'} works too."
    )


# A model on each route that sends a run's request headers, with headers its config authors under the names they set.
# Authored in other letter cases than the callback's names, which still name the same headers.
_AUTHORED_HEADERS = {"X-Run": "authored", "x-static": "kept"}
_AUTHORED_EXTRA_HEADERS = {"X-Depth": "authored", "x-extra": "kept"}
_OPENAI_ROUTE = {
    "provider": "openai",
    "id": "gpt-6-astra",
    "api_key": "test-key",
    "extra_kwargs": {
        "base_url": "https://gateway.example/v1",
        "default_headers": _AUTHORED_HEADERS,
        "extra_headers": _AUTHORED_EXTRA_HEADERS,
    },
}
_HEADER_ROUTES: dict[str, dict[str, Any]] = {
    "chat": {**_OPENAI_ROUTE, "api": "chat_completions"},
    "responses": {**_OPENAI_ROUTE, "api": "responses"},
    "anthropic": {
        "provider": "anthropic",
        "id": "claude-sonnet-5-5",
        "api_key": "test-key",
        "extra_kwargs": {
            "base_url": "https://gateway.example",
            "default_headers": _AUTHORED_HEADERS,
            "request_params": {"extra_headers": _AUTHORED_EXTRA_HEADERS},
        },
    },
}
_SUMMARY_PROMPT_MARKER = "durable conversation handoff summary"

type _Reply = str | tuple[str, dict[str, object]]


def _wire_reply(route: str, reply: _Reply, number: int) -> httpx.Response:
    """Return ``reply``, a text or one tool call, as ``route`` streams it; ``number`` keeps its IDs unique in the turn."""
    text, call = (reply, None) if isinstance(reply, str) else ("", reply)
    if route == "chat":
        message: dict[str, Any] = {"role": "assistant", "content": text or None}
        if call is not None:
            message["tool_calls"] = [tool_call(call[0], f"call_{number}", **call[1])]
        choice = {"index": 0, "message": message, "finish_reason": "stop" if call is None else "tool_calls"}
        usage = {"prompt_tokens": 40, "completion_tokens": 9, "total_tokens": 49}
        body = {"id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "gpt-6-astra"}
        return streamed_reply({**body, "choices": [choice], "usage": usage})
    if route == "responses":
        item = (
            {
                "id": f"msg_{number}",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
            if call is None
            else {
                "id": f"fc_{number}",
                "type": "function_call",
                "call_id": f"call_{number}",
                "name": call[0],
                "arguments": json.dumps(call[1]),
                "status": "completed",
            }
        )
        usage = {"input_tokens": 40, "output_tokens": 9, "total_tokens": 49}
        body = {"id": f"resp_{number}", "object": "response", "created_at": 1, "model": "gpt-6-astra"}
        return streamed_reply({**body, "status": "completed", "output": [item], "usage": usage})
    block = (
        {"type": "text", "text": text}
        if call is None
        else {"type": "tool_use", "id": f"toolu_{number}", "name": call[0], "input": call[1]}
    )
    stop_reason = "end_turn" if call is None else "tool_use"
    message = claude_message([block], stop_reason=stop_reason, usage={"input_tokens": 40, "output_tokens": 9})
    message["id"] = f"msg_{number}"
    return streamed_reply(message)


def _model_server(monkeypatch: pytest.MonkeyPatch, route: str, *replies: _Reply | Callable[[str], _Reply]) -> Wire:
    """Answer the turn's model requests on ``route`` with ``replies`` in order, and a summary request with a summary.

    A callable reply is called with the request's body. The returned wire records every request sent.
    """
    pending = list(replies)

    def respond(request: httpx2.Request) -> httpx.Response:
        body = request.content.decode()
        reply = "the summary" if _SUMMARY_PROMPT_MARKER in body else pending.pop(0)
        return _wire_reply(route, reply if isinstance(reply, str | tuple) else reply(body), len(wire.requests))

    wire = Wire(respond)
    monkeypatch.setattr(model_http_client, "_loop_client", lambda: wire.client)
    return wire


def _continue_first_subagent(body: str) -> _Reply:
    """Continue the subagent whose receipt the lead's request holds."""
    match = re.search(r"Subagent ID: (\w+)", body)
    assert match is not None
    return ("continue_subagent", {"subagent_id": match[1], "message": "once more"})


def _header_config(route: str, **model: object) -> dict[str, Any]:
    """The lead `coder` delegates to `helper`, which delegates back, on a model served on ``route``."""
    config = _config(delegate_to=["helper"])
    config["models"] = {"default": {**_HEADER_ROUTES[route], **model}}
    config["agents"]["helper"] = {"display_name": "Helper", "tools": ["shell"], "delegate_to": ["coder"]}
    return config


def _run_headers(run: HeadlessRun) -> dict[str, str]:
    return {
        "x-agent": run.agent_name,
        "x-run": run.subagent_id or "lead",
        "x-parent": run.parent_subagent_id or "-",
        "x-depth": str(run.depth),
    }


def _sent_run(request: httpx2.Request) -> tuple[str, ...]:
    return tuple(request.headers[name] for name in ("x-agent", "x-run", "x-parent", "x-depth"))


@pytest.mark.asyncio
@pytest.mark.parametrize("route", _HEADER_ROUTES)
async def test_request_headers_follow_the_run(route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each model request carries the headers of the run that sent it, over the ones the model's config authors.

    The lead's subagent runs a nested one, and the lead's follow-up to its subagent keeps that subagent's ID.
    """
    wire = _model_server(
        monkeypatch,
        route,
        ("run_subagent", {"agent_name": "helper", "task": "plan it"}),
        ("run_subagent", {"agent_name": "coder", "task": "check it"}),
        "checked",
        "planned",
        _continue_first_subagent,
        "planned again",
        "done",
    )
    result = await run_turn(
        _header_config(route),
        agent="coder",
        prompt="go",
        storage=tmp_path / "storage",
        request_headers=_run_headers,
    )

    assert result.text == "done"
    runs = [_sent_run(request) for request in wire.requests]
    child_id, nested_id = runs[1][1], runs[2][1]
    lead, child, nested = (
        ("coder", "lead", "-", "0"),
        ("helper", child_id, "-", "1"),
        ("coder", nested_id, child_id, "2"),
    )
    assert len({"lead", child_id, nested_id}) == 3
    assert runs == [lead, child, nested, child, lead, child, lead]
    assert {(request.headers["x-static"], request.headers["x-extra"]) for request in wire.requests} == {
        ("kept", "kept"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("route", _HEADER_ROUTES)
async def test_requests_carry_the_authored_headers_without_request_headers(
    route: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a callback, a request carries the headers the model's config authors, not an earlier turn's callback ones."""
    wire = _model_server(monkeypatch, route, "done", "done")
    config = _header_config(route)
    await run_turn(config, agent="coder", prompt="go", storage=tmp_path / "storage", request_headers=_run_headers)
    await run_turn(config, agent="coder", prompt="go", storage=tmp_path / "storage")

    request = wire.requests[-1]
    assert {name: request.headers.get(name) for name in ("x-run", "x-static", "x-depth", "x-extra", "x-agent")} == {
        "x-run": "authored",
        "x-static": "kept",
        "x-depth": "authored",
        "x-extra": "kept",
        "x-agent": None,
    }


@pytest.mark.asyncio
async def test_compaction_request_carries_headers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A follow-up to a subagent that asked to compact sends its summary request with that subagent's headers."""
    wire = _model_server(
        monkeypatch,
        "chat",
        ("run_subagent", {"agent_name": "helper", "task": "plan it"}),
        ("compact_context", {}),
        "planned",
        _continue_first_subagent,
        "planned again",
        "done",
    )
    config = _header_config("chat", context_window=200_000)
    config["agents"]["helper"]["tools"] = ["compact_context"]
    await run_turn(config, agent="coder", prompt="go", storage=tmp_path / "storage", request_headers=_run_headers)

    runs = [_sent_run(request) for request in wire.requests]
    summaries = [
        index for index, request in enumerate(wire.requests) if _SUMMARY_PROMPT_MARKER in request.content.decode()
    ]
    child_id = runs[1][1]
    assert child_id != "lead"
    # The follow-up compacts its history before its first request, right after the lead's request that continued it.
    assert summaries == [4]
    assert runs[4] == ("helper", child_id, "-", "1")


@pytest.mark.asyncio
async def test_headers_not_sent_to_other_hosts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A media URL the route does not send is downloaded without the run's headers, which only the model request reads."""
    media = Wire(httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.4\n%%EOF\n"))
    monkeypatch.setattr(model_http_client, "_loop_client", lambda: media.client)
    family = FAMILIES["openai_chat"]
    wire = Wire(family.reply())
    headers = Mock(return_value={"x-run": "lead"})
    file = File(url="https://media.example/report.pdf")
    with bind_request_headers(headers):
        await complete(
            pipeline_provider(family.adapter(tmp_path, wire)),
            ModelRequest(messages=[Message(role="user", content="Read it.", files=[file])]),
        )

    (download,) = media.requests
    assert (download.url.host, download.headers.get("x-run")) == ("media.example", None)
    assert [request.headers.get("x-run") for request in wire.requests] == ["lead"]
    headers.assert_called_once_with()
