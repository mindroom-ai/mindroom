"""Model fallback chains: which failures fail over, in what order, and what the answer records."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, cast

import httpx2
import openai
import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from mindroom.agent_loop.model_loop import EMPTY_REQUEST
from mindroom.agent_loop.run import run_agent
from mindroom.agent_loop.spec import AgentSpec, RunContext, UnstoredSession
from mindroom.ai_run_metadata import build_ai_run_metadata_content
from mindroom.budgets.pricing import model_prices, run_cost_usd
from mindroom.config.budgets import BudgetsConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ModelPricing, ReasoningConfig
from mindroom.constants import AI_RUN_METADATA_KEY, resolve_runtime_paths
from mindroom.history.summarizer import SummaryModel, summarize_chunk
from mindroom.history.summary_call import SummaryRetryPolicy, generate_compaction_summary
from mindroom.llm.errors import (
    QUOTA_EXHAUSTED,
    TOKENS_PER_MINUTE,
    AuthenticationFailed,
    ContextWindowExceeded,
    InvalidRequest,
    ModelSafeguardRefusal,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
    fails_over,
)
from mindroom.llm.messages import FALLBACK_MODEL_KEY, REWRITTEN_ATTEMPT_KEY, Message, model_provenance
from mindroom.llm.provider import (
    Capabilities,
    DeferredTools,
    ModelIdentity,
    ModelRequest,
    ModelSelected,
    NativeCompactionReplay,
    TextDelta,
    ToolCallReady,
    ToolSpec,
    TurnCompleted,
    UsageReported,
)
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.native_compaction import NATIVE_CHECKPOINT_KEY
from mindroom.providers.anthropic.prompt_cache import prepare_request_body
from mindroom.providers.errors import classify_provider_error
from mindroom.providers.openai.chat_wire import ChatWire, chat_request_kwargs
from mindroom.providers.openai.errors import provider_error
from mindroom.providers.openai.response_replay import RESPONSE_OUTPUT_KEY
from mindroom.providers.openai.responses_wire import (
    ResponsesWire,
    conversation_digest,
    responses_request_kwargs,
)
from mindroom.providers.openai.settings import ChatSettings, ResponsesSettings
from mindroom.providers.pipeline import fallback as fallback_stage
from mindroom.providers.pipeline import retry as retry_stage
from mindroom.providers.pipeline.fallback import ChainModel, fallback_stream
from mindroom.providers.pipeline.wrapped import WrappedProvider
from mindroom.providers.registry import build_model_provider, fallback_chain
from tests.fakes.request_sizing import PlainRequestSizing
from tests.fakes.token_counter import fake_token_counter
from tests.providers.bare_adapter import drain, drain_into
from tests.providers.golden_support import pipeline_provider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import ModelProvider, StreamEvent, TokenCounter

type _Step = StreamEvent | BaseException | Callable[[], None]


@pytest.fixture(autouse=True)
def _no_cooldowns() -> Iterator[None]:
    fallback_stage._COOLDOWN_UNTIL.clear()
    yield
    fallback_stage._COOLDOWN_UNTIL.clear()


@dataclass
class _Model(PlainRequestSizing):
    """Answer each request with the next scripted attempt, its last attempt repeating.

    An exception step raises, and a callable step runs while the attempt streams.
    """

    name: str
    attempts: list[list[_Step]]
    identity: ModelIdentity = field(init=False)
    capabilities: Capabilities = field(default_factory=Capabilities)
    native_history_route: None = None
    token_counter: TokenCounter = field(default_factory=fake_token_counter)
    model_params: dict[str, object] = field(default_factory=dict)
    requests: list[ModelRequest] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.identity = ModelIdentity(id=f"{self.name}-id", provider=f"{self.name}-provider")

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        for step in self.attempts[min(len(self.requests), len(self.attempts)) - 1]:
            if isinstance(step, BaseException):
                raise step
            if callable(step):
                step()
                continue
            yield step


def _answer(model: str, text: str = "answer") -> list[_Step]:
    message = Message(
        role="assistant",
        content=text,
        provider_data=model_provenance(f"{model}-id", f"{model}-provider"),
    )
    return [TextDelta(text), TurnCompleted(message=message, stop_reason="end")]


def _quota(*, reset_in: float | None = None) -> ProviderError:
    return ProviderError(
        "The usage limit has been reached",
        provider="Codex",
        status_code=429,
        code=QUOTA_EXHAUSTED,
        retry_after_seconds=reset_in,
    )


def _chain(*models: _Model, context_window: int | None = None) -> list[ChainModel]:
    return [ChainModel(model.name, context_window, partial(lambda found: found, model), model.name) for model in models]


def _with_fallbacks(primary: _Model, *fallbacks: _Model) -> WrappedProvider:
    return WrappedProvider(
        primary,
        partial(fallback_stream, model_name=primary.name, chain=_chain(*fallbacks), cooldown_key=primary.name),
    )


def _request(**fields: object) -> ModelRequest:
    return ModelRequest(messages=(Message(role="user", content="hello"),), **fields)


def _text(events: list[StreamEvent]) -> str:
    return "".join(event.text for event in events if isinstance(event, TextDelta))


async def _persisted_run(
    model: ModelProvider,
    *,
    run_id: str = "run",
    session_id: str = "session",
    raises: type[BaseException] | None = None,
) -> RunRecord:
    saved: list[RunRecord] = []

    async def persist(record: RunRecord) -> None:
        saved.append(record)

    spec = AgentSpec(id="code", name="Code", model=model, system_messages=(), toolkits=())
    stream = run_agent(
        spec,
        [Message(role="user", content="hello")],
        ctx=RunContext(run_id=run_id, session=UnstoredSession(session_id)),
        persist=persist,
        request_template=EMPTY_REQUEST,
    )
    if raises is not None:
        with pytest.raises(raises):
            async for _ in stream:
                pass
    else:
        async for _ in stream:
            pass
    [record] = saved
    return record


@pytest.mark.asyncio
async def test_a_quota_failure_fails_over_to_the_chain_in_order_and_logs_it() -> None:
    primary = _Model("merlin", [[_quota()]])
    codex = _Model("codex", [[ProviderUnavailable("down", provider="Codex", status_code=503)]])
    bedrock = _Model("bedrock", [_answer("bedrock")])

    with capture_logs() as logs:
        events = await drain(_with_fallbacks(primary, codex, bedrock), _request())

    assert _text(events) == "answer"
    assert [len(model.requests) for model in (primary, codex, bedrock)] == [1, 1, 1]
    used = [
        (log["from_model"], log["to_model"], log["error_type"]) for log in logs if log["event"] == "model_fallback_used"
    ]
    assert used == [("merlin", "codex", "ProviderError"), ("codex", "bedrock", "ProviderUnavailable")]
    assert all(log["log_level"] == "warning" for log in logs if log["event"] == "model_fallback_used")


@pytest.mark.parametrize(
    "error",
    [
        InvalidRequest("bad body", provider="P", status_code=400),
        ModelSafeguardRefusal(provider="P", usage=MessageMetrics()),
        ContextWindowExceeded("too long", provider="P", status_code=400),
        ProviderError("forbidden", provider="P", status_code=403),
    ],
    ids=["invalid-request", "refusal", "context-window", "unknown"],
)
@pytest.mark.asyncio
async def test_a_failure_any_model_would_repeat_never_fails_over(error: ProviderError) -> None:
    fallback = _Model("fallback", [_answer("fallback")])

    with pytest.raises(type(error)):
        await drain(_with_fallbacks(_Model("primary", [[error]]), fallback), _request())

    assert fallback.requests == []


@pytest.mark.parametrize(
    "error",
    [
        RateLimited("slow down", provider="P", status_code=429),
        ProviderUnavailable("overloaded", provider="P", status_code=529),
        AuthenticationFailed("not signed in", provider="P", status_code=401),
        ContextWindowExceeded("Request too large", provider="P", status_code=429, code=TOKENS_PER_MINUTE),
        ProviderError("Overloaded (Claude Code already retried the request.)", provider="P", status_code=529),
        ProviderError("conflict", provider="P", status_code=409),
        ProviderError(
            "currently overloaded (Claude Code already retried the request.)",
            provider="P",
            already_retried=True,
        ),
    ],
    ids=[
        "rate-limit",
        "unavailable",
        "auth",
        "tokens-per-minute",
        "route-retried",
        "conflict-409",
        "already-retried-statusless",
    ],
)
def test_a_failure_of_the_provider_alone_fails_over(error: ProviderError) -> None:
    assert fails_over(error)


@pytest.mark.asyncio
async def test_a_cancelled_request_never_fails_over() -> None:
    fallback = _Model("fallback", [_answer("fallback")])

    with pytest.raises(asyncio.CancelledError):
        await drain(_with_fallbacks(_Model("primary", [[asyncio.CancelledError()]]), fallback), _request())

    assert fallback.requests == []


@pytest.mark.parametrize(
    "output",
    [
        TextDelta("partial"),
        ToolCallReady({"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}),
    ],
    ids=["text", "tool-call"],
)
@pytest.mark.asyncio
async def test_no_failover_after_the_caller_received_output(output: StreamEvent) -> None:
    fallback = _Model("fallback", [_answer("fallback")])
    events: list[StreamEvent] = []

    with pytest.raises(ProviderUnavailable):
        await drain_into(events, _with_fallbacks(_Model("primary", [[output, _transient()]]), fallback), _request())

    assert [event for event in events if not isinstance(event, ModelSelected)] == [output]
    assert fallback.requests == []


def _transient() -> ProviderUnavailable:
    return ProviderUnavailable("connection reset", provider="P")


@pytest.mark.asyncio
async def test_an_exhausted_chain_surfaces_the_last_failure() -> None:
    last = RateLimited("still limited", provider="Bedrock", status_code=429)
    provider = _with_fallbacks(
        _Model("primary", [[_quota()]]),
        _Model("codex", [[_transient()]]),
        _Model("b", [[last]]),
    )

    with pytest.raises(RateLimited) as raised:
        await drain(provider, _request())

    assert raised.value is last


@pytest.mark.asyncio
async def test_a_fallback_is_sent_the_request_without_the_first_routes_replay_state() -> None:
    fallback = _Model("fallback", [_answer("fallback")])
    native = NativeCompactionReplay(route="primary-route", threshold=1000)

    events = await drain(_with_fallbacks(_Model("primary", [[_quota()]]), fallback), _request(native_compaction=native))

    [sent] = fallback.requests
    assert sent.native_compaction is None
    assert (sent.portable_replay, sent.strip_encrypted_replay, sent.prompt_hold) == (True, True, None)
    turn = events[-1]
    assert isinstance(turn, TurnCompleted)
    assert (turn.message.provider_data or {})[REWRITTEN_ATTEMPT_KEY] is True


@pytest.mark.asyncio
async def test_failed_primary_and_answering_fallback_keep_separate_usage_and_cost() -> None:
    config = Config(
        models={
            "paid": ModelConfig(
                provider="anthropic",
                id="claude-opus-5-5",
                pricing=ModelPricing(input=5, output=25),
                fallbacks=["cheap"],
            ),
            "cheap": ModelConfig(
                provider="openai",
                id="gpt-6-mini",
                pricing=ModelPricing(input=1, output=2),
            ),
        },
    )
    primary_usage = MessageMetrics(tokens=TokenCounts(input_tokens=1_000_000, total_tokens=1_000_000))
    fallback_usage = MessageMetrics(tokens=TokenCounts(input_tokens=500_000, total_tokens=500_000))
    primary = _Model("paid", [[UsageReported(primary_usage, final=False), _quota()]])
    primary.identity = ModelIdentity(id="claude-opus-5-5", provider="Anthropic")
    answer = Message(
        role="assistant",
        content="hi",
        provider_data=model_provenance("gpt-6-mini", "OpenAI"),
        metrics=fallback_usage,
    )
    fallback = _Model(
        "cheap",
        [
            [
                UsageReported(fallback_usage, final=True),
                TextDelta("hi"),
                TurnCompleted(message=answer, stop_reason="end"),
            ],
        ],
    )
    fallback.identity = ModelIdentity(id="gpt-6-mini", provider="OpenAI")

    record = await _persisted_run(_with_fallbacks(primary, fallback))
    assert (record.model_provider, record.model, record.model_name) == ("OpenAI", "gpt-6-mini", "cheap")
    assert record.metrics is not None
    assert record.metrics.tokens.input_tokens == 1_500_000
    assert [
        (entry.provider, entry.id, entry.tokens.input_tokens) for entry in (record.metrics.details or {})["model"]
    ] == [
        ("Anthropic", "claude-opus-5-5", 1_000_000),
        ("OpenAI", "gpt-6-mini", 500_000),
    ]
    # Primary billed $5 (1M * $5/M) + fallback billed $0.50 (500k * $1/M) = $5.50.
    assert run_cost_usd(
        record.metrics,
        model_provider=record.model_provider,
        model_id=record.model,
        prices=model_prices(config),
    ) == pytest.approx(5.5)


@pytest.mark.asyncio
async def test_a_model_is_tried_last_until_the_reset_its_failure_named() -> None:
    primary = _Model("codex", [[_quota(reset_in=3600)], _answer("codex")])
    fallback = _Model("bedrock", [_answer("bedrock")])
    provider = _with_fallbacks(primary, fallback)

    await drain(provider, _request())
    with capture_logs() as logs:
        await drain(provider, _request())

    assert (len(primary.requests), len(fallback.requests)) == (1, 2)
    assert not [log for log in logs if log["event"] == "model_fallback_used"]
    fallback_stage._COOLDOWN_UNTIL["codex"] = time.monotonic() - 1
    events = await drain(provider, _request())
    assert len(primary.requests) == 2
    turn = events[-1]
    assert isinstance(turn, TurnCompleted)
    assert REWRITTEN_ATTEMPT_KEY not in (turn.message.provider_data or {})
    assert "codex" not in fallback_stage._COOLDOWN_UNTIL


@pytest.mark.asyncio
async def test_a_failure_without_a_reset_is_probed_again_on_the_next_request() -> None:
    primary = _Model("codex", [[_quota()]])
    provider = _with_fallbacks(primary, _Model("bedrock", [_answer("bedrock")]))

    await drain(provider, _request())
    await drain(provider, _request())

    assert len(primary.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ProviderUnavailable("upstream 500", provider="P", status_code=500),
        ProviderUnavailable("stream stalled", provider="P", status_code=504, code=retry_stage.STREAM_STALLED),
    ],
    ids=["http-500", "stream-stalled"],
)
async def test_uncommitted_unavailable_cools_down_primary_and_recovers_after_expiry(
    error: ProviderUnavailable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    monkeypatch.setattr(fallback_stage.time, "monotonic", lambda: now[0])
    primary = _Model("primary", [[error], _answer("primary")])
    fallback = _Model("fallback", [_answer("fallback")])
    provider = _with_fallbacks(primary, fallback)

    await drain(provider, _request())
    assert (len(primary.requests), len(fallback.requests)) == (1, 1)
    assert fallback_stage._COOLDOWN_UNTIL["primary"] == 1000.0 + fallback_stage._UNAVAILABLE_COOLDOWN_SECONDS

    # During cooldown, the next request skips primary and goes straight to fallback.
    now[0] = 1060.0
    await drain(provider, _request())
    assert (len(primary.requests), len(fallback.requests)) == (1, 2)

    # After cooldown expires, primary is probed again and clears cooldown on success.
    now[0] = 1120.0
    events = await drain(provider, _request())
    assert (len(primary.requests), len(fallback.requests)) == (2, 2)
    assert _text(events) == "answer"
    assert "primary" not in fallback_stage._COOLDOWN_UNTIL


@pytest.mark.asyncio
async def test_committed_unavailable_without_reset_does_not_cool_down_primary() -> None:
    primary = _Model(
        "primary",
        [
            [TextDelta("partial"), ProviderUnavailable("dropped mid-stream", provider="P", status_code=503)],
            _answer("primary"),
        ],
    )
    fallback = _Model("fallback", [_answer("fallback")])
    provider = _with_fallbacks(primary, fallback)

    with pytest.raises(ProviderUnavailable):
        await drain(provider, _request())
    assert "primary" not in fallback_stage._COOLDOWN_UNTIL
    assert fallback.requests == []

    await drain(provider, _request())
    assert (len(primary.requests), len(fallback.requests)) == (2, 0)


@pytest.mark.asyncio
async def test_unavailable_explicit_reset_hint_wins_and_default_never_shortens_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [500.0]
    monkeypatch.setattr(fallback_stage.time, "monotonic", lambda: now[0])
    primary = _Model(
        "primary",
        [[ProviderUnavailable("overloaded", provider="P", status_code=503, retry_after_seconds=600)]],
    )
    provider = _with_fallbacks(primary, _Model("fallback", [_answer("fallback")]))

    await drain(provider, _request())
    assert fallback_stage._COOLDOWN_UNTIL["primary"] == 1100.0

    # A subsequent default cooldown must not shorten the existing longer deadline.
    fallback_stage._remember_reset("primary", ProviderUnavailable("down", provider="P", status_code=500))
    assert fallback_stage._COOLDOWN_UNTIL["primary"] == 1100.0

    # An explicit short hint is respected directly rather than replaced by the default window.
    fallback_stage._remember_reset(
        "short",
        ProviderUnavailable("overloaded", provider="P", status_code=503, retry_after_seconds=15),
    )
    assert fallback_stage._COOLDOWN_UNTIL["short"] == 515.0


@pytest.mark.asyncio
async def test_rate_limited_without_reset_is_probed_again_on_the_next_request() -> None:
    primary = _Model("primary", [[RateLimited("slow down", provider="P", status_code=429)]])
    fallback = _Model("fallback", [_answer("fallback")])
    provider = _with_fallbacks(primary, fallback)

    await drain(provider, _request())
    await drain(provider, _request())

    assert (len(primary.requests), len(fallback.requests)) == (2, 2)
    assert "primary" not in fallback_stage._COOLDOWN_UNTIL


@pytest.mark.asyncio
async def test_every_model_cooling_down_is_still_tried_in_order() -> None:
    primary = _Model("codex", [[_quota(reset_in=3600)], _answer("codex")])
    fallback = _Model("bedrock", [[_quota(reset_in=3600)]])
    provider = _with_fallbacks(primary, fallback)

    with pytest.raises(ProviderError):
        await drain(provider, _request())
    events = await drain(provider, _request())

    assert _text(events) == "answer"
    assert (len(primary.requests), len(fallback.requests)) == (2, 1)


@pytest.mark.asyncio
async def test_a_fallback_whose_context_window_the_request_exceeds_is_skipped() -> None:
    small = _Model("small", [_answer("small")])
    large = _Model("large", [_answer("large")])
    chain = [*_chain(small, context_window=10), *_chain(large)]
    provider = WrappedProvider(
        _Model("primary", [[_quota()]]),
        partial(fallback_stream, model_name="primary", chain=chain, cooldown_key="primary"),
    )

    with capture_logs() as logs:
        await drain(provider, ModelRequest(messages=(Message(role="user", content="long " * 100),)))

    assert (len(small.requests), len(large.requests)) == (0, 1)
    assert [log["model"] for log in logs if log["event"] == "model_fallback_skipped"] == ["small"]


@pytest.mark.asyncio
async def test_compaction_summary_fails_over_on_quota_but_defers_transient_retries() -> None:
    billed = MessageMetrics(tokens=TokenCounts(input_tokens=40, output_tokens=10, total_tokens=50))
    answer = Message(
        role="assistant",
        content="Compact summary.",
        provider_data=model_provenance("fallback-id", "fallback-provider"),
        metrics=billed,
    )
    fallback = _Model("fallback", [[TextDelta("Compact summary."), TurnCompleted(message=answer, stop_reason="end")]])
    recorded: list[tuple[ModelIdentity, MessageMetrics]] = []

    async def on_usage(identity: ModelIdentity, usage: MessageMetrics) -> None:
        recorded.append((identity, usage))

    summary = await generate_compaction_summary(
        model=_with_fallbacks(_Model("primary", [[_quota()]]), fallback),
        summary_input="Conversation.",
        summary_prompt="Summarize.",
        timeout_seconds=10,
        on_usage=on_usage,
    )

    assert summary == "Compact summary."
    assert [(identity.id, identity.provider, usage.tokens.total_tokens) for identity, usage in recorded] == [
        ("fallback-id", "fallback-provider", 50),
    ]

    # Retryable transient errors stay with compaction's own retry policy instead of failing over immediately.
    transient_fallback = _Model("fallback", [_answer("fallback")])
    with pytest.raises(ProviderUnavailable):
        await generate_compaction_summary(
            model=_with_fallbacks(_Model("primary", [[_transient()]]), transient_fallback),
            summary_input="Conversation.",
            summary_prompt="Summarize.",
            timeout_seconds=10,
            on_usage=None,
        )
    assert transient_fallback.requests == []


@pytest.mark.asyncio
async def test_the_run_records_and_publishes_the_model_that_answered() -> None:
    config = Config(
        models={
            "merlin": ModelConfig(provider="openai", id="merlin-id", fallbacks=["bedrock"]),
            "bedrock": ModelConfig(provider="anthropic", id="bedrock-id", display_name="Bedrock", context_window=9000),
        },
    )
    answer = Message(role="assistant", content="hi", provider_data=model_provenance("bedrock-id", "Anthropic"))
    answer.metrics = MessageMetrics(tokens=TokenCounts(input_tokens=3, total_tokens=3))
    fallback = _Model("bedrock", [[TextDelta("hi"), TurnCompleted(message=answer, stop_reason="end")]])

    record = await _persisted_run(_with_fallbacks(_Model("merlin", [[_quota()]]), fallback))
    assert (record.model, record.model_provider, record.model_name) == ("bedrock-id", "Anthropic", "bedrock")
    assert [(entry.id, entry.provider) for entry in (record.metrics.details or {})["model"]] == [
        ("bedrock-id", "Anthropic"),
    ]
    metadata = build_ai_run_metadata_content(
        config=config,
        model_name="merlin",
        run_id="run",
        session_id="session",
        status=RunStatus.completed,
        model=record.model,
        model_provider=record.model_provider,
        answering_model_name=record.model_name,
        prices={},
        tool_count=0,
    )[AI_RUN_METADATA_KEY]
    assert metadata["model"] == {
        "config": "bedrock",
        "display_name": "Bedrock",
        "id": "bedrock-id",
        "provider": "Anthropic",
        "fallback_from": "merlin",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("with_usage", [True, False], ids=["with-usage", "without-usage"])
async def test_a_fallback_cut_off_after_output_records_the_fallback(*, with_usage: bool) -> None:
    billed = MessageMetrics(tokens=TokenCounts(input_tokens=100, total_tokens=100))
    steps: list[_Step] = [TextDelta("partial"), _transient()]
    if with_usage:
        steps.insert(0, UsageReported(billed, final=False))
    fallback = _Model("fallback", [steps])

    record = await _persisted_run(
        _with_fallbacks(_Model("primary", [[_quota()]]), fallback),
        raises=ProviderUnavailable,
    )

    assert (record.model, record.model_provider, record.model_name) == ("fallback-id", "fallback-provider", "fallback")
    cut_off = record.messages[-1]
    assert (cut_off.provider_data or {})["mindroom_model"] == {"id": "fallback-id", "provider": "fallback-provider"}
    expected_usage = [("fallback-id", "fallback-provider", 100)] if with_usage else []
    assert [
        (entry.id, entry.provider, entry.tokens.input_tokens)
        for entry in (record.metrics.details or {}).get("model", [])
    ] == expected_usage


def _broken_build() -> _Model:
    msg = "the claude_code extra is not installed"
    raise RuntimeError(msg)


@pytest.mark.asyncio
async def test_a_fallback_that_cannot_be_built_is_skipped_and_the_real_failure_stays() -> None:
    good = _Model("good", [_answer("good")])
    chain = [ChainModel("broken", None, _broken_build, "broken"), *_chain(good)]
    provider = WrappedProvider(
        _Model("primary", [[_quota()]]),
        partial(fallback_stream, model_name="primary", chain=chain, cooldown_key="primary"),
    )

    with capture_logs() as logs:
        events = await drain(provider, _request())

    assert _text(events) == "answer"
    assert len(good.requests) == 1
    [skipped] = [log for log in logs if log["event"] == "model_fallback_skipped"]
    assert (skipped["model"], skipped["reason"], skipped["error_type"]) == ("broken", "unavailable", "RuntimeError")

    only_broken = WrappedProvider(
        _Model("primary", [[_quota()]]),
        partial(
            fallback_stream,
            model_name="primary",
            chain=[ChainModel("broken", None, _broken_build, "broken")],
            cooldown_key="primary",
        ),
    )
    with pytest.raises(ProviderError, match="usage limit"):
        await drain(only_broken, _request())


@pytest.mark.asyncio
async def test_a_cooling_primary_does_not_override_a_fallbacks_refusal() -> None:
    primary = _Model("primary", [[_quota(reset_in=3600)], _answer("primary", "must not run")])
    first_fallback = _Model(
        "fb1",
        [_answer("fb1", "first ok"), [ModelSafeguardRefusal(provider="F1", usage=MessageMetrics())]],
    )
    second_fallback = _Model("fb2", [_answer("fb2", "must not run")])
    provider = _with_fallbacks(primary, first_fallback, second_fallback)

    await drain(provider, _request())
    with pytest.raises(ModelSafeguardRefusal):
        await drain(provider, _request())

    assert (len(primary.requests), len(first_fallback.requests), len(second_fallback.requests)) == (1, 2, 0)


@pytest.mark.asyncio
async def test_a_success_keeps_a_cooldown_a_concurrent_request_set_meanwhile() -> None:
    def concurrent_failure() -> None:
        fallback_stage._COOLDOWN_UNTIL["primary"] = time.monotonic() + 3600

    primary = _Model("primary", [[concurrent_failure, *_answer("primary")]])

    await drain(_with_fallbacks(primary, _Model("fallback", [_answer("fallback")])), _request())

    assert "primary" in fallback_stage._COOLDOWN_UNTIL


@pytest.mark.asyncio
async def test_a_rate_limit_fails_over_only_once_the_retry_stage_gave_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry_stage, "_retry_delay_seconds", lambda _attempt: 0.0)
    adapter = _Model("primary", [[RateLimited("slow down", provider="P", status_code=429)]])
    fallback = _Model("fallback", [_answer("fallback")])
    chain = partial(fallback_stream, model_name="primary", chain=_chain(fallback), cooldown_key="primary")

    events = await drain(WrappedProvider(pipeline_provider(adapter), chain), _request())

    assert _text(events) == "answer"
    # One request and the retry stage's four reissues, then the fallback.
    assert (len(adapter.requests), len(fallback.requests)) == (5, 1)


# --- The chain a configured model gets ---------------------------------------------------------------------------


@pytest.fixture
def runtime_paths(tmp_path: Path) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    return resolve_runtime_paths(config_path=config_path, storage_path=tmp_path / "data", process_env={})


def _synthetic(*, fallbacks: list[str] | None = None, levels: list[str] | None = None) -> ModelConfig:
    reasoning = {"levels": levels, "default": levels[0]} if levels else None
    return ModelConfig.model_validate(
        {"provider": "synthetic", "id": "lorem", "fallbacks": fallbacks or [], "reasoning": reasoning},
    )


def test_a_fallback_brings_its_own_chain_right_after_it(runtime_paths: RuntimePaths) -> None:
    config = Config(
        models={
            "merlin": _synthetic(fallbacks=["codex", "bedrock"]),
            "codex": _synthetic(fallbacks=["kimi", "bedrock"]),
            "kimi": _synthetic(),
            "bedrock": _synthetic(),
        },
    )

    assert [model.name for model in fallback_chain(config, runtime_paths, "merlin")] == ["codex", "kimi", "bedrock"]
    assert fallback_chain(config, runtime_paths, "kimi") == ()


def test_only_a_model_with_fallbacks_gets_the_fallback_stage(runtime_paths: RuntimePaths) -> None:
    config = Config(models={"merlin": _synthetic(fallbacks=["codex"]), "codex": _synthetic()})

    chained = build_model_provider(config, runtime_paths, "merlin")
    plain = build_model_provider(config, runtime_paths, "codex")

    assert isinstance(chained, WrappedProvider)
    assert cast("partial[object]", chained.stage).func is fallback_stream
    assert not (isinstance(plain, WrappedProvider) and getattr(plain.stage, "func", None) is fallback_stream)
    [codex] = fallback_chain(config, runtime_paths, "merlin")
    assert codex.build() is codex.build()


def test_a_fallback_keeps_the_reasoning_level_only_when_it_offers_it(
    runtime_paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[str] = []
    monkeypatch.setattr(
        "mindroom.providers.registry._build_single_provider",
        lambda _config, _paths, reference, **_kwargs: built.append(reference),
    )
    config = Config(
        models={
            "merlin": _synthetic(fallbacks=["codex", "bedrock"], levels=["medium", "high"]),
            "codex": _synthetic(levels=["low", "high"]),
            "bedrock": _synthetic(levels=["low"]),
        },
    )

    for model in fallback_chain(config, runtime_paths, "merlin@high"):
        model.build()

    assert built == ["codex@high", "bedrock"]


@pytest.mark.parametrize(
    ("models", "message"),
    [
        ({"a": {"fallbacks": ["missing"]}}, "models.a.fallbacks: unknown model 'missing'"),
        ({"a": {"fallbacks": ["a"]}}, "models.a.fallbacks must not name the model itself"),
        ({"a": {"fallbacks": ["b", "b"]}, "b": {}}, "models.a.fallbacks must not repeat a model"),
        ({"a": {"fallbacks": ["b"]}, "b": {"fallbacks": ["c"]}, "c": {"fallbacks": ["a"]}}, "a -> b -> c -> a"),
        ({"a": {"fallbacks": ["b@high"]}, "b": {"reasoning": {"levels": ["high"], "default": "high"}}}, "unknown"),
    ],
    ids=["unknown", "self", "repeated", "cycle", "level-reference"],
)
def test_invalid_fallbacks_fail_config_load(models: dict[str, dict[str, object]], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Config(models={name: {"provider": "synthetic", "id": "lorem", **fields} for name, fields in models.items()})


# --- Codex's usage limit ------------------------------------------------------------------------------------------


def _codex_429(body: dict[str, object]) -> openai.OpenAIError:
    request = httpx2.Request("POST", "https://chatgpt.com/backend-api/codex/responses")
    return openai.RateLimitError("Error code: 429", response=httpx2.Response(429, request=request), body=body)


def _codex_stream_error(body: dict[str, object]) -> openai.OpenAIError:
    request = httpx2.Request("POST", "https://chatgpt.com/backend-api/codex/responses")
    return openai.APIError("The usage limit has been reached", request=request, body=body)


@pytest.mark.parametrize(
    ("make_error", "reset", "expected_seconds"),
    [
        (_codex_429, {"resets_in_seconds": 7200}, 7200),
        (_codex_429, {"resets_at": "now+7200"}, 7200),
        (_codex_stream_error, {"resets_in_seconds": 1800}, 1800),
    ],
    ids=["resets-in", "resets-at", "stream-api-error"],
)
def test_codex_usage_limit_is_quota_exhausted_until_its_reset(
    make_error: Callable[[dict[str, object]], openai.OpenAIError],
    reset: dict[str, object],
    expected_seconds: int,
) -> None:
    if reset.get("resets_at") == "now+7200":
        reset = {"resets_at": int(time.time()) + 7200}
    body = {"type": "usage_limit_reached", "message": "The usage limit has been reached", **reset}

    error = provider_error(make_error(body), provider="Codex")

    assert type(error) is ProviderError
    assert error.code == QUOTA_EXHAUSTED
    assert not error.retryable
    assert error.retry_after_seconds == pytest.approx(expected_seconds, abs=5)
    assert fails_over(error)


def test_codex_usage_limit_wording_alone_is_quota_exhausted() -> None:
    error = classify_provider_error("The usage limit has been reached", provider="Codex", status_code=429)

    assert error.code == QUOTA_EXHAUSTED


@pytest.mark.asyncio
async def test_native_deferred_tools_are_stripped_for_non_native_bedrock_fallback() -> None:
    """A Bedrock Claude fallback never receives native tool_search or defer_loading, while keeping the tool callable."""
    primary = _Model("opus", [[_quota()]], capabilities=Capabilities(native_tool_search=True))
    fallback = _Model("bedrock-opus", [_answer("bedrock-opus")], capabilities=Capabilities(native_tool_search=False))
    tool = ToolSpec(
        name="web_search",
        description="Search the web",
        parameters={"type": "object", "properties": {}},
        strict=False,
    )
    request = _request(tools=(tool,), deferred_tools=DeferredTools(names=frozenset({"web_search"}), portable=False))

    await drain(_with_fallbacks(primary, fallback), request)

    [sent] = fallback.requests
    assert sent.deferred_tools == DeferredTools()
    assert [spec.name for spec in sent.tools] == ["web_search"]
    wire_body = prepare_request_body(
        {
            "model": "anthropic.claude-opus-5-5",
            "max_tokens": 256,
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"name": "web_search", "description": "Search the web", "input_schema": tool.parameters}],
        },
        system=(),
        route="bedrock_claude",
        deferred_tool_names=sent.deferred_tools.names,
        tool_choice=None,
        extended_cache_time=False,
        cache_system_prompt=False,
    )
    tool_types = [item.get("type") for item in wire_body["tools"]]
    assert not any(tool_type and "tool_search" in tool_type for tool_type in tool_types)
    assert all("defer_loading" not in item for item in wire_body["tools"])
    assert [item["name"] for item in wire_body["tools"]] == ["web_search"]


@pytest.mark.asyncio
async def test_portable_replay_works_when_primary_recovers_after_fallback_tool_call() -> None:
    """When fallback B stores a Responses tool-call turn and primary A recovers, A keeps its own checkpoint but never reuses B's state."""
    native = NativeCompactionReplay(route="openai-a", threshold=50_000)
    older_user = Message(role="user", content="earlier turn before checkpoint")
    primary_checkpoint = Message(
        role="assistant",
        content="earlier answer",
        provider_data={
            **model_provenance("gpt-6", "OpenAI"),
            NATIVE_CHECKPOINT_KEY: {
                "route": native.route,
                "threshold": native.threshold,
                "items": [{"type": "compaction", "id": "cmp_a", "encrypted_content": "checkpoint-from-a"}],
            },
        },
    )
    user = Message(role="user", content="check weather")
    call = {
        "id": "fc_b",
        "call_id": "call_b",
        "type": "function",
        "function": {"name": "weather", "arguments": '{"city": "NYC"}'},
    }
    fallback_msg = Message(
        role="assistant",
        tool_calls=[call],
        provider_data={
            **model_provenance("gpt-6", "OpenAI"),
            "response_id": "resp_project_b",
            "mindroom_response_stored": True,
            "mindroom_conversation_sha256": conversation_digest([older_user, primary_checkpoint, user]),
            RESPONSE_OUTPUT_KEY: [
                {"type": "reasoning", "id": "rs_b", "encrypted_content": "cipher-from-b", "summary": []},
                {
                    "type": "function_call",
                    "id": "fc_b",
                    "call_id": "call_b",
                    "name": "weather",
                    "arguments": '{"city": "NYC"}',
                },
            ],
        },
    )
    primary = _Model("project-a", [[_transient()], _answer("project-a", "Sunny.")])
    fallback = _Model("project-b", [[TurnCompleted(message=fallback_msg, stop_reason="tool_use")]])
    provider = _with_fallbacks(primary, fallback)

    first_events = await drain(
        provider,
        ModelRequest(messages=(older_user, primary_checkpoint, user), native_compaction=native),
    )
    first_turn = first_events[-1]
    assert isinstance(first_turn, TurnCompleted)
    assert (first_turn.message.provider_data or {})[FALLBACK_MODEL_KEY] == "project-b"

    # Expire primary A's unavailability cooldown so the follow-up turn probes A again.
    fallback_stage._COOLDOWN_UNTIL["project-a"] = time.monotonic() - 1
    tool_result = Message(role="tool", tool_call_id="call_b", content="72F sunny")
    await drain(
        provider,
        ModelRequest(
            messages=(older_user, primary_checkpoint, user, first_turn.message, tool_result),
            native_compaction=native,
        ),
    )

    second_request = primary.requests[1]
    assert second_request.portable_replay is True
    assert second_request.native_compaction == native
    wire = ResponsesWire(route="openai", store=True, explicit_breakpoint=False, default_instructions=None)
    body = responses_request_kwargs(
        model_id="gpt-6",
        request=second_request,
        settings=ResponsesSettings(),
        wire=wire,
        provider="OpenAI",
    )
    assert "previous_response_id" not in body
    assert body["context_management"] == [{"type": "compaction", "compact_threshold": 50_000}]
    assert any(
        item.get("type") == "compaction" and item.get("encrypted_content") == "checkpoint-from-a"
        for item in body["input"]
        if isinstance(item, dict)
    )
    assert not any(item.get("encrypted_content") == "cipher-from-b" for item in body["input"] if isinstance(item, dict))
    assert any(item.get("type") == "function_call" and item.get("call_id") == "call_b" for item in body["input"])
    assert any(item.get("type") == "function_call_output" and item.get("call_id") == "call_b" for item in body["input"])


@pytest.mark.asyncio
async def test_openrouter_chat_fallback_strips_encrypted_and_signed_reasoning_details() -> None:
    """OpenRouter Chat strips foreign signed/encrypted reasoning while preserving each candidate's own."""

    def _or_turn(tag: str, call_id: str) -> tuple[list[dict[str, object]], Message]:
        details: list[dict[str, object]] = [
            {"type": "reasoning.encrypted", "format": "openai-responses-v1", "index": 0, "data": f"sealed-{tag}"},
            {
                "type": "reasoning.text",
                "format": "anthropic-claude-v1",
                "index": 1,
                "text": f"Signed {tag.upper()}",
                "signature": f"sig-{tag}",
            },
            {
                "type": "reasoning.summary",
                "format": "openai-responses-v1",
                "index": 2,
                "summary": f"Summary {tag.upper()}",
            },
        ]
        msg = Message(
            role="assistant",
            content=f"Checking {tag.upper()}.",
            tool_calls=[{"id": call_id, "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
            provider_data={"reasoning_details": details},
        )
        return details, msg

    def _or_chat_kwargs(sent: ModelRequest) -> dict[str, object]:
        return chat_request_kwargs(
            model_id="openai/gpt-6",
            messages=sent.messages,
            tools=(),
            tool_choice=None,
            output_schema=None,
            structured_output="native",
            settings=ChatSettings(),
            wire=ChatWire(route="openrouter", system_role="system"),
            provider="OpenRouter",
            fallback_model=sent.fallback_model,
        )

    a_details, a_turn = _or_turn("a", "call_1")
    b_details, b_tool_turn = _or_turn("b", "call_2")
    primary = _Model("or-a", [[_transient()], _answer("or-a", "recovered")])
    fallback = _Model("or-b", [[TurnCompleted(message=b_tool_turn, stop_reason="tool_use")], _answer("or-b", "done")])
    provider = _with_fallbacks(primary, fallback)

    # Turn 1: primary fails -> fallback B answers with its own signed reasoning (stamped FALLBACK_MODEL_KEY="or-b").
    t1_messages = (
        Message(role="user", content="hi"),
        a_turn,
        Message(role="tool", tool_call_id="call_1", content="r1"),
    )
    t1_events = await drain(provider, ModelRequest(messages=t1_messages))
    b_saved = t1_events[-1]
    assert isinstance(b_saved, TurnCompleted)
    assert (b_saved.message.provider_data or {})[FALLBACK_MODEL_KEY] == "or-b"

    # Turn 2a: fallback B continues its own tool call -> strips A's foreign signed reasoning, keeps B's own.
    t2_messages = (*t1_messages, b_saved.message, Message(role="tool", tool_call_id="call_2", content="r2"))
    await drain(provider, ModelRequest(messages=t2_messages))
    sent_to_b = fallback.requests[1]
    assert (sent_to_b.strip_encrypted_replay, sent_to_b.fallback_model) == (True, "or-b")
    b_messages = cast("list[dict[str, object]]", _or_chat_kwargs(sent_to_b)["messages"])
    assert b_messages[1]["reasoning_details"] == [a_details[2]]
    assert b_messages[3]["reasoning_details"] == b_details
    assert [call["id"] for call in cast("list[dict[str, object]]", b_messages[1]["tool_calls"])] == ["call_1"]
    assert [call["id"] for call in cast("list[dict[str, object]]", b_messages[3]["tool_calls"])] == ["call_2"]

    # Turn 2b: primary A recovers after cooldown -> keeps A's own signed reasoning, strips B's foreign signed reasoning.
    fallback_stage._COOLDOWN_UNTIL["or-a"] = time.monotonic() - 1
    await drain(provider, ModelRequest(messages=t2_messages))
    sent_to_a = primary.requests[1]
    assert (sent_to_a.strip_encrypted_replay, sent_to_a.fallback_model) == (False, None)
    a_messages = cast("list[dict[str, object]]", _or_chat_kwargs(sent_to_a)["messages"])
    assert a_messages[1]["reasoning_details"] == a_details
    assert a_messages[3]["reasoning_details"] == [b_details[2]]


@pytest.mark.asyncio
async def test_same_id_backup_account_and_level_metadata() -> None:
    """Exact configured references survive same-ID accounts and @level references."""
    config = Config(
        models={
            "gpt": ModelConfig(provider="openai", id="gpt-6", api_key="key-a", fallbacks=["gpt-backup"]),
            "gpt-backup": ModelConfig(
                provider="openai",
                id="gpt-6",
                api_key="key-b",
                display_name="Backup org",
            ),
            "opus": ModelConfig(
                provider="anthropic",
                id="claude-opus-5-5",
                reasoning=ReasoningConfig(levels=["medium", "xhigh"], default="medium"),
                fallbacks=["backup"],
            ),
            "backup": ModelConfig(
                provider="anthropic",
                id="claude-opus-5-5",
                display_name="Backup",
                reasoning=ReasoningConfig(levels=["medium", "xhigh"], default="medium"),
            ),
        },
    )

    # 1. Same-ID backup account stamps exact backup reference and fallback_from.
    primary = _Model("gpt", [[_quota()]])
    primary.identity = ModelIdentity(id="gpt-6", provider="OpenAI")
    backup = _Model(
        "gpt-backup",
        [
            [
                TextDelta("from backup"),
                TurnCompleted(
                    message=Message(
                        role="assistant",
                        content="from backup",
                        provider_data=model_provenance("gpt-6", "OpenAI"),
                    ),
                    stop_reason="end",
                ),
            ],
        ],
    )
    backup.identity = ModelIdentity(id="gpt-6", provider="OpenAI")
    record = await _persisted_run(_with_fallbacks(primary, backup), run_id="r1", session_id="s1")
    assert record.model_name == "gpt-backup"
    metadata = build_ai_run_metadata_content(
        config=config,
        model_name="gpt",
        run_id="r1",
        session_id="s1",
        status=RunStatus.completed,
        model=record.model,
        model_provider=record.model_provider,
        answering_model_name=record.model_name,
        prices={},
        tool_count=0,
    )[AI_RUN_METADATA_KEY]
    assert metadata["model"] == {
        "config": "gpt-backup",
        "display_name": "Backup org",
        "id": "gpt-6",
        "provider": "OpenAI",
        "fallback_from": "gpt",
    }

    # 2. @level preserved on fallback metadata.
    level_meta = build_ai_run_metadata_content(
        config=config,
        model_name="opus@xhigh",
        run_id="r2",
        session_id="s1",
        status=RunStatus.completed,
        model="claude-opus-5-5",
        model_provider="Anthropic",
        answering_model_name="backup@xhigh",
        prices={},
        tool_count=0,
    )[AI_RUN_METADATA_KEY]
    assert level_meta["model"] == {
        "config": "backup@xhigh",
        "display_name": "Backup (xhigh)",
        "id": "claude-opus-5-5",
        "provider": "Anthropic",
        "fallback_from": "opus@xhigh",
    }


def _cooldown_config(**overrides: object) -> Config:
    primary: dict[str, object] = {
        "provider": "openai",
        "id": "gpt-6",
        "api_key": "k",
        "host": "https://a.example",
        "fallbacks": ["bad-cred"],
        **overrides,
    }
    return Config(
        models={
            "default": ModelConfig.model_validate(primary),
            "bad-cred": ModelConfig(provider="openai", id="gpt-6", api_key="env:MISSING_CRED"),
        },
    )


@pytest.mark.asyncio
async def test_cooldown_ownership_committed_reset_and_max_deadline(
    runtime_paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cooldowns isolate replaced routes/keys, remember committed resets, and never shorten."""
    # 1. Pure in-memory cooldown key rotates when provider, id, or host changes and performs no credential reads.
    from mindroom.providers import registry as provider_registry  # noqa: PLC0415

    configs = (
        _cooldown_config(),
        _cooldown_config(host="https://b.example"),
        _cooldown_config(id="gpt-6-mini"),
        _cooldown_config(provider="openrouter"),
    )
    store_reads: list[str] = []
    real_dashboard_key = provider_registry.model_with_dashboard_key

    def _track_dashboard_key(name: str, cfg: ModelConfig, paths: RuntimePaths) -> ModelConfig:
        store_reads.append(name)
        return real_dashboard_key(name, cfg, paths)

    monkeypatch.setattr(provider_registry, "model_with_dashboard_key", _track_dashboard_key)
    keys = [
        cast(
            "partial[object]",
            cast("WrappedProvider", build_model_provider(cfg, runtime_paths, "default")).stage,
        ).keywords["cooldown_key"]
        for cfg in configs
    ]
    assert len(set(keys)) == 4
    assert "bad-cred" not in store_reads

    # 2. Committed quota failure still records cooldown (without replaying), and a shorter hint never shortens it.
    primary = _Model("primary", [[TextDelta("partial"), _quota(reset_in=3600)], _answer("primary")])
    fallback = _Model("fallback", [_answer("fallback")])
    provider = _with_fallbacks(primary, fallback)
    with pytest.raises(ProviderError):
        await drain(provider, _request())
    assert fallback.requests == []
    long_until = fallback_stage._COOLDOWN_UNTIL["primary"]
    fallback_stage._remember_reset("primary", _quota(reset_in=30))
    assert fallback_stage._COOLDOWN_UNTIL["primary"] == long_until
    await drain(provider, _request())
    assert (len(primary.requests), len(fallback.requests)) == (1, 1)


@pytest.mark.asyncio
async def test_build_model_provider_enforces_exact_level_on_budget_fallback(
    runtime_paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """free@high with budgets.fallback_model=paid@low blocks paid@high, while free@low allows paid@low."""
    from mindroom.providers import registry as provider_registry  # noqa: PLC0415
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity  # noqa: PLC0415

    config = Config(
        models={
            "free": ModelConfig(
                provider="openai",
                id="free-m",
                api_key="k",
                reasoning=ReasoningConfig(levels=["low", "high"], default="low"),
                fallbacks=["paid"],
            ),
            "paid": ModelConfig(
                provider="openai",
                id="paid-m",
                api_key="k",
                pricing=ModelPricing(input=5, output=25),
                reasoning=ReasoningConfig(levels=["low", "high"], default="low"),
            ),
        },
        budgets=BudgetsConfig(fallback_model="paid@low", monthly_limit_usd=0),
    )
    calls: list[str] = []

    def _build_fake(_cfg: Config, _paths: RuntimePaths, ref: str, **_kw: object) -> _Model:
        if ref.startswith("free"):
            return _Model(ref, [[_quota()]])
        return _Model(ref, [[partial(calls.append, ref), *_answer(ref, f"answered:{ref}")]])

    monkeypatch.setattr(provider_registry, "_build_single_provider", _build_fake)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:localhost",
        room_id="!r:localhost",
        thread_id=None,
        resolved_thread_id=None,
        session_id="s1",
    )

    high_provider = build_model_provider(config, runtime_paths, "free@high", execution_identity=identity)
    with pytest.raises(ProviderError):
        await drain(high_provider, _request())
    assert calls == []

    low_provider = build_model_provider(config, runtime_paths, "free@low", execution_identity=identity)
    events = await drain(low_provider, _request())
    assert _text(events) == "answered:paid@low"
    assert calls == ["paid@low"]


@pytest.mark.asyncio
async def test_compaction_chunk_final_transient_failover_and_distinct_refuser_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compaction retries transient primary once then fails over on final retry, and never retries the same refuser."""
    from mindroom.history import summarizer as compaction_module  # noqa: PLC0415
    from tests.history_helpers import _summarizer  # noqa: PLC0415

    monkeypatch.setattr(
        compaction_module,
        "DEFAULT_SUMMARY_RETRY_POLICY",
        SummaryRetryPolicy(max_attempts=2, same_input_retry_delay_seconds=0.0),
    )
    runs = [
        RunRecord(
            run_id="r1",
            status=RunStatus.completed,
            messages=[Message(role="user", content="hello"), Message(role="assistant", content="world")],
        ),
    ]

    # 1. Sustained 503 on primary: attempt 1 retries primary, final attempt fails over to healthy backup.
    primary = _Model("primary", [[_transient()], [_transient()]])
    backup = _Model("backup", [_answer("backup", "Compact summary.")])
    chunk = await summarize_chunk(_summarizer(_with_fallbacks(primary, backup), 16_000, name="primary"), None, runs)
    assert chunk is not None
    assert chunk.summary == "Compact summary."
    assert (len(primary.requests), len(backup.requests)) == (2, 1)

    # 2. Primary fails over to B on quota and B refuses: refusal fallback ALSO configured as B is NOT invoked again.
    fallback_stage._COOLDOWN_UNTIL.clear()
    quota_primary = _Model("primary", [[_quota()]])
    refusal = ModelSafeguardRefusal(provider="backup-provider", model_id="backup-id", usage=MessageMetrics())
    avail_b = _Model("backup", [[refusal]])
    refusal_b = _Model("backup", [_answer("backup", "must not run")])
    summarizer_same_b = _summarizer(
        _with_fallbacks(quota_primary, avail_b),
        16_000,
        name="primary",
        fallback=SummaryModel(refusal_b, "backup", 16_000),
    )
    with pytest.raises(ModelSafeguardRefusal):
        await summarize_chunk(summarizer_same_b, None, runs)
    assert len(avail_b.requests) == 1
    assert refusal_b.requests == []
