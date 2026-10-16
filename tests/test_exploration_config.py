"""The exploration automation needs the graph and has documented defaults."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mindroom.config.agent import AgentConfig
from mindroom.config.automations import ExplorationAutomation
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig

_MODELS = {
    "default": ModelConfig(provider="openai", id="gpt-6-astra"),
    "luna": ModelConfig(provider="openai", id="gpt-6-luna"),
}


def _config(
    *,
    knowledge_graph: bool = True,
    defaults: list[object] | None = None,
    **entry: object,
) -> Config:
    automations = None if defaults is not None else [{"name": "exploration", **entry}]
    return Config(
        agents={
            "mind": AgentConfig(
                display_name="Mind",
                knowledge_graph=knowledge_graph,
                tools=["duckduckgo"],
                automations=automations,
            ),
        },
        defaults={"automations": defaults or []},
        models=_MODELS,
        router=RouterConfig(model="default"),
        knowledge_graph={"model": "luna"},
    )


def test_exploration_has_documented_defaults() -> None:
    """A minimal entry runs nightly with bounded calls and leads."""
    [entry] = _config().get_agent("mind").automations or []
    assert isinstance(entry, ExplorationAutomation)
    assert entry.cron == "30 2 * * *"
    assert entry.max_tool_calls == 40
    assert entry.max_leads == 20
    assert entry.revisit_days == 7


def test_exploration_requires_the_knowledge_graph() -> None:
    """Exploration feeds the graph, so an agent without it cannot list it."""
    with pytest.raises(ValidationError, match="knowledge_graph"):
        _config(knowledge_graph=False)


def test_inherited_exploration_is_skipped_without_the_graph() -> None:
    """A default entry applies only to agents that build a graph."""
    config = _config(knowledge_graph=False, defaults=[{"name": "exploration"}])
    assert config.resolve_entity("mind").automations == []
