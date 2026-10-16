"""Knowledge graph configuration: the global section, the per-agent switch, and their validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig

_MODELS = {
    "default": ModelConfig(provider="openai", id="gpt-6-astra"),
    "luna": ModelConfig(provider="openai", id="gpt-6-luna"),
}


def _config(**overrides: object) -> Config:
    data: dict[str, object] = {
        "agents": {"mind": AgentConfig(display_name="Mind", knowledge_graph=True)},
        "models": _MODELS,
        "knowledge_graph": {"model": "luna"},
    }
    data.update(overrides)
    return Config(**data)


def test_agent_inherits_knowledge_graph_from_defaults() -> None:
    """An agent without its own setting follows defaults; its own setting wins."""
    config = Config(
        agents={
            "mind": AgentConfig(display_name="Mind"),
            "scratch": AgentConfig(display_name="Scratch", knowledge_graph=False),
        },
        defaults=DefaultsConfig(knowledge_graph=True),
        models=_MODELS,
        knowledge_graph={"model": "luna"},
    )
    assert config.agent_uses_knowledge_graph("mind")
    assert not config.agent_uses_knowledge_graph("scratch")
    assert config.knowledge_graph_agents() == ["mind"]


def test_knowledge_graph_is_off_by_default() -> None:
    """Without any setting no agent builds a graph, and no model is required."""
    config = Config(agents={"mind": AgentConfig(display_name="Mind")}, models=_MODELS)
    assert not config.agent_uses_knowledge_graph("mind")
    assert config.knowledge_graph_agents() == []


def test_enabled_graph_requires_a_model() -> None:
    """An enabled graph needs knowledge_graph.model."""
    with pytest.raises(ValidationError, match=r"knowledge_graph\.model"):
        _config(knowledge_graph={})


def test_enabled_graph_requires_a_known_model() -> None:
    """knowledge_graph.model must name a configured model."""
    with pytest.raises(ValidationError, match="Unknown knowledge_graph model"):
        _config(knowledge_graph={"model": "nope"})


def test_unknown_knowledge_graph_field_rejected() -> None:
    """Typos in the section fail config load."""
    with pytest.raises(ValidationError, match="extra"):
        _config(knowledge_graph={"model": "luna", "modle": "luna"})


def test_ingest_settings_have_documented_defaults() -> None:
    """Ingestion defaults match the documented values."""
    ingest = _config().knowledge_graph.ingest
    assert ingest.interval_seconds == 600
    assert ingest.idle_seconds == 120
    assert ingest.max_sessions_per_cycle == 10
    assert ingest.max_episode_chars == 24000
    assert ingest.max_tool_result_chars == 4000
    assert ingest.backfill_days == 7


def test_max_episode_chars_stays_below_the_redaction_limit() -> None:
    """Episodes are redacted whole, so their cap stays under the redaction input limit."""
    with pytest.raises(ValidationError, match="max_episode_chars"):
        _config(knowledge_graph={"model": "luna", "ingest": {"max_episode_chars": 70000}})


def test_tool_result_cap_stays_below_the_redaction_limit() -> None:
    """Tool results are redacted whole, so their cap stays under the redaction input limit."""
    with pytest.raises(ValidationError, match="max_tool_result_chars"):
        _config(knowledge_graph={"model": "luna", "ingest": {"max_tool_result_chars": 100_000}})


def test_one_tool_result_fits_in_an_episode() -> None:
    """A tool result is never cut further by the episode limit, so the episode must hold one twice over."""
    with pytest.raises(ValidationError, match="max_tool_result_chars"):
        _config(knowledge_graph={"model": "luna", "ingest": {"max_episode_chars": 2000, "max_tool_result_chars": 1500}})
