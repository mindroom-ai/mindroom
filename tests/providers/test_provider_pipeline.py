"""The provider-neutral pipeline stages: retries, the idle limit, media fallback, tool search, and logging."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, cast

import pytest
from structlog.testing import capture_logs

from mindroom.config.models import DebugConfig, ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.llm.errors import (
    MALFORMED_TOOL_CALL,
    MODEL_SAFEGUARD_REFUSAL_MESSAGE,
    SAFEGUARD_REFUSAL,
    InvalidRequest,
    ModelSafeguardRefusal,
    ProviderError,
    ProviderUnavailable,
)
from mindroom.llm.media import Audio, Image
from mindroom.llm.messages import Message
from mindroom.llm.provider import (
    Capabilities,
    ModelIdentity,
    ModelRequest,
    StreamProgress,
    TextDelta,
    ToolCallReady,
    ToolSpec,
    TurnCompleted,
    UsageReported,
    complete,
)
from mindroom.llm.request_context import bind_llm_request_log_context
from mindroom.llm.usage import MessageMetrics
from mindroom.media_delivery import VIEWED_IMAGE_ID_PREFIX
from mindroom.providers.errors import classify_provider_error
from mindroom.providers.gemini.wire import MALFORMED_FUNCTION_CALL_GUIDANCE
from mindroom.providers.pipeline import request_log as request_log_stage
from mindroom.providers.pipeline import retry as retry_stage
from mindroom.providers.pipeline.compose import stream_idle_timeout_seconds, wrap_provider
from mindroom.providers.pipeline.deferred_tools import portable_tool_search_stream
from mindroom.providers.pipeline.guidance_retry import guidance_retry_stream
from mindroom.providers.pipeline.media import FALLBACK_MARKER, media_fallback_stream
from mindroom.providers.pipeline.refusal import refusal_stream
from mindroom.providers.pipeline.request_log import RequestLog, logged_stream
from mindroom.providers.pipeline.retry import retrying_stream
from mindroom.providers.pipeline.wrapped import AGGREGATE_USAGE_KEY, WrappedProvider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.llm.provider import StreamEvent

_PROMPT = "Use the attached image."
_FALLBACK_PROMPT = "Media was not inspected."

type _Step = StreamEvent | Exception | float


_IDENTITY = ModelIdentity(id="scripted", provider="Scripted", provider_name="Scripted Chat")
_CAPABILITIES = Capabilities()


@dataclass
class _ScriptedProvider:
    """Answer each request with the next scripted attempt; an exception step raises, a float step waits."""

    attempts: list[list[_Step]]
    identity: ModelIdentity = _IDENTITY
    capabilities: Capabilities = _CAPABILITIES
    native_history_route: None = None
    replay_estimator: None = None
    model_params: dict[str, object] = field(default_factory=dict)
    requests: list[ModelRequest] = field(default_factory=list)
    closed: int = 0

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        steps = self.attempts[len(self.requests) - 1]
        try:
            for step in steps:
                if isinstance(step, Exception):
                    raise step
                if isinstance(step, float):
                    await asyncio.sleep(step)
                    continue
                yield cast("StreamEvent", step)
        finally:
            self.closed += 1


def _turn(text: str = "done", usage: MessageMetrics | None = None) -> TurnCompleted:
    message = Message(role="assistant", content=text, provider_data={"mindroom_model": {"id": "scripted"}})
    if usage is not None:
        message.metrics = usage
    return TurnCompleted(message=message, stop_reason="end")


def _transient(message: str = "overloaded") -> ProviderUnavailable:
    return ProviderUnavailable(message, provider="Scripted Chat", status_code=503)


def _malformed() -> ProviderError:
    message = "Generation ended with finish reason: MALFORMED_FUNCTION_CALL"
    return ProviderError(message, provider="Scripted Chat", code=MALFORMED_TOOL_CALL)


async def _drain(provider: WrappedProvider, request: ModelRequest) -> list[StreamEvent]:
    return [event async for event in provider.stream(request)]


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry_stage, "_retry_delay_seconds", lambda _attempt: 0.0)


def _user(text: str = "hi", **media: object) -> Message:
    return Message(role="user", content=text, **media)


# --- Stream retry ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_failure_before_output_is_retried_and_its_usage_carried() -> None:
    """A failed attempt's reported usage is added to the finished turn, which is marked as aggregated."""
    failed_usage = MessageMetrics(input_tokens=40, output_tokens=0, total_tokens=40)
    final_usage = MessageMetrics(input_tokens=42, output_tokens=12, total_tokens=54)
    inner = _ScriptedProvider(
        [
            [UsageReported(failed_usage), _transient()],
            [TextDelta("done"), UsageReported(final_usage), _turn(usage=final_usage)],
        ],
    )
    provider = WrappedProvider(inner, partial(retrying_stream, idle_timeout_seconds=None))

    events = await _drain(provider, ModelRequest(messages=[_user()]))

    assert len(inner.requests) == 2
    assert inner.closed == 2
    reported = [event.usage for event in events if isinstance(event, UsageReported)]
    assert reported == [failed_usage, final_usage]
    finished = events[-1]
    assert isinstance(finished, TurnCompleted)
    assert finished.message.metrics.to_dict() == {"input_tokens": 82, "output_tokens": 12, "total_tokens": 94}
    assert finished.message.provider_data == {"mindroom_model": {"id": "scripted"}, "mindroom_aggregate_usage": True}


@pytest.mark.asyncio
async def test_failure_after_visible_output_is_not_retried() -> None:
    inner = _ScriptedProvider([[TextDelta("partial"), _transient()], [_turn()]])
    provider = WrappedProvider(inner, partial(retrying_stream, idle_timeout_seconds=None))

    with pytest.raises(ProviderUnavailable):
        await _drain(provider, ModelRequest(messages=[_user()]))

    assert len(inner.requests) == 1


@pytest.mark.asyncio
async def test_tool_call_counts_as_visible_output() -> None:
    inner = _ScriptedProvider([[ToolCallReady({"id": "call_1"}), _transient()], [_turn()]])
    provider = WrappedProvider(inner, partial(retrying_stream, idle_timeout_seconds=None))

    with pytest.raises(ProviderUnavailable):
        await _drain(provider, ModelRequest(messages=[_user()]))

    assert len(inner.requests) == 1


@pytest.mark.asyncio
async def test_progress_events_are_not_visible_output() -> None:
    inner = _ScriptedProvider([[StreamProgress(), _transient()], [_turn()]])
    provider = WrappedProvider(inner, partial(retrying_stream, idle_timeout_seconds=None))

    events = await _drain(provider, ModelRequest(messages=[_user()]))

    assert (len(inner.requests), events[0]) == (2, StreamProgress())


@pytest.mark.asyncio
async def test_non_retryable_failure_propagates_at_once() -> None:
    inner = _ScriptedProvider([[InvalidRequest("bad body", provider="Scripted Chat", status_code=400)], [_turn()]])
    provider = WrappedProvider(inner, partial(retrying_stream, idle_timeout_seconds=None))

    with pytest.raises(InvalidRequest):
        await _drain(provider, ModelRequest(messages=[_user()]))

    assert len(inner.requests) == 1


@pytest.mark.asyncio
async def test_transient_retries_are_bounded() -> None:
    inner = _ScriptedProvider([[_transient()] for _ in range(6)])
    provider = WrappedProvider(inner, partial(retrying_stream, idle_timeout_seconds=None))

    with pytest.raises(ProviderUnavailable):
        await _drain(provider, ModelRequest(messages=[_user()]))

    assert len(inner.requests) == 5


@pytest.mark.asyncio
async def test_silent_stream_is_retried_once_then_fails_as_stalled() -> None:
    inner = _ScriptedProvider([[1.0, _turn()], [1.0, _turn()], [_turn()]])
    provider = WrappedProvider(inner, partial(retrying_stream, idle_timeout_seconds=0.01))

    with pytest.raises(ProviderUnavailable, match="stalled") as info:
        await _drain(provider, ModelRequest(messages=[_user()]))

    assert (info.value.status_code, info.value.code, len(inner.requests)) == (504, "stream_stalled", 2)


@pytest.mark.asyncio
async def test_non_streaming_requests_are_not_retried() -> None:
    inner = _ScriptedProvider([[_transient()], [_turn()]])
    provider = WrappedProvider(inner, partial(retrying_stream, idle_timeout_seconds=None))

    with pytest.raises(ProviderUnavailable):
        await _drain(provider, ModelRequest(messages=[_user()], stream=False))

    assert len(inner.requests) == 1


@pytest.mark.asyncio
async def test_a_timeout_the_provider_raises_is_not_reported_as_a_stall() -> None:
    """An SDK's own timeout inside the idle limit propagates as raised instead of becoming a retried stall."""
    inner = _ScriptedProvider([[TimeoutError("read timed out")], [_turn()]])
    provider = WrappedProvider(inner, partial(retrying_stream, idle_timeout_seconds=60.0))

    with pytest.raises(TimeoutError, match="read timed out"):
        await _drain(provider, ModelRequest(messages=[_user()]))

    assert len(inner.requests) == 1


@pytest.mark.asyncio
async def test_a_failed_attempt_without_tokens_leaves_the_finished_turn_unmarked() -> None:
    """The finished turn's token counts stay one request's own, so they still describe its context."""
    final_usage = MessageMetrics(input_tokens=42, output_tokens=12, total_tokens=54, duration=2.0)
    inner = _ScriptedProvider(
        [
            [UsageReported(MessageMetrics(duration=1.5)), _transient()],
            [UsageReported(final_usage), _turn(usage=final_usage)],
        ],
    )
    provider = WrappedProvider(inner, partial(retrying_stream, idle_timeout_seconds=None))

    events = await _drain(provider, ModelRequest(messages=[_user()]))

    finished = events[-1]
    assert isinstance(finished, TurnCompleted)
    assert finished.message.metrics.to_dict() == {
        "input_tokens": 42,
        "output_tokens": 12,
        "total_tokens": 54,
        "duration": 3.5,
    }
    assert finished.message.provider_data == {"mindroom_model": {"id": "scripted"}}


# --- Media fallback -------------------------------------------------------------------------------------------


def _image(image_id: str = "image-1") -> Image:
    return Image(id=image_id, content=b"\x89PNG\r\n\x1a\n0000", mime_type="image/png")


@pytest.mark.asyncio
async def test_media_the_route_omits_is_replaced_by_the_fallback_note() -> None:
    inner = _ScriptedProvider([[_turn()]], capabilities=Capabilities(omitted_media=frozenset({"audio"})))
    provider = WrappedProvider(inner, partial(media_fallback_stream, fallback_prompt=_FALLBACK_PROMPT))
    audio = Audio(content=b"RIFF", mime_type="audio/wav")

    await _drain(provider, ModelRequest(messages=[_user(_PROMPT, audio=[audio], images=[_image()])]))

    sent = inner.requests[0].messages[0]
    assert sent.audio is None
    assert sent.images == [_image()]
    assert sent.content == f"{_PROMPT}\n\n{FALLBACK_MARKER}\n{_FALLBACK_PROMPT}"


@pytest.mark.asyncio
async def test_rejected_media_is_removed_for_one_retry_and_remembered_for_the_route() -> None:
    rejection = InvalidRequest("This model does not support image input", provider="Scripted Chat", status_code=400)
    inner = _ScriptedProvider([[UsageReported(MessageMetrics(input_tokens=5)), rejection], [_turn()], [_turn()]])
    provider = WrappedProvider(inner, partial(media_fallback_stream, fallback_prompt=_FALLBACK_PROMPT))
    request = ModelRequest(messages=[_user(_PROMPT, images=[_image()])])

    events = await _drain(provider, request)

    retried = inner.requests[1].messages[0]
    assert (retried.images, retried.content) == (None, f"{_PROMPT}\n\n{FALLBACK_MARKER}\n{_FALLBACK_PROMPT}")
    finished = events[-1]
    assert isinstance(finished, TurnCompleted)
    assert finished.message.metrics.input_tokens == 5
    assert finished.message.provider_data["mindroom_aggregate_usage"] is True
    # The capability gap is learned, so the next request leaves the image out up front.
    await _drain(provider, request)
    assert len(inner.requests) == 3
    assert inner.requests[2].messages[0].images is None


@pytest.mark.asyncio
async def test_input_specific_rejection_retries_without_learning() -> None:
    rejection = InvalidRequest("Image exceeds 5 MB maximum", provider="Scripted Chat", status_code=400)
    inner = _ScriptedProvider([[rejection], [_turn()], [_turn()]])
    provider = WrappedProvider(inner, partial(media_fallback_stream, fallback_prompt=_FALLBACK_PROMPT))
    request = ModelRequest(messages=[_user(_PROMPT, images=[_image()])])

    await _drain(provider, request)
    await _drain(provider, request)

    assert inner.requests[1].messages[0].images is None
    assert inner.requests[2].messages[0].images == [_image()]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [413, 501])
async def test_a_payload_or_server_rejection_retries_without_learning(status: int) -> None:
    """A 413 or a final server error proves nothing about the model, even when its text names a capability gap."""
    rejection = classify_provider_error(
        "This model does not support image input",
        provider="Scripted Chat",
        status_code=status,
    )
    inner = _ScriptedProvider([[rejection], [_turn()], [_turn()]])
    provider = WrappedProvider(inner, partial(media_fallback_stream, fallback_prompt=_FALLBACK_PROMPT))
    request = ModelRequest(messages=[_user(_PROMPT, images=[_image()])])

    await _drain(provider, request)
    await _drain(provider, request)

    assert inner.requests[1].messages[0].images is None
    assert inner.requests[2].messages[0].images == [_image()]


@pytest.mark.asyncio
async def test_a_mixed_media_rejection_retries_without_learning() -> None:
    """A rejection of a request with two media kinds cannot say which kind the model lacks."""
    rejection = InvalidRequest("This model does not support image input", provider="Scripted Chat", status_code=400)
    inner = _ScriptedProvider([[rejection], [_turn()], [_turn()]])
    provider = WrappedProvider(inner, partial(media_fallback_stream, fallback_prompt=_FALLBACK_PROMPT))
    audio = Audio(content=b"RIFF", mime_type="audio/wav")
    request = ModelRequest(messages=[_user(_PROMPT, audio=[audio], images=[_image()])])

    await _drain(provider, request)
    await _drain(provider, request)

    assert (inner.requests[1].messages[0].images, inner.requests[1].messages[0].audio) == (None, None)
    assert (inner.requests[2].messages[0].images, inner.requests[2].messages[0].audio) == ([_image()], [audio])


@pytest.mark.asyncio
async def test_a_learned_media_limit_stays_with_its_endpoint() -> None:
    """The same model id behind another endpoint keeps sending the media one endpoint rejected."""
    rejection = InvalidRequest("This model does not support image input", provider="Scripted Chat", status_code=400)
    first = _ScriptedProvider([[rejection], [_turn()]], identity=replace(_IDENTITY, endpoint="http://one/v1"))
    other = _ScriptedProvider([[_turn()]], identity=replace(_IDENTITY, endpoint="http://two/v1"))
    request = ModelRequest(messages=[_user(_PROMPT, images=[_image()])])

    await _drain(WrappedProvider(first, partial(media_fallback_stream, fallback_prompt=_FALLBACK_PROMPT)), request)
    await _drain(WrappedProvider(other, partial(media_fallback_stream, fallback_prompt=_FALLBACK_PROMPT)), request)

    assert other.requests[0].messages[0].images == [_image()]


@pytest.mark.asyncio
async def test_retryable_failures_and_visible_output_are_not_media_retries() -> None:
    transient = _ScriptedProvider([[_transient()]])
    visible = _ScriptedProvider(
        [[TextDelta("partial"), InvalidRequest("This model does not support image input", provider="x")]],
    )
    request = ModelRequest(messages=[_user(_PROMPT, images=[_image()])])

    for inner in (transient, visible):
        with pytest.raises(ProviderError):
            await _drain(WrappedProvider(inner, partial(media_fallback_stream, fallback_prompt="x")), request)
        assert len(inner.requests) == 1


@pytest.mark.asyncio
async def test_removed_viewed_image_marks_its_receipt_unsupported() -> None:
    receipt = json.dumps({"view_status": "ready", "attachment_id": "att-1"})
    messages = [
        Message(
            role="assistant",
            tool_calls=[
                {"id": "call_1", "type": "function", "function": {"name": "get_attachment", "arguments": "{}"}},
            ],
        ),
        Message(role="tool", tool_call_id="call_1", content=receipt),
        _user("Viewed image follows.", images=[_image(f"{VIEWED_IMAGE_ID_PREFIX}att-1")]),
    ]
    inner = _ScriptedProvider([[_turn()]], capabilities=Capabilities(omitted_media=frozenset({"image"})))

    await _drain(
        WrappedProvider(inner, partial(media_fallback_stream, fallback_prompt="x")),
        ModelRequest(messages=messages),
    )

    tool_result = json.loads(inner.requests[0].messages[1].content)
    assert tool_result["view_status"] == "unsupported"
    assert messages[1].content == receipt


# --- Portable tool search -------------------------------------------------------------------------------------


def _tool(name: str) -> ToolSpec:
    return ToolSpec(name=name, description=name, parameters={"type": "object"})


@pytest.mark.asyncio
async def test_portable_tool_search_hides_deferred_tools_behind_call_tool() -> None:
    stored_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "lookup_order", "arguments": '{"order_id": "A-1"}'},
    }
    messages = [
        Message(role="assistant", tool_calls=[stored_call]),
        Message(role="tool", tool_call_id="call_1", tool_name="lookup_order", content="shipped"),
    ]
    inner = _ScriptedProvider([[_turn()]])
    request = ModelRequest(
        messages=messages,
        tools=[_tool("call_tool"), _tool("lookup_order"), _tool("tool_search")],
        deferred_tool_names=frozenset({"lookup_order"}),
    )

    await _drain(WrappedProvider(inner, portable_tool_search_stream), request)

    sent = inner.requests[0]
    assert [tool.name for tool in sent.tools] == ["call_tool", "tool_search"]
    assert sent.deferred_tool_names == frozenset()
    assert sent.messages[0].tool_calls == [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "call_tool",
                "arguments": '{"name": "lookup_order", "arguments": {"order_id": "A-1"}}',
            },
        },
    ]
    assert sent.messages[1].tool_name == "call_tool"
    assert messages[0].tool_calls == [stored_call]


@pytest.mark.asyncio
async def test_native_tool_search_routes_receive_the_deferred_names() -> None:
    inner = _ScriptedProvider([[_turn()]], capabilities=Capabilities(native_tool_search=True))
    request = ModelRequest(
        messages=[_user()],
        tools=[_tool("lookup_order")],
        deferred_tool_names=frozenset({"lookup_order"}),
    )

    await _drain(WrappedProvider(inner, portable_tool_search_stream), request)

    assert inner.requests[0] is request


def _stored_call(call_id: str, name: str, arguments: str) -> dict[str, object]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


@pytest.mark.asyncio
async def test_portable_tool_search_projects_stored_calls_byte_stably() -> None:
    """Repeated requests wrap the same deferred call identically and leave eager and unknown calls untouched."""
    eager = _stored_call("call_2", "shell", "{}")
    legacy = _stored_call("call_3", "load_tool", '{"tool_name": "crm"}')
    assistant = Message(role="assistant", tool_calls=[_stored_call("call_1", "lookup_order", '{"note": "é"}'), eager])
    untouched = Message(role="assistant", tool_calls=[legacy])
    inner = _ScriptedProvider([[_turn()], [_turn()]])
    request = ModelRequest(messages=[assistant, untouched], deferred_tool_names=frozenset({"lookup_order"}))

    await _drain(WrappedProvider(inner, portable_tool_search_stream), request)
    await _drain(WrappedProvider(inner, portable_tool_search_stream), request)

    first, second = (sent.messages for sent in inner.requests)
    assert first[0].tool_calls == second[0].tool_calls
    assert first[0].tool_calls[0]["function"]["arguments"] == '{"name": "lookup_order", "arguments": {"note": "é"}}'
    assert first[0].tool_calls[1] is eager
    assert first[1] is untouched


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "arguments"),
    [
        ("{'order_id': 'A-1'}", {"order_id": "A-1"}),
        ("[]", {}),
        ("", {}),
        ('"A-1"', {}),
        ('{{"id": 1}}', {}),
    ],
    ids=["python-literal", "empty-list", "empty", "not-an-object", "unparseable"],
)
async def test_portable_tool_search_wraps_stored_arguments_as_an_object(stored: str, arguments: dict[str, str]) -> None:
    """Stored arguments Agno accepted keep their values; malformed ones wrap as empty instead of breaking the thread."""
    assistant = Message(role="assistant", tool_calls=[_stored_call("call_1", "lookup_order", stored)])
    inner = _ScriptedProvider([[_turn()]])
    request = ModelRequest(messages=[assistant], deferred_tool_names=frozenset({"lookup_order"}))

    await _drain(WrappedProvider(inner, portable_tool_search_stream), request)

    wrapped = inner.requests[0].messages[0].tool_calls[0]["function"]["arguments"]
    assert json.loads(wrapped) == {"name": "lookup_order", "arguments": arguments}


# --- Safeguard refusal ----------------------------------------------------------------------------------------


def _refusal(usage: MessageMetrics) -> TurnCompleted:
    return TurnCompleted(message=Message(role="assistant", metrics=usage), stop_reason="refusal")


@pytest.mark.asyncio
async def test_a_refused_turn_raises_the_typed_refusal_after_its_usage() -> None:
    usage = MessageMetrics(input_tokens=40, output_tokens=9, total_tokens=49)
    inner = _ScriptedProvider([[UsageReported(usage), _refusal(usage)]])
    events = WrappedProvider(inner, refusal_stream).stream(ModelRequest(messages=[_user()]))

    assert await anext(events) == UsageReported(usage)
    with pytest.raises(ModelSafeguardRefusal) as raised:
        await anext(events)

    refusal = raised.value
    assert (str(refusal), refusal.code, refusal.provider, refusal.retryable) == (
        MODEL_SAFEGUARD_REFUSAL_MESSAGE,
        SAFEGUARD_REFUSAL,
        "Scripted Chat",
        False,
    )
    assert inner.closed == 1


@pytest.mark.asyncio
async def test_a_refusal_is_neither_retried_nor_resent_without_media() -> None:
    """A transient failure before still retries; the refused attempt ends the request with its media intact."""
    usage = MessageMetrics(input_tokens=40, output_tokens=9, total_tokens=49)
    inner = _ScriptedProvider([[_transient()], [UsageReported(usage), _refusal(usage)]])
    provider = wrap_provider(inner, idle_timeout_seconds=None, media_fallback_prompt="x", request_log=None)

    with pytest.raises(ModelSafeguardRefusal):
        await complete(provider, ModelRequest(messages=[_user(_PROMPT, images=[_image()])]))

    assert len(inner.requests) == 2
    assert all(request.messages[0].images for request in inner.requests)


# --- Malformed tool-call guidance -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_malformed_tool_call_is_retried_with_guidance_on_the_retried_request_only() -> None:
    """The guidance is appended to the resent request, never to the caller's conversation; usage carries over."""
    failed = MessageMetrics(input_tokens=30, total_tokens=30)
    final = MessageMetrics(input_tokens=42, output_tokens=12, total_tokens=54)
    inner = _ScriptedProvider(
        [[UsageReported(failed), _malformed()], [TextDelta("done"), UsageReported(final), _turn(usage=final)]],
    )
    message = _user()
    conversation = [message]
    request = ModelRequest(messages=conversation)

    events = await _drain(WrappedProvider(inner, guidance_retry_stream), request)

    first, retried = inner.requests
    *resent, guidance = retried.messages
    assert first is request
    assert resent == [message]
    assert (guidance.role, guidance.content) == ("user", MALFORMED_FUNCTION_CALL_GUIDANCE)
    assert conversation == [message]
    finished = events[-1]
    assert isinstance(finished, TurnCompleted)
    assert finished.message.metrics.to_dict() == {"input_tokens": 72, "output_tokens": 12, "total_tokens": 84}
    assert (finished.message.provider_data or {})[AGGREGATE_USAGE_KEY] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attempt",
    [[TextDelta("partial"), _malformed()], [InvalidRequest("bad", provider="Scripted Chat", status_code=400)]],
    ids=["malformed-after-output", "other-error"],
)
async def test_only_a_malformed_tool_call_before_output_gets_guidance(attempt: list[_Step]) -> None:
    inner = _ScriptedProvider([attempt, [_turn()]])

    with pytest.raises(ProviderError):
        await _drain(WrappedProvider(inner, guidance_retry_stream), ModelRequest(messages=[_user()]))

    assert len(inner.requests) == 1


# --- Request logging ------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_log_records_the_request_and_its_usage(tmp_path: Path) -> None:
    usage = MessageMetrics(input_tokens=42, output_tokens=12, total_tokens=54, cache_read_tokens=30)
    inner = _ScriptedProvider([[UsageReported(usage), _turn(usage=usage)]], model_params={"temperature": 0.2})
    log = RequestLog(
        model_name="default",
        configured_provider="openai",
        debug=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )
    request = ModelRequest(messages=[Message(role="system", content="Be brief."), _user()], tools=[_tool("search")])

    await _drain(WrappedProvider(inner, partial(logged_stream, log=log)), request)

    (log_file,) = tmp_path.glob("llm-requests-*.jsonl")
    request_record, response_record = (json.loads(line) for line in log_file.read_text().splitlines())
    assert request_record["system_prompt"] == "Be brief."
    assert request_record["message_count"] == 2
    assert [tool["name"] for tool in request_record["tools"]] == ["search"]
    assert request_record["model_params"] == {"temperature": 0.2}
    assert response_record["request_log_id"] == request_record["request_log_id"]
    assert response_record["usage"] == {
        "input_tokens": 42,
        "output_tokens": 12,
        "cache_read_tokens": 30,
        "cache_write_tokens": 0,
    }


def _request_log(tmp_path: Path, *, enabled: bool = True, provider: str = "openai") -> RequestLog:
    return RequestLog(
        model_name="default",
        configured_provider=provider,
        debug=DebugConfig(log_llm_requests=enabled, llm_request_log_dir=str(tmp_path) if enabled else None),
        default_log_dir=tmp_path / "default",
    )


def _log_records(tmp_path: Path) -> list[dict[str, object]]:
    (log_file,) = tmp_path.glob("llm-requests-*.jsonl")
    return [json.loads(line) for line in log_file.read_text().splitlines()]


@pytest.mark.asyncio
async def test_request_log_masks_credentials_before_writing(tmp_path: Path) -> None:
    inner = _ScriptedProvider([[_turn()]])
    leaked = "call failed with Authorization: Bearer auth-secret and api_key=api-secret"
    request = ModelRequest(messages=[_user(leaked)])

    with bind_llm_request_log_context(callback_url="https://example.test/cb?code=code-secret&keep=1"):
        await _drain(WrappedProvider(inner, partial(logged_stream, log=_request_log(tmp_path))), request)

    [record] = _log_records(tmp_path)
    assert record["messages"][0]["content"] == (
        "call failed with Authorization: Bearer ***redacted*** and api_key=***redacted***"
    )
    assert record["callback_url"] == "https://example.test/cb?code=***redacted***&keep=1"
    assert record["agent_id"] == "default"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "context_tokens", "uncached", "ratio"),
    [("OpenAI", 1_000, 200, 0.8), ("Anthropic", 1_900, 1_100, 0.421053)],
    ids=["cache-inside-input", "cache-beside-input"],
)
async def test_usage_telemetry_is_logged_without_the_request_log(
    tmp_path: Path,
    provider: str,
    context_tokens: int,
    uncached: int,
    ratio: float,
) -> None:
    """Every request reports its usage; Anthropic counts cached tokens beside the input, not inside it."""
    usage = MessageMetrics(input_tokens=1_000, output_tokens=50, reasoning_tokens=20, cache_read_tokens=800)
    usage.cache_write_tokens = 100
    identity = replace(_IDENTITY, provider=provider)
    inner = _ScriptedProvider([[UsageReported(usage), _turn(usage=usage)]], identity=identity)
    log = _request_log(tmp_path, enabled=False, provider=provider.lower())

    with capture_logs() as logs, bind_llm_request_log_context(correlation_id="corr-1", full_prompt="private"):
        await _drain(WrappedProvider(inner, partial(logged_stream, log=log)), ModelRequest(messages=[_user()]))

    assert list(tmp_path.iterdir()) == []
    assert [entry for entry in logs if entry["event"] == "LLM usage"] == [
        {
            "event": "LLM usage",
            "log_level": "info",
            "model_name": "default",
            "model_id": "scripted",
            "provider": provider,
            "usage_available": True,
            "correlation_id": "corr-1",
            "input_tokens": 1_000,
            "context_input_tokens": context_tokens,
            "output_tokens": 50,
            "reasoning_tokens": 20,
            "cache_read_tokens": 800,
            "cache_write_tokens": 100,
            "uncached_input_tokens": uncached,
            "cache_read_ratio": ratio,
        },
    ]


@pytest.mark.asyncio
async def test_a_request_without_usage_is_reported_and_writes_no_response_record(tmp_path: Path) -> None:
    served = Message(role="assistant", content="ok", provider_data={"model_extra": {"provider": "OpenInference"}})
    inner = _ScriptedProvider([[TurnCompleted(message=served, stop_reason="end")]])

    with capture_logs() as logs:
        await _drain(
            WrappedProvider(inner, partial(logged_stream, log=_request_log(tmp_path))),
            ModelRequest(messages=[_user()]),
        )

    [usage_log] = [entry for entry in logs if entry["event"] == "LLM usage"]
    assert (usage_log["usage_available"], usage_log["served_provider"]) == (False, "OpenInference")
    assert [record.get("record") for record in _log_records(tmp_path)] == [None]


@pytest.mark.asyncio
async def test_a_failing_request_log_neither_fails_the_request_nor_hides_its_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unwritable(_path: Path, _payload: dict[str, object]) -> None:
        message = "disk full"
        raise OSError(message)

    monkeypatch.setattr(request_log_stage, "_write_line", unwritable)
    usage = MessageMetrics(input_tokens=3, output_tokens=1)
    answered = _ScriptedProvider([[UsageReported(usage), _turn(usage=usage)]])
    failed = _ScriptedProvider([[InvalidRequest("bad request", provider="Scripted Chat", status_code=400)]])

    with capture_logs() as logs:
        events = await _drain(WrappedProvider(answered, partial(logged_stream, log=_request_log(tmp_path))), _request())
        with pytest.raises(InvalidRequest, match="bad request"):
            await _drain(WrappedProvider(failed, partial(logged_stream, log=_request_log(tmp_path))), _request())

    assert isinstance(events[-1], TurnCompleted)
    assert [entry["usage_available"] for entry in logs if entry["event"] == "LLM usage"] == [True, False]


@pytest.mark.asyncio
async def test_a_response_record_that_cannot_be_written_is_reported_and_the_answer_still_arrives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disk that fills between the request and response records costs only the response record."""
    write_line = request_log_stage._write_line
    written: list[dict[str, object]] = []

    def fills_after_one_line(path: Path, payload: dict[str, object]) -> None:
        if written:
            message = "No space left on device"
            raise OSError(message)
        written.append(payload)
        write_line(path, payload)

    monkeypatch.setattr(request_log_stage, "_write_line", fills_after_one_line)
    usage = MessageMetrics(input_tokens=3, output_tokens=1)
    inner = _ScriptedProvider([[UsageReported(usage), _turn(usage=usage)]])

    with capture_logs() as logs:
        events = await _drain(WrappedProvider(inner, partial(logged_stream, log=_request_log(tmp_path))), _request())

    assert isinstance(events[-1], TurnCompleted)
    assert [record.get("record") for record in _log_records(tmp_path)] == [None]
    assert [entry["event"] for entry in logs if entry["log_level"] in {"info", "error"}] == [
        "LLM usage",
        "Failed to emit LLM response telemetry",
    ]


@pytest.mark.asyncio
async def test_usage_seen_before_a_stream_is_abandoned_is_still_recorded(tmp_path: Path) -> None:
    usage = MessageMetrics(input_tokens=3, cache_read_tokens=42)
    inner = _ScriptedProvider([[UsageReported(usage), TextDelta("ok"), TextDelta("never read"), _turn()]])
    stream = WrappedProvider(inner, partial(logged_stream, log=_request_log(tmp_path))).stream(_request())

    assert isinstance(await anext(stream), UsageReported)
    await stream.aclose()

    request_record, response_record = _log_records(tmp_path)
    assert response_record["request_log_id"] == request_record["request_log_id"]
    assert response_record["usage"]["cache_read_tokens"] == 42


def _request() -> ModelRequest:
    return ModelRequest(messages=[_user()])


# --- Composition ----------------------------------------------------------------------------------------------


def test_stream_idle_limit_policy(tmp_path: Path) -> None:
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "data", process_env={})
    with_base_url = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={"OPENAI_BASE_URL": "http://localhost:4000/v1"},
    )
    with_vertex_base_url = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={"ANTHROPIC_VERTEX_BASE_URL": "https://vertex.proxy.example/v1"},
    )
    hosted = ModelConfig(provider="openai", id="gpt-6-astra")
    assert stream_idle_timeout_seconds(hosted, paths) == 300.0
    assert stream_idle_timeout_seconds(hosted, with_base_url) is None
    assert stream_idle_timeout_seconds(hosted, with_vertex_base_url) == 300.0
    vertex = ModelConfig(provider="vertexai_claude", id="claude-sonnet-5-5")
    assert stream_idle_timeout_seconds(vertex, paths) == 300.0
    assert stream_idle_timeout_seconds(vertex, with_vertex_base_url) is None
    assert stream_idle_timeout_seconds(ModelConfig(provider="llama_cpp", id="m"), paths) is None
    assert (
        stream_idle_timeout_seconds(ModelConfig(provider="zai", id="m", extra_kwargs={"base_url": "http://x"}), paths)
        is None
    )
    assert (
        stream_idle_timeout_seconds(ModelConfig(provider="openai", id="m", stream_idle_timeout_seconds=0), paths)
        is None
    )
    assert (
        stream_idle_timeout_seconds(ModelConfig(provider="groq", id="m", stream_idle_timeout_seconds=12), paths) == 12
    )


@pytest.mark.asyncio
async def test_composed_pipeline_drains_with_complete() -> None:
    inner = _ScriptedProvider([[_transient()], [TextDelta("done"), _turn()]])
    provider = wrap_provider(inner, idle_timeout_seconds=None, media_fallback_prompt="x", request_log=None)

    turn = await complete(provider, ModelRequest(messages=[_user()]))

    assert (turn.content, turn.stop_reason, len(inner.requests)) == ("done", "end", 2)
    assert provider.identity == inner.identity


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rejection",
    [
        InvalidRequest("This model does not support image input", provider="Scripted Chat", status_code=400),
        _malformed(),
    ],
    ids=["media-retry", "guidance-retry"],
)
async def test_reported_usage_always_sums_to_the_finished_turn(rejection: ProviderError) -> None:
    """Usage of transient retries inside a media or guidance retry is counted once, in reports and final metrics."""
    inner = _ScriptedProvider(
        [
            [UsageReported(MessageMetrics(input_tokens=5)), _transient()],
            [UsageReported(MessageMetrics(input_tokens=7)), rejection],
            [UsageReported(MessageMetrics(input_tokens=10)), _turn(usage=MessageMetrics(input_tokens=10))],
        ],
    )
    provider = wrap_provider(inner, idle_timeout_seconds=None, media_fallback_prompt="x", request_log=None)

    events = await _drain(provider, ModelRequest(messages=[_user(_PROMPT, images=[_image()])]))

    reported = sum(event.usage.input_tokens for event in events if isinstance(event, UsageReported))
    finished = events[-1]
    assert isinstance(finished, TurnCompleted)
    assert reported == finished.message.metrics.input_tokens == 22
