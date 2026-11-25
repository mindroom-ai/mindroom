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

from mindroom.agent_loop.model_loop import EMPTY_REQUEST, LoopHooks
from mindroom.agent_loop.run import run_agent
from mindroom.agent_loop.spec import AgentSpec, RunContext, UnstoredSession
from mindroom.ai_run_metadata import build_ai_run_metadata_content
from mindroom.budgets.pricing import model_prices, run_cost_usd
from mindroom.config.budgets import BudgetsConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ModelPricing, ReasoningConfig
from mindroom.constants import AI_RUN_METADATA_KEY, resolve_runtime_paths
from mindroom.history.compaction import SummaryModel, _summarize_chunk
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
    is_transient_status,
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
    from mindroom.llm.provider import StreamEvent, TokenCounter

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
                fallbacks=["free"],
            ),
            "free": ModelConfig(provider="codex", id="gpt-6-sol"),
        },
    )
    saved: list[RunRecord] = []

    async def persist(record: RunRecord) -> None:
        saved.append(record)

    primary_usage = MessageMetrics(tokens=TokenCounts(input_tokens=1_000_000, total_tokens=1_000_000))
    fallback_usage = MessageMetrics(tokens=TokenCounts(input_tokens=500_000, total_tokens=500_000))
    primary = _Model("paid", [[UsageReported(primary_usage, final=False), _quota()]])
    primary.identity = ModelIdentity(id="claude-opus-5-5", provider="Anthropic")
    answer = Message(
        role="assistant",
        content="hi",
        provider_data=model_provenance("gpt-6-sol", "Codex"),
        metrics=fallback_usage,
    )
    fallback = _Model(
        "free",
        [
            [
                UsageReported(fallback_usage, final=True),
                TextDelta("hi"),
                TurnCompleted(message=answer, stop_reason="end"),
            ],
        ],
    )
    fallback.identity = ModelIdentity(id="gpt-6-sol", provider="Codex")
    spec = AgentSpec(id="code", name="Code", model=_with_fallbacks(primary, fallback), system_messages=(), toolkits=())

    async for _ in run_agent(
        spec,
        [Message(role="user", content="hello")],
        ctx=RunContext(run_id="run", session=UnstoredSession("session")),
        persist=persist,
        request_template=EMPTY_REQUEST,
    ):
        pass

    [record] = saved
    assert record.metrics is not None
    assert record.metrics.tokens.input_tokens == 1_500_000
    assert [
        (entry.provider, entry.id, entry.tokens.input_tokens) for entry in (record.metrics.details or {})["model"]
    ] == [
        ("Anthropic", "claude-opus-5-5", 1_000_000),
        ("Codex", "gpt-6-sol", 500_000),
    ]
    # Paid primary tokens must NOT be folded into the unpriced fallback's identity.
    assert run_cost_usd(
        record.metrics,
        model_provider=record.model_provider,
        model_id=record.model,
        prices=model_prices(config),
    ) == pytest.approx(5.0)


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
async def test_compaction_summary_fails_over_on_quota_but_keeps_transient_caller_retries() -> None:
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
    saved: list[RunRecord] = []

    async def persist(record: RunRecord) -> None:
        saved.append(record)

    answer = Message(role="assistant", content="hi", provider_data=model_provenance("bedrock-id", "Anthropic"))
    answer.metrics = MessageMetrics(tokens=TokenCounts(input_tokens=3, total_tokens=3))
    fallback = _Model("bedrock", [[TextDelta("hi"), TurnCompleted(message=answer, stop_reason="end")]])
    spec = AgentSpec(
        id="code",
        name="Code",
        model=_with_fallbacks(_Model("merlin", [[_quota()]]), fallback),
        system_messages=(),
        toolkits=(),
    )

    async for _ in run_agent(
        spec,
        [Message(role="user", content="hello")],
        ctx=RunContext(run_id="run", session=UnstoredSession("session")),
        persist=persist,
        request_template=EMPTY_REQUEST,
    ):
        pass

    [record] = saved
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
async def test_a_fallback_cut_off_after_output_counts_its_usage_under_the_fallback() -> None:
    saved: list[RunRecord] = []

    async def persist(record: RunRecord) -> None:
        saved.append(record)

    billed = MessageMetrics(tokens=TokenCounts(input_tokens=100, total_tokens=100))
    fallback = _Model("fallback", [[UsageReported(billed, final=False), TextDelta("partial"), _transient()]])
    spec = AgentSpec(
        id="code",
        name="Code",
        model=_with_fallbacks(_Model("primary", [[_quota()]]), fallback),
        system_messages=(),
        toolkits=(),
    )

    with pytest.raises(ProviderUnavailable):
        async for _ in run_agent(
            spec,
            [Message(role="user", content="hello")],
            ctx=RunContext(run_id="run", session=UnstoredSession("session")),
            persist=persist,
            request_template=EMPTY_REQUEST,
        ):
            pass

    [record] = saved
    cut_off = record.messages[-1]
    assert (cut_off.provider_data or {})["mindroom_model"] == {"id": "fallback-id", "provider": "fallback-provider"}
    assert [
        (entry.id, entry.provider, entry.tokens.input_tokens) for entry in (record.metrics.details or {})["model"]
    ] == [
        ("fallback-id", "fallback-provider", 100),
    ]


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


def _codex_429(body: dict[str, object]) -> openai.RateLimitError:
    request = httpx2.Request("POST", "https://chatgpt.com/backend-api/codex/responses")
    return openai.RateLimitError("Error code: 429", response=httpx2.Response(429, request=request), body=body)


@pytest.mark.parametrize(
    "reset",
    [{"resets_in_seconds": 7200}, {"resets_at": "now+7200"}],
    ids=["resets-in", "resets-at"],
)
def test_codex_usage_limit_is_quota_exhausted_until_its_reset(reset: dict[str, object]) -> None:
    if reset.get("resets_at") == "now+7200":
        reset = {"resets_at": int(time.time()) + 7200}
    body = {"type": "usage_limit_reached", "message": "The usage limit has been reached", **reset}

    error = provider_error(_codex_429(body), provider="Codex")

    assert type(error) is ProviderError
    assert error.code == QUOTA_EXHAUSTED
    assert not error.retryable
    assert error.retry_after_seconds == pytest.approx(7200, abs=5)
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
    a_details = [
        {"type": "reasoning.encrypted", "format": "openai-responses-v1", "index": 0, "data": "sealed-a"},
        {
            "type": "reasoning.text",
            "format": "anthropic-claude-v1",
            "index": 1,
            "text": "Signed A",
            "signature": "sig-a",
        },
        {"type": "reasoning.summary", "format": "openai-responses-v1", "index": 2, "summary": "Summary A"},
    ]
    b_details = [
        {"type": "reasoning.encrypted", "format": "openai-responses-v1", "index": 0, "data": "sealed-b"},
        {
            "type": "reasoning.text",
            "format": "anthropic-claude-v1",
            "index": 1,
            "text": "Signed B",
            "signature": "sig-b",
        },
        {"type": "reasoning.summary", "format": "openai-responses-v1", "index": 2, "summary": "Summary B"},
    ]
    a_turn = Message(
        role="assistant",
        content="Checking A.",
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
        provider_data={"reasoning_details": a_details},
    )
    b_tool_turn = Message(
        role="assistant",
        content="Checking B.",
        tool_calls=[{"id": "call_2", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
        provider_data={"reasoning_details": b_details},
    )
    primary = _Model("or-a", [[_transient()], [_transient()], _answer("or-a", "recovered")])
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
    b_kwargs = chat_request_kwargs(
        model_id="openai/gpt-6",
        messages=sent_to_b.messages,
        tools=(),
        tool_choice=None,
        output_schema=None,
        structured_output="native",
        settings=ChatSettings(),
        wire=ChatWire(route="openrouter", system_role="system"),
        provider="OpenRouter",
        fallback_model=sent_to_b.fallback_model,
    )
    assert b_kwargs["messages"][1]["reasoning_details"] == [a_details[2]]
    assert b_kwargs["messages"][3]["reasoning_details"] == b_details
    assert [call["id"] for call in b_kwargs["messages"][1]["tool_calls"]] == ["call_1"]
    assert [call["id"] for call in b_kwargs["messages"][3]["tool_calls"]] == ["call_2"]

    # Turn 2b: primary A recovers -> keeps A's own signed reasoning, strips B's foreign signed reasoning.
    await drain(provider, ModelRequest(messages=t2_messages))
    sent_to_a = primary.requests[2]
    assert (sent_to_a.strip_encrypted_replay, sent_to_a.fallback_model) == (False, None)
    a_kwargs = chat_request_kwargs(
        model_id="openai/gpt-6",
        messages=sent_to_a.messages,
        tools=(),
        tool_choice=None,
        output_schema=None,
        structured_output="native",
        settings=ChatSettings(),
        wire=ChatWire(route="openrouter", system_role="system"),
        provider="OpenRouter",
        fallback_model=sent_to_a.fallback_model,
    )
    assert a_kwargs["messages"][1]["reasoning_details"] == a_details
    assert a_kwargs["messages"][3]["reasoning_details"] == [b_details[2]]


@pytest.mark.asyncio
async def test_same_id_backup_account_level_cut_off_without_usage_and_live_badge() -> None:
    """Exact configured references survive same-ID accounts, @level, live callbacks, and usage-free cut-offs."""
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

    # 1. Same-ID backup account + live callback fired before first TextDelta.
    events_order: list[str] = []
    saved: list[RunRecord] = []

    async def persist(record: RunRecord) -> None:
        saved.append(record)

    primary = _Model("gpt", [[_quota()]])
    primary.identity = ModelIdentity(id="gpt-6", provider="OpenAI")
    backup = _Model(
        "gpt-backup",
        [
            [
                TextDelta("from "),
                TextDelta("backup"),
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
    spec = AgentSpec(id="code", name="Code", model=_with_fallbacks(primary, backup), system_messages=(), toolkits=())

    async for event in run_agent(
        spec,
        [Message(role="user", content="hello")],
        ctx=RunContext(run_id="r1", session=UnstoredSession("s1")),
        persist=persist,
        request_template=EMPTY_REQUEST,
        hooks=LoopHooks(on_model_changed=lambda name: events_order.append(f"model:{name}")),
    ):
        if isinstance(event, TextDelta):
            events_order.append(f"text:{event.text}")  # noqa: PERF401 - asserts live chronological ordering

    assert events_order == ["model:gpt-backup", "text:from ", "text:backup"]
    [record] = saved
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

    # 3. Cut-off fallback that emitted text without reporting usage still updates record.model & record.model_name.
    saved.clear()
    cut_off_fallback = _Model("backup", [[TextDelta("partial"), _transient()]])
    cut_spec = AgentSpec(
        id="code",
        name="Code",
        model=_with_fallbacks(_Model("opus", [[_quota()]]), cut_off_fallback),
        system_messages=(),
        toolkits=(),
    )
    with pytest.raises(ProviderUnavailable):
        async for _ in run_agent(
            cut_spec,
            [Message(role="user", content="hello")],
            ctx=RunContext(run_id="r3", session=UnstoredSession("s1")),
            persist=persist,
            request_template=EMPTY_REQUEST,
        ):
            pass
    [cut_record] = saved
    assert (cut_record.model, cut_record.model_provider, cut_record.model_name) == (
        "backup-id",
        "backup-provider",
        "backup",
    )


@pytest.mark.asyncio
async def test_cooldown_ownership_committed_reset_max_deadline_and_stream_api_error(
    runtime_paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cooldowns isolate replaced routes/keys, remember committed resets, never shorten, and parse stream APIError."""
    # 1. Pure in-memory cooldown key rotates when provider, id, or host changes and performs no credential reads.
    from mindroom.providers import registry as provider_registry  # noqa: PLC0415

    cfg_base = Config(
        models={
            "default": ModelConfig(
                provider="openai",
                id="gpt-6",
                api_key="k",
                host="https://a.example",
                fallbacks=["bad-cred"],
            ),
            "bad-cred": ModelConfig(provider="openai", id="gpt-6", api_key="env:MISSING_CRED"),
        },
    )
    cfg_host = Config(
        models={
            "default": ModelConfig(
                provider="openai",
                id="gpt-6",
                api_key="k",
                host="https://b.example",
                fallbacks=["bad-cred"],
            ),
            "bad-cred": ModelConfig(provider="openai", id="gpt-6", api_key="env:MISSING_CRED"),
        },
    )
    cfg_id = Config(
        models={
            "default": ModelConfig(
                provider="openai",
                id="gpt-6-mini",
                api_key="k",
                host="https://a.example",
                fallbacks=["bad-cred"],
            ),
            "bad-cred": ModelConfig(provider="openai", id="gpt-6", api_key="env:MISSING_CRED"),
        },
    )
    cfg_provider = Config(
        models={
            "default": ModelConfig(
                provider="openrouter",
                id="gpt-6",
                api_key="k",
                host="https://a.example",
                fallbacks=["bad-cred"],
            ),
            "bad-cred": ModelConfig(provider="openai", id="gpt-6", api_key="env:MISSING_CRED"),
        },
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
        for cfg in (cfg_base, cfg_host, cfg_id, cfg_provider)
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

    # 3. Streamed openai.APIError preserves Codex body reset hints, and shared transient classifier includes 409.
    stream_err = openai.APIError(
        "The usage limit has been reached",
        request=httpx2.Request("POST", "https://chatgpt.com/backend-api/codex/responses"),
        body={"type": "usage_limit_reached", "message": "The usage limit has been reached", "resets_in_seconds": 1800},
    )
    normalized = provider_error(stream_err, provider="Codex")
    assert normalized.code == QUOTA_EXHAUSTED
    assert normalized.retry_after_seconds == pytest.approx(1800)
    assert is_transient_status(409)


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
        model = _Model(ref, [_answer(ref, f"answered:{ref}")])
        orig_stream = model.stream

        async def _counted(req: ModelRequest) -> AsyncIterator[StreamEvent]:
            calls.append(ref)
            async for ev in orig_stream(req):
                yield ev

        model.stream = _counted  # type: ignore[method-assign]
        return model

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
async def test_failed_primary_usage_reconciles_with_per_request_usage_stats(tmp_path: Path) -> None:
    """When a failed primary emits usage before a fallback answers, include_requests=True reconciles both models."""
    from datetime import UTC, datetime  # noqa: PLC0415

    from mindroom.history.storage import new_scope_session  # noqa: PLC0415
    from mindroom.history.types import HistoryScope  # noqa: PLC0415
    from mindroom.session_storage.agent_storage import create_session_storage  # noqa: PLC0415
    from mindroom.usage.stats import collect_admin_usage  # noqa: PLC0415

    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={},
    )
    config = Config(
        models={"default": _synthetic()},
        agents={"code": {"display_name": "Code", "role": "Coder"}},
    )
    primary = _Model(
        "primary",
        [[UsageReported(MessageMetrics(tokens=TokenCounts(input_tokens=100, output_tokens=40))), _transient()]],
    )
    fallback_msg = Message(
        role="assistant",
        content="ok",
        metrics=MessageMetrics(tokens=TokenCounts(input_tokens=200, output_tokens=80)),
        provider_data=model_provenance("fallback-id", "fallback-provider"),
    )
    fallback = _Model("fallback", [[TextDelta("ok"), TurnCompleted(message=fallback_msg, stop_reason="end")]])
    spec = AgentSpec(id="code", name="Code", model=_with_fallbacks(primary, fallback), system_messages=(), toolkits=())
    saved: list[RunRecord] = []

    async def persist(record: RunRecord) -> None:
        saved.append(record)

    async for _ in run_agent(
        spec,
        [Message(role="user", content="hello")],
        ctx=RunContext(
            run_id="r-reconcile",
            session=UnstoredSession("s1"),
            metadata={"requester_id": "@alice:localhost"},
        ),
        persist=persist,
        request_template=EMPTY_REQUEST,
    ):
        pass

    [record] = saved
    record.created_at = int(datetime.now(tz=UTC).timestamp())
    storage = create_session_storage("code", config, paths, execution_identity=None)
    storage.save_run(
        new_scope_session(session_id="s1", scope=HistoryScope(kind="agent", scope_id="code")),
        record,
        record_usage=True,
    )

    report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
    assert report.request_coverage is not None
    assert report.request_coverage.unavailable_sources == 0
    by_provider = {
        (row.model_provider, row.model): (row.totals.input_tokens, row.totals.output_tokens)
        for row in report.request_breakdown
    }
    assert by_provider == {
        ("primary-provider", "primary-id"): (100, 40),
        ("fallback-provider", "fallback-id"): (200, 80),
    }

    from mindroom.helper_usage import _run_usage  # noqa: PLC0415
    from mindroom.usage.storage import helper_usage_snapshot  # noqa: PLC0415

    helper_snap = helper_usage_snapshot(
        _run_usage(record),
        usage_id="workflow:r-reconcile",
        kind="workflow",
        requester_id="@alice:localhost",
    )
    helper_rows = [
        (
            r["model_provider"],
            r["model"],
            cast("dict[str, object]", r["metrics"])["input_tokens"],
            cast("dict[str, object]", r["metrics"])["output_tokens"],
        )
        for r in cast("list[dict[str, object]]", helper_snap["requests"])
    ]
    assert helper_rows == [
        ("primary-provider", "primary-id", 100, 40),
        ("fallback-provider", "fallback-id", 200, 80),
    ]


@pytest.mark.asyncio
async def test_compaction_chunk_final_transient_failover_and_distinct_refuser_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compaction retries transient primary once then fails over on final retry, and never retries the same refuser."""
    from mindroom.history import compaction as compaction_module  # noqa: PLC0415
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
    chunk = await _summarize_chunk(_summarizer(_with_fallbacks(primary, backup), 16_000, name="primary"), None, runs)
    assert chunk is not None
    assert chunk.summary == "Compact summary."
    assert (len(primary.requests), len(backup.requests)) == (2, 1)

    # 2. Primary fails over to B on quota and B refuses: refusal fallback ALSO configured as B is NOT invoked again.
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
        await _summarize_chunk(summarizer_same_b, None, runs)
    assert len(avail_b.requests) == 1
    assert refusal_b.requests == []
