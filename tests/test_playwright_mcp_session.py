"""Shared MCP actor lifetime and serialized calls."""

import asyncio
import contextlib
import json
import os
import shutil
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from mcp import StdioServerParameters
from mcp.types import CallToolResult, ListToolsResult, TextContent

from mindroom.playwright_mcp_session import PlaywrightMCPSession


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record actual actor ordering and context cleanup around an async session seam."""
    events: list[str] = []

    class Session:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "Session":
            events.append("session")
            return self

        async def __aexit__(self, *_args: object) -> None:
            events.append("closed")

        async def initialize(self) -> None:
            events.append("initialize")

        async def list_tools(self) -> ListToolsResult:
            return ListToolsResult(tools=[])

        async def call_tool(self, name: str, _arguments: dict[str, object], **_kwargs: object) -> CallToolResult:
            events.append(name)
            if name == "stuck":
                await asyncio.Event().wait()
            await asyncio.sleep(0.01)
            events.append(name + ":done")
            return CallToolResult(content=[TextContent(type="text", text=name)])

    @asynccontextmanager
    async def stdio(_parameters: StdioServerParameters) -> AsyncIterator[tuple[object, object]]:
        try:
            writer, reader = anyio.create_memory_object_stream(0)
            yield reader, writer
        finally:
            events.append("reaped")

    monkeypatch.setattr("mindroom.playwright_mcp_session.ClientSession", Session)
    monkeypatch.setattr("mindroom.playwright_mcp_session.stdio_client", stdio)
    return events


@pytest.mark.asyncio
async def test_shared_actor_serializes_calls_and_closes(transport: list[str]) -> None:
    """Calls run sequentially and entered transport resources exit exactly once."""
    actor = PlaywrightMCPSession(StdioServerParameters(command="unused"), call_timeout_seconds=1)
    assert not actor.running
    assert await actor.list_tools() == ()
    await asyncio.gather(actor.call_tool("one", {}), actor.call_tool("two", {}))
    await actor.close()
    assert transport == ["session", "initialize", "one", "one:done", "two", "two:done", "closed", "reaped"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["timeout", "cancel", "close"])
async def test_stuck_call_releases_transport(transport: list[str], operation: str) -> None:
    """Abandoned work cannot block bounded closure of the owning transport."""
    actor = PlaywrightMCPSession(StdioServerParameters(command="unused"), call_timeout_seconds=0.05)
    call = asyncio.create_task(actor.call_tool("stuck", {}))
    while "stuck" not in transport:  # noqa: ASYNC110 - observe fake collaborator dispatch
        await asyncio.sleep(0)
    if operation == "cancel":
        call.cancel()
    elif operation == "close":
        await asyncio.wait_for(actor.close(), 0.5)
    with pytest.raises((TimeoutError, asyncio.CancelledError, RuntimeError)):
        await call
    assert not actor.running
    assert transport[-2:] == ["closed", "reaped"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["timeout", "cancel", "crash", "startup", "close"])
@pytest.mark.parametrize("detached", [False, True])
async def test_real_stdio_child_tree_is_reaped(tmp_path: Path, operation: str, detached: bool) -> None:
    """A real Node child cannot outlive timeout, cancellation, or server crash."""
    node = shutil.which("node")
    if node is None or sys.platform != "linux":
        pytest.skip("Linux subreaper and Node fixture required")
    script = tmp_path / "server.cjs"
    pidfile = tmp_path / "child.pid"
    script.write_text("""
const fs = require('fs');
const child = require('child_process').spawn(process.execPath, ['-e', 'setInterval(()=>{},1000)'], {stdio:'ignore', detached:process.argv[3] === 'true'});
fs.writeFileSync(process.argv[2], String(child.pid));
require('readline').createInterface({input:process.stdin}).on('line', line => {
  const request = JSON.parse(line);
  if (request.method === 'initialize' && process.argv[4] !== 'startup') {
    setTimeout(() => console.log(JSON.stringify({jsonrpc:'2.0',id:request.id,result:{protocolVersion:'2025-11-25',capabilities:{tools:{}},serverInfo:{name:'fixture',version:'1'}}})), process.argv[4] === 'close' ? 50 : 0);
  } else if (request.method === 'tools/call' && request.params.name === 'crash') process.exit(1);
});
setInterval(()=>{},1000);
""")
    session = PlaywrightMCPSession(
        StdioServerParameters(command=node, args=[str(script), str(pidfile), str(detached).lower(), operation]),
        call_timeout_seconds=0.3,
    )
    call = asyncio.create_task(session.call_tool("crash" if operation == "crash" else "stuck", {}))
    async with asyncio.timeout(2):
        while not pidfile.exists():  # noqa: ASYNC110 - external child readiness file
            await asyncio.sleep(0.01)
    pid = int(pidfile.read_text())
    if operation == "cancel":
        call.cancel()
    elif operation == "close":
        await asyncio.wait_for(session.close(), 6)
    try:
        with pytest.raises((TimeoutError, asyncio.CancelledError, ExceptionGroup, RuntimeError)):
            await asyncio.wait_for(call, 6)
        await asyncio.wait_for(session.close(), 6)
        stat = Path(f"/proc/{pid}/stat")
        assert not stat.exists() or stat.read_text().split()[2] == "Z"
    finally:
        # Clean only the child created by this fixture when RED exposes a leak.
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, 9)


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [0, 23])
async def test_live_supervisor_reaps_adopted_exits_and_preserves_main_status(tmp_path: Path, exit_code: int) -> None:
    """Adopted zombies disappear during service while live children and main status survive."""
    node = shutil.which("node")
    if node is None or sys.platform != "linux":
        pytest.skip("Linux subreaper and Node fixture required")
    script = tmp_path / "live-server.cjs"
    script.write_text("""
const {spawn} = require('child_process');
const orphan = `
const {spawn} = require('child_process');
const short = spawn(process.execPath, ['-e', 'setTimeout(()=>process.exit(0),150)'], {stdio:'ignore', detached:true});
const live = spawn(process.execPath, ['-e', 'setInterval(()=>{},1000)'], {stdio:'ignore', detached:true});
console.log(JSON.stringify([short.pid, live.pid]));
short.unref(); live.unref();
`;
require('readline').createInterface({input:process.stdin}).on('line', line => {
  if (line === 'spawn') spawn(process.execPath, ['-e', orphan], {stdio:['ignore','inherit','inherit']});
  else if (line === 'ping') console.log('alive');
  else if (line === 'exit') process.exit(Number(process.argv[2]));
});
""")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-m",
        "mindroom.playwright_mcp_process",
        node,
        str(script),
        str(exit_code),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    children: list[int] = []
    try:
        for _ in range(6):
            process.stdin.write(b"spawn\n")
            await process.stdin.drain()
            pids = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
            children.extend(pids)
            short, live = (Path(f"/proc/{pid}") for pid in pids)
            async with asyncio.timeout(2):
                while short.exists():  # noqa: ASYNC110 - observe actual kernel child reaping
                    await asyncio.sleep(0.01)
            assert live.exists()
            assert (live / "stat").read_text().split()[2] != "Z"
            process.stdin.write(b"ping\n")
            await process.stdin.drain()
            assert await asyncio.wait_for(process.stdout.readline(), 2) == b"alive\n"
            assert process.returncode is None
        process.stdin.write(b"exit\n")
        await process.stdin.drain()
        assert await asyncio.wait_for(process.wait(), 5) == exit_code
    finally:
        if process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), 5)
    assert all(not Path(f"/proc/{pid}").exists() for pid in children)
