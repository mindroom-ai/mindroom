"""The TypeSafe leaf adapter is bounded and validates the exact Choice contract."""

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
    InvalidJudgmentResponseError,
    JudgmentCapacity,
    SystemOneClient,
    decode_response,
)
from mindroom.judgment.state import (
    PINNED_MODEL,
    QUEUED_MESSAGE_QUESTION,
    JudgmentMessage,
    JudgmentRequest,
    QueuedJudgmentInput,
    build_queued_judgment_request,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from mindroom.judgment.answers import ChoiceResponse


pytestmark = pytest.mark.asyncio


def _request() -> JudgmentRequest:
    return build_queued_judgment_request(
        QueuedJudgmentInput(
            active=JudgmentMessage(sender="alice", text="Install the package."),
            queued=(JudgmentMessage(sender="alice", text="Thanks."),),
        ),
    )


def _response_body(
    *,
    choice: str = "finish",
    finish: object = 0.8,
    wrap_up: object = 0.2,
    confidence: object = 0.6,
    model: str = PINNED_MODEL,
) -> bytes:
    return json.dumps(
        {
            "model": model,
            "answers": {
                QUEUED_MESSAGE_QUESTION.question_id: {
                    "type": "choice",
                    "choice": choice,
                    "probabilities": {"finish": finish, "wrap_up": wrap_up},
                    "confidence": confidence,
                },
            },
            "usage": {"input_tokens": 123, "output_tokens": 7},
        },
        allow_nan=True,
        separators=(",", ":"),
    ).encode()


async def test_client_posts_exact_contract_and_retains_api_confidence() -> None:
    """Changing the endpoint, auth, request schema, or confidence source must fail."""
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_response_body(confidence=0.731234))

    client = SystemOneClient(
        api_key="test-secret",
        model=PINNED_MODEL,
        transport=httpx.MockTransport(respond),
    )
    result = await client.judge(_request(), owner="turn-1", allow_network=True)

    assert result.failure is None
    assert result.answer is not None
    assert result.answer.choice == "finish"
    assert result.answer.probabilities == (("finish", 0.8), ("wrap_up", 0.2))
    assert result.answer.confidence == 0.731234
    assert result.model_id == PINNED_MODEL
    assert result.input_tokens == 123
    assert result.output_tokens == 7
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert str(seen[0].url) == _TYPE_SAFE_ENDPOINT
    assert seen[0].headers["authorization"] == "Bearer test-secret"
    assert json.loads(seen[0].content) == json.loads(_request().body or b"")


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
    """Incomplete state must fail closed even when the harness enables its mock transport."""
    incomplete = build_queued_judgment_request(
        QueuedJudgmentInput(
            active=JudgmentMessage(sender="alice", text="Do it."),
            queued=(JudgmentMessage(sender="alice", text="token=sk-secret"),),
        ),
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
        (_response_body(finish=True, wrap_up=0.0), "probability"),
        (_response_body(finish=float("nan"), wrap_up=0.0), "number"),
        (_response_body(finish=1.1, wrap_up=-0.1), "range"),
        (_response_body(finish=0.7, wrap_up=0.2), "sum"),
        (_response_body(choice="finish", finish=0.2, wrap_up=0.8), "highest"),
        (_response_body(confidence=False), "confidence"),
        (_response_body(confidence=1.1), "range"),
        (_response_body(model="jev-9.0.0"), "model"),
        (b"not-json", "JSON"),
    ],
)
async def test_decode_response_rejects_malformed_numbers_and_model_drift(body: bytes, match: str) -> None:
    """Malformed provider output must never be normalized into an accepted decision."""
    with pytest.raises(InvalidJudgmentResponseError, match=match):
        decode_response(body, expected_model=PINNED_MODEL)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"extra": None}),
        lambda payload: payload["answers"].update({"other_question": payload["answers"].pop("queued_message_effect")}),
        lambda payload: payload["answers"]["queued_message_effect"]["probabilities"].update({"other": 0.0}),
        lambda payload: payload["answers"]["queued_message_effect"].update({"extra": None}),
        lambda payload: payload["usage"].update({"input_tokens": -1}),
        lambda payload: payload["usage"].update({"output_tokens": True}),
    ],
)
async def test_decode_response_rejects_schema_drift(mutate: Callable[[dict[str, Any]], None]) -> None:
    """Extra, missing, or mistyped contract fields must remain visible as invalid fixtures."""
    payload = json.loads(_response_body())
    mutate(payload)

    with pytest.raises(InvalidJudgmentResponseError):
        decode_response(json.dumps(payload).encode(), expected_model=PINNED_MODEL)


async def test_decode_response_accepts_small_probability_sum_rounding() -> None:
    """Decimal serialization noise inside the documented distribution must remain usable."""
    decoded = decode_response(
        _response_body(finish=0.7000003, wrap_up=0.3),
        expected_model=PINNED_MODEL,
    )

    assert decoded.answer.probabilities == (("finish", 0.7000003), ("wrap_up", 0.3))


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
    capacity = JudgmentCapacity(max_concurrent=2, max_per_owner=1)
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
    capacity = JudgmentCapacity(max_concurrent=1, max_per_owner=1)
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
        _response_body(finish=10**400),
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
    original = client_module.decode_response

    def slow_decode(body: bytes, *, expected_model: str) -> ChoiceResponse:
        response = original(body, expected_model=expected_model)
        clock[0] = 2.0
        return response

    monkeypatch.setattr(client_module, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(client_module, "decode_response", slow_decode)
    client = SystemOneClient(
        api_key="synthetic",
        model=PINNED_MODEL,
        timeout_seconds=0.5,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=_response_body())),
    )
    result = await client.judge(_request(), owner="test", allow_network=True)
    assert result.failure == "timeout"
    assert result.answer is None
