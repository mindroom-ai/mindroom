"""Cold-cache model downshift: configuration, cache expiry, and the judged switch."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from mindroom.config.judgment import LLMJudgmentConfig
from mindroom.config.main import Config

_MODELS: dict[str, dict[str, Any]] = {
    "opus": {"provider": "anthropic", "id": "claude-opus-5-5", "context_window": 200_000},
    "haiku": {"provider": "anthropic", "id": "claude-haiku-5-5", "context_window": 200_000},
    "tiny": {"provider": "anthropic", "id": "claude-tiny", "context_window": 32_000},
    "unsized": {"provider": "anthropic", "id": "claude-unsized"},
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
        ("opus", "unsized", True),
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
