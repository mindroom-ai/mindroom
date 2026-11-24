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
from pydantic import BaseModel, ValidationError
from structlog.testing import capture_logs

from mindroom.agent_loop.helper import complete_prompt
from mindroom.agent_loop.model_loop import EMPTY_REQUEST
from mindroom.agent_loop.run import run_agent
from mindroom.agent_loop.spec import AgentSpec, RunContext, UnstoredSession
from mindroom.agent_loop.structured import json_output_prompt
from mindroom.ai_run_metadata import build_ai_run_metadata_content
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import AI_RUN_METADATA_KEY, resolve_runtime_paths
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
)
from mindroom.llm.messages import REWRITTEN_ATTEMPT_KEY, Message, model_provenance
from mindroom.llm.provider import (
    STRUCTURED_OUTPUT_TOOL,
    Capabilities,
    ModelIdentity,
    ModelRequest,
    NativeCompactionReplay,
    RequestOverrides,
    TextDelta,
    ToolCallReady,
    TurnCompleted,
    UsageReported,
)
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.providers.errors import classify_provider_error
from mindroom.providers.openai.errors import provider_error
from mindroom.providers.pipeline import fallback as fallback_stage
from mindroom.providers.pipeline import retry as retry_stage
from mindroom.providers.pipeline.fallback import ChainModel, fails_over, fallback_stream
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
    from mindroom.llm.provider import StreamEvent, StructuredOutput, TokenCounter

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
    return [ChainModel(model.name, context_window, partial(lambda found: found, model)) for model in models]


def _with_fallbacks(primary: _Model, *fallbacks: _Model) -> WrappedProvider:
    return WrappedProvider(primary, partial(fallback_stream, model_name=primary.name, chain=_chain(*fallbacks)))


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
    ],
    ids=["rate-limit", "unavailable", "auth", "tokens-per-minute", "route-retried"],
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

    assert events == [output]
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
async def test_the_failed_models_usage_is_forwarded_and_summed_into_the_answer() -> None:
    spent = MessageMetrics(tokens=TokenCounts(input_tokens=7, total_tokens=7))
    primary = _Model("primary", [[UsageReported(spent, final=False), _quota()]])

    events = await drain(_with_fallbacks(primary, _Model("fallback", [_answer("fallback")])), _request())

    # The failed attempt's partial report is settled as final before the fallback answers.
    assert [(event.usage, event.final) for event in events if isinstance(event, UsageReported)] == [
        (spent, False),
        (spent, True),
    ]
    turn = events[-1]
    assert isinstance(turn, TurnCompleted)
    assert turn.message.metrics.tokens.input_tokens == 7


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
        partial(fallback_stream, model_name="primary", chain=chain),
    )

    with capture_logs() as logs:
        await drain(provider, ModelRequest(messages=(Message(role="user", content="long " * 100),)))

    assert (len(small.requests), len(large.requests)) == (0, 1)
    assert [log["model"] for log in logs if log["event"] == "model_fallback_skipped"] == ["small"]


@pytest.mark.asyncio
async def test_a_request_whose_caller_retries_it_itself_never_fails_over() -> None:
    fallback = _Model("fallback", [_answer("fallback")])
    request = _request(overrides=RequestOverrides(caller_retries=True))

    with pytest.raises(ProviderError):
        await drain(_with_fallbacks(_Model("primary", [[_quota()]]), fallback), request)

    assert fallback.requests == []


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
    assert (record.model, record.model_provider) == ("bedrock-id", "Anthropic")
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
    chain = [ChainModel("broken", None, _broken_build), *_chain(good)]
    provider = WrappedProvider(
        _Model("primary", [[_quota()]]),
        partial(fallback_stream, model_name="primary", chain=chain),
    )

    with capture_logs() as logs:
        events = await drain(provider, _request())

    assert _text(events) == "answer"
    assert len(good.requests) == 1
    [skipped] = [log for log in logs if log["event"] == "model_fallback_skipped"]
    assert (skipped["model"], skipped["reason"], skipped["error_type"]) == ("broken", "unavailable", "RuntimeError")

    only_broken = WrappedProvider(
        _Model("primary", [[_quota()]]),
        partial(fallback_stream, model_name="primary", chain=[ChainModel("broken", None, _broken_build)]),
    )
    with pytest.raises(ProviderError, match="usage limit"):
        await drain(only_broken, _request())


@pytest.mark.asyncio
async def test_a_demoted_model_is_still_tried_after_a_fallbacks_own_failure() -> None:
    fallback_stage._COOLDOWN_UNTIL["primary"] = time.monotonic() + 3600
    primary = _Model("primary", [_answer("primary", "from primary")])
    fallback = _Model("fallback", [[InvalidRequest("unsupported parameter", provider="F", status_code=400)]])

    events = await drain(_with_fallbacks(primary, fallback), _request())

    assert _text(events) == "from primary"
    assert (len(fallback.requests), len(primary.requests)) == (1, 1)


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
    chain = partial(fallback_stream, model_name="primary", chain=_chain(fallback))

    events = await drain(WrappedProvider(pipeline_provider(adapter), chain), _request())

    assert _text(events) == "answer"
    # One request and the retry stage's four reissues, then the fallback.
    assert (len(adapter.requests), len(fallback.requests)) == (5, 1)


# --- Structured output across structured-output modes -----------------------------------------------------------


class _Workflow(BaseModel):
    name: str
    cron: str


_WORKFLOW = _Workflow(name="standup", cron="0 9 * * 1-5")


@dataclass
class _StructuredModel(_Model):
    """Answer a structured request as an adapter of its ``capabilities`` mode does.

    A ``forced_tool`` route answers with the forced call, a JSON-mode route with fenced JSON its reader must repair, and
    an enforcing route with the bare object.
    """

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        answer = _WORKFLOW.model_dump_json()
        mode = self.capabilities.structured_output
        if mode == "forced_tool":
            call = {"id": "c1", "type": "function", "function": {"name": STRUCTURED_OUTPUT_TOOL, "arguments": answer}}
            yield ToolCallReady(call)
            message = Message(role="assistant", tool_calls=[call])
            stop = "tool_use"
        else:
            text = f"Here it is:\n```json\n{answer}\n```" if mode == "json_mode" else answer
            yield TextDelta(text)
            message = Message(role="assistant", content=text)
            stop = "end"
        message.provider_data = model_provenance(self.identity.id, self.identity.provider)
        yield TurnCompleted(message=message, stop_reason=stop)


@pytest.mark.parametrize(
    ("primary_mode", "fallback_mode"),
    [
        ("forced_tool", "json_mode"),
        ("json_schema", "json_mode"),
        ("native", "json_mode"),
        ("json_mode", "forced_tool"),
        ("json_schema", "forced_tool"),
        ("forced_tool", "native"),
    ],
)
@pytest.mark.asyncio
async def test_a_structured_request_is_answered_whatever_the_fallbacks_mode(
    primary_mode: StructuredOutput,
    fallback_mode: StructuredOutput,
) -> None:
    primary = _Model("primary", [[_quota()]], capabilities=Capabilities(structured_output=primary_mode))
    fallback = _StructuredModel("fallback", [], capabilities=Capabilities(structured_output=fallback_mode))

    result = await complete_prompt(
        _with_fallbacks(primary, fallback),
        "Every weekday at nine, post the standup.",
        instructions=["Parse the schedule."],
        schema=_Workflow,
    )

    assert result.content == _WORKFLOW
    assert (result.usage.model, result.usage.provider) == ("fallback-id", "fallback-provider")
    [sent] = fallback.requests
    system = sent.messages[0]
    assert system.role == "system"
    assert isinstance(system.content, str)
    # A JSON-mode fallback gets the JSON prompt exactly once, whoever shaped the request.
    expected_prompts = 1 if "json_mode" in (primary_mode, fallback_mode) else 0
    assert system.content.count(json_output_prompt(_Workflow)) == expected_prompts
    assert system.content.startswith("Parse the schedule.")


@pytest.mark.asyncio
async def test_a_json_mode_answer_that_does_not_validate_reaches_a_strict_caller_as_its_text() -> None:
    primary = _Model("primary", [[_quota()]], capabilities=Capabilities(structured_output="forced_tool"))
    fallback = _Model(
        "fallback",
        [_answer("fallback", "not json")],
        capabilities=Capabilities(structured_output="json_mode"),
    )

    with capture_logs() as logs:
        result = await complete_prompt(_with_fallbacks(primary, fallback), "hi", schema=_Workflow)

    assert result.content == "not json"
    assert [log["schema"] for log in logs if log["event"] == "helper_structured_output_unparsed"] == ["_Workflow"]


def _schema_request(system: Message) -> ModelRequest:
    return ModelRequest(messages=(system, Message(role="user", content="standup at nine")), output_schema=_Workflow)


@pytest.mark.asyncio
async def test_a_json_mode_fallback_is_sized_with_the_json_prompt_it_is_sent() -> None:
    request = _schema_request(Message(role="system", content="Parse the schedule."))
    small = _StructuredModel("small", [], capabilities=Capabilities(structured_output="json_mode"))
    large = _StructuredModel("large", [], capabilities=Capabilities(structured_output="json_mode"))
    # The request alone fits the small window; with the JSON prompt the fallback needs, it does not.
    window = small.request_tokens(request) + 1
    chain = [*_chain(small, context_window=window), *_chain(large)]
    primary = _Model("primary", [[_quota()]], capabilities=Capabilities(structured_output="forced_tool"))

    events = await drain(WrappedProvider(primary, partial(fallback_stream, model_name="primary", chain=chain)), request)

    assert (len(small.requests), len(large.requests)) == (0, 1)
    turn = events[-1]
    assert isinstance(turn, TurnCompleted)
    assert turn.message.content == _WORKFLOW.model_dump_json()


@pytest.mark.asyncio
async def test_the_json_prompt_joins_a_system_message_made_of_parts() -> None:
    parts = [{"type": "text", "text": "Parse the schedule."}]
    fallback = _StructuredModel("fallback", [], capabilities=Capabilities(structured_output="json_mode"))
    primary = _Model("primary", [[_quota()]], capabilities=Capabilities(structured_output="json_schema"))

    await drain(_with_fallbacks(primary, fallback), _schema_request(Message(role="system", content=parts)))

    [sent] = fallback.requests
    assert [message.role for message in sent.messages] == ["system", "user"]
    assert sent.messages[0].content == [*parts, {"type": "text", "text": json_output_prompt(_Workflow)}]


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
