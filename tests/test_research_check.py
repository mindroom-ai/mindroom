"""Tests for the bundled research_check plugin."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.hooks import EVENT_MESSAGE_AFTER_RESPONSE, AfterResponseContext, HookRegistry, MessageEnvelope
from mindroom.hooks.context import ResponseResult
from mindroom.judgment.answers import JudgmentError, JudgmentResponse
from mindroom.judgment.execution import JudgmentCapacity, run_judgment
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget
from mindroom.research_check import hooks as research_check
from mindroom.tool_system.events import ToolTraceEntry, format_tool_combined
from mindroom.tool_system.plugins import isolated_plugin_runtime
from tests.conftest import bind_runtime_paths, message_origin, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.judgment.answers import JudgmentFailure
    from mindroom.judgment.state import JudgmentRequest

_SETTINGS = {"judgment": {"provider": "openai_decisions"}}
_REPLY = "Go to Café Noir on Oudegracht 12, it has the best coffee in Utrecht."


def _config(tmp_path: Path, **overrides: object) -> Config:
    return bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", rooms=["!room:localhost"])},
            models={"default": ModelConfig(provider="test", id="test-model")},
            **overrides,
        ),
        test_runtime_paths(tmp_path),
    )


def _envelope(*, body: str = "Where can I get good coffee in Utrecht?", **overrides: Any) -> MessageEnvelope:  # noqa: ANN401
    values: dict[str, Any] = {
        "source_event_id": "$question",
        "target": MessageTarget.resolve("!room:localhost", "$thread", "$question"),
        "body": body,
        "attachment_ids": (),
        "mentioned_agents": (),
        "agent_name": "code",
        "origin": message_origin(sender_id="@user:localhost", requester_id="@user:localhost", source_kind="message"),
    }
    return MessageEnvelope(**(values | overrides))


def _dispatched_envelope(source_kind: str, *, hook_source: str | None = None) -> MessageEnvelope:
    """A turn MindRoom started on the person's behalf, such as a plugin follow-up, schedule, or webhook."""
    return _envelope(
        hook_source=hook_source,
        origin=message_origin(
            sender_id="@mindroom_code:localhost",
            requester_id="@user:localhost",
            sender_entity_name="code",
            source_kind=source_kind,
            original_sender="@user:localhost",
        ),
    )


@dataclass
class _Sent:
    room_id: str
    body: str
    thread_id: str | None
    source_hook: str
    extra_content: dict[str, Any] | None
    trigger_dispatch: bool


@dataclass
class _Harness:
    """A real after_response context with a capturing sender and a scripted judgment backend."""

    tmp_path: Path
    decision: bool | None = True
    failure: JudgmentFailure | None = None
    credential: bool = True
    sent: list[_Sent] = field(default_factory=list)
    requests: list[JudgmentRequest] = field(default_factory=list)
    bound: list[tuple[str, str]] = field(default_factory=list)

    async def _send(
        self,
        room_id: str,
        body: str,
        thread_id: str | None,
        source_hook: str,
        extra_content: dict[str, Any] | None,
        *,
        trigger_dispatch: bool = False,
    ) -> str | None:
        self.sent.append(_Sent(room_id, body, thread_id, source_hook, extra_content, trigger_dispatch))
        return "$follow-up"

    async def _answer(self, request: JudgmentRequest) -> JudgmentResponse[bool]:
        self.requests.append(request)
        if self.failure is not None:
            raise JudgmentError(self.failure)
        return JudgmentResponse(model="judge", decision=self.decision, probability=None, usage=None)

    def create_evaluator(
        self,
        _settings: object,
        _config: Config,
        _runtime_paths: object,
        *,
        owner: str,
        question_id: str,
    ) -> partial[Any] | None:
        self.bound.append((owner, question_id))
        if not self.credential:
            return None
        capacity = JudgmentCapacity(max_concurrent=1, max_per_owner=1)
        return partial(
            run_judgment,
            evaluate=self._answer,
            owner=owner,
            timeout_seconds=1.0,
            allow_network=True,
            capacity=capacity,
        )

    def context(
        self,
        *,
        settings: dict[str, Any] | None = None,
        envelope: MessageEnvelope | None = None,
        response_text: str = _REPLY,
        response_kind: str = "ai",
        tool_trace: tuple[ToolTraceEntry, ...] = (),
        config: Config | None = None,
    ) -> AfterResponseContext:
        config = config or _config(self.tmp_path)
        return AfterResponseContext(
            event_name=EVENT_MESSAGE_AFTER_RESPONSE,
            plugin_name="research_check",
            settings=_SETTINGS if settings is None else settings,
            config=config,
            runtime_paths=runtime_paths_for(config),
            logger=get_logger("tests.research_check"),
            correlation_id="corr-research",
            message_sender=self._send,
            result=ResponseResult(
                response_text=response_text,
                response_event_id="$reply",
                delivery_kind="sent",
                response_kind=response_kind,
                envelope=envelope or _envelope(),
                tool_trace=tool_trace,
            ),
        )


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Harness:
    """Route the plugin's judgment through a scripted backend."""
    harness = _Harness(tmp_path)
    monkeypatch.setattr(research_check, "create_judgment_evaluator", harness.create_evaluator)
    return harness


def _conversation(request: JudgmentRequest) -> list[dict[str, str]]:
    assert request.body is not None
    return json.loads(request.body)["state"]["conversation"]


@pytest.mark.asyncio
async def test_true_decision_sends_follow_up_in_thread(harness: _Harness) -> None:
    """A reply judged unresearched makes the same agent verify it in the same thread for the same requester."""
    await research_check.check_research(harness.context())

    assert len(harness.sent) == 1
    sent = harness.sent[0]
    assert sent.room_id == "!room:localhost"
    assert sent.thread_id == "$thread"
    assert sent.body.startswith('@code Research check on your reply that starts "Go to Café Noir on Oudegracht 12')
    assert sent.trigger_dispatch is True
    assert sent.source_hook == "research_check:message:after_response"
    assert sent.extra_content is not None
    assert sent.extra_content[ORIGINAL_SENDER_KEY] == "@user:localhost"
    assert harness.bound == [(f"{runtime_paths_for(_config(harness.tmp_path)).storage_root}:code", "research_check")]


@pytest.mark.asyncio
async def test_room_level_reply_follows_up_at_room_level(harness: _Harness) -> None:
    """A room-mode reply gets its follow-up in the room conversation, not a new thread."""
    envelope = _envelope(target=MessageTarget.resolve("!room:localhost", None, "$question", room_mode=True))

    await research_check.check_research(harness.context(envelope=envelope))

    assert [sent.thread_id for sent in harness.sent] == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize(("decision", "failure"), [(False, None), (None, None), (True, "timeout")])
async def test_false_abstain_and_failure_send_nothing(
    harness: _Harness,
    decision: bool | None,
    failure: JudgmentFailure | None,
) -> None:
    """Only a confident true decision sends a follow-up."""
    harness.decision = decision
    harness.failure = failure

    await research_check.check_research(harness.context())

    assert len(harness.requests) == 1
    assert harness.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "context_kwargs",
    [
        {"envelope": _dispatched_envelope("hook_dispatch", hook_source="research_check:message:after_response")},
        {"envelope": _dispatched_envelope("hook_dispatch", hook_source="automation/dreaming")},
        {"envelope": _dispatched_envelope("scheduled")},
        {"envelope": _dispatched_envelope("external_trigger")},
        {"response_kind": "team"},
        {"envelope": _envelope(body="Q3-results.pdf", attachment_ids=("att_q3",))},
        {"settings": _SETTINGS | {"agents": ["other"]}},
    ],
    ids=[
        "own-follow-up",
        "automation",
        "scheduled",
        "external-trigger",
        "team-reply",
        "attached-file",
        "filtered-agent",
    ],
)
async def test_skips_turns_no_person_asked_for_other_kinds_and_filtered_agents(
    harness: _Harness,
    context_kwargs: dict[str, Any],
) -> None:
    """Follow-up turns, automation turns, non-agent replies, and unlisted agents are never judged."""
    await research_check.check_research(harness.context(**context_kwargs))

    assert harness.bound == []
    assert harness.sent == []


@pytest.mark.asyncio
async def test_agents_that_hide_tool_calls_are_not_checked(harness: _Harness) -> None:
    """Hidden tool calls never reach the reply's trace, so the judge would wrongly see no lookups."""
    config = _config(harness.tmp_path)
    config.agents["code"].show_tool_calls = False

    await research_check.check_research(harness.context(config=config))

    assert harness.bound == []
    assert harness.sent == []


@pytest.mark.asyncio
async def test_missing_credential_skips(harness: _Harness) -> None:
    """A backend without a usable credential leaves the reply alone."""
    harness.credential = False

    await research_check.check_research(harness.context())

    assert harness.sent == []


@pytest.mark.asyncio
async def test_invalid_settings_raise(harness: _Harness) -> None:
    """Missing or unknown settings fail the hook loudly instead of silently never checking."""
    with pytest.raises(ValidationError):
        await research_check.check_research(harness.context(settings={}))
    with pytest.raises(ValidationError):
        await research_check.check_research(harness.context(settings=_SETTINGS | {"threshold": 0.5}))
    assert harness.sent == []


@pytest.mark.asyncio
async def test_unknown_llm_model_raises(harness: _Harness) -> None:
    """An llm judgment must name a configured model alias."""
    settings = {"judgment": {"provider": "llm", "model": "missing"}}

    with pytest.raises(ValueError, match="missing"):
        await research_check.check_research(harness.context(settings=settings))
    assert harness.bound == []


@pytest.mark.asyncio
async def test_request_lists_tool_calls_redacted_and_clipped(harness: _Harness) -> None:
    """The judge sees the question, every lookup (clipped and redacted), and the reply."""
    secret = "api_key=" + "Zx9" * 8
    trace = (
        ToolTraceEntry(
            type="tool_call_completed",
            tool_name="web_search",
            args_preview=f"query=coffee utrecht, {secret}",
            result_preview="x" * 1000,
        ),
        *(ToolTraceEntry(type="tool_call_completed", tool_name=f"tool_{index}") for index in range(40)),
    )

    await research_check.check_research(
        harness.context(tool_trace=trace, settings=_SETTINGS | {"instructions": "Be strict."}),
    )

    [request] = harness.requests
    question, tools, reply = _conversation(request)
    assert question == {"role": "user", "text": "Where can I get good coffee in Utrecht?"}
    assert reply == {"role": "assistant", "text": _REPLY}
    assert tools["role"] == "assistant"
    assert "web_search" in tools["text"]
    assert "query=coffee utrecht" in tools["text"]
    assert secret not in tools["text"]
    assert "x" * 600 not in tools["text"]
    assert "tool_39" in tools["text"]
    assert "more" not in tools["text"]
    assert request.body is not None
    assert json.loads(request.body)["guidance"] == "Be strict."


@pytest.mark.asyncio
async def test_judge_sees_the_whole_search_result_preview(harness: _Harness) -> None:
    """Every hit in MindRoom's 500-character result preview reaches the judge, so a supported reply is not flagged."""
    hits = (
        '[{"title": "Koffiebar Noir - Oudegracht 12, Utrecht", "body": "Specialty coffee bar on the Oudegracht, open daily 8:00-17:00."}, '
        '{"title": "Bocca Coffee Utrecht", "body": "Roastery and cafe at Ganzenmarkt 2, open 8:30-17:30."}]'
    )
    trace = (
        ToolTraceEntry(
            type="tool_call_completed",
            tool_name="duckduckgo_search",
            args_preview="query=coffee",
            result_preview=hits,
        ),
    )

    await research_check.check_research(harness.context(tool_trace=trace))

    [request] = harness.requests
    assert "Ganzenmarkt 2, open 8:30-17:30" in _conversation(request)[1]["text"]


@pytest.mark.asyncio
async def test_many_long_tool_calls_still_fit_one_request(harness: _Harness) -> None:
    """A tool-heavy reply is still judged; the tool list is shortened to fit instead of skipping the check."""
    trace = tuple(
        ToolTraceEntry(
            type="tool_call_completed",
            tool_name="web_fetch",
            args_preview=f"url=https://example.com/{index}/" + "a" * 1000,
            result_preview="r" * 1000,
        )
        for index in range(30)
    )

    await research_check.check_research(harness.context(tool_trace=trace, response_text="word " * 300))

    [request] = harness.requests
    tools = _conversation(request)[1]["text"]
    assert "example.com/0/" in tools
    assert tools.splitlines()[-1].endswith(
        "more tool calls not shown here, which may support claims the calls above do not",
    )


@pytest.mark.asyncio
async def test_multibyte_tool_calls_still_fit_one_request(harness: _Harness) -> None:
    """The tool list is budgeted in bytes, so non-Latin search results do not push the request over its limit."""
    trace = tuple(
        ToolTraceEntry(
            type="tool_call_completed",
            tool_name="web_search",
            args_preview="query=ラーメン",
            result_preview="東京" * 250,
        )
        for _ in range(12)
    )

    await research_check.check_research(
        harness.context(tool_trace=trace, response_text="ラーメン一蘭に行ってください。" * 50),
    )

    [request] = harness.requests
    assert "more tool calls not shown here" in _conversation(request)[1]["text"].splitlines()[-1]


@pytest.mark.asyncio
async def test_follow_up_quotes_the_reply_text_not_its_tool_markers(harness: _Harness) -> None:
    """The quote names the checked reply even if newer replies follow it, and skips the inline tool-call markers."""
    marker, entry = format_tool_combined("web_search", {"query": "coffee"}, "no results", tool_index=1)
    reply = f"{marker}\n\n{_REPLY}"

    await research_check.check_research(harness.context(response_text=reply, tool_trace=(entry,)))

    [sent] = harness.sent
    assert 'starts "Go to Café Noir on Oudegracht 12' in sent.body
    assert "web_search" not in sent.body


@pytest.mark.asyncio
async def test_follow_up_quote_mentions_no_one(harness: _Harness) -> None:
    """Mentions in the quoted reply would tag other agents or people in the follow-up."""
    reply = "Ask @researcher or @alice:example.org to double-check Café Noir."

    await research_check.check_research(harness.context(response_text=reply))

    [sent] = harness.sent
    assert sent.body.count("@") == 1
    assert sent.body.startswith("@code ")


@pytest.mark.asyncio
async def test_replies_to_other_agents_are_not_checked(harness: _Harness) -> None:
    """Only a person's request earns a follow-up; an agent asking another agent does not."""
    envelope = _envelope(
        origin=message_origin(
            sender_id="@mindroom_helper:localhost",
            requester_id="@mindroom_helper:localhost",
            sender_entity_name="helper",
            requester_entity_name="helper",
            source_kind="message",
        ),
    )

    await research_check.check_research(harness.context(envelope=envelope))

    assert harness.bound == []
    assert harness.sent == []


@pytest.mark.asyncio
async def test_failed_search_retry_is_still_judged(harness: _Harness) -> None:
    """A search tool's "provide an API key" error followed by a retry must not get the whole request refused."""
    trace = (
        ToolTraceEntry(
            type="tool_call_completed",
            tool_name="serpapi_search",
            args_preview="query=coffee",
            result_preview="Please provide an API key",
        ),
        ToolTraceEntry(
            type="tool_call_completed",
            tool_name="duckduckgo_search",
            args_preview="query=coffee",
            result_preview="No results found.",
        ),
    )

    await research_check.check_research(harness.context(tool_trace=trace))

    [request] = harness.requests
    assert "duckduckgo_search" in _conversation(request)[1]["text"]


@pytest.mark.asyncio
async def test_request_says_when_no_tools_ran(harness: _Harness) -> None:
    """A reply made without any tool call tells the judge so explicitly."""
    await research_check.check_research(harness.context())

    [request] = harness.requests
    assert _conversation(request)[1]["text"].endswith("none")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "context_kwargs",
    [{"envelope": _envelope(body="")}, {"response_text": "word " * 4000}],
    ids=["media-only", "oversized"],
)
async def test_media_only_and_oversized_inputs_skip(harness: _Harness, context_kwargs: dict[str, Any]) -> None:
    """Inputs the judgment request cannot carry whole are never sent to the backend."""
    await research_check.check_research(harness.context(**context_kwargs))

    assert harness.requests == []
    assert harness.sent == []


def test_plugin_loads_from_package_spec(tmp_path: Path) -> None:
    """Operators enable the bundled plugin with a python: spec."""
    config = _config(tmp_path, plugins=[{"path": "python:mindroom.research_check", "settings": _SETTINGS}])

    with isolated_plugin_runtime(config, runtime_paths_for(config)) as plugins:
        registry = HookRegistry.from_plugins(plugins)

    assert [plugin.name for plugin in plugins] == ["research_check"]
    assert [(hook.hook_name, hook.timeout_ms) for hook in registry.hooks_for(EVENT_MESSAGE_AFTER_RESPONSE)] == [
        ("check_research", 35_000),
    ]
