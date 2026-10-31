"""API tests for the capability-authenticated background-script gateway."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mindroom.api import script_gateway
from mindroom.api.script_gateway import bind_script_tool_broker, router
from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.script_runs.broker import (
    ScriptBrokerAuthenticationError,
    ScriptRuntimeResolver,
    ScriptRuntimeUnavailableError,
    ScriptToolBroker,
    digest_arguments,
)
from mindroom.script_runs.models import (
    ScriptCallClaim,
    ScriptCallRecord,
    ScriptCallState,
    ScriptRunRecord,
    ScriptToolGrant,
)
from mindroom.script_runs.store import (
    ScriptCallConflictError,
    ScriptCallRateLimitError,
    ScriptCapabilityError,
    ScriptRunNotFoundError,
    script_capability_hash,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from httpx import Response

    from mindroom.script_runs.broker import ScriptToolCallRequest

_CALLS_URL = "/api/script-gateway/calls"
_RECEIPT_URL = "/api/script-gateway/runs/run-1/calls/call-1"
_AUTHORIZATION = {"Authorization": "Bearer secret-token"}
_UNAVAILABLE = "Background script call is unavailable."
_OWNER_RUNTIME_UNAVAILABLE = "Background script owner runtime is temporarily unavailable."


def _receipt(state: ScriptCallState, *, result: object | None = None) -> ScriptCallRecord:
    return ScriptCallRecord(
        run_id="run-1",
        call_id="call-1",
        grant=ScriptToolGrant("website", "read_url"),
        arguments_digest=digest_arguments({"url": "https://example.org/"}),
        state=state,
        created_at="2026-08-18T00:00:00Z",
        result=result,
    )


@dataclass
class _GatewayBroker:
    """A broker returning fixed receipts, or raising ``error`` from both gateway operations."""

    submit_receipt: ScriptCallRecord = field(default_factory=lambda: _receipt(ScriptCallState.COMPLETED))
    get_receipt: ScriptCallRecord = field(default_factory=lambda: _receipt(ScriptCallState.COMPLETED))
    error: Exception | None = None
    submitted: ScriptToolCallRequest | None = None
    authorization: str | None = None

    async def accept_authenticated(
        self,
        request: ScriptToolCallRequest,
        authorization: str | None,
    ) -> ScriptCallRecord:
        if self.error is not None:
            raise self.error
        self.submitted = request
        self.authorization = authorization
        return self.submit_receipt

    async def get_authenticated(
        self,
        run_id: str,
        call_id: str,
        authorization: str | None,
    ) -> ScriptCallRecord:
        if self.error is not None:
            raise self.error
        self.authorization = authorization
        assert (run_id, call_id) == ("run-1", "call-1")
        return self.get_receipt


def _app(broker: object) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    bind_script_tool_broker(app, broker)
    return app


async def _post(
    app: FastAPI,
    *,
    payload: dict[str, object] | None = None,
    content: str | AsyncIterator[bytes] | None = None,
) -> Response:
    """Submit a call as JSON *payload*, the default payload, or raw JSON *content*."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        if content is not None:
            return await client.post(
                _CALLS_URL,
                content=content,
                headers={**_AUTHORIZATION, "Content-Type": "application/json"},
            )
        return await client.post(_CALLS_URL, json=_payload() if payload is None else payload, headers=_AUTHORIZATION)


async def _get(app: FastAPI) -> Response:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(_RECEIPT_URL, headers=_AUTHORIZATION)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "error", "status_code", "detail"),
    [
        pytest.param(
            "post",
            ScriptCallRateLimitError("Background script tool-call rate limit exceeded."),
            429,
            "Background script tool-call rate limit exceeded.",
            id="durable-rate-limit",
        ),
        pytest.param("post", ScriptBrokerAuthenticationError("unknown"), 404, _UNAVAILABLE, id="unknown-capability"),
        pytest.param("post", ScriptBrokerAuthenticationError("revoked"), 404, _UNAVAILABLE, id="revoked-capability"),
        pytest.param(
            "post",
            ScriptCapabilityError("revoked after authentication"),
            404,
            _UNAVAILABLE,
            id="revoked-after-authentication",
        ),
        pytest.param("post", ScriptRunNotFoundError("run-1"), 404, _UNAVAILABLE, id="run-deleted-post"),
        pytest.param("get", ScriptRunNotFoundError("run-1"), 404, _UNAVAILABLE, id="run-deleted-get"),
        pytest.param(
            "post",
            ScriptRuntimeUnavailableError(_OWNER_RUNTIME_UNAVAILABLE),
            503,
            _OWNER_RUNTIME_UNAVAILABLE,
            id="owner-runtime-unavailable-post",
        ),
        pytest.param(
            "get",
            ScriptRuntimeUnavailableError(_OWNER_RUNTIME_UNAVAILABLE),
            503,
            _OWNER_RUNTIME_UNAVAILABLE,
            id="owner-runtime-unavailable-get",
        ),
    ],
)
async def test_script_gateway_maps_broker_failures_to_stable_responses(
    method: str,
    error: Exception,
    status_code: int,
    detail: str,
) -> None:
    """Broker failures get stable responses.

    A durable rate limit is a non-retry 429, and a restarting owner runtime is a retryable 503.
    Unknown, revoked, and deleted runs are one indistinguishable 404, so capabilities cannot be enumerated.
    """
    app = _app(_GatewayBroker(error=error))

    response = await (_post(app) if method == "post" else _get(app))

    assert response.status_code == status_code
    assert response.json() == {"detail": detail}


def test_script_gateway_binds_through_public_app_state_attributes() -> None:
    """Broker binding must use Starlette's public state attribute contract."""
    broker = _GatewayBroker()
    app = FastAPI()
    app.state = SimpleNamespace()

    bind_script_tool_broker(app, broker)

    assert app.state.script_tool_broker is broker


@pytest.mark.asyncio
@pytest.mark.parametrize("bind_empty", [False, True], ids=["unbound", "empty"])
async def test_script_gateway_fails_closed_without_a_bound_broker(bind_empty: bool) -> None:
    """An app without lifecycle broker wiring, or with an explicitly empty binding, must not accept script calls."""
    app = FastAPI()
    if bind_empty:
        app.state.script_tool_broker = None
    app.include_router(router)

    response = await _post(app)

    assert response.status_code == 503
    assert response.json() == {"detail": "Background script gateway is unavailable."}


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
    broker = _GatewayBroker(submit_receipt=_receipt(ScriptCallState.COMPLETED, result="page body"))

    response = await _post(_app(broker))

    assert response.status_code == 200
    assert response.json() == {
        "run_id": "run-1",
        "call_id": "call-1",
        "toolkit_name": "website",
        "function_name": "read_url",
        "arguments_digest": digest_arguments({"url": "https://example.org/"}),
        "state": "completed",
        "created_at": "2026-08-18T00:00:00Z",
        "result": "page body",
        "error": None,
    }
    assert broker.authorization == "Bearer secret-token"
    assert broker.submitted is not None
    assert broker.submitted.run_id == "run-1"


@pytest.mark.asyncio
async def test_script_gateway_returns_pending_only_after_durable_acceptance() -> None:
    """A durable accepted claim may return pending while execution continues."""
    broker = _GatewayBroker(submit_receipt=_receipt(ScriptCallState.PENDING))
    response = await _post(_app(broker))

    assert response.status_code == 202
    assert response.json()["state"] == "pending"


@pytest.mark.asyncio
async def test_script_gateway_returns_the_durable_conflict_after_slow_claim_resolution() -> None:
    """A slow durable conflict remains a conflict rather than a synthetic gateway timeout."""
    grant = ScriptToolGrant("website", "read_url")
    run = ScriptRunRecord(
        run_id="run-1",
        agent_name="watcher",
        owner_user_id="@alice:example.test",
        room_id="!room:example.test",
        source_digest="source",
        grants=(grant,),
        token_hash=script_capability_hash("secret-token"),
    )
    old_arguments_digest = digest_arguments({"url": "https://old.example/"})
    call = ScriptCallRecord(
        run_id="run-1",
        call_id="call-1",
        grant=grant,
        arguments_digest=old_arguments_digest,
        state=ScriptCallState.COMPLETED,
        created_at="2026-08-18T00:00:00Z",
        result="later",
    )

    class BlockingStore:
        def get_run(self, run_id: str) -> ScriptRunRecord:
            assert run_id == "run-1"
            return run

        def claim_call(self, **_kwargs: object) -> ScriptCallClaim:
            time.sleep(0.1)
            message = "Stable call ID was already claimed with different arguments."
            raise ScriptCallConflictError(message)

        def get_call(self, run_id: str, call_id: str) -> ScriptCallRecord:
            assert (run_id, call_id) == ("run-1", "call-1")
            return call

    runtime_resolver = MagicMock(spec=ScriptRuntimeResolver)
    runtime_resolver.is_authorized.return_value = True
    broker = ScriptToolBroker(store=BlockingStore(), runtime_resolver=runtime_resolver)  # type: ignore[arg-type]
    broker.open_call_admission()

    response = await _post(_app(broker))

    assert response.status_code == 409
    assert response.json() == {"detail": "Stable call ID conflicts with its accepted request."}

    old_response = await _get(_app(broker))

    assert old_response.status_code == 200
    assert old_response.json()["arguments_digest"] == old_arguments_digest


@pytest.mark.asyncio
async def test_script_gateway_rejects_oversized_request_before_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """An oversized body must not reach capability lookup or tool dispatch."""
    broker = _GatewayBroker()
    monkeypatch.setattr(script_gateway, "_MAX_REQUEST_BYTES", 32)

    response = await _post(_app(broker))

    assert response.status_code == 413
    assert broker.submitted is None


@pytest.mark.asyncio
async def test_script_gateway_rejects_a_non_finite_field_without_echoing_it() -> None:
    """A rejected NaN, which JSON responses cannot render, gets a 422 that does not repeat the body."""
    broker = _GatewayBroker()

    response = await _post(_app(broker), content=json.dumps({**_payload(), "run_id": float("nan")}))

    assert response.status_code == 422
    assert response.json() == {
        "detail": [{"type": "string_type", "loc": ["run_id"], "msg": "Input should be a valid string"}],
    }
    assert broker.submitted is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments_json",
    [
        '{"x":NaN}',
        '{"x":Infinity}',
        '{"x":-Infinity}',
        '{"x":1e400}',
        '{"x":[1,{"y":Infinity}]}',
        '{"x":{"y":[-1e400]}}',
    ],
)
async def test_script_gateway_rejects_non_finite_arguments_before_broker(arguments_json: str) -> None:
    """A NaN or infinity anywhere in the arguments, which the call digest cannot encode, gets a 422."""
    broker = _GatewayBroker()
    body = (
        '{"run_id":"run-1","call_id":"call-1","toolkit_name":"website","function_name":"read_url",'
        f'"arguments":{arguments_json}}}'
    )

    response = await _post(_app(broker), content=body)

    assert response.status_code == 422
    assert response.json() == {
        "detail": [
            {"type": "finite_number", "loc": ["arguments"], "msg": "Arguments must contain only finite numbers"},
        ],
    }
    assert broker.submitted is None


@pytest.mark.asyncio
async def test_script_gateway_accepts_finite_numbers_in_arguments() -> None:
    """Ordinary integers and floats at any depth still reach the broker unchanged."""
    broker = _GatewayBroker()
    arguments = {"count": 3, "ratio": -0.5, "nested": [1e300, {"big": 10**30, "flag": True}]}

    response = await _post(_app(broker), payload={**_payload(), "arguments": arguments})

    assert response.status_code == 200
    assert broker.submitted is not None
    assert broker.submitted.arguments == arguments


@pytest.mark.asyncio
@pytest.mark.parametrize("extra_bytes", [0, 1], ids=["exact_limit", "overflow"])
async def test_script_gateway_bounds_chunked_requests_without_content_length(extra_bytes: int) -> None:
    """Streamed calls obey the 64 KiB cap before broker admission, including at the boundary."""
    broker = _GatewayBroker()
    body = json.dumps(_payload()).encode().ljust(65536, b" ")

    async def chunks() -> AsyncIterator[bytes]:
        yield body[:32768]
        yield body[32768:]
        if extra_bytes:
            yield b" "
            pytest.fail("Read past the overflowing chunk")

    response = await _post(_app(broker), content=chunks())

    assert "content-length" not in response.request.headers
    if extra_bytes:
        assert response.status_code == 413
        assert response.json() == {"detail": "Script call request is too large."}
        assert broker.submitted is None
    else:
        assert response.status_code == 200
        assert broker.submitted is not None
        assert broker.submitted.arguments == {"url": "https://example.org/"}
        assert response.json()["call_id"] == "call-1"


@pytest.mark.asyncio
async def test_script_gateway_cancellation_waits_for_broker_acceptance_ownership() -> None:
    """Cancelling the submitter cannot return before the broker owns the accepted call."""
    acceptance_started = asyncio.Event()
    finish_acceptance = asyncio.Event()

    class BlockingBroker(_GatewayBroker):
        async def accept_authenticated(
            self,
            request: ScriptToolCallRequest,
            authorization: str | None,
        ) -> ScriptCallRecord:
            del request, authorization

            async def accept() -> ScriptCallRecord:
                acceptance_started.set()
                await finish_acceptance.wait()
                return self.submit_receipt

            return await run_coroutine_until_complete(accept())

    broker = BlockingBroker(submit_receipt=_receipt(ScriptCallState.PENDING))
    submitter = asyncio.create_task(_post(_app(broker)))
    await acceptance_started.wait()
    submitter.cancel()
    await asyncio.sleep(0)

    assert not submitter.done()

    finish_acceptance.set()
    with pytest.raises(asyncio.CancelledError):
        await submitter


@pytest.mark.asyncio
async def test_script_gateway_accepts_slow_valid_claim_without_gateway_timeout() -> None:
    """A valid acceptance taking longer than one second is not rewritten as a gateway 503."""

    class SlowBroker(_GatewayBroker):
        async def accept_authenticated(
            self,
            request: ScriptToolCallRequest,
            authorization: str | None,
        ) -> ScriptCallRecord:
            del request, authorization
            await asyncio.sleep(1.01)
            return self.submit_receipt

    broker = SlowBroker(submit_receipt=_receipt(ScriptCallState.PENDING))
    response = await _post(_app(broker))

    assert response.status_code == 202
    assert response.json()["state"] == "pending"
