"""Real grant authentication, strict bounded requests and owner-only receipts."""

# ruff: noqa: D103, ARG001
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx
import pytest
from agno.tools.toolkit import Toolkit
from fastapi import FastAPI

from mindroom.agent_cli.session import CliTurnOwner, TurnToolRegistry
from mindroom.agent_cli.turn import LiveTurnTools
from mindroom.api.agent_cli import bind_agent_cli_registry, router
from mindroom.tool_system.agent_tool_calls import DeferredAgentToolkit
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context
from tests.test_agent_tool_calls import _catalog

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.types import Message, Receive, Scope, Send


@pytest.mark.asyncio
async def test_real_api_auth_precedes_validation_and_hides_wrong_owner(
    tmp_path: Path,
) -> None:

    catalog = await _catalog(tmp_path, [Toolkit(name="calculator", tools=[lambda: 1])])

    async def authorize(key: object, arguments: object) -> None:
        return None

    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run", "worker"),
        catalog=catalog,
        worker=None,
        authorize=authorize,
    )
    registry = TurnToolRegistry()
    registry.register(owner)
    grant = owner.issue(now_ns=time.time_ns(), expires_at_ns=time.time_ns() + 10**12)
    app = FastAPI()
    bind_agent_cli_registry(app, registry)
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        unavailable = await client.post("/api/agent-cli/operations", json={})
        assert unavailable.status_code == 401
        headers = {"Authorization": f"Bearer {grant.raw_token}"}
        listing = await client.post("/api/agent-cli/operations", headers=headers, json={"operation": "tools.list"})
        assert listing.status_code == 200
        assert listing.json()["items"][0]["toolkit"] == "calculator"
        wrong = await client.get(f"/api/agent-cli/calls/{uuid4()}", headers=headers)
        assert (wrong.status_code, wrong.json()) == (unavailable.status_code, unavailable.json())
        malformed = await client.post(
            "/api/agent-cli/operations",
            headers=headers,
            content='{"operation":"tools.list","operation":"tools.call"}',
        )
        assert malformed.status_code == 422
        oversized = await client.post("/api/agent-cli/operations", headers=headers, content=b" " * 65537)
        assert oversized.status_code == 413
        identity = await client.post(
            "/api/agent-cli/operations",
            headers=headers,
            json={"operation": "tools.list", "requester_id": "other"},
        )
        assert identity.status_code == 422
        cursor = await client.post(
            "/api/agent-cli/operations",
            headers=headers,
            json={"operation": "tools.list", "cursor": "99"},
        )
        assert (cursor.status_code, cursor.json()["detail"]) == (422, "Invalid discovery cursor")
        bind_agent_cli_registry(app, None)
        detached = await client.post("/api/agent-cli/operations", headers=headers, json={"operation": "tools.list"})
        assert detached.status_code == 401
        bind_agent_cli_registry(app, registry)
        rebound = await client.post("/api/agent-cli/operations", headers=headers, json={"operation": "tools.list"})
        assert rebound.status_code == 200
        owner.revoke()
        denied = await client.post("/api/agent-cli/operations", headers=headers, json={"operation": "tools.list"})
        assert (denied.status_code, denied.json()) == (401, unavailable.json())
    await owner.close()


@pytest.mark.asyncio
async def test_internal_factory_key_error_is_not_client_validation(tmp_path: Path) -> None:
    catalog = await _catalog(tmp_path, [])

    async def broken_factory() -> Toolkit:
        msg = "internal settings missing"
        raise KeyError(msg)

    async def authorize(key: object, arguments: object) -> None:
        return None

    catalog.add_deferred(DeferredAgentToolkit("broken", "Broken integration", broken_factory))
    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run", "worker"),
        catalog=catalog,
        worker=None,
        authorize=authorize,
    )
    registry = TurnToolRegistry()
    registry.register(owner)
    grant = owner.issue(now_ns=time.time_ns(), expires_at_ns=time.time_ns() + 10**12)
    app = FastAPI()
    bind_agent_cli_registry(app, registry)
    app.include_router(router)
    headers = {"Authorization": f"Bearer {grant.raw_token}"}
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client,
        owner._window("bash"),
    ):
        for toolkit, expected_status in (("missing", 422), ("broken", 500)):
            response = await client.post(
                "/api/agent-cli/operations",
                headers=headers,
                json={"operation": "tools.describe", "toolkit": toolkit, "function": "run"},
            )
            assert response.status_code == expected_status
    await owner.close()


@pytest.mark.asyncio
async def test_live_retry_disconnect_conflict_and_restart_are_owner_scoped(tmp_path: Path) -> None:  # noqa: PLR0915
    """Lost HTTP replies never own invocation lifetime or survive owner restart."""
    executed = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def change(value: str) -> str:
        executed.append(value)
        started.set()
        await release.wait()
        return value

    async def authorize(key: object, arguments: object) -> None:
        return None

    async def make_owner(name: str) -> tuple[LiveTurnTools, dict[str, str]]:
        catalog = await _catalog(tmp_path / name, [Toolkit(name="state", tools=[change])])
        owner = LiveTurnTools(
            CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), name, "run", name),
            catalog=catalog,
            worker=None,
            authorize=authorize,
        )
        registry.register(owner)
        grant = owner.issue(now_ns=time.time_ns(), expires_at_ns=time.time_ns() + 10**12)
        return owner, {"Authorization": f"Bearer {grant.raw_token}"}

    registry = TurnToolRegistry()
    owner, headers = await make_owner("first")
    other, other_headers = await make_owner("other")
    app = FastAPI()
    bind_agent_cli_registry(app, registry)
    app.include_router(router)
    accepted = asyncio.Event()

    async def disconnecting_app(scope: Scope, receive: Receive, send: Send) -> None:
        async def interrupted_send(message: Message) -> None:
            if message["type"] == "http.response.start" and (b"x-lose-reply", b"1") in scope["headers"]:
                accepted.set()
                await asyncio.Event().wait()
            await send(message)

        await app(scope, receive, interrupted_send)

    call_id = str(uuid4())
    payload = {
        "operation": "tools.call",
        "call_id": call_id,
        "toolkit": "state",
        "function": "change",
        "arguments": {"value": "once"},
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=disconnecting_app),
        base_url="http://test",
    ) as client:
        outside = await client.post("/api/agent-cli/operations", headers=headers, json=payload)
        assert (outside.status_code, outside.json()) == (
            409,
            {"detail": "Agent CLI tool commands require an active Bash call"},
        )
        async with owner._window("parent-bash"):
            lost = asyncio.create_task(
                client.post("/api/agent-cli/operations", headers=headers | {"x-lose-reply": "1"}, json=payload),
            )
            await accepted.wait()
            lost.cancel()
            with pytest.raises(asyncio.CancelledError):
                await lost
            first = await client.post("/api/agent-cli/operations", headers=headers, json=payload)
            assert first.status_code == 200
            assert first.json()["status"] == "running"
            for changed in [{"arguments": {"value": "changed"}}, {"function": "different"}, {"toolkit": "different"}]:
                conflict = await client.post("/api/agent-cli/operations", headers=headers, json=payload | changed)
                assert conflict.status_code == 409
            wrong = await client.get(f"/api/agent-cli/calls/{call_id}", headers=other_headers)
            unknown = await client.get(f"/api/agent-cli/calls/{call_id}")
            assert (wrong.status_code, wrong.json()) == (unknown.status_code, unknown.json())
            await started.wait()
            retried = await client.post("/api/agent-cli/operations", headers=headers, json=payload)
            assert retried.json()["status"] == "running"
            release.set()
        completed = await client.get(f"/api/agent-cli/calls/{call_id}", headers=headers)
        assert completed.json()["status"] == "completed"
        assert completed.json()["outcome"] == "once"
        replay = await client.post("/api/agent-cli/operations", headers=headers, json=payload)
        assert replay.json() == completed.json()
        assert executed == ["once"]
        bind_agent_cli_registry(app, None)
        await owner.close()
        await other.close()
        registry = TurnToolRegistry()
        restarted, restarted_headers = await make_owner("first")
        bind_agent_cli_registry(app, registry)
        for authority in (headers, restarted_headers):
            response = await client.get(f"/api/agent-cli/calls/{call_id}", headers=authority)
            assert (response.status_code, response.json()) == (unknown.status_code, unknown.json())
        await restarted.close()
