"""Automation settings: shorthand, overrides, inheritance, and the validation that runs at config load."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig


def _config(defaults: list[object] | None = None, **agents: AgentConfig) -> Config:
    return Config(
        defaults={"automations": defaults} if defaults is not None else {},
        agents=agents,
        router=RouterConfig(model="default"),
    )


def _mind(**fields: object) -> AgentConfig:
    return AgentConfig(display_name="Mind", memory_backend="file", **fields)


def test_a_bare_name_enables_a_built_in_with_its_defaults() -> None:
    """`- prompt_curation` is shorthand for the built-in with every default."""
    (automation,) = _config(mind=_mind(automations=["prompt_curation"])).resolve_entity("mind").automations

    assert automation.name == "prompt_curation"
    assert automation.cron == "0 4 * * *"
    assert automation.room is None
    assert automation.trigger_tokens == 50_000
    assert (automation.min_reduction, automation.max_reduction) == (0.10, 0.15)
    assert automation.max_file_shrink == 0.25
    assert automation.max_content_loss == 0.05
    assert automation.protected_files == []


def test_a_mapping_overrides_fields() -> None:
    """A mapping entry overrides only the fields it sets."""
    entry = {"name": "prompt_curation", "cron": "30 2 * * 1", "room": "personal", "trigger_tokens": 30_000}
    (automation,) = _config(mind=_mind(automations=[entry])).resolve_entity("mind").automations

    assert (automation.cron, automation.room, automation.trigger_tokens) == ("30 2 * * 1", "personal", 30_000)
    assert automation.max_file_shrink == 0.25


def test_eligible_agents_inherit_default_automations() -> None:
    """Defaults reach file-memory shared agents; private and non-file agents are skipped, and [] opts out."""
    config = _config(
        ["prompt_curation"],
        mind=_mind(),
        opted_out=_mind(automations=[]),
        mem0=AgentConfig(display_name="Mem0", memory_backend="mem0"),
        private=_mind(private=AgentPrivateConfig(per="user")),
    )

    assert [automation.name for automation in config.resolve_entity("mind").automations] == ["prompt_curation"]
    for agent_name in ("opted_out", "mem0", "private"):
        assert config.resolve_entity(agent_name).automations == []


@pytest.mark.parametrize(
    ("agent", "message"),
    [
        (_mind(private=AgentPrivateConfig(per="user"), automations=["prompt_curation"]), "private"),
        (AgentConfig(display_name="Mem0", memory_backend="mem0", automations=["prompt_curation"]), "memory_backend"),
    ],
)
def test_an_agent_cannot_list_automations_it_cannot_run(agent: AgentConfig, message: str) -> None:
    """Unattended runs need a shared agent, and prompt curation needs file memory."""
    with pytest.raises(ValidationError, match=message):
        _config(mind=agent)


@pytest.mark.parametrize(
    "entry",
    [
        "unknown_automation",
        {"name": "prompt_curation", "cron": "not a cron"},
        {"name": "prompt_curation", "min_reduction": 0.2, "max_reduction": 0.1},
        {"name": "prompt_curation", "protected_files": ["../SOUL.md"]},
        {"name": "prompt_curation", "unknown": 1},
    ],
)
def test_invalid_entries_fail_config_load(entry: object) -> None:
    """Unknown built-ins, bad cron, reversed bounds, escaping paths, and unknown fields are rejected."""
    with pytest.raises(ValidationError):
        _mind(automations=[entry])


def test_a_built_in_is_listed_once() -> None:
    """The same built-in cannot run twice for one agent."""
    with pytest.raises(ValidationError, match="Duplicate"):
        _mind(automations=["prompt_curation", {"name": "prompt_curation", "cron": "0 5 * * *"}])
