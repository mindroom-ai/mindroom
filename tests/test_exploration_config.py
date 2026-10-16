"""The exploration automation: it needs the graph, names only the agent's own tools, and never ones that act beyond it."""

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
    agent_tools: list[str] | None = None,
    defaults: list[object] | None = None,
    **entry: object,
) -> Config:
    automations = None if defaults is not None else [{"name": "exploration", "tools": ["duckduckgo"], **entry}]
    return Config(
        agents={
            "mind": AgentConfig(
                display_name="Mind",
                knowledge_graph=knowledge_graph,
                tools=agent_tools if agent_tools is not None else ["duckduckgo", "github"],
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
    config = _config(knowledge_graph=False, defaults=[{"name": "exploration", "tools": ["duckduckgo"]}])
    assert config.resolve_entity("mind").automations == []


def test_exploration_requires_a_tool_list() -> None:
    """The operator names what exploration may call; nothing is allowed by default."""
    with pytest.raises(ValidationError, match="tools"):
        _config(tools=[])


def test_exploration_tools_must_be_the_agents_own() -> None:
    """Exploration calls only toolkits the agent already has, or functions of them."""
    with pytest.raises(ValidationError, match="slack"):
        _config(tools=["slack"])
    config = _config(tools=["github.get_repository", "knowledge_graph"])
    assert config.agent_exploration("mind") is not None


@pytest.mark.parametrize("toolkit", ["scheduler", "delegate.delegate_task", "matrix_message", "self_config"])
def test_exploration_rejects_toolkits_that_act_beyond_it(toolkit: str) -> None:
    """Tools that start other runs or change the agent would act outside the allowlist."""
    with pytest.raises(ValidationError, match="cannot be used by exploration"):
        ExplorationAutomation(tools=[toolkit])


def test_agent_exploration_returns_the_entry() -> None:
    """The turn policy finds an agent's exploration settings by name."""
    entry = _config(max_tool_calls=5).agent_exploration("mind")
    assert entry is not None
    assert entry.max_tool_calls == 5
