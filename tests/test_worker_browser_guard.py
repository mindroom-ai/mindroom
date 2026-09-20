"""Authenticated loopback URL verifier behavior."""

import asyncio
import inspect
import json
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from mindroom.worker_computer import browser_guard
from mindroom.worker_computer.browser_guard import BrowserURLVerifier


def _verification_reader(verifier: BrowserURLVerifier) -> asyncio.StreamReader:
    """Supply one authenticated request entirely in memory."""
    body = json.dumps({"url": "https://fixture.example/"}).encode()
    reader = asyncio.StreamReader()
    reader.feed_data(
        (
            "POST /verify HTTP/1.1\r\n"
            f"Authorization: Bearer {verifier.token}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode()
        + body,
    )
    return reader


@pytest.mark.asyncio
@pytest.mark.parametrize("late_error", [False, True])
async def test_cancelled_request_retains_validation_capacity(
    monkeypatch: pytest.MonkeyPatch,
    *,
    late_error: bool,
) -> None:
    """A timed-out waiter cannot free capacity while its validation still runs."""
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    calls = 0

    def validate(url: str, *, allow_private_networks: bool, allow_loopback: bool) -> str:
        nonlocal calls
        assert not allow_private_networks
        assert not allow_loopback
        calls += 1
        if calls == 1:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
            if late_error:
                msg = "fixture validation failed after waiter cancellation"
                raise RuntimeError(msg)
        return url

    monkeypatch.setattr(browser_guard, "validate_browser_fetch_url", validate)
    monkeypatch.setattr(browser_guard, "_MAX_CONNECTIONS", 1)
    verifier = BrowserURLVerifier()
    request = asyncio.create_task(verifier._verify_request(_verification_reader(verifier)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        await verifier.close()
        assert await verifier._verify_request(_verification_reader(verifier)) == (503, False)
        assert calls == 1
    finally:
        release.set()
        request.cancel()
        await asyncio.gather(request, *getattr(verifier, "_validations", ()), return_exceptions=True)
        await verifier.close()
    assert await verifier._verify_request(_verification_reader(verifier)) == (200, True)


@pytest.mark.asyncio
async def test_verifier_bounds_connections_at_accept_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Excess clients are closed before a handler can queue validation work."""
    server = Mock(spec=asyncio.Server)
    server.sockets = [SimpleNamespace(getsockname=lambda: ("127.0.0.1", 12345))]
    server.wait_closed = AsyncMock()
    listener = AsyncMock(return_value=server)
    monkeypatch.setattr(asyncio, "start_server", listener)
    monkeypatch.setattr(browser_guard, "_MAX_CONNECTIONS", 2, raising=False)
    verifier = BrowserURLVerifier()
    await verifier.start()
    accept = listener.call_args.args[0]
    writers = [Mock(spec=asyncio.StreamWriter) for _ in range(3)]
    scheduled = []
    try:
        for writer in writers:
            writer.wait_closed = AsyncMock()
            writer.transport = Mock()
            callback = accept(asyncio.StreamReader(), writer)
            if inspect.isawaitable(callback):
                scheduled.append(asyncio.create_task(callback))
        assert [writer.close.called for writer in writers] == [False, False, True]
    finally:
        for task in scheduled:
            task.cancel()
        await asyncio.gather(*scheduled, return_exceptions=True)
        await verifier.close()
    assert all(writer.transport.abort.called for writer in writers[:2])
    late_writer = Mock(spec=asyncio.StreamWriter)
    accept(asyncio.StreamReader(), late_writer)
    late_writer.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(("private", "loopback"), [(False, False), (True, False), (False, True)])
async def test_verifier_auth_and_address_policy(private: bool, loopback: bool) -> None:
    """Private opt-in never allows metadata; callback is authenticated."""
    verifier = BrowserURLVerifier(allow_private_networks=private, allow_loopback=loopback)
    await verifier.start()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(verifier.endpoint, json={"url": "http://127.0.0.1"})
            assert response.status_code == 403
            headers = {"Authorization": "Bearer " + verifier.token}
            for url, allowed in [
                ("http://127.0.0.1", private or loopback),
                ("http://localhost:5173", private or loopback),
                ("http://[::1]:5173", private or loopback),
                ("http://10.0.0.1", private),
                ("http://169.254.169.254", False),
                ("http://8.8.8.8", True),
                ("file:///etc/passwd", False),
            ]:
                response = await client.post(verifier.endpoint, json={"url": url}, headers=headers)
                assert response.json() == {"allowed": allowed}
            response = await client.post(verifier.endpoint, json={"url": "x" * 8193}, headers=headers)
            assert response.json() == {"allowed": False}
    finally:
        await verifier.close()


@pytest.mark.asyncio
async def test_guard_hook_concurrent_install_and_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The packaged JS hook awaits one shared route and aborts malformed/failed callbacks."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for packaged init hook")
    script = tmp_path / "guard-test.cjs"
    script.write_text("""
const assert = require('node:assert/strict');
const hook = require(process.argv[2]).default;
let finish, handler, installations=0;
const context = {route: async (_pattern, callback) => {installations++; handler=callback; await new Promise(r=>finish=r);}};
const page = {context:()=>context};
(async()=>{
 let completed=0;
 const first=hook({page}).then(()=>completed++), second=hook({page}).then(()=>completed++);
 await new Promise(r=>setImmediate(r)); assert.equal(installations,1); assert.equal(completed,0);
 finish(); await Promise.all([first,second]); assert.equal(completed,2);
 const results=[];
 const route={request:()=>({url:()=> 'http://127.0.0.1/'}),continue:async()=>results.push('allow'),abort:async()=>results.push('deny')};
 for(const body of ['{"allowed":true}', '{"allowed":false}', '{"allowed":true,"extra":1}', '{}', 'no']) {
  global.fetch=async()=>({ok:true,text:async()=>body}); await handler(route);
 }
 global.fetch=async()=>{throw Error('timeout')}; await handler(route);
 assert.deepEqual(results,['allow','deny','deny','deny','deny','deny']);
 console.log('guard verified');
})().catch(error=>{console.error(error);process.exit(1)});
""")
    monkeypatch.setenv("MINDROOM_BROWSER_VERIFY_ENDPOINT", "http://127.0.0.1:1/verify")
    monkeypatch.setenv("MINDROOM_BROWSER_VERIFY_TOKEN", "fixture")
    process = await asyncio.create_subprocess_exec(
        node,
        str(script),
        str(Path(browser_guard.__file__).with_suffix(".cjs")),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    assert b"guard verified" in stdout
