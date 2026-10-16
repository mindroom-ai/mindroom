"""Cold-cache switch to the cheap model: configuration, idle threads, and the judged switch."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from mindroom.config.judgment import LLMJudgmentConfig
from mindroom.config.main import Config
from mindroom.judgment.answers import JudgmentResult
from mindroom.judgment.cold_cache import idle_seconds_before_turn, select_cold_cache_model
from mindroom.judgment.state import MAX_REQUEST_BYTES, JudgmentMessage
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.judgment.state import JudgmentRequest

_MODELS: dict[str, dict[str, Any]] = {
    "opus": {"provider": "anthropic", "id": "claude-opus-5-5", "context_window": 200_000},
    "haiku": {"provider": "anthropic", "id": "claude-haiku-5-5", "context_window": 200_000},
    "tiny": {"provider": "anthropic", "id": "claude-tiny", "context_window": 32_000},
    "unsized": {"provider": "anthropic", "id": "claude-unsized"},
    "unsized2": {"provider": "anthropic", "id": "claude-unsized-2"},
}


def _config(
    cold_cache: dict[str, Any] | None = None,
    *,
    agent_model: str = "opus",
    cheap_model: str | None = "haiku",
    default_cheap_model: str | None = None,
) -> Config:
    agent: dict[str, Any] = {"display_name": "Assistant", "model": agent_model}
    if cheap_model is not None:
        agent["cheap_model"] = cheap_model
    if cold_cache is not None:
        agent["cold_cache"] = cold_cache
    data: dict[str, Any] = {"models": _MODELS, "agents": {"assistant": agent}}
    if default_cheap_model is not None:
        data["defaults"] = {"cheap_model": default_cheap_model}
    return Config.model_validate(data)


def test_cold_cache_defaults_to_self_judging_cheap_model() -> None:
    """Without a judgment backend, the cheap model decides whether it can answer."""
    settings = _config({}).agents["assistant"].cold_cache
    assert settings is not None
    assert settings.effective_judgment("haiku") == LLMJudgmentConfig(provider="llm", model="haiku")


def test_cheap_model_rejects_unknown_model() -> None:
    """A misspelled cheap model fails configuration instead of silently never switching."""
    with pytest.raises(ValidationError, match="Unknown cheap_model for agent 'assistant'"):
        _config(cheap_model="nope")


def test_cold_cache_rejects_unknown_judgment_model() -> None:
    """The judge's model alias is validated like other judgment settings."""
    with pytest.raises(ValidationError, match="Unknown judgment model"):
        _config({"judgment": {"provider": "llm", "model": "nope"}})


def test_cheap_model_rejects_same_model() -> None:
    """Switching to the agent's own model would be a no-op that still pays for a judgment."""
    with pytest.raises(ValidationError, match="cheap_model must differ"):
        _config(cheap_model="opus")


def test_cold_cache_requires_cheap_model() -> None:
    """Cold-cache turns switch to the agent's cheap model, so one must be configured."""
    with pytest.raises(ValidationError, match="cold_cache needs a cheap_model"):
        _config({}, cheap_model=None)


def test_default_cheap_model_applies_to_agents_without_their_own() -> None:
    """One defaults.cheap_model serves every agent, and an agent's own cheap_model wins."""
    inherited = _config({}, cheap_model=None, default_cheap_model="haiku")
    assert inherited.get_agent_cheap_model("assistant") == "haiku"
    own = _config({}, cheap_model="haiku", default_cheap_model="unsized")
    assert own.get_agent_cheap_model("assistant") == "haiku"


def test_default_cheap_model_skips_the_agent_already_on_it() -> None:
    """An agent whose model is the default cheap model has no cheaper alternative."""
    config = _config(agent_model="haiku", cheap_model=None, default_cheap_model="haiku")
    assert config.get_agent_cheap_model("assistant") is None
    with pytest.raises(ValidationError, match="cold_cache needs a cheap_model"):
        _config({}, agent_model="haiku", cheap_model=None, default_cheap_model="haiku")


def test_default_cheap_model_is_validated() -> None:
    """A misspelled default fails configuration, and an inherited default must fit the agent's window."""
    with pytest.raises(ValidationError, match=r"Unknown defaults\.cheap_model"):
        _config(cheap_model=None, default_cheap_model="nope")
    with pytest.raises(ValidationError, match="context window"):
        _config({}, cheap_model=None, default_cheap_model="tiny")
    # Unused by an agent without cold_cache, an ill-fitting default is harmless.
    assert _config(cheap_model=None, default_cheap_model="tiny").get_agent_cheap_model("assistant") == "tiny"


@pytest.mark.parametrize(
    ("agent_model", "cheap_model", "accepted"),
    [
        ("opus", "tiny", False),
        ("opus", "unsized", False),
        ("unsized", "unsized2", True),
        ("unsized", "haiku", False),
        ("unsized", "opus", False),
    ],
)
def test_cheap_model_context_window_must_not_shrink(agent_model: str, cheap_model: str, accepted: bool) -> None:
    """A smaller window would compact the thread and shrink the agent model's later context."""
    if accepted:
        assert _config(agent_model=agent_model, cheap_model=cheap_model).agents["assistant"].cheap_model == cheap_model
        return
    with pytest.raises(ValidationError, match="context window"):
        _config(agent_model=agent_model, cheap_model=cheap_model)


_NOW = 1_800_000_000.0


_AGENT = "@mindroom_assistant:localhost"


def _message(
    event_id: str,
    sender: str,
    seconds_ago: float,
    *,
    edited_seconds_ago: float | None = None,
) -> ResolvedVisibleMessage:
    message = ResolvedVisibleMessage.synthetic(
        sender=sender,
        body=event_id,
        event_id=event_id,
        timestamp=int((_NOW - seconds_ago) * 1000),
    )
    if edited_seconds_ago is None:
        return message
    return replace(message, edited_timestamp=int((_NOW - edited_seconds_ago) * 1000))


def _idle(*history: ResolvedVisibleMessage) -> float | None:
    return idle_seconds_before_turn(history, agent_user_id=_AGENT, source_event_ids=("$source",), now=_NOW)


def test_idle_counts_from_the_agents_last_reply_before_the_turn() -> None:
    """The newest reply's final edit marks the last cache write; later messages are this turn's."""
    assert (
        _idle(
            _message("$root", "@alice:localhost", 9000),
            _message("$old", _AGENT, 8000),
            _message("$reply", _AGENT, 7300, edited_seconds_ago=7000),
            _message("$other", "@alice:localhost", 100),
            _message("$source", "@alice:localhost", 5),
            _message("$placeholder", _AGENT, 1),
        )
        == 7000
    )


def test_idle_is_unknown_until_the_agent_replied() -> None:
    """A new thread, or one the agent never answered, has no cache to have gone cold."""
    assert _idle(_message("$root", "@alice:localhost", 60), _message("$source", "@alice:localhost", 5)) is None
    assert _idle(_message("$source", "@alice:localhost", 5), _message("$late", _AGENT, 1)) is None


class _FakeJudge:
    def __init__(self, decision: bool | None = True, failure: str | None = None) -> None:
        self.decision = decision
        self.failure = failure
        self.requests: list[JudgmentRequest] = []

    async def __call__(self, request: JudgmentRequest) -> JudgmentResult[bool]:
        self.requests.append(request)
        return JudgmentResult(
            decision=self.decision,
            probability=None,
            failure=self.failure,
            model_id="judge",
            latency_ms=1,
            input_tokens=None,
            output_tokens=None,
            state_bytes=request.state_bytes,
        )


async def _select(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    judge: _FakeJudge | None,
    *,
    idle_seconds: float | None = 7200,
    context: tuple[JudgmentMessage, ...] | None = (),
    prompt: str = "What time is it in Tokyo?",
    cold_cache: dict[str, Any] | None = None,
) -> str:
    config = _config(cold_cache or {})
    monkeypatch.setattr("mindroom.judgment.cold_cache.create_judgment_evaluator", lambda *_args, **_kwargs: judge)
    settings = config.agents["assistant"].cold_cache
    assert settings is not None
    return await select_cold_cache_model(
        settings,
        config=config,
        runtime_paths=test_runtime_paths(tmp_path),
        agent_name="assistant",
        agent_model="opus",
        cheap_model="haiku",
        idle_seconds=idle_seconds,
        context=context,
        prompt=prompt,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("idle_seconds", "cold_cache", "reason"),
    [(None, {}, "no_earlier_reply"), (3599, {}, "warm"), (600, {"idle_seconds": 900}, "warm")],
)
async def test_select_without_a_cold_cache_skips_judgment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    idle_seconds: float | None,
    cold_cache: dict[str, Any],
    reason: str,
) -> None:
    """A warm cache is cheaper than any cold write, and a new thread is not judged on its first request."""
    judge = _FakeJudge()
    with capture_logs() as logs:
        chosen = await _select(tmp_path, monkeypatch, judge, idle_seconds=idle_seconds, cold_cache=cold_cache)
    assert chosen == "opus"
    assert judge.requests == []
    assert [log["reason"] for log in logs if log["event"] == "Cold cache model selected"] == [reason]


@pytest.mark.asyncio
async def test_select_cold_accepts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An accepting judge moves a cold turn to the cheap model, judging the whole conversation and the request."""
    judge = _FakeJudge(decision=True)
    context = tuple(JudgmentMessage("user" if index % 2 else "assistant", f"turn {index}") for index in range(40))
    with capture_logs() as logs:
        chosen = await _select(tmp_path, monkeypatch, judge, context=context)
    assert chosen == "haiku"
    body = judge.requests[0].body
    assert body is not None
    payload = json.loads(body)
    assert "cheaper model claude-haiku-5-5" in payload["question"]["instructions"]
    assert "main model claude-opus-5-5" in payload["question"]["instructions"]
    assert "conversation so far and the current request together" in payload["question"]["instructions"]
    assert "even when the current request alone looks simple" in payload["question"]["criteria"]["false"]
    conversation = payload["state"]["conversation"]
    assert [message["text"] for message in conversation] == [
        *(f"turn {index}" for index in range(40)),
        "What time is it in Tokyo?",
    ]
    selected = [log for log in logs if log["event"] == "Cold cache model selected"]
    assert selected == [
        {
            "event": "Cold cache model selected",
            "log_level": "info",
            "agent": "assistant",
            "agent_model": "opus",
            "chosen_model": "haiku",
            "idle_seconds": 7200,
            "decision": True,
            "reason": "judged",
        },
    ]


@pytest.mark.asyncio
async def test_select_logs_why_it_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Operators can see why a cold-cache agent kept its model, for example when the thread history is unavailable."""
    with capture_logs() as logs:
        await _select(tmp_path, monkeypatch, _FakeJudge(), context=None)
    selected = [log for log in logs if log["event"] == "Cold cache model selected"]
    assert [(log["chosen_model"], log["decision"], log["reason"]) for log in selected] == [
        ("opus", None, "history_unavailable"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("decision", "failure"), [(False, None), (None, None), (None, "timeout")])
async def test_select_cold_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision: bool | None,
    failure: str | None,
) -> None:
    """Anything short of an accepting judgment keeps the agent model."""
    assert await _select(tmp_path, monkeypatch, _FakeJudge(decision, failure)) == "opus"


@pytest.mark.asyncio
async def test_select_without_context_or_backend_keeps_agent_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unavailable history, a missing credential, or an oversize request never switch models."""
    judge = _FakeJudge()
    assert await _select(tmp_path, monkeypatch, judge, context=None) == "opus"
    assert judge.requests == []
    typesafe = {"judgment": {"provider": "typesafe"}}
    assert await _select(tmp_path, monkeypatch, None, cold_cache=typesafe) == "opus"
    assert await _select(tmp_path, monkeypatch, judge, prompt="x" * MAX_REQUEST_BYTES) == "opus"
    assert judge.requests == []
