"""The TypeSafe leaf adapter is bounded and validates the exact participation contract."""

from __future__ import annotations

import asyncio
import gzip
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx
import pytest

import mindroom.judgment.client as client_module
from mindroom.judgment.client import (
    _MAX_RESPONSE_BYTES,
    _TYPE_SAFE_ENDPOINT,
    SystemOneClient,
    _decode_response,
    _InvalidJudgmentResponseError,
    _JudgmentCapacity,
)
from mindroom.judgment.state import (
    PINNED_MODEL,
    JudgmentMessage,
    JudgmentRequest,
    build_participation_judgment_request,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from mindroom.judgment.answers import JudgmentResponse


pytestmark = pytest.mark.asyncio


def _request() -> JudgmentRequest:
    return build_participation_judgment_request(
        (JudgmentMessage("user", "How do I install the package?"),),
        instructions="Offer technical help.",
    )


def _response_body(
    *,
    probability: object = 0.8,
    model: str = PINNED_MODEL,
) -> bytes:
    return json.dumps(
        {
            "model": model,
            "answers": {"participation": {"type": "noul", "noul": probability}},
            "usage": {"input_tokens": 123, "output_tokens": 7},
        },
        allow_nan=True,
        separators=(",", ":"),
    ).encode()


async def test_client_posts_exact_contract_and_retains_probability() -> None:
    """Changing the endpoint, auth, request schema, or probability must fail."""
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_response_body(probability=0.731234))

    client = SystemOneClient(
        api_key="test-secret",
        model=PINNED_MODEL,
        transport=httpx.MockTransport(respond),
    )
    result = await client.judge(_request(), owner="turn-1", allow_network=True)

    assert result.failure is None
    assert result.answer is not None
    assert result.answer.probability == 0.731234
    assert result.model_id == PINNED_MODEL
    assert result.input_tokens == 123
    assert result.output_tokens == 7
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert str(seen[0].url) == _TYPE_SAFE_ENDPOINT
    assert seen[0].headers["authorization"] == "Bearer test-secret"
    assert json.loads(seen[0].content) == json.loads(_request().body or b"")


@pytest.mark.parametrize("probability", [0.0, 0.8, 1.0, True, -0.1, 1.1, "0.9", None, float("nan"), float("inf")])
async def test_participation_client_validates_noul_and_network_opt_in(probability: object) -> None:
    """A malformed Noul must not approve participation or bypass network opt-in."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "model": PINNED_MODEL,
                    "answers": {"participation": {"type": "noul", "noul": probability}},
                    "usage": {"input_tokens": 100, "output_tokens": 1},
                },
            ).encode(),
        )

    client = SystemOneClient(api_key="test-secret", model=PINNED_MODEL, transport=httpx.MockTransport(respond))
    request = build_participation_judgment_request((JudgmentMessage("user", "Any thoughts?"),), instructions="")
    refused = await client.judge(request, owner="agent")
    assert refused.failure == "network_disabled"
    assert not requests
    result = await client.judge(request, owner="agent", allow_network=True)
    assert len(requests) == 1
    assert requests[0].content == request.body
    assert requests[0].headers["authorization"] == "Bearer test-secret"
    if type(probability) is float and 0 <= probability <= 1:
        assert result.answer is not None
        assert result.answer.probability == probability
        assert result.failure is None
    else:
        assert result.answer is None
        assert result.failure == "invalid_response"


async def test_client_requires_explicit_network_opt_in() -> None:
    """A caller omission must never send externally."""
    calls = 0

    def refuse(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        msg = "transport must not run"
        raise AssertionError(msg)

    client = SystemOneClient(api_key="secret", model=PINNED_MODEL, transport=httpx.MockTransport(refuse))
    result = await client.judge(_request(), owner="turn-1")

    assert result.failure == "network_disabled"
    assert result.answer is None
    assert calls == 0


async def test_incomplete_state_never_reaches_transport() -> None:
    """Incomplete state must fail closed even when the caller enables transport."""
    incomplete = build_participation_judgment_request(
        (JudgmentMessage("user", "token=sk-secret"),),
        instructions="",
    )
    client = SystemOneClient(
        api_key="secret",
        model=PINNED_MODEL,
        transport=httpx.MockTransport(lambda _: pytest.fail("transport must not run")),
    )

    result = await client.judge(incomplete, owner="turn-1", allow_network=True)

    assert result.failure == "incomplete_state"
    assert result.answer is None


@pytest.mark.parametrize("body", [b"x" * 16_001, b"{}"])
async def test_client_rejects_requests_not_issued_by_the_bounded_builder(body: bytes) -> None:
    """Forged or mutated request bytes must not bypass the state builder at the network boundary."""
    forged = replace(_request(), body=body, state_bytes=len(body))
    client = SystemOneClient(
        api_key="secret",
        model=PINNED_MODEL,
        transport=httpx.MockTransport(lambda _: pytest.fail("transport must not run")),
    )

    result = await client.judge(forged, owner="turn-1", allow_network=True)

    assert result.failure == "invalid_request"
    assert result.answer is None


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (
            b'{"model":"jev-1.13.0","model":"jev-1.13.0","answers":{},"usage":{}}',
            "duplicate",
        ),
        (_response_body(probability=True), "probability"),
        (_response_body(probability=float("nan")), "number"),
        (_response_body(probability=1.1), "range"),
        (_response_body(probability=-0.1), "range"),
        (_response_body(model="jev-9.0.0"), "model"),
        (b"not-json", "JSON"),
    ],
)
async def test_decode_response_rejects_malformed_numbers_and_model_drift(body: bytes, match: str) -> None:
    """Malformed provider output must never be normalized into an accepted decision."""
    with pytest.raises(_InvalidJudgmentResponseError, match=match):
        _decode_response(body, expected_model=PINNED_MODEL)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"extra": None}),
        lambda payload: payload["answers"].update({"other_question": payload["answers"].pop("participation")}),
        lambda payload: payload["answers"]["participation"].update({"type": "choice"}),
        lambda payload: payload["answers"]["participation"].pop("noul"),
        lambda payload: payload["answers"]["participation"].update({"extra": None}),
        lambda payload: payload["usage"].update({"input_tokens": -1}),
        lambda payload: payload["usage"].update({"output_tokens": True}),
    ],
)
async def test_decode_response_rejects_schema_drift(mutate: Callable[[dict[str, Any]], None]) -> None:
    """Extra, missing, or mistyped contract fields must remain visible as invalid fixtures."""
    payload = json.loads(_response_body())
    mutate(payload)

    with pytest.raises(_InvalidJudgmentResponseError):
        _decode_response(json.dumps(payload).encode(), expected_model=PINNED_MODEL)


async def test_http_failures_are_not_retried_or_leaked_to_logs(caplog: pytest.LogCaptureFixture) -> None:
    """A failed paid call must be one attempt and must not log response bodies or keys."""
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, text="secret-response-body")

    client = SystemOneClient(
        api_key="secret-api-key",
        model=PINNED_MODEL,
        transport=httpx.MockTransport(respond),
    )
    result = await client.judge(_request(), owner="turn-1", allow_network=True)

    assert result.failure == "rate_limited"
    assert calls == 1
    assert "secret-api-key" not in caplog.text
    assert "secret-response-body" not in caplog.text


async def test_model_drift_has_a_distinct_closed_failure() -> None:
    """A new provider version must be visible to the harness instead of looking generically malformed."""
    client = SystemOneClient(
        api_key="secret",
        model=PINNED_MODEL,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=_response_body(model="jev-1.14.0")),
        ),
    )

    result = await client.judge(_request(), owner="turn-1", allow_network=True)

    assert result.failure == "model_drift"
    assert result.answer is None


async def test_total_deadline_includes_the_response_wait() -> None:
    """A stalled transport must release its caller inside the total configured budget."""

    async def respond(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, content=_response_body())

    client = SystemOneClient(
        api_key="secret",
        model=PINNED_MODEL,
        timeout_seconds=0.01,
        transport=httpx.MockTransport(respond),
    )

    async with asyncio.timeout(0.5):
        result = await client.judge(_request(), owner="turn-1", allow_network=True)

    assert result.failure == "timeout"


class _OneChunkStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.body


async def test_decoded_response_body_is_bounded_while_streaming() -> None:
    """A compressed response must not expand past the 64 KiB response budget."""
    expanded = b" " * (_MAX_RESPONSE_BYTES + 1)

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            stream=_OneChunkStream(gzip.compress(expanded)),
        )

    client = SystemOneClient(
        api_key="secret",
        model=PINNED_MODEL,
        transport=httpx.MockTransport(respond),
    )
    result = await client.judge(_request(), owner="turn-1", allow_network=True)

    assert result.failure == "response_too_large"


async def test_shared_capacity_rejects_globally_and_per_owner_without_waiting() -> None:
    """Separate clients must not create separate global or owner wait queues."""
    capacity = _JudgmentCapacity(max_concurrent=2, max_per_owner=1)
    first_entered = asyncio.Event()
    globally_full = asyncio.Event()
    release = asyncio.Event()
    active = 0

    async def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal active
        active += 1
        if active == 1:
            first_entered.set()
        if active == 2:
            globally_full.set()
        await release.wait()
        return httpx.Response(200, content=_response_body())

    transport = httpx.MockTransport(respond)
    first_client = SystemOneClient(api_key="secret", model=PINNED_MODEL, transport=transport, capacity=capacity)
    second_client = SystemOneClient(api_key="secret", model=PINNED_MODEL, transport=transport, capacity=capacity)
    first = asyncio.create_task(first_client.judge(_request(), owner="owner-a", allow_network=True))
    await first_entered.wait()
    same_owner = await second_client.judge(_request(), owner="owner-a", allow_network=True)
    second = asyncio.create_task(second_client.judge(_request(), owner="owner-b", allow_network=True))
    await globally_full.wait()
    rejected_globally = await first_client.judge(_request(), owner="owner-c", allow_network=True)
    release.set()
    completed = await asyncio.gather(first, second)

    assert same_owner.failure == "capacity_exhausted"
    assert rejected_globally.failure == "capacity_exhausted"
    assert all(result.failure is None for result in completed)


async def test_cancellation_propagates_and_releases_capacity() -> None:
    """Cancelling one request must not fabricate a result or strand its shared slot."""
    capacity = _JudgmentCapacity(max_concurrent=1, max_per_owner=1)
    entered = asyncio.Event()
    calls = 0

    async def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await asyncio.Event().wait()
        return httpx.Response(200, content=_response_body())

    client = SystemOneClient(
        api_key="secret",
        model=PINNED_MODEL,
        transport=httpx.MockTransport(respond),
        capacity=capacity,
    )
    cancelled = asyncio.create_task(client.judge(_request(), owner="turn-1", allow_network=True))
    await entered.wait()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    recovered = await client.judge(_request(), owner="turn-2", allow_network=True)

    assert recovered.failure is None
    assert calls == 2


@pytest.mark.parametrize(
    "body",
    [
        _response_body(probability=10**400),
        b'{"n":' + b"1" * 5_000 + b"}",
        b"[" * 20_000 + b"0" + b"]" * 20_000,
    ],
    ids=["huge_probability", "integer_digit_limit", "deep_nesting"],
)
async def test_adversarial_json_returns_invalid_response(body: bytes) -> None:
    """Parser recursion/digit limits and integer overflow remain closed failures."""
    client = SystemOneClient(
        api_key="synthetic",
        model=PINNED_MODEL,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)),
    )
    result = await client.judge(_request(), owner="test", allow_network=True)
    assert result.failure == "invalid_response"
    assert result.answer is None


async def test_synchronous_decode_overrun_cannot_return_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-yielding decoder still has to satisfy the total wall-time budget."""
    clock = [0.0]
    original = client_module._decode_response

    def slow_decode(body: bytes, *, expected_model: str) -> JudgmentResponse:
        response = original(body, expected_model=expected_model)
        clock[0] = 2.0
        return response

    monkeypatch.setattr(client_module, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(client_module, "_decode_response", slow_decode)
    client = SystemOneClient(
        api_key="synthetic",
        model=PINNED_MODEL,
        timeout_seconds=0.5,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=_response_body())),
    )
    result = await client.judge(_request(), owner="test", allow_network=True)
    assert result.failure == "timeout"
    assert result.answer is None
