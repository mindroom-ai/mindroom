"""Cold-cache model downshift: configuration, cache expiry, and the judged switch."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession
from pydantic import ValidationError
from structlog.testing import capture_logs

from mindroom.cold_cache import _is_thread_cache_warm, _prompt_cache_ttl_seconds, select_cold_cache_model
from mindroom.config.judgment import LLMJudgmentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.judgment.answers import JudgmentResult
from mindroom.judgment.state import MAX_REQUEST_BYTES, JudgmentMessage
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


def _config(cold_cache: dict[str, Any], *, agent_model: str = "opus") -> Config:
    return Config.model_validate(
        {
            "models": _MODELS,
            "agents": {"assistant": {"display_name": "Assistant", "model": agent_model, "cold_cache": cold_cache}},
        },
    )


def test_cold_cache_defaults_to_self_judging_llm() -> None:
    """Without a judgment backend, the smaller model decides whether it can answer."""
    settings = _config({"model": "haiku"}).agents["assistant"].cold_cache
    assert settings is not None
    assert settings.effective_judgment() == LLMJudgmentConfig(provider="llm", model="haiku")


def test_cold_cache_rejects_unknown_model() -> None:
    """A misspelled smaller model fails configuration instead of silently never switching."""
    with pytest.raises(ValidationError, match="Unknown cold_cache model for agent 'assistant'"):
        _config({"model": "nope"})


def test_cold_cache_rejects_unknown_judgment_model() -> None:
    """The judge's model alias is validated like other judgment settings."""
    with pytest.raises(ValidationError, match="Unknown judgment model"):
        _config({"model": "haiku", "judgment": {"provider": "llm", "model": "nope"}})


def test_cold_cache_rejects_same_model() -> None:
    """Switching to the agent's own model would be a no-op that still pays for a judgment."""
    with pytest.raises(ValidationError, match="cold_cache model must differ"):
        _config({"model": "opus"})


@pytest.mark.parametrize(
    ("agent_model", "cold_model", "accepted"),
    [
        ("opus", "tiny", False),
        ("opus", "unsized", False),
        ("unsized", "unsized2", True),
        ("unsized", "haiku", False),
        ("unsized", "opus", False),
    ],
)
def test_cold_cache_context_window_must_not_shrink(agent_model: str, cold_model: str, accepted: bool) -> None:
    """A smaller window would compact the thread and shrink the agent model's later context."""
    if accepted:
        assert _config({"model": cold_model}, agent_model=agent_model).agents["assistant"].cold_cache is not None
        return
    with pytest.raises(ValidationError, match="context window"):
        _config({"model": cold_model}, agent_model=agent_model)


_NOW = 1_800_000_000.0


def _model(**overrides: object) -> ModelConfig:
    return ModelConfig.model_validate({**_MODELS["opus"], **overrides})


def _session(*runs: tuple[str | None, float], updated_at: float | None) -> AgentSession:
    return AgentSession(
        session_id="session",
        runs=[RunOutput(model=model, created_at=int(created_at)) for model, created_at in runs],
        updated_at=None if updated_at is None else int(updated_at),
    )


def test_ttl_claude_default_is_one_hour() -> None:
    """MindRoom enables Anthropic's 1h cache TTL for Claude providers by default."""
    assert _prompt_cache_ttl_seconds(_model()) == 3600


def test_ttl_claude_without_extended_cache_is_five_minutes() -> None:
    """Turning off the extended cache leaves Anthropic's 5m TTL."""
    assert _prompt_cache_ttl_seconds(_model(extra_kwargs={"extended_cache_time": False})) == 300


def test_ttl_other_providers_is_one_hour() -> None:
    """Providers without an exact TTL use OpenAI's documented upper bound, erring toward warm."""
    assert _prompt_cache_ttl_seconds(_model(provider="openai", id="gpt-6.1")) == 3600


@pytest.mark.parametrize(
    ("session", "warm"),
    [
        (None, False),
        (_session(updated_at=_NOW - 10), False),
        (_session(("claude-haiku-5-5", _NOW - 20), updated_at=_NOW - 10), False),
        (_session(("claude-opus-5-5", _NOW - 3700), updated_at=_NOW - 3599), True),
        (_session(("claude-opus-5-5", _NOW - 3700), updated_at=_NOW - 3601), False),
        (_session(("claude-opus-5-5", _NOW - 3700), updated_at=None), True),
        (_session(("claude-opus-5-5", _NOW - 20), ("claude-haiku-5-5", _NOW - 30), updated_at=_NOW - 10), True),
        (_session(("claude-opus-5-5", _NOW - 40), (None, _NOW - 10), updated_at=_NOW - 5), True),
        (_session(("claude-opus-5-5", _NOW - 7300), (None, _NOW - 7250), updated_at=_NOW - 7200), False),
    ],
)
def test_warmth(session: AgentSession | None, warm: bool) -> None:
    """Only the agent model serving the newest run within the TTL keeps the cache warm."""
    assert _is_thread_cache_warm(session, _model(), now=_NOW) is warm


@pytest.mark.parametrize(
    ("provider", "configured_id", "stored_id"),
    [("codex", "openai-codex/gpt-5.6", "gpt-5.6-sol"), ("kimi", "kimi-code/kimi-k3", "kimi-k3")],
)
def test_warmth_matches_rewritten_model_ids(provider: str, configured_id: str, stored_id: str) -> None:
    """Codex and Kimi store the endpoint id they rewrite, which must still count as the agent model."""
    session = _session((stored_id, _NOW - 20), updated_at=_NOW - 10)
    assert _is_thread_cache_warm(session, _model(provider=provider, id=configured_id), now=_NOW)


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
    session: AgentSession | None = None,
    context: tuple[JudgmentMessage, ...] | None = (),
    prompt: str = "What time is it in Tokyo?",
    cold_cache: dict[str, Any] | None = None,
) -> str:
    config = _config(cold_cache or {"model": "haiku"})
    monkeypatch.setattr("mindroom.cold_cache.create_judgment_evaluator", lambda *_args, **_kwargs: judge)
    settings = config.agents["assistant"].cold_cache
    assert settings is not None
    return await select_cold_cache_model(
        settings,
        config=config,
        runtime_paths=test_runtime_paths(tmp_path),
        agent_name="assistant",
        agent_model="opus",
        session=session,
        context=context,
        prompt=prompt,
        now=_NOW,
    )


@pytest.mark.asyncio
async def test_select_warm_skips_judgment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A warm agent-model cache is cheaper than any cold write, so no judgment runs."""
    judge = _FakeJudge()
    warm = _session(("claude-opus-5-5", _NOW - 70), updated_at=_NOW - 60)
    assert await _select(tmp_path, monkeypatch, judge, session=warm) == "opus"
    assert judge.requests == []


@pytest.mark.asyncio
async def test_select_cold_accepts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An accepting judge moves a cold turn to the smaller model, judging recent context and the request."""
    judge = _FakeJudge(decision=True)
    context = tuple(JudgmentMessage("user" if index % 2 else "assistant", f"turn {index}") for index in range(12))
    with capture_logs() as logs:
        chosen = await _select(tmp_path, monkeypatch, judge, session=None, context=context)
    assert chosen == "haiku"
    body = judge.requests[0].body
    assert body is not None
    conversation = json.loads(body)["state"]["conversation"]
    assert [message["text"] for message in conversation] == [
        *(f"turn {index}" for index in range(5, 12)),
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
            "cache_age_seconds": None,
            "decision": True,
            "reason": "judged",
        },
    ]


@pytest.mark.asyncio
async def test_select_logs_why_it_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Operators can see why a cold-cache agent kept its model, for example in room mode."""
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
    stale = _session(("claude-opus-5-5", _NOW - 7300), updated_at=_NOW - 7200)
    assert await _select(tmp_path, monkeypatch, _FakeJudge(decision, failure), session=stale) == "opus"


@pytest.mark.asyncio
async def test_select_without_context_or_backend_keeps_agent_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unavailable history, a missing credential, or an oversize request never switch models."""
    judge = _FakeJudge()
    assert await _select(tmp_path, monkeypatch, judge, context=None) == "opus"
    assert judge.requests == []
    typesafe = {"model": "haiku", "judgment": {"provider": "typesafe"}}
    assert await _select(tmp_path, monkeypatch, None, cold_cache=typesafe) == "opus"
    assert await _select(tmp_path, monkeypatch, judge, prompt="x" * MAX_REQUEST_BYTES) == "opus"
    assert judge.requests == []
