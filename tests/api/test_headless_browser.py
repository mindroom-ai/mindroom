"""Headless dedicated workers retain browser resources across fresh requests."""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import asdict
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio
from agno.tools.function import ToolResult
from fastapi import FastAPI

from mindroom.api import sandbox_runner, sandbox_runner_app, sandbox_worker_prep
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools import browser as browser_module
from mindroom.tool_system.media_transport import decode_media_result
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_key
from mindroom.workers.backends.local import local_worker_state_paths_for_root
from mindroom.workers.models import WorkerHandle
from tests.browser_lifecycle_helpers import LifecycleBrowser

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


class BrowserProcess(LifecycleBrowser):
    """Observe the environment passed to the real browser launch boundary."""

    def __init__(self) -> None:
        super().__init__()
        self.launch: dict[str, object] = {}

    async def launch_persistent_context(self, **kwargs: object) -> BrowserProcess:
        """Record launch options and acquire the observable external context."""
        self.launch = kwargs
        await super().launch_persistent_context(**kwargs)
        return self


@pytest.fixture
def browser_processes(monkeypatch: pytest.MonkeyPatch) -> list[BrowserProcess]:
    """Replace only the external driver, preserving real browser ownership and tabs."""
    processes: list[BrowserProcess] = []

    def start() -> BrowserProcess:
        process = BrowserProcess()
        processes.append(process)
        return process

    monkeypatch.setattr(browser_module, "async_playwright", start)
    return processes


@pytest_asyncio.fixture(params=["user_agent", "user", "shared"])
async def headless_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    browser_processes: list[BrowserProcess],
) -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, object], Path, Config]]:
    """Exercise the real ASGI lifespan and dispatch with prepared isolated storage."""
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="writer",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="first",
    )
    key = resolve_worker_key(request.param, identity, agent_name="writer")
    root = tmp_path / "worker"
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=root,
        process_env={
            "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY": key,
            "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT": str(root),
            "MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE": "forkserver",
        },
    )
    app = FastAPI(lifespan=sandbox_runner_app._lifespan)
    config = Config()
    sandbox_runner.initialize_sandbox_runner_app(app, paths, config=config, runner_token="runner")  # noqa: S106
    app.include_router(sandbox_runner.router)
    prepared = sandbox_worker_prep.PreparedWorkerRequest(
        handle=WorkerHandle("test", key, "http://worker/execute", "test", "ready", "docker", 0, 0),
        paths=local_worker_state_paths_for_root(root),
        runtime_overrides={"base_dir": root / "workspace"},
    )
    monkeypatch.setattr(sandbox_worker_prep, "prepare_worker_request", lambda **_kwargs: prepared)

    async def subprocess(*_args: object, **_kwargs: object) -> sandbox_runner.SandboxRunnerExecuteResponse:
        return sandbox_runner.SandboxRunnerExecuteResponse(ok=False, error="per-call subprocess loses browser state")

    monkeypatch.setattr(sandbox_runner, "_execute_request_subprocess", subprocess)
    payload = {
        "tool_name": "browser",
        "function_name": "browser_control",
        "worker_key": key,
        "worker_scope": request.param,
        "execution_identity": asdict(identity),
        "private_agent_names": [],
    }
    async with (
        sandbox_runner_app._lifespan(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://runner",
            headers={"X-Mindroom-Sandbox-Token": "runner"},
        ) as client,
    ):
        yield client, payload, root, config
    assert all(not process.live_resources for process in browser_processes)


async def _call(client: httpx.AsyncClient, payload: dict[str, object], **kwargs: object) -> dict[str, object]:
    response = await client.post("/api/sandbox-runner/execute", json={**payload, "kwargs": kwargs})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"], body
    return json.loads(body["result"])


@pytest.mark.asyncio
async def test_concurrent_headless_calls_share_profile_and_keep_tabs(
    headless_client: tuple[httpx.AsyncClient, dict[str, object], Path, Config],
    browser_processes: list[BrowserProcess],
) -> None:
    """Separate validated requests must launch one browser and retain both tab IDs."""
    client, payload, _root, _config = headless_client
    opened = await asyncio.gather(
        *(_call(client, payload, action="open", targetUrl=f"https://1.1.1.1/{index}") for index in range(2)),
    )
    assert len(browser_processes) == 1
    tabs = await _call(client, payload, action="tabs")
    ids = {tab["targetId"] for tab in tabs["tabs"]}
    assert {result["targetId"] for result in opened} <= ids


@pytest.mark.asyncio
@pytest.mark.parametrize("save_only", [False, True])
async def test_headless_screenshot_preserves_inline_media_and_save_only(
    headless_client: tuple[httpx.AsyncClient, dict[str, object], Path, Config],
    browser_processes: list[BrowserProcess],
    monkeypatch: pytest.MonkeyPatch,
    save_only: bool,
) -> None:
    """Headless screenshots transport image bytes while save-only keeps its JSON receipt."""
    client, payload, root, _config = headless_client
    opened = await _call(client, payload, action="open", targetUrl="https://1.1.1.1/screenshot")
    image_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
    )

    async def capture(*, path: str, **_kwargs: object) -> bytes:
        (root / path).write_bytes(image_bytes)
        return image_bytes

    monkeypatch.setattr(browser_processes[0].pages[-1], "screenshot", capture, raising=False)

    response = await client.post(
        "/api/sandbox-runner/execute",
        json={
            **payload,
            "kwargs": {"action": "screenshot", "targetId": opened["targetId"], "saveOnly": save_only},
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"], body
    if save_only:
        receipt = json.loads(body["result"])
        assert "view_status" not in receipt
    else:
        result = decode_media_result(body["result"])
        assert isinstance(result, ToolResult)
        assert result.images
        assert result.images[0].content == image_bytes
        receipt = json.loads(result.content)
        assert receipt["view_status"] == "ready"
    assert (root / receipt["path"]).read_bytes() == image_bytes


@pytest.mark.asyncio
async def test_headless_downloads_survive_session_transfer_and_browser_stop(
    headless_client: tuple[httpx.AsyncClient, dict[str, object], Path, Config],
    browser_processes: list[BrowserProcess],
) -> None:
    """Retained and native tabs save unique workspace copies without a visible display."""
    client, payload, root, _config = headless_client
    await _call(client, payload, action="open", targetUrl="https://1.1.1.1/first")
    process = browser_processes[0]
    retained_page = process.pages[-1]
    await _call(client, payload, action="tabs")
    native_page = process.add_native_page("https://1.1.1.1/native")

    class Download:
        """Supply completed bytes through the native page event boundary."""

        suggested_filename = "../../document.txt"

        async def save_as(self, destination: Path) -> None:
            """Persist the externally supplied download at the browser's destination."""
            destination.write_text("download contents")

    await retained_page.emit("download", Download())
    await native_page.emit("download", Download())
    await _call(client, payload, action="stop")
    saved = list((root / "workspace" / "browser").iterdir())
    assert len(saved) == 2
    assert all(path.name.endswith("-document.txt") for path in saved)
    assert all(path.read_text() == "download contents" for path in saved)
    assert not (root / "document.txt").exists()
    assert not process.live_resources


@pytest.mark.asyncio
async def test_current_request_output_policy_keeps_existing_tabs(
    headless_client: tuple[httpx.AsyncClient, dict[str, object], Path, Config],
    browser_processes: list[BrowserProcess],
) -> None:
    """Changing output destination applies to this request without losing browser state."""
    client, payload, root, config = headless_client
    opened = await _call(client, payload, action="open", targetUrl="https://1.1.1.1/first")
    config.defaults.tool_output_auto_save_threshold_bytes = 1
    response = await client.post(
        "/api/sandbox-runner/execute",
        json={**payload, "kwargs": {"action": "tabs"}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"], response.json()
    assert len(browser_processes) == 1
    receipt = response.json()["result"]["mindroom_tool_output"]
    assert receipt["status"] == "saved_to_file"
    saved = json.loads((root / "workspace" / receipt["path"]).read_text())
    assert opened["targetId"] in {tab["targetId"] for tab in saved["tabs"]}


@pytest.mark.asyncio
async def test_headless_request_scope_mismatch_is_rejected(
    headless_client: tuple[httpx.AsyncClient, dict[str, object], Path, Config],
    browser_processes: list[BrowserProcess],
) -> None:
    """A pinned user-agent worker cannot reuse its profile with ambiguous scope."""
    client, payload, _root, _config = headless_client
    response = await client.post(
        "/api/sandbox-runner/execute",
        json={**payload, "worker_scope": None, "kwargs": {"action": "start"}},
    )
    assert response.status_code == 400
    assert not browser_processes


@pytest.mark.asyncio
async def test_headless_environment_rotation_closes_old_profile(
    headless_client: tuple[httpx.AsyncClient, dict[str, object], Path, Config],
    browser_processes: list[BrowserProcess],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prepared proxy changes replace the browser without exposing ambient secrets."""
    client, payload, _root, _config = headless_client
    monkeypatch.setenv("UNRELATED_CONTROL_SECRET", "must-stay-in-runner")
    monkeypatch.setenv("HTTP_PROXY", "http://first:3128")
    await _call(client, payload, action="start")
    first = browser_processes[0]
    assert first.launch["headless"] is True
    assert first.launch["env"]["HTTP_PROXY"] == "http://first:3128"
    assert "UNRELATED_CONTROL_SECRET" not in first.launch["env"]
    monkeypatch.setenv("HTTP_PROXY", "http://second:3128")
    await _call(client, payload, action="start")
    assert not first.live_resources
    assert len(browser_processes) == 2
    assert browser_processes[1].launch["env"]["HTTP_PROXY"] == "http://second:3128"


@pytest.mark.asyncio
async def test_headless_cancellation_drains_profile_before_reuse(
    headless_client: tuple[httpx.AsyncClient, dict[str, object], Path, Config],
    browser_processes: list[BrowserProcess],
) -> None:
    """Repeated cancellation during cleanup cannot orphan the held profile."""
    client, payload, _root, _config = headless_client
    await _call(client, payload, action="start")
    first = browser_processes[0]
    first.pause_at = "new_page"
    active = asyncio.create_task(_call(client, payload, action="open", targetUrl="https://1.1.1.1/blocked"))
    await asyncio.wait_for(first.reached.wait(), timeout=2)
    first.pause_at = "context_close"
    first.reached.clear()
    active.cancel()
    await asyncio.wait_for(first.reached.wait(), timeout=2)
    active.cancel()
    first.proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await active
    assert not first.live_resources
    await _call(client, payload, action="start")
    assert len(browser_processes) == 2


@pytest.mark.asyncio
async def test_headless_worker_preserves_configured_desktop_target(
    headless_client: tuple[httpx.AsyncClient, dict[str, object], Path, Config],
    browser_processes: list[BrowserProcess],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retaining host resources must not turn a configured desktop browser into a host browser."""
    client, payload, _root, _config = headless_client

    async def desktop(_self: browser_module.BrowserTools, **kwargs: object) -> str:
        return json.dumps({"target": "desktop", "action": kwargs["action"]})

    monkeypatch.setattr(browser_module.BrowserTools, "_desktop_browser", desktop)
    payload["tool_config_overrides"] = {
        "default_target": "desktop",
        "device_user_id": "@desktop:example.org",
        "device_id": "DEVICE",
        "device_ed25519": "fingerprint",
    }
    assert await _call(client, payload, action="tabs") == {"target": "desktop", "action": "tabs"}
    assert not browser_processes


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_key", [None, "v1:default:unscoped:writer"])
async def test_generic_and_unscoped_runners_keep_subprocess_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker_key: str | None,
) -> None:
    """Only scoped dedicated workers may move browser resources into their ASGI process."""
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE": "forkserver",
            **({"MINDROOM_SANDBOX_DEDICATED_WORKER_KEY": worker_key} if worker_key else {}),
        },
    )
    app = FastAPI(lifespan=sandbox_runner_app._lifespan)
    sandbox_runner.initialize_sandbox_runner_app(app, paths, config=Config(), runner_token="runner")  # noqa: S106
    app.include_router(sandbox_runner.router)
    prepared = sandbox_worker_prep.PreparedWorkerRequest(
        handle=WorkerHandle("test", worker_key or "generic", "http://worker/execute", "test", "ready", "docker", 0, 0),
        paths=local_worker_state_paths_for_root(tmp_path),
        runtime_overrides={"base_dir": tmp_path / "workspace"},
    )
    monkeypatch.setattr(sandbox_worker_prep, "prepare_worker_request", lambda **_kwargs: prepared)

    async def subprocess(*_args: object, **_kwargs: object) -> sandbox_runner.SandboxRunnerExecuteResponse:
        return sandbox_runner.SandboxRunnerExecuteResponse(ok=True, result="isolated")

    monkeypatch.setattr(sandbox_runner, "_execute_request_subprocess", subprocess)
    async with (
        sandbox_runner_app._lifespan(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://runner",
            headers={"X-Mindroom-Sandbox-Token": "runner"},
        ) as client,
    ):
        response = await client.post(
            "/api/sandbox-runner/execute",
            json={"tool_name": "browser", "function_name": "browser_control", "kwargs": {"action": "start"}},
        )
        assert response.status_code == 200, response.text
        assert response.json()["result"] == "isolated"


@pytest.mark.asyncio
@pytest.mark.parametrize("headless_client", ["user_agent"], indirect=True)
async def test_headless_browser_ignores_computer_provider_selection(
    headless_client: tuple[httpx.AsyncClient, dict[str, object], Path, Config],
    browser_processes: list[BrowserProcess],
) -> None:
    """Computer provider exclusivity does not disable ordinary headless calls."""
    client, payload, _root, config = headless_client
    config.agents["writer"] = AgentConfig(
        display_name="Writer",
        tools=["browser", "browser_mcp"],
        worker_scope="user_agent",
    )
    await _call(client, payload, action="start")
    assert len(browser_processes) == 1
    assert browser_processes[0].launch["headless"] is True
