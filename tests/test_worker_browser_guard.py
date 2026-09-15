"""Authenticated loopback URL verifier behavior."""

import asyncio
import shutil
from pathlib import Path

import httpx
import pytest

from mindroom.worker_computer import browser_guard
from mindroom.worker_computer.browser_guard import BrowserURLVerifier


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_verifier_auth_and_address_policy(private: bool) -> None:
    """Private opt-in never allows metadata; callback is authenticated."""
    verifier = BrowserURLVerifier(allow_private_networks=private)
    await verifier.start()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(verifier.endpoint, json={"url": "http://127.0.0.1"})
            assert response.status_code == 403
            headers = {"Authorization": "Bearer " + verifier.token}
            for url, allowed in [
                ("http://127.0.0.1", private),
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
