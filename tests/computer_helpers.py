"""Local authenticated worker peer for public Computer gateway tests."""

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from aiohttp import web
from fastapi import FastAPI

from mindroom.api import computers, config_lifecycle
from mindroom.api.main import _RuntimeDashboardCorsMiddleware
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.worker_computer.runtime import ComputerControlError, WorkerComputerRuntime
from mindroom.worker_computer.sessions import ComputerError, ComputerSessionStore, ComputerTarget
from mindroom.workers.models import WorkerHandle
from tests.test_computer_sessions import authorized_target
from tests.test_worker_computer_runtime import FakeDisplay


@dataclass
class ComputerPeer:
    """Real local HTTP/WS peer with runtime control semantics and inspectable requests."""

    runtime: WorkerComputerRuntime = field(default_factory=lambda: WorkerComputerRuntime(FakeDisplay()))
    handle: WorkerHandle | None = None
    allowed: bool = True
    now: float = 100.0
    generation: int = 0
    openid_status: int = 200
    openid_subject: str = "@alice:example.org"
    requests: list[tuple[str, str]] = field(default_factory=list)

    async def http(self, request: web.Request) -> web.Response:
        """Require worker headers on internal requests and return real runtime status."""
        self.requests.append((request.path_qs, request.headers.get("X-Mindroom-Sandbox-Token", "")))
        if request.headers.get("X-Mindroom-Sandbox-Token") != "worker-secret":
            return web.Response(status=401)
        try:
            if request.path.endswith("/start"):
                status = await self.runtime.ensure_started()
            elif request.path.endswith("/control"):
                body = await request.json()
                if body["action"] == "take":
                    status = await self.runtime.take_control(body["session_id"], generation=body["generation"])
                elif body["action"] == "release":
                    status = await self.runtime.release_control(body["session_id"], generation=body["generation"])
                else:
                    status = await self.runtime.stop(generation=body["generation"])
            else:
                status = self.runtime.status()
        except ComputerControlError:
            return web.Response(status=409)
        return web.json_response(status)

    async def openid(self, request: web.Request) -> web.Response:
        """Verify an isolated short-lived token through the actual HTTP verifier."""
        if request.query.get("access_token") != "openid-secret":
            return web.Response(status=401)
        return web.json_response(
            {"sub": self.openid_subject},
            status=self.openid_status,
            headers={"Location": "https://attacker.invalid/collect"},
        )

    async def stream(self, request: web.Request) -> web.WebSocketResponse:
        """Attach real ownership and invalidate streams on release/stop."""
        self.requests.append((request.path_qs, request.headers.get("X-Mindroom-Sandbox-Token", "")))
        if request.headers.get("X-Mindroom-Sandbox-Token") != "worker-secret":
            raise web.HTTPUnauthorized
        session_id = request.query["session_id"]
        event = await self.runtime.attach_stream(session_id, request.query["generation"])
        websocket = web.WebSocketResponse(protocols=("binary",))
        await websocket.prepare(request)
        await websocket.send_bytes(b"screen")

        async def echo() -> None:
            async for message in websocket:
                if message.type == aiohttp.WSMsgType.BINARY:
                    await websocket.send_bytes(b"control" if self.runtime.allows_input(session_id, event) else b"view")

        tasks = [asyncio.create_task(echo()), asyncio.create_task(event.wait())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.runtime.detach_stream(session_id, event)
            await websocket.close()
        return websocket


def computer_app(peer: ComputerPeer, tmp_path: Path) -> FastAPI:
    """Build a gateway with real transport and an explicit live authorizer."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        upstream = web.Application()
        upstream.router.add_get("/_matrix/federation/v1/openid/userinfo", peer.openid)
        upstream.router.add_get("/computer/stream", peer.stream)
        upstream.router.add_route("*", "/computer{tail:.*}", peer.http)
        runner = web.AppRunner(upstream, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        origin = f"http://127.0.0.1:{port}"
        peer.handle = WorkerHandle(
            "worker-id",
            "worker",
            origin + "/api/sandbox-runner/execute",
            "worker-secret",
            "ready",
            "docker",
            100,
            100,
            debug_metadata={"api_root": origin + "/api/sandbox-runner"},
        )
        paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env={
                "MATRIX_HOMESERVER": origin,
                "MATRIX_SERVER_NAME": "example.org",
                "MINDROOM_WORKER_COMPUTER_ENABLED": "1",
                "MINDROOM_COMPUTER_ALLOWED_ORIGINS": '["https://chat.example.org"]',
            },
        )
        config = Config()
        state = config_lifecycle.ensure_app_state(app)
        state.api_state = config_lifecycle.ApiState(
            threading.Lock(),
            config_lifecycle.ApiSnapshot(1, paths, config.model_dump(), runtime_config=config),
        )
        state.computer_sessions = ComputerSessionStore(clock=lambda: peer.now, capacity=2)

        async def authorize(requester: str, room: str, agent: str) -> ComputerTarget:
            if not peer.allowed:
                raise ComputerError(403, "Access revoked.")
            target = authorized_target()
            assert (requester, room, agent) == (target.requester_id, target.room_id, target.agent_user_id)
            return target

        state.computer_runtime = computers.ComputerRuntime(authorize, 1, config)
        try:
            yield
        finally:
            state.computer_sessions.close_all()
            await peer.runtime.close()
            await runner.cleanup()

    app = FastAPI(lifespan=lifespan)
    app.include_router(computers.router)
    app.add_middleware(
        _RuntimeDashboardCorsMiddleware,
        api_app=app,
        fallback_runtime_paths=resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path),
    )
    return app
