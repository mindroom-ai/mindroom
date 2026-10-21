"""The provider-neutral pipeline stages: retries, the idle limit, media fallback, tool search, and logging."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, cast

import pytest

from mindroom.config.models import DebugConfig, ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.llm.errors import InvalidRequest, ProviderError, ProviderUnavailable
from mindroom.llm.media import Audio, Image
from mindroom.llm.messages import Message
from mindroom.llm.provider import (
    Capabilities,
    ModelIdentity,
    ModelRequest,
    TextDelta,
    ToolCallReady,
    ToolSpec,
    TurnCompleted,
    UsageReported,
    complete,
)
from mindroom.llm.usage import MessageMetrics
from mindroom.media_delivery import VIEWED_IMAGE_ID_PREFIX
from mindroom.providers.pipeline import retry as retry_stage
from mindroom.providers.pipeline.compose import stream_idle_timeout_seconds, wrap_provider
from mindroom.providers.pipeline.deferred_tools import portable_tool_search_stream
from mindroom.providers.pipeline.media import FALLBACK_MARKER, forget_learned_media_limits, media_fallback_stream
from mindroom.providers.pipeline.request_log import RequestLog, logged_stream
from mindroom.providers.pipeline.retry import retrying_stream
from mindroom.providers.pipeline.wrapped import WrappedProvider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
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

    def format_tool_results(self, messages: Sequence[Message], results: Sequence[Message]) -> list[Message]:
        del messages
        return list(results)


def _turn(text: str = "done", usage: MessageMetrics | None = None) -> TurnCompleted:
    message = Message(role="assistant", content=text, provider_data={"mindroom_model": {"id": "scripted"}})
    if usage is not None:
        message.metrics = usage
    return TurnCompleted(message=message, stop_reason="end")


def _transient(message: str = "overloaded") -> ProviderUnavailable:
    return ProviderUnavailable(message, provider="Scripted Chat", status_code=503)


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


# --- Media fallback -------------------------------------------------------------------------------------------


@pytest.fixture
def _fresh_media_limits() -> AsyncIterator[None]:
    forget_learned_media_limits()
    yield
    forget_learned_media_limits()


def _image(image_id: str = "image-1") -> Image:
    return Image(id=image_id, content=b"\x89PNG\r\n\x1a\n0000", mime_type="image/png")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fresh_media_limits")
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
@pytest.mark.usefixtures("_fresh_media_limits")
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
@pytest.mark.usefixtures("_fresh_media_limits")
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
@pytest.mark.usefixtures("_fresh_media_limits")
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
@pytest.mark.usefixtures("_fresh_media_limits")
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


@pytest.mark.asyncio
async def test_portable_tool_search_renames_combined_gemini_results() -> None:
    combined = Message(
        role="tool",
        content=["shipped"],
        tool_calls=[{"tool_call_id": "call_1", "tool_name": "lookup_order", "content": "shipped"}],
    )
    inner = _ScriptedProvider([[_turn()]])
    request = ModelRequest(messages=[combined], deferred_tool_names=frozenset({"lookup_order"}))

    await _drain(WrappedProvider(inner, portable_tool_search_stream), request)

    assert inner.requests[0].messages[0].tool_calls == [
        {"tool_call_id": "call_1", "tool_name": "call_tool", "content": "shipped"},
    ]


# --- Request logging ------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_log_records_the_request_and_its_usage(tmp_path: Path) -> None:
    usage = MessageMetrics(input_tokens=42, output_tokens=12, total_tokens=54, cache_read_tokens=30)
    inner = _ScriptedProvider([[UsageReported(usage), _turn(usage=usage)]])
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
    assert response_record["request_log_id"] == request_record["request_log_id"]
    assert response_record["usage"] == {
        "input_tokens": 42,
        "output_tokens": 12,
        "cache_read_tokens": 30,
        "cache_write_tokens": 0,
    }


# --- Composition ----------------------------------------------------------------------------------------------


def test_stream_idle_limit_policy(tmp_path: Path) -> None:
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "data", process_env={})
    with_base_url = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={"OPENAI_BASE_URL": "http://localhost:4000/v1"},
    )
    hosted = ModelConfig(provider="openai", id="gpt-6-astra")
    assert stream_idle_timeout_seconds(hosted, paths) == 300.0
    assert stream_idle_timeout_seconds(hosted, with_base_url) is None
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
@pytest.mark.usefixtures("_fresh_media_limits")
async def test_composed_pipeline_drains_with_complete() -> None:
    inner = _ScriptedProvider([[_transient()], [TextDelta("done"), _turn()]])
    provider = wrap_provider(inner, idle_timeout_seconds=None, media_fallback_prompt="x", request_log=None)

    turn = await complete(provider, ModelRequest(messages=[_user()]))

    assert (turn.content, turn.stop_reason, len(inner.requests)) == ("done", "end", 2)
    assert provider.identity == inner.identity


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fresh_media_limits")
async def test_reported_usage_always_sums_to_the_finished_turn() -> None:
    """Usage of transient retries inside a media retry is counted once, in reports and in the final metrics."""
    rejection = InvalidRequest("This model does not support image input", provider="Scripted Chat", status_code=400)
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
