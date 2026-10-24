"""Tests for personal agent templates, the roster store, and pure expansion."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from mindroom.config.personal_agents import (
    PersonalAgentInstance,
    PersonalAgentRosterEntry,
    PersonalAgentTemplateConfig,
    append_personal_agent_roster_entry,
    expand_personal_agents,
    personal_agent_data,
    personal_agent_display_name,
    personal_agent_roster_lock_path,
    personal_agent_roster_path,
    personal_agent_username,
    read_personal_agent_roster,
)
from mindroom.constants import tracking_dir
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _entry(user_id: str = "@Alice.Smith:example.org", **kwargs: object) -> PersonalAgentRosterEntry:
    username = personal_agent_username(user_id)
    fields: dict[str, object] = {
        "template": "mind",
        "user_id": user_id,
        "username": username,
        "agent_name": f"mind_{username}",
        "matrix_username": None,
        "user_display_name": "Alice",
        "source_room_id": "!lobby:example.org",
    }
    return PersonalAgentRosterEntry.model_validate(fields | kwargs)


def test_username_slug() -> None:
    """The username slug keeps lowercase word characters, cuts to 40, and never ends up empty."""
    assert personal_agent_username("@Alice.Smith:example.org") == "alice_smith"
    assert personal_agent_username("@=/:example.org") == "__"
    assert personal_agent_username("@" + "a" * 60 + ":x") == "a" * 40
    assert personal_agent_username("@:x") == "user"


def test_display_name_fallback_and_sanitizing() -> None:
    """The display name drops control characters, is cut to 100, and falls back to the localpart."""
    assert personal_agent_display_name("@alice:x", None) == "alice"
    assert personal_agent_display_name("@alice:x", "\n") == "alice"
    assert personal_agent_display_name("@alice:x", "Al\nice" + "x" * 200) == ("Alice" + "x" * 200)[:100]


def test_generated_agent_renders_placeholders_and_forces_owner_access() -> None:
    """Placeholders render by exact token replacement and the owner always gets access."""
    template = {
        "agent": {
            "display_name": "{user_display_name}'s Mind",
            "role": "Helps {user} as {username}. {json}",
            "instructions": ["Owner: {user}"],
        },
    }
    agent = personal_agent_data(template, _entry())
    assert agent["display_name"] == "Alice's Mind"
    assert agent["role"] == "Helps @Alice.Smith:example.org as alice_smith. {json}"
    assert agent["instructions"] == ["Owner: @Alice.Smith:example.org"]
    assert agent["access"] == {"members_of_rooms": [], "users": ["@Alice.Smith:example.org"]}
    assert agent["credential_managers"] == ["@Alice.Smith:example.org"]


def test_placeholder_values_are_not_rendered_again() -> None:
    """A placeholder inside a substituted value stays literal."""
    template = {"agent": {"display_name": "{user_display_name} {username}"}}
    agent = personal_agent_data(template, _entry(user_display_name="{username}"))
    assert agent["display_name"] == "{username} alice_smith"


def test_override_replaces_top_level_fields_and_keeps_owner() -> None:
    """A user override replaces template fields whole but cannot lock the owner out."""
    template = {
        "agent": {"display_name": "Mind", "tools": ["file"], "access": {"members_of_rooms": ["lobby"]}},
        "users": {"@Alice.Smith:example.org": {"tools": ["shell"], "access": {"users": ["@bob:example.org"]}}},
    }
    agent = personal_agent_data(template, _entry())
    assert agent["tools"] == ["shell"]
    assert agent["access"] == {"users": ["@bob:example.org", "@Alice.Smith:example.org"], "members_of_rooms": []}


def test_generated_agent_does_not_alias_the_template() -> None:
    """Mutating a generated agent leaves the template untouched."""
    template = {"agent": {"display_name": "Mind", "tools": ["file"]}}
    personal_agent_data(template, _entry())["tools"].append("shell")
    assert template["agent"]["tools"] == ["file"]


def test_expansion_adds_agents_instances_and_probes_only_missing_users() -> None:
    """Expansion adds roster agents and probes only templates or overrides without an agent."""
    data = {
        "agents": {"other": {"display_name": "Other"}},
        "personal_agents": {
            "mind": {"agent": {"display_name": "Mind"}, "users": {"@carol:example.org": {}}},
            "empty": {"agent": {"display_name": "Empty"}},
        },
    }
    expansion = expand_personal_agents(data, [_entry()])
    assert set(expansion.data["agents"]) == {"other", "mind_alice_smith"}
    assert expansion.instances == {"mind_alice_smith": PersonalAgentInstance("mind", "@Alice.Smith:example.org", None)}
    probe_agents = expansion.probe_data["agents"]
    assert {"other", "mind_alice_smith"} < set(probe_agents)
    assert len(probe_agents) == 4  # one probe for "empty", one for @carol
    assert "mind_alice_smith" not in data["agents"]  # input not mutated


def test_expansion_without_missing_users_has_no_probe() -> None:
    """Expansion builds no probe copy when every template and override has an agent."""
    data = {"personal_agents": {"mind": {"agent": {"display_name": "Mind"}}}}
    assert expand_personal_agents(data, [_entry()]).probe_data is None


def test_expansion_rejects_authored_agent_with_generated_name() -> None:
    """An authored agent that takes a generated name fails with the template and owner named."""
    data = {
        "agents": {"mind_alice_smith": {"display_name": "Mine"}},
        "personal_agents": {"mind": {"agent": {"display_name": "Mind"}}},
    }
    with pytest.raises(
        ValueError,
        match=r"agents\.mind_alice_smith .*personal_agents\.mind.*@Alice\.Smith:example\.org",
    ):
        expand_personal_agents(data, [_entry()])


def test_expansion_ignores_roster_entries_of_removed_templates() -> None:
    """Roster entries of a removed template generate nothing."""
    assert expand_personal_agents({"agents": {}}, [_entry()]).instances == {}


def test_expansion_leaves_malformed_sections_for_pydantic() -> None:
    """Malformed template sections pass through unchanged for the schema to report."""
    for data in ("nope", {"personal_agents": []}, {"personal_agents": {"mind": {"agent": "x"}}}):
        expansion = expand_personal_agents(data, [_entry()])
        assert expansion.data is data
        assert expansion.instances == {}
        assert expansion.probe_data is None


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("name", "mind", r"\{username\}"),
        ("name", "mind-{username}", "letters, digits"),
        ("matrix_username", "{username}", "other character"),
        ("matrix_username", "{username}.MIND", "localpart"),
        ("users", {"bob": {}}, "concrete Matrix user IDs"),
        ("users", {"@bob:x": {"nonsense": 1}}, "nonsense"),
        ("users", {"@bob:x": {"private": {"per": "user"}}}, "private"),
        ("users", {"@bob:x": {"allow_self_config": True}}, "allow_self_config"),
    ],
)
def test_template_validation(field: str, value: object, match: str) -> None:
    """Template fields reject names, accounts, and overrides that cannot work."""
    with pytest.raises(ValidationError, match=match):
        PersonalAgentTemplateConfig.model_validate({"agent": {"display_name": "Mind"}, field: value})


def test_template_accepts_custom_names_and_name_pattern() -> None:
    """Custom name patterns validate and the default pattern uses the template key."""
    template = PersonalAgentTemplateConfig.model_validate(
        {"name": "{username}_mind", "matrix_username": "{username}.mind", "agent": {"display_name": "Mind"}},
    )
    assert template.name_pattern("mind") == "{username}_mind"
    assert PersonalAgentTemplateConfig.model_validate({"agent": {"display_name": "Mind"}}).name_pattern("mind") == (
        "mind_{username}"
    )


def test_template_rejects_private_and_self_config_agent() -> None:
    """A template agent cannot be private or self-configurable."""
    for extra in ({"private": {"per": "user"}}, {"allow_self_config": True}):
        with pytest.raises(ValidationError):
            PersonalAgentTemplateConfig.model_validate({"agent": {"display_name": "Mind", **extra}})


def test_roster_round_trip(tmp_path: Path) -> None:
    """The roster is empty before the first entry and reads back what was appended."""
    paths = test_runtime_paths(tmp_path)
    assert read_personal_agent_roster(paths) == []
    append_personal_agent_roster_entry(paths, _entry())
    assert read_personal_agent_roster(paths) == [_entry()]
    assert personal_agent_roster_path(paths) == tracking_dir(paths) / "personal_agents.json"
    assert personal_agent_roster_lock_path(paths) == tracking_dir(paths) / "personal_agents.lock"


def test_roster_append_keeps_earlier_entries(tmp_path: Path) -> None:
    """Appending rewrites the roster with earlier entries intact."""
    paths = test_runtime_paths(tmp_path)
    first, second = _entry(), _entry("@bob:example.org")
    append_personal_agent_roster_entry(paths, first)
    append_personal_agent_roster_entry(paths, second)
    assert read_personal_agent_roster(paths) == [first, second]
