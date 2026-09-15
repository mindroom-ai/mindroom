"""Exercise the real Docker worker, public computer gateway and native noVNC.

Use --help for inputs. All identities and services belong to disposable local
fixtures. --serve keeps the gateway alive for the Chat Playwright spec.
"""

from __future__ import annotations

# ruff: noqa: N999 - command name follows the existing script CLI convention
import argparse
import asyncio
import hashlib
import json
import secrets
import shutil
import socket
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

import httpx
import uvicorn
import yaml
from agno.tools.function import ToolResult
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright
from testing.worker_computer_matrix import create_matrix_fixture
from testing.worker_computer_native import connect_viewer, native_json, native_tabs

from mindroom.api import computers, config_lifecycle
from mindroom.api.main import _RuntimeDashboardCorsMiddleware
from mindroom.config.main import Config
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    build_agent_toolkit_worker_target,
    resolve_worker_key,
    tool_execution_identity,
)
from mindroom.worker_computer.mcp_results import decode_browser_mcp_result
from mindroom.worker_computer.sessions import ComputerError, ComputerTarget
from mindroom.workers.models import WorkerSpec
from mindroom.workers.runtime import shutdown_primary_worker_manager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from playwright.async_api import Page

    from mindroom.workers.models import WorkerHandle

ASSETS = Path(__file__).resolve().parents[1] / "tests/fixtures/worker_computer"
SECCOMP_PROFILE = Path(__file__).resolve().parents[1] / "src/mindroom/workers/backends/seccomp/worker-computer.json"
WORKER_CONTEXT_PATH_SCRIPT = """import sys
from mindroom.api.sandbox_exec import runner_storage_root
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system.worker_routing import resolve_agent_owned_path
print(resolve_agent_owned_path(
    sys.argv[1], agent_name='writer',
    base_storage_path=runner_storage_root(resolve_runtime_paths()),
))
"""
FRAMEBUFFER = """() => {
    const c = document.querySelector('canvas');
    if (!c || c.width !== 1280 || c.height !== 800) return false;
    const p = c.getContext('2d').getImageData(1200, 700, 1, 1).data;
    return p[0] === 17 && p[1] === 51 && p[2] === 85 && p[3] === 255;
}"""


def loopback_origin(value: str) -> str:
    """Reject fixture inputs that could contact an external service."""
    url = urlsplit(value)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"} or url.path not in {"", "/"}:
        message = "Fixture services must use a loopback HTTP origin."
        raise argparse.ArgumentTypeError(message)
    return value.rstrip("/")


async def command(*args: str) -> str:
    """Run a fixture-owned process and surface failures."""
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE)
    output, _ = await process.communicate()
    assert process.returncode == 0, args
    return output.decode().strip()


class Fixture:
    """Local authorization boundary around the real gateway and Docker manager."""

    def __init__(self, args: argparse.Namespace, origin: str, *, owned_matrix_id: str | None = None) -> None:
        self.args = args
        self.origin = origin
        self.owned_matrix_id = owned_matrix_id
        self.token = secrets.token_urlsafe(32)
        self.container_ids: set[str] = set()
        self.container_security: dict[str, dict[str, Any]] = {}
        self.matrix = json.loads(args.matrix_fixture.read_text()) if args.matrix_fixture else None
        if self.matrix:
            loopback_origin(self.matrix["homeserver"])
        self.server_name = self.matrix["server_name"] if self.matrix else "computer.localhost"
        self.room = self.matrix["room_id"] if self.matrix else "!fixture:computer.localhost"
        self.agent = (
            self.matrix["users"]["mindroom_writer"]["user_id"] if self.matrix else "@mindroom_writer:computer.localhost"
        )
        self.viewer = self.matrix["users"]["computer_viewer"]["user_id"] if self.matrix else "@alice:computer.localhost"
        self.other = self.matrix["users"]["computer_other"]["user_id"] if self.matrix else "@bob:computer.localhost"
        workspace = args.output / "data/agents/writer/workspace"
        self.history = workspace / "thread_exports/thread.yaml"
        self.history.parent.mkdir(parents=True)
        self.history.write_text("messages: []\n")
        (workspace / "AGENTS.md").write_text("Browser fixture context\n")
        self.config = Config.model_validate(
            {
                "agents": {
                    "writer": {
                        "display_name": "Writer",
                        "role": "Browser fixture",
                        "model": "default",
                        "tools": [args.provider, "shell"],
                        "worker_tools": [args.provider, "shell"],
                        "worker_scope": "user_agent",
                        "knowledge_bases": ["threads"],
                        "context_files": ["AGENTS.md"],
                    },
                },
                "models": {"default": {"provider": "openai", "id": "test-model"}},
                "knowledge_bases": {"threads": {"path": str(self.history.parent)}},
            },
        )
        (args.output / "config.yaml").write_text(yaml.safe_dump(self.config.model_dump(mode="json")))
        self.paths = resolve_primary_runtime_paths(
            config_path=args.output / "config.yaml",
            storage_path=args.output / "data",
            process_env={
                "MATRIX_HOMESERVER": self.matrix["homeserver"] if self.matrix else origin,
                "MATRIX_SERVER_NAME": self.server_name,
                "MINDROOM_WORKER_COMPUTER_ENABLED": "1",
                "MINDROOM_COMPUTER_ALLOWED_ORIGINS": json.dumps(
                    [origin, *([args.chat_origin] if args.chat_origin else [])],
                ),
                "MINDROOM_WORKER_BACKEND": "docker",
                "MINDROOM_SANDBOX_PROXY_TOKEN": self.token,
                "MINDROOM_DOCKER_WORKER_IMAGE": args.image,
                "MINDROOM_DOCKER_WORKER_NAME_PREFIX": "mindroom-computer-test-" + secrets.token_hex(4),
                "MINDROOM_DOCKER_WORKER_READY_TIMEOUT_SECONDS": "90",
            },
        )

    def identity(self, user: str) -> ToolExecutionIdentity:
        """Use the same requester/agent scope as normal browser and shell routing."""
        return ToolExecutionIdentity(
            channel="matrix",
            agent_name="writer",
            requester_id=user,
            room_id=self.room,
            thread_id=None,
            resolved_thread_id=None,
            session_id="fixture",
        )

    def target(self, user: str) -> ComputerTarget:
        """Resolve the fixture's dedicated user-agent worker."""
        key = resolve_worker_key("user_agent", self.identity(user), agent_name="writer")
        return ComputerTarget(
            user,
            self.room,
            self.agent,
            WorkerSpec(key, private_agent_names=frozenset(), mirrored_credential_services=frozenset()),
            "local-fixture",
        )

    async def authorize(self, requester: str, room: str, agent: str) -> ComputerTarget:
        """Replace orchestrator policy only; optionally check actual local Matrix membership."""
        if requester not in {self.viewer, self.other} or (room, agent) != (self.room, self.agent):
            raise ComputerError(403, "Fixture access denied.")
        if self.matrix:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    self.matrix["homeserver"] + "/_matrix/client/v3/rooms/" + quote(room, safe="") + "/joined_members",
                    headers={"Authorization": "Bearer " + self.matrix["users"]["mindroom_writer"]["access_token"]},
                )
                response.raise_for_status()
                joined = response.json()["joined"]
                if requester not in joined or agent not in joined:
                    raise ComputerError(403, "Fixture membership denied.")
        return self.target(requester)

    async def reconcile_worker(self) -> WorkerHandle:
        """Exercise primary worker reconciliation before the next tool dispatch."""
        return await asyncio.to_thread(
            computers._resolve_worker,
            self.config,
            self.paths,
            self.target(self.viewer),
            start=True,
        )

    async def execute(
        self,
        tool: str,
        function: str,
        kwargs: dict[str, Any],
        *,
        user: str | None = None,
    ) -> dict[str, Any]:
        """Send an actual validated runner request to the configured primary manager worker."""
        user = user or self.viewer
        target = self.target(user)
        handle = await asyncio.to_thread(computers._resolve_worker, self.config, self.paths, target, start=False)
        container_id = await command("docker", "inspect", "--format", "{{.Id}}", handle.worker_id)
        if container_id not in self.container_ids:
            host_config = json.loads(
                await command("docker", "inspect", "--format", "{{json .HostConfig}}", handle.worker_id),
            )
            cap_drop = host_config.get("CapDrop") or []
            security_options = host_config.get("SecurityOpt") or []
            assert "ALL" in {str(capability).upper() for capability in cap_drop}
            assert "no-new-privileges:true" in security_options
            seccomp_options = [
                option.removeprefix("seccomp=")
                for option in security_options
                if isinstance(option, str) and option.startswith("seccomp=")
            ]
            assert len(seccomp_options) == 1
            expected_profile_sha256 = hashlib.sha256(SECCOMP_PROFILE.read_bytes()).hexdigest()
            actual_profile_sha256 = hashlib.sha256(seccomp_options[0].encode()).hexdigest()
            assert actual_profile_sha256 == expected_profile_sha256
            self.container_security[container_id] = {
                "cap_drop": cap_drop,
                "no_new_privileges": True,
                "seccomp_profile_sha256": actual_profile_sha256,
            }
        self.container_ids.add(container_id)
        (self.args.output / "containers.json").write_text(json.dumps(sorted(self.container_ids)) + "\n")
        (self.args.output / "container-security.json").write_text(
            json.dumps(self.container_security, indent=2, sort_keys=True) + "\n",
        )
        payload = {
            "tool_name": tool,
            "function_name": function,
            "worker_key": target.spec.worker_key,
            "worker_scope": "user_agent",
            "routing_agent_name": "writer",
            "execution_identity": asdict(self.identity(user)),
            "private_agent_names": [],
            "kwargs": kwargs,
        }
        if tool in {"browser", "browser_mcp"}:
            payload["tool_config_overrides"] = {"allow_private_networks": True}
        async with httpx.AsyncClient(timeout=100) as client:
            response = await client.post(
                handle.endpoint,
                headers={"X-Mindroom-Sandbox-Token": handle.auth_token},
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    async def browser(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401, PLR0911 - fixture provider translation
        """Return the public tool result, failing on runner errors."""
        if self.args.provider == "browser_mcp":
            action = kwargs["action"]
            if action in {"open", "navigate"}:
                if action == "navigate" and "targetId" in kwargs:
                    await self.native("browser_tabs", action="select", index=kwargs["targetId"])
                await self.native("browser_navigate", url=kwargs["targetUrl"])
                tabs = await self.browser(action="tabs")
                return {"targetId": tabs["activeTargetId"]}
            if action == "tabs":
                tabs = native_tabs((await self.native("browser_tabs", action="list")).content)
                assert tabs, "Native tab list is empty"
                return {"tabs": tabs, "activeTargetId": next(tab["index"] for tab in tabs if tab["current"])}
            if action == "focus":
                await self.native("browser_tabs", action="select", index=kwargs["targetId"])
                return {"targetId": kwargs["targetId"]}
            if action == "snapshot":
                return {"snapshot": (await self.native("browser_snapshot")).content}
            request = kwargs["request"]
            if request["kind"] == "evaluate":
                return {"result": native_json((await self.native("browser_evaluate", function=request["fn"])).content)}
            if request["kind"] == "click":
                return {"result": (await self.native("browser_click", target=request["ref"])).content}
            raise AssertionError(kwargs)
        body = await self.execute("browser", "browser_control", kwargs)
        assert body["ok"], body
        return json.loads(body["result"])

    async def native(self, function: str, **kwargs: Any) -> ToolResult:  # noqa: ANN401 - native tool JSON
        """Call the actual native entrypoint and retain a bounded text transcript."""
        body = await self.execute("browser_mcp", function, kwargs)
        assert body["ok"], body
        result = decode_browser_mcp_result(body["result"])
        assert isinstance(result, ToolResult), result
        with (self.args.output / "native-transcript.jsonl").open("a") as transcript:
            transcript.write(json.dumps({"function": function, "arguments": kwargs, "text": result.content}) + "\n")
        assert "### Error" not in result.content, result.content
        return result

    async def primary_native(self, function: str, **kwargs: Any) -> object:  # noqa: ANN401 - native tool JSON
        """Use the real primary toolkit/proxy, including scoped image decoding."""
        import mindroom.tools  # noqa: PLC0415, F401 - normal registry bootstrap

        identity = self.identity(self.viewer)
        target = build_agent_toolkit_worker_target(
            "user_agent",
            "writer",
            is_private=False,
            execution_identity=identity,
            runtime_paths=self.paths,
        )
        with tool_execution_identity(identity):
            toolkit = get_tool_by_name(
                "browser_mcp",
                self.paths,
                runtime_config=self.config,
                tool_config_overrides={"allow_private_networks": True},
                worker_tools_override=["browser_mcp", "shell"],
                worker_target=target,
            )
            return await toolkit.async_functions[function].entrypoint(**kwargs)

    async def native_files(self) -> dict[str, Any]:
        """Verify native upload/download paths and real primary text/media decoding."""
        await self.shell(["sh", "-c", "printf native-upload-content > native-upload.txt"])
        await self.native("browser_click", target="#upload")
        workspace = (await self.shell(["pwd"])).strip()
        await self.native("browser_file_upload", paths=[workspace + "/native-upload.txt"])
        async with asyncio.timeout(10):
            while await self.evaluate("()=>window.uploadContent") != "native-upload-content":  # noqa: ASYNC110
                await asyncio.sleep(0.05)
        assert await self.evaluate("()=>window.uploadName") == "native-upload.txt"
        image = await self.primary_native("browser_take_screenshot", type="png", scale="css")
        assert isinstance(image, ToolResult)
        assert image.images
        data = image.images[0].content
        assert isinstance(data, bytes)
        assert data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"))
        text = await self.primary_native("browser_snapshot")
        assert isinstance(text, ToolResult)
        assert "Remote text" in text.content
        named = await self.native("browser_take_screenshot", filename="native.png", type="png", scale="css")
        assert not named.images
        await self.native("browser_pdf_save", filename="native.pdf")
        files = await self.shell(
            [
                "node",
                "-e",
                "const fs=require('fs');for(const [p,h] of [['browser/native.png','89504e470d0a1a0a'],['browser/native.pdf','25504446']]){"
                "const b=fs.readFileSync(p);if(!b.toString('hex').startsWith(h))throw Error(p);console.log(p+':verified:'+b.length)}",
            ],
        )
        assert "native.png:verified:" in files
        assert "native.pdf:verified:" in files
        return {
            "upload_content_and_name": True,
            "named_image_and_pdf": True,
            "primary_inline_image_sha256": hashlib.sha256(data).hexdigest(),
            "primary_text": True,
        }

    async def evaluate(self, expression: str) -> Any:  # noqa: ANN401 - arbitrary page JSON
        """Read page state through the agent browser contract."""
        return (await self.browser(action="act", request={"kind": "evaluate", "fn": expression}))["result"]

    async def remove_owned_matrix(self) -> None:
        """Remove only the Matrix server created by this run, before signal re-raising."""
        if self.owned_matrix_id is None:
            return
        container_id = self.owned_matrix_id
        await command("docker", "rm", "-f", container_id)
        remaining = set((await command("docker", "ps", "-aq", "--no-trunc")).splitlines())
        assert container_id not in remaining
        self.owned_matrix_id = None
        (self.args.output / "matrix-cleanup.json").write_text(
            json.dumps({"container_id": container_id, "absent": True}) + "\n",
        )
        print(f"Exact fixture Matrix removed: {container_id}", flush=True)

    async def shell(self, args: list[str], *, background: bool = False, user: str | None = None) -> str:
        """Run commands in the exact worker used by the browser."""
        kwargs: dict[str, Any] = {"args": args}
        if background:
            kwargs["timeout"] = 0
        body = await self.execute("shell", "run_shell_command", kwargs, user=user)
        assert body["ok"], body
        return body["result"]

    async def prepare_page(self) -> dict[str, Any]:
        """Serve deterministic page/download content inside the worker loopback network."""
        html = json.dumps((ASSETS / "page.html").read_text())
        source = """require('http').createServer((req,res)=>{
if(req.url==='/download'){
 res.writeHead(200,{'Content-Type':'text/plain','Content-Disposition':'attachment; filename="fixture.txt"'});
 res.end('worker-shared-download-ok'); return;
}
res.writeHead(200,{'Content-Type':'text/html'});res.end(HTML);
}).listen(8767,'127.0.0.1');""".replace("HTML", html)
        await self.shell(["node", "-e", source], background=True)
        return await self.browser(action="open", targetUrl="http://127.0.0.1:8767/")

    @asynccontextmanager
    async def lifespan(self, app: FastAPI) -> AsyncIterator[None]:
        """Own only this run's manager, stream sessions and worker containers."""
        state = config_lifecycle.ensure_app_state(app)
        state.api_state = config_lifecycle.ApiState(
            threading.Lock(),
            config_lifecycle.ApiSnapshot(
                1,
                self.paths,
                self.config.model_dump(),
                runtime_config=self.config,
            ),
        )
        state.computer_runtime = computers.ComputerRuntime(self.authorize, 1, self.config)
        try:
            if self.args.serve:
                await asyncio.to_thread(
                    computers._resolve_worker,
                    self.config,
                    self.paths,
                    self.target(self.viewer),
                    start=True,
                )
                await self.prepare_page()
                metadata = {
                    "api_origin": self.origin,
                    "ui_origin": self.args.chat_origin,
                    "homeserver": self.matrix["homeserver"],
                    "room_id": self.room,
                    "thread_id": self.matrix["thread_id"],
                    "agent_user_id": self.agent,
                    "viewer": self.matrix["users"]["computer_viewer"],
                }
                path = self.args.output / "chat-fixture.json"
                path.touch(mode=0o600)
                path.write_text(json.dumps(metadata, indent=2) + "\n")
                print(f"Chat fixture ready: {path}", flush=True)
            yield
        finally:
            # Uvicorn re-raises shutdown signals after lifespan exit, before main's
            # outer finally can reliably run. Retire Matrix here, even if later
            # worker shutdown waits on a local browser driver handshake.
            try:
                await self.remove_owned_matrix()
            finally:
                if state.computer_sessions is not None:
                    state.computer_sessions.close_all()
                await asyncio.to_thread(shutdown_primary_worker_manager)
                # Verify exact IDs, never delete by a name prefix or global prune.
                remaining = set((await command("docker", "ps", "-aq", "--no-trunc")).splitlines())
                for container_id in sorted(self.container_ids & remaining):
                    await command("docker", "rm", "-f", container_id)
                remaining = set((await command("docker", "ps", "-aq", "--no-trunc")).splitlines())
                assert not self.container_ids & remaining
                print("Exact fixture workers removed.", flush=True)

    def app(self) -> FastAPI:
        """Expose actual public routes plus loopback-only fixture helpers."""
        app = FastAPI(lifespan=self.lifespan)
        app.include_router(computers.router)
        app.add_middleware(_RuntimeDashboardCorsMiddleware, api_app=app, fallback_runtime_paths=self.paths)
        if self.args.novnc:
            app.mount("/novnc", StaticFiles(directory=self.args.novnc))

        @app.get("/_matrix/federation/v1/openid/userinfo")
        async def openid(request: Request) -> JSONResponse:
            token = request.query_params.get("access_token", "")
            subjects = {"fixture-alice": self.viewer, "fixture-bob": self.other}
            if self.matrix or token not in subjects:
                return JSONResponse({"error": "Rejected fixture token"}, status_code=401)
            return JSONResponse({"sub": subjects[token]})

        @app.get("/")
        async def viewer() -> HTMLResponse:
            return HTMLResponse((ASSETS / "viewer.html").read_text())

        @app.get("/fixture/text")
        async def text_value() -> JSONResponse:
            # Test-only readback route; this fixture binds only to loopback.
            value = await self.evaluate("()=>document.querySelector('#shared-input').value")
            snapshot = await self.browser(action="snapshot")
            (self.args.output / "agent-snapshot.json").write_text(json.dumps(snapshot, indent=2) + "\n")
            return JSONResponse({"value": value, "snapshot": snapshot["snapshot"]})

        return app


async def connect(page: Page, session: dict[str, Any]) -> None:
    """Wait for a real connected noVNC stream."""
    await connect_viewer(page, session)


async def exercise(fixture: Fixture) -> dict[str, Any]:  # noqa: PLR0915 - sequential acceptance scenario
    """Check gateway auth, shared browser/files, takeover, foreground and persistence."""
    result: dict[str, Any] = {"provider": fixture.args.provider}
    async with httpx.AsyncClient(base_url=fixture.origin, timeout=100) as client:

        async def create(user: str = "alice") -> dict[str, Any]:
            response = await client.post(
                "/api/computers/sessions",
                json={
                    "openid_token": {
                        "access_token": "fixture-" + user,
                        "token_type": "Bearer",
                        "matrix_server_name": fixture.server_name,
                        "expires_in": 60,
                    },
                    "room_id": fixture.room,
                    "agent_user_id": fixture.agent,
                },
            )
            assert response.status_code == 200, response.text
            return response.json()

        denied = await client.post(
            "/api/computers/sessions",
            json={
                "openid_token": {
                    "access_token": "bad-token",
                    "token_type": "Bearer",
                    "matrix_server_name": fixture.server_name,
                    "expires_in": 60,
                },
                "room_id": fixture.room,
                "agent_user_id": fixture.agent,
            },
        )
        assert denied.status_code == 401
        session = await create()
        opened = await fixture.prepare_page()
        process_security = await fixture.shell(
            [
                "sh",
                "-c",
                "id; for p in /proc/[0-9]*; do "
                "[ -r \"$p/cmdline\" ] || continue; c=$(tr '\\0' ' ' < \"$p/cmdline\"); "
                'case "$c" in *chromium*|*sandbox_runner*) printf \'%s %s\\n\' "$p" "$c"; '
                "sed -n '/^Uid:/p;/^CapEff:/p;/^CapBnd:/p;/^NoNewPrivs:/p;/^Seccomp:/p' \"$p/status\";; esac; done",
            ],
        )
        (fixture.args.output / "worker-process-security.txt").write_text(process_security)
        assert "uid=1000" in process_security
        process_check = await fixture.shell(
            [
                "node",
                "-e",
                r"""
const fs=require('fs');let count=0;
if(process.getuid()!==1000)throw Error('worker uid');
for(const pid of fs.readdirSync('/proc').filter(p=>/^\d+$/.test(p))){
 let cmd,status;try{cmd=fs.readFileSync('/proc/'+pid+'/cmdline','utf8');
 status=fs.readFileSync('/proc/'+pid+'/status','utf8')}catch{continue}
 if(!cmd.split('\0')[0].endsWith('/chromium'))continue;
 const fields=Object.fromEntries(status.split('\n').filter(l=>/^(Uid|CapEff|NoNewPrivs|Seccomp):/.test(l)).map(l=>l.split(/:\s*/)));
 if(cmd.includes('--no-sandbox')||fields.CapEff!=='0000000000000000'||fields.NoNewPrivs!=='1'||fields.Seccomp!=='2')throw Error(JSON.stringify({pid,fields}));count++;
}
if(count<2)throw Error('browser subprocesses absent');console.log('SECURITY_VERIFIED');
""",
            ]
        )
        assert "SECURITY_VERIFIED" in process_check, process_check
        result["effective_browser_security"] = True
        sandbox_probe = """\
from playwright.sync_api import sync_playwright
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        executable_path="/usr/bin/chromium",
        headless=True,
        chromium_sandbox=True,
    )
    page = browser.new_page()
    page.goto("chrome://sandbox")
    print(page.locator("body").inner_text())
    browser.close()
"""
        sandbox_status = await fixture.shell(
            ["uv", "run", "--project", "/app", "--no-sync", "python", "-c", sandbox_probe],
        )
        assert "Layer 1 Sandbox\tNamespace" in sandbox_status
        assert "PID namespaces\tYes" in sandbox_status
        assert "Network namespaces\tYes" in sandbox_status
        assert "Seccomp-BPF sandbox\tYes" in sandbox_status
        assert "You are adequately sandboxed." in sandbox_status
        result["chromium_sandbox_status"] = sandbox_status
        tabs = await fixture.browser(action="tabs")
        assert tabs["activeTargetId"] == opened["targetId"]
        if fixture.args.provider == "browser_mcp":
            result["native_files"] = await fixture.native_files()
            await fixture.evaluate("()=>{window.fixtureContinuity='same-session';return true}")
            await fixture.native("browser_type", target="#shared-input", text="native-tool-text")
            assert "native-tool-text" in (await fixture.native("browser_snapshot")).content
            assert await fixture.evaluate("()=>window.fixtureContinuity") == "same-session"
            await fixture.native("browser_type", target="#shared-input", text="")
            result["native_session_continuity"] = True
        else:
            result["same_target_across_requests"] = opened["targetId"]
        await fixture.evaluate(
            "()=>{document.querySelector('#shared-input').focus();localStorage.setItem('persist','yes');}",
        )
        await fixture.shell(["sh", "-c", "printf alice-only > isolation-marker.txt"])
        await fixture.browser(action="act", request={"kind": "click", "ref": "#download"})
        async with asyncio.timeout(20):
            while "worker-shared-download-ok" not in await fixture.shell(  # noqa: ASYNC110 - remote filesystem readiness
                ["find", ".", "-name", "*fixture.txt", "-type", "f", "-exec", "cat", "{}", "+"],
            ):
                await asyncio.sleep(0.1)
        result["download_read_through_shell"] = True
        await fixture.evaluate("()=>document.querySelector('#shared-input').focus()")
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path=fixture.args.chromium,
                headless=True,
                args=["--no-sandbox"],
            )
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 800})
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on(
                    "requestfailed",
                    lambda request: (
                        (fixture.args.output / "viewer-request-failures.log")
                        .open("a")
                        .write(request.url + " " + str(request.failure) + "\n")
                    ),
                )
                await page.goto(fixture.origin)
                await connect(page, session)
                await page.wait_for_function(FRAMEBUFFER)
                await page.evaluate("window.rfb.sendKey(120,'KeyX')")
                await asyncio.sleep(0.2)
                assert await fixture.evaluate("()=>document.querySelector('#shared-input').value") == ""
                result["watch_input_rejected"] = True
                path = "/api/computers/sessions/" + session["session_id"]
                headers = {"Authorization": "Bearer " + session["session_token"]}
                # A real reply rewrites thread exports before its browser call.
                # The running worker and noVNC stream must survive that update.
                before_worker = await asyncio.to_thread(
                    computers._resolve_worker,
                    fixture.config,
                    fixture.paths,
                    fixture.target(fixture.viewer),
                    start=False,
                )
                before_container = await command("docker", "inspect", "--format", "{{.Id}}", before_worker.worker_id)
                before_tabs = await fixture.browser(action="tabs")
                before_target = before_tabs["activeTargetId"]
                assert before_target == opened["targetId"]
                before_session = await client.get(path, headers=headers)
                assert before_session.status_code == 200
                before_session_id = before_session.json()["session_id"]
                assert before_session_id == session["session_id"]
                fixture.history.write_text("messages: [navigate to another website]\n")
                (fixture.history.parent.parent / "AGENTS.md").write_text("Updated browser fixture context\n")
                after_worker = await fixture.reconcile_worker()
                navigated = await fixture.browser(
                    action="navigate",
                    targetId=opened["targetId"],
                    targetUrl="http://127.0.0.1:8767/?after-chat=1",
                )
                assert navigated["targetId"] == before_target
                after_container = await command("docker", "inspect", "--format", "{{.Id}}", after_worker.worker_id)
                assert after_container == before_container
                worker_config = yaml.safe_load(
                    await command("docker", "exec", after_container, "sh", "-c", 'cat "$MINDROOM_CONFIG_PATH"'),
                )
                history_path = worker_config["knowledge_bases"]["threads"]["path"] + "/thread.yaml"
                # Context paths resolve from canonical agent state, whereas the
                # fixture's shell intentionally keeps its worker scratch cwd.
                context_path = await command(
                    "docker",
                    "exec",
                    after_container,
                    "uv",
                    "run",
                    "--project",
                    "/app",
                    "--no-sync",
                    "python",
                    "-c",
                    WORKER_CONTEXT_PATH_SCRIPT,
                    worker_config["agents"]["writer"]["context_files"][0],
                )
                history_output = await fixture.shell(["cat", history_path])
                context_output = await fixture.shell(["cat", context_path])
                assert "messages: [navigate to another website]" in history_output.splitlines(), history_output
                assert "Updated browser fixture context" in context_output.splitlines(), context_output
                current_session = await client.get(path, headers=headers)
                assert current_session.status_code == 200
                assert current_session.json()["session_id"] == before_session_id
                await page.wait_for_function(FRAMEBUFFER)
                assert await page.evaluate("window.probe.connected && !window.probe.disconnected")
                result["chat_update_container_id"] = after_container
                result["chat_updates_visible_in_worker"] = True
                await fixture.evaluate("()=>document.querySelector('#shared-input').focus()")
                result["chat_updates_preserve_connection"] = True

                async def control(action: str) -> dict[str, Any]:
                    response = await client.post(path + "/control", headers=headers, json={"action": action})
                    assert response.status_code == 200, response.text
                    return response.json()

                assert (await control("take"))["mode"] == "control"
                await page.evaluate("window.rfb.sendKey(121,'KeyY')")
                await asyncio.sleep(0.2)
                blocked = await fixture.execute(
                    fixture.args.provider,
                    "browser_tabs" if fixture.args.provider == "browser_mcp" else "browser_control",
                    {"action": "list" if fixture.args.provider == "browser_mcp" else "tabs"},
                )
                assert not blocked["ok"], blocked
                assert "control" in json.dumps(blocked).lower(), blocked
                await control("release")
                await page.wait_for_function("window.probe.disconnected")
                async with asyncio.timeout(10):
                    while await fixture.evaluate("()=>document.querySelector('#shared-input').value") != "y":  # noqa: ASYNC110 - remote browser input readiness
                        await asyncio.sleep(0.05)
                snapshot = await fixture.browser(action="snapshot")
                assert ": y" in snapshot["snapshot"], snapshot
                (fixture.args.output / "agent-snapshot.json").write_text(json.dumps(snapshot, indent=2) + "\n")
                result["agent_snapshot_sees_typed_text"] = True
                result["takeover_and_release"] = True
                await connect(page, session)
                await page.wait_for_function(FRAMEBUFFER)
                await page.screenshot(path=str(fixture.args.output / "typed-visible.png"))

                for action in ("focus", "navigate"):
                    await control("take")
                    # Native Ctrl+T changes Chromium's tab strip, bypassing tool state.
                    await page.evaluate("""() => {
                        window.rfb.sendKey(0xffe3, 'ControlLeft', true);
                        window.rfb.sendKey(116, 'KeyT');
                        window.rfb.sendKey(0xffe3, 'ControlLeft', false);
                    }""")
                    await page.wait_for_function("!(" + FRAMEBUFFER + ")()")
                    await control("release")
                    await page.wait_for_function("window.probe.disconnected")
                    native_tabs = await fixture.browser(action="tabs")
                    assert any(tab["title"] == "New Tab" for tab in native_tabs["tabs"]), native_tabs
                    if fixture.args.provider == "browser_mcp":
                        opened["targetId"] = next(
                            tab["index"] for tab in native_tabs["tabs"] if tab["url"] == "http://127.0.0.1:8767/"
                        )
                    selected = await fixture.browser(
                        action=action,
                        targetId=opened["targetId"],
                        targetUrl="http://127.0.0.1:8767/",
                    )
                    assert selected["targetId"] == opened["targetId"]
                    await connect(page, session)
                    await page.wait_for_function(FRAMEBUFFER)
                    await page.screenshot(path=str(fixture.args.output / (action + "-visible.png")))
                    result[action + "_visibly_selected"] = True

                await page.evaluate("window.rfb.disconnect()")
                await page.wait_for_function("window.probe.disconnected")
                await connect(page, session)
                assert (await control("stop"))["state"] == "stopped"
                await page.wait_for_function("window.probe.disconnected")
                assert (await client.get(path, headers=headers)).status_code == 401
                new_session = await create()
                assert new_session["session_id"] != session["session_id"]
                await fixture.browser(action="open", targetUrl="http://127.0.0.1:8767/")
                assert await fixture.evaluate("()=>localStorage.getItem('persist')") == "yes"
                assert "worker-shared-download-ok" in await fixture.shell(
                    ["find", ".", "-name", "*fixture.txt", "-type", "f", "-exec", "cat", "{}", "+"],
                )
                result["profile_and_download_after_restart"] = True
                await create("bob")
                assert "isolated" in await fixture.shell(
                    ["sh", "-c", "test ! -e isolation-marker.txt && printf isolated"],
                    user=fixture.other,
                )
                assert len(fixture.container_ids) == 2
                result["requester_isolation"] = True
                assert not errors, errors
                result["page_errors"] = errors
            finally:
                await browser.close()
    return result


async def main() -> None:  # noqa: C901, PLR0915 - CLI setup and owned service lifetime
    """Run acceptance once or keep a loopback Chat fixture alive until interrupted."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="mindroom-worker-computer-test:local")
    parser.add_argument("--provider", choices=("browser", "browser_mcp"), default="browser")
    parser.add_argument("--build", action="store_true", help="Build the existing full worker Dockerfile first.")
    parser.add_argument("--output", type=Path, required=True, help="New persistent directory for state and evidence.")
    parser.add_argument("--novnc", type=Path, help="Path to @novnc/novnc 1.7.0 package (for standalone acceptance).")
    parser.add_argument("--chromium", default=shutil.which("chromium") or shutil.which("chromium-browser"))
    parser.add_argument("--serve", action="store_true", help="Serve the Chat fixture until Ctrl+C.")
    parser.add_argument("--chat-origin", type=loopback_origin)
    parser.add_argument("--matrix-fixture", type=Path, help="Disposable loopback Matrix fixture JSON (see docs).")
    parser.add_argument(
        "--matrix-image",
        default="ghcr.io/mindroom-ai/mindroom-tuwunel:latest",
        help="Locally available Matrix image used by --serve when no fixture is supplied.",
    )
    args = parser.parse_args()
    if args.serve:
        if not args.chat_origin:
            parser.error("--serve requires --chat-origin")
    elif not args.novnc or args.matrix_fixture:
        parser.error("Standalone acceptance requires --novnc and uses its own controlled Matrix verifier")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    if args.build:
        await command(
            "docker",
            "build",
            "-t",
            args.image,
            "-f",
            "local/instances/deploy/Dockerfile.mindroom",
            str(ASSETS.parents[2]),
        )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    if args.serve:
        owned_matrix = None
        fixture = None
        try:
            if not args.matrix_fixture:
                owned_matrix = await asyncio.to_thread(create_matrix_fixture, args.output, args.matrix_image)
                args.matrix_fixture = args.output / "matrix-fixture.json"
            fixture = Fixture(
                args,
                f"http://127.0.0.1:{listener.getsockname()[1]}",
                owned_matrix_id=owned_matrix["container_id"] if owned_matrix else None,
            )
            server = uvicorn.Server(uvicorn.Config(fixture.app(), log_level="warning", ws="websockets-sansio"))
            await server.serve(sockets=[listener])
        finally:
            listener.close()
            if fixture is not None:
                await fixture.remove_owned_matrix()
            elif owned_matrix:
                await command("docker", "rm", "-f", owned_matrix["container_id"])
        return
    fixture = Fixture(args, f"http://127.0.0.1:{listener.getsockname()[1]}")
    server = uvicorn.Server(uvicorn.Config(fixture.app(), log_level="warning", ws="websockets-sansio"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                    message = "Fixture server failed to start"
                    raise RuntimeError(message)
                await asyncio.sleep(0.01)
        result = await exercise(fixture)
        (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result), flush=True)
    finally:
        server.should_exit = True
        await serving
        listener.close()


if __name__ == "__main__":
    asyncio.run(main())
