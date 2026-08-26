"""API tests for the capability-authenticated background-script gateway."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
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
)

if TYPE_CHECKING:
    from mindroom.script_runs.broker import ScriptToolCallRequest


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
    submit_receipt: ScriptCallRecord
    get_receipt: ScriptCallRecord
    submitted: ScriptToolCallRequest | None = None
    authorization: str | None = None

    async def accept_authenticated(
        self,
        request: ScriptToolCallRequest,
        authorization: str | None,
    ) -> ScriptCallRecord:
        self.submitted = request
        self.authorization = authorization
        return self.submit_receipt

    async def get_authenticated(
        self,
        run_id: str,
        call_id: str,
        authorization: str | None,
    ) -> ScriptCallRecord:
        self.authorization = authorization
        assert (run_id, call_id) == ("run-1", "call-1")
        return self.get_receipt


def _app(broker: object) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    bind_script_tool_broker(app, broker)
    return app


@pytest.mark.asyncio
async def test_script_gateway_reports_durable_rate_limit() -> None:
    """A new call rejected by the atomic claim quota receives a stable non-retry response."""

    class RateLimitedBroker(_GatewayBroker):
        async def accept_authenticated(
            self,
            request: ScriptToolCallRequest,
            authorization: str | None,
        ) -> ScriptCallRecord:
            del request, authorization
            message = "Background script tool-call rate limit exceeded."
            raise ScriptCallRateLimitError(message)

    broker = RateLimitedBroker(
        submit_receipt=_receipt(ScriptCallState.COMPLETED),
        get_receipt=_receipt(ScriptCallState.COMPLETED),
    )
    async with AsyncClient(transport=ASGITransport(app=_app(broker)), base_url="http://test") as client:
        response = await client.post(
            "/api/script-gateway/calls",
            json=_payload(),
            headers={"Authorization": "Bearer secret-token"},
        )

    assert response.status_code == 429
    assert response.json() == {"detail": "Background script tool-call rate limit exceeded."}


def test_script_gateway_binds_through_public_app_state_attributes() -> None:
    """Broker binding must use Starlette's public state attribute contract."""
    broker = _GatewayBroker(
        submit_receipt=_receipt(ScriptCallState.COMPLETED),
        get_receipt=_receipt(ScriptCallState.COMPLETED),
    )
    app = FastAPI()
    app.state = SimpleNamespace()

    bind_script_tool_broker(app, broker)

    assert app.state.script_tool_broker is broker


@pytest.mark.asyncio
async def test_script_gateway_fails_closed_when_broker_is_unbound() -> None:
    """An app without lifecycle broker wiring must not accept script calls."""
    app = FastAPI()
    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/script-gateway/calls",
            json=_payload(),
            headers={"Authorization": "Bearer secret-token"},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "Background script gateway is unavailable."}


@pytest.mark.asyncio
async def test_script_gateway_fails_closed_when_public_broker_state_is_empty() -> None:
    """An explicitly empty lifecycle binding must remain unavailable."""
    app = FastAPI()
    app.state.script_tool_broker = None
    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/script-gateway/calls",
            json=_payload(),
            headers={"Authorization": "Bearer secret-token"},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "Background script gateway is unavailable."}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["post", "get"])
async def test_script_gateway_hides_run_deletion_races(method: str) -> None:
    """A run removed after authentication remains an unavailable call instead of becoming a 500."""

    class RemovedRunBroker(_GatewayBroker):
        async def accept_authenticated(
            self,
            request: ScriptToolCallRequest,
            authorization: str | None,
        ) -> ScriptCallRecord:
            del request, authorization
            message = "run disappeared"
            raise ScriptRunNotFoundError(message)

        async def get_authenticated(
            self,
            run_id: str,
            call_id: str,
            authorization: str | None,
        ) -> ScriptCallRecord:
            del run_id, call_id, authorization
            message = "run disappeared"
            raise ScriptRunNotFoundError(message)

    broker = RemovedRunBroker(
        submit_receipt=_receipt(ScriptCallState.COMPLETED),
        get_receipt=_receipt(ScriptCallState.COMPLETED),
    )
    async with AsyncClient(transport=ASGITransport(app=_app(broker)), base_url="http://test") as client:
        if method == "post":
            response = await client.post(
                "/api/script-gateway/calls",
                json=_payload(),
                headers={"Authorization": "Bearer secret-token"},
            )
        else:
            response = await client.get(
                "/api/script-gateway/runs/run-1/calls/call-1",
                headers={"Authorization": "Bearer secret-token"},
            )

    assert response.status_code == 404
    assert response.json() == {"detail": "Background script call is unavailable."}


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
    broker = _GatewayBroker(
        submit_receipt=_receipt(ScriptCallState.PENDING),
        get_receipt=_receipt(ScriptCallState.PENDING),
    )
    async with AsyncClient(transport=ASGITransport(app=_app(broker)), base_url="http://test") as client:
        response = await client.post(
            "/api/script-gateway/calls",
            json=_payload(),
            headers={"Authorization": "Bearer secret-token"},
        )

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
        token_hash=hashlib.sha256(b"secret-token").hexdigest(),
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

        def require_active_capability(self, run_id: str, token: str) -> ScriptRunRecord:
            assert (run_id, token) == ("run-1", "secret-token")
            time.sleep(0.1)
            return run

        def claim_call(self, **_kwargs: object) -> ScriptCallClaim:
            message = "Stable call ID was already claimed with different arguments."
            raise ScriptCallConflictError(message)

        def get_call(self, run_id: str, call_id: str) -> ScriptCallRecord:
            assert (run_id, call_id) == ("run-1", "call-1")
            return call

    runtime_resolver = MagicMock(spec=ScriptRuntimeResolver)
    runtime_resolver.is_authorized.return_value = True
    broker = ScriptToolBroker(store=BlockingStore(), runtime_resolver=runtime_resolver)  # type: ignore[arg-type]
    broker.open_call_admission()

    async with AsyncClient(transport=ASGITransport(app=_app(broker)), base_url="http://test") as client:
        response = await client.post(
            "/api/script-gateway/calls",
            json=_payload(),
            headers={"Authorization": "Bearer secret-token"},
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "Stable call ID conflicts with its accepted request."}

    async with AsyncClient(transport=ASGITransport(app=_app(broker)), base_url="http://test") as client:
        old_response = await client.get(
            "/api/script-gateway/runs/run-1/calls/call-1",
            headers={"Authorization": "Bearer secret-token"},
        )

    assert old_response.status_code == 200
    assert old_response.json()["arguments_digest"] == old_arguments_digest


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
        async def accept_authenticated(
            self,
            request: ScriptToolCallRequest,
            authorization: str | None,
        ) -> ScriptCallRecord:
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
@pytest.mark.parametrize("method", ["post", "get"])
async def test_script_gateway_reports_transient_owner_runtime_unavailability(method: str) -> None:
    """A valid capability whose live bot is restarting remains retryable."""

    class UnavailableBroker(_GatewayBroker):
        async def accept_authenticated(
            self,
            request: ScriptToolCallRequest,
            authorization: str | None,
        ) -> ScriptCallRecord:
            del request, authorization
            message = "Background script owner runtime is temporarily unavailable."
            raise ScriptRuntimeUnavailableError(message)

        async def get_authenticated(
            self,
            run_id: str,
            call_id: str,
            authorization: str | None,
        ) -> ScriptCallRecord:
            del run_id, call_id, authorization
            message = "Background script owner runtime is temporarily unavailable."
            raise ScriptRuntimeUnavailableError(message)

    broker = UnavailableBroker(
        submit_receipt=_receipt(ScriptCallState.COMPLETED),
        get_receipt=_receipt(ScriptCallState.COMPLETED),
    )
    async with AsyncClient(transport=ASGITransport(app=_app(broker)), base_url="http://test") as client:
        if method == "post":
            response = await client.post(
                "/api/script-gateway/calls",
                json=_payload(),
                headers={"Authorization": "Bearer secret-token"},
            )
        else:
            response = await client.get(
                "/api/script-gateway/runs/run-1/calls/call-1",
                headers={"Authorization": "Bearer secret-token"},
            )

    assert response.status_code == 503
    assert response.json() == {"detail": "Background script owner runtime is temporarily unavailable."}


@pytest.mark.asyncio
async def test_script_gateway_hides_capability_revocation_racing_after_authentication() -> None:
    """A run revoked between authentication and claim must keep the generic unavailable response."""

    class RacingBroker(_GatewayBroker):
        async def accept_authenticated(
            self,
            request: ScriptToolCallRequest,
            authorization: str | None,
        ) -> ScriptCallRecord:
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

    broker = BlockingBroker(
        submit_receipt=_receipt(ScriptCallState.PENDING),
        get_receipt=_receipt(ScriptCallState.PENDING),
    )
    async with AsyncClient(transport=ASGITransport(app=_app(broker)), base_url="http://test") as client:
        submitter = asyncio.create_task(
            client.post(
                "/api/script-gateway/calls",
                json=_payload(),
                headers={"Authorization": "Bearer secret-token"},
            ),
        )
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

    broker = SlowBroker(
        submit_receipt=_receipt(ScriptCallState.PENDING),
        get_receipt=_receipt(ScriptCallState.PENDING),
    )
    async with AsyncClient(transport=ASGITransport(app=_app(broker)), base_url="http://test") as client:
        response = await client.post(
            "/api/script-gateway/calls",
            json=_payload(),
            headers={"Authorization": "Bearer secret-token"},
        )

    assert response.status_code == 202
    assert response.json()["state"] == "pending"
