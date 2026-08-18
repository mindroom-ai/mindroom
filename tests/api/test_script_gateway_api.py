"""API tests for the capability-authenticated background-script gateway."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mindroom.api import script_gateway
from mindroom.api.script_gateway import bind_script_tool_broker, router
from mindroom.script_runs.broker import ScriptBrokerAuthenticationError, ScriptCallReceipt
from mindroom.script_runs.models import ScriptCallState, ScriptToolGrant
from mindroom.script_runs.store import ScriptCapabilityError

if TYPE_CHECKING:
    from mindroom.script_runs.broker import ScriptToolCallRequest


def _receipt(state: ScriptCallState, *, result: object | None = None) -> ScriptCallReceipt:
    return ScriptCallReceipt(
        run_id="run-1",
        call_id="call-1",
        grant=ScriptToolGrant("website", "read_url"),
        state=state,
        created_at="2026-08-18T00:00:00Z",
        updated_at="2026-08-18T00:00:01Z",
        result=result,
    )


@dataclass
class _GatewayBroker:
    submit_receipt: ScriptCallReceipt
    get_receipt: ScriptCallReceipt
    submit_gate: asyncio.Event | None = None
    submitted: ScriptToolCallRequest | None = None
    authorization: str | None = None

    async def submit_authenticated(
        self,
        request: ScriptToolCallRequest,
        authorization: str | None,
    ) -> ScriptCallReceipt:
        self.submitted = request
        self.authorization = authorization
        if self.submit_gate is not None:
            await self.submit_gate.wait()
        return self.submit_receipt

    def get_authenticated(
        self,
        run_id: str,
        call_id: str,
        authorization: str | None,
    ) -> ScriptCallReceipt:
        self.authorization = authorization
        assert (run_id, call_id) == ("run-1", "call-1")
        return self.get_receipt


def _app(broker: object) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    bind_script_tool_broker(app, broker)
    return app


def _payload() -> dict[str, object]:
    return {
        "run_id": "run-1",
        "call_id": "call-1",
        "toolkit_name": "website",
        "function_name": "read_url",
        "arguments": {"url": "https://example.org/"},
    }


@pytest.mark.asyncio
async def test_script_gateway_passes_bearer_only_to_broker_and_returns_wire_receipt() -> None:
    """The gateway body cannot override durable owner identity and grants."""
    broker = _GatewayBroker(
        submit_receipt=_receipt(ScriptCallState.COMPLETED, result="page body"),
        get_receipt=_receipt(ScriptCallState.COMPLETED, result="page body"),
    )

    async with AsyncClient(transport=ASGITransport(app=_app(broker)), base_url="http://test") as client:
        response = await client.post(
            "/api/script-gateway/calls",
            json=_payload(),
            headers={"Authorization": "Bearer secret-token"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "run_id": "run-1",
        "call_id": "call-1",
        "toolkit_name": "website",
        "function_name": "read_url",
        "state": "completed",
        "created_at": "2026-08-18T00:00:00Z",
        "updated_at": "2026-08-18T00:00:01Z",
        "result": "page body",
        "error": None,
    }
    assert broker.authorization == "Bearer secret-token"
    assert broker.submitted is not None
    assert broker.submitted.token == ""


@pytest.mark.asyncio
async def test_script_gateway_returns_pending_after_bounded_initial_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """A long approval cannot pin the initial HTTP request indefinitely."""
    gate = asyncio.Event()
    broker = _GatewayBroker(
        submit_receipt=_receipt(ScriptCallState.COMPLETED, result="later"),
        get_receipt=_receipt(ScriptCallState.PENDING),
        submit_gate=gate,
    )
    monkeypatch.setattr(script_gateway, "_INITIAL_WAIT_SECONDS", 0.001)

    async with AsyncClient(transport=ASGITransport(app=_app(broker)), base_url="http://test") as client:
        response = await client.post(
            "/api/script-gateway/calls",
            json=_payload(),
            headers={"Authorization": "Bearer secret-token"},
        )
        gate.set()
        await asyncio.sleep(0)

    assert response.status_code == 202
    assert response.json()["state"] == "pending"


@pytest.mark.asyncio
async def test_script_gateway_rejects_oversized_request_before_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """An oversized body must not reach capability lookup or tool dispatch."""
    broker = _GatewayBroker(
        submit_receipt=_receipt(ScriptCallState.COMPLETED),
        get_receipt=_receipt(ScriptCallState.COMPLETED),
    )
    monkeypatch.setattr(script_gateway, "_MAX_REQUEST_BYTES", 32)

    async with AsyncClient(transport=ASGITransport(app=_app(broker)), base_url="http://test") as client:
        response = await client.post(
            "/api/script-gateway/calls",
            json=_payload(),
            headers={"Authorization": "Bearer secret-token"},
        )

    assert response.status_code == 413
    assert broker.submitted is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unknown", "revoked"])
async def test_script_gateway_unknown_and_revoked_capabilities_are_indistinguishable(failure: str) -> None:
    """Capability enumeration must not reveal whether a durable run exists."""

    class RejectingBroker(_GatewayBroker):
        async def submit_authenticated(
            self,
            request: ScriptToolCallRequest,
            authorization: str | None,
        ) -> ScriptCallReceipt:
            del request, authorization
            raise ScriptBrokerAuthenticationError(failure)

    broker = RejectingBroker(
        submit_receipt=_receipt(ScriptCallState.COMPLETED),
        get_receipt=_receipt(ScriptCallState.COMPLETED),
    )

    async with AsyncClient(transport=ASGITransport(app=_app(broker)), base_url="http://test") as client:
        response = await client.post(
            "/api/script-gateway/calls",
            json=_payload(),
            headers={"Authorization": "Bearer secret-token"},
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "Background script call is unavailable."}


@pytest.mark.asyncio
async def test_script_gateway_hides_capability_revocation_racing_after_authentication() -> None:
    """A run revoked between authentication and claim must keep the generic unavailable response."""

    class RacingBroker(_GatewayBroker):
        async def submit_authenticated(
            self,
            request: ScriptToolCallRequest,
            authorization: str | None,
        ) -> ScriptCallReceipt:
            del request, authorization
            message = "revoked after authentication"
            raise ScriptCapabilityError(message)

    broker = RacingBroker(
        submit_receipt=_receipt(ScriptCallState.COMPLETED),
        get_receipt=_receipt(ScriptCallState.COMPLETED),
    )

    async with AsyncClient(transport=ASGITransport(app=_app(broker)), base_url="http://test") as client:
        response = await client.post(
            "/api/script-gateway/calls",
            json=_payload(),
            headers={"Authorization": "Bearer secret-token"},
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "Background script call is unavailable."}
