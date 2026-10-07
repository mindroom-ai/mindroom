"""Automation settings: shorthand, overrides, inheritance, and the validation that runs at config load."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.automations import DreamingAutomation, PluginAutomation
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig


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
    assert automation.max_content_loss == 0.05


def test_dreaming_runs_nightly_by_default() -> None:
    """`- dreaming` checks at 03:15 and posts in the agent's first room with the agent's model."""
    (automation,) = _config(mind=_mind(automations=["dreaming"])).resolve_entity("mind").automations

    assert automation.name == "dreaming"
    assert (automation.cron, automation.room, automation.model) == ("15 3 * * *", None, None)


def test_both_built_ins_can_run_for_one_agent() -> None:
    """Each entry parses into its own built-in by name."""
    entries = ["prompt_curation", {"name": "dreaming", "cron": "0 2 * * *"}]
    automations = _config(mind=_mind(automations=entries)).resolve_entity("mind").automations

    assert [(automation.name, automation.cron) for automation in automations] == [
        ("prompt_curation", "0 4 * * *"),
        ("dreaming", "0 2 * * *"),
    ]


def test_a_mapping_overrides_fields() -> None:
    """A mapping entry overrides only the fields it sets."""
    entry = {"name": "prompt_curation", "cron": "30 2 * * 1", "room": "personal", "trigger_tokens": 30_000}
    (automation,) = _config(mind=_mind(automations=[entry])).resolve_entity("mind").automations

    assert (automation.cron, automation.room, automation.trigger_tokens) == ("30 2 * * 1", "personal", 30_000)
    assert automation.max_content_loss == 0.05


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
        (
            AgentConfig(display_name="Mem0", memory_backend="mem0", automations=["dreaming"]),
            "memory_backend",
        ),
    ],
)
def test_an_agent_cannot_list_automations_it_cannot_run(agent: AgentConfig, message: str) -> None:
    """Unattended runs need a shared agent, and the built-ins need file memory."""
    with pytest.raises(ValidationError, match=message):
        _config(mind=agent)


@pytest.mark.parametrize(
    "entry",
    [
        "unknown_automation",
        {"name": "prompt_curation", "cron": "not a cron"},
        {"name": "prompt_curation", "cron": "0 0 4 * * *"},
        {"name": "prompt_curation", "cron": "0 0 31 2 *"},
        {"name": "prompt_curation", "min_reduction": 0.2, "max_reduction": 0.1},
        {"name": "prompt_curation", "min_reduction": 0.3, "max_reduction": 0.35},
        {"name": "prompt_curation", "unknown": 1},
        {"name": "dreaming", "trigger_tokens": 1},
        {"name": "dreaming", "cron": "0 0 31 2 *"},
        {"name": "Weekly-Digest", "cron": "0 9 * * 1"},
        {"name": "weekly_digest", "cron": "0 9 * * 1", "trigger_tokens": 1},
    ],
)
def test_invalid_entries_fail_config_load(entry: object) -> None:
    """A plugin name without a cron, bad cron, reversed bounds, a min_reduction above the per-file bound, and fields another built-in owns are rejected."""
    with pytest.raises(ValidationError):
        _mind(automations=[entry])


def test_a_built_in_is_listed_once() -> None:
    """The same built-in cannot run twice for one agent."""
    with pytest.raises(ValidationError, match="Duplicate"):
        _mind(automations=["prompt_curation", {"name": "prompt_curation", "cron": "0 5 * * *"}])


def test_an_automation_model_must_be_a_configured_model() -> None:
    """A model override names a key of models, both on an agent and in the defaults."""
    models = {"large": ModelConfig(provider="test", id="test-large")}
    entry = {"name": "prompt_curation", "model": "large"}
    (automation,) = (
        Config(agents={"mind": _mind(automations=[entry])}, models=models).resolve_entity("mind").automations
    )
    assert automation.model == "large"

    missing = {"name": "prompt_curation", "model": "missing"}
    with pytest.raises(ValidationError, match="unknown model 'missing'"):
        Config(agents={"mind": _mind(automations=[missing])}, models=models)
    with pytest.raises(ValidationError, match="unknown model 'missing'"):
        Config(defaults={"automations": [missing]}, agents={"mind": _mind()}, models=models)


def test_a_plugin_automation_parses_with_its_options() -> None:
    """Any other name is an automation a plugin provides; its options are kept for the plugin to read."""
    entry = {"name": "weekly_digest", "cron": "0 9 * * 1", "options": {"target": "digest.md"}}
    (automation,) = _config(mind=_mind(automations=[entry])).resolve_entity("mind").automations

    assert isinstance(automation, PluginAutomation)
    assert automation.options == {"target": "digest.md"}


def test_a_built_in_name_is_never_a_plugin_automation() -> None:
    """Built-in names stay with the built-ins, so an entry always matches one kind of automation."""
    with pytest.raises(ValidationError, match="built-in"):
        PluginAutomation(name="dreaming", cron="0 3 * * *")


def test_entries_given_as_models_keep_their_kind() -> None:
    """Code that builds a config from model instances gets each instance's own kind."""
    config = _config(
        mind=_mind(automations=[DreamingAutomation(), PluginAutomation(name="weekly_digest", cron="0 9 * * 1")]),
    )

    assert [type(entry) for entry in config.resolve_entity("mind").automations] == [
        DreamingAutomation,
        PluginAutomation,
    ]


def test_a_saved_config_loads_back_with_the_same_automations() -> None:
    """The dashboard saves the authored config, and loading it again gives the same entries."""
    config = _config(
        mind=_mind(automations=["dreaming", {"name": "weekly_digest", "cron": "0 9 * * 1", "options": {"a": 1}}]),
    )

    again = Config.model_validate(config.authored_model_dump())

    assert again.resolve_entity("mind").automations == config.resolve_entity("mind").automations


def test_inherited_defaults_are_filtered_entry_by_entry() -> None:
    """An agent without file memory still inherits the default automations that do not need it."""
    config = _config(
        ["dreaming", {"name": "weekly_digest", "cron": "0 9 * * 1"}],
        mind=_mind(),
        mem0=AgentConfig(display_name="Mem0", memory_backend="mem0"),
        private=_mind(private=AgentPrivateConfig(per="user")),
    )

    assert [entry.name for entry in config.resolve_entity("mind").automations] == ["dreaming", "weekly_digest"]
    assert [entry.name for entry in config.resolve_entity("mem0").automations] == ["weekly_digest"]
    assert config.resolve_entity("private").automations == []


def test_a_plugin_automation_can_be_listed_for_an_agent_without_file_memory() -> None:
    """Whether a plugin automation needs file memory is known once its plugin loads, so config load accepts it."""
    agent = AgentConfig(
        display_name="Mem0",
        memory_backend="mem0",
        automations=[{"name": "weekly_digest", "cron": "0 9 * * 1"}],
    )

    (automation,) = _config(mem0=agent).resolve_entity("mem0").automations

    assert automation.name == "weekly_digest"
