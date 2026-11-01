"""Tests for personal agent templates, the roster store, and pure expansion."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from mindroom.approval.tool_owners import _catalog_authority
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.config.personal_agents import (
    PersonalAgentInstance,
    PersonalAgentRosterEntry,
    PersonalAgentTemplateConfig,
    expand_personal_agents,
    personal_agent_data,
    personal_agent_display_name,
    personal_agent_roster_lock_path,
    personal_agent_roster_path,
    personal_agent_username,
    read_personal_agent_roster,
)
from mindroom.constants import tracking_dir
from mindroom.personal_agents import _append_personal_agent_roster_entry
from mindroom.tool_system.sandbox_proxy import runner_config_snapshot
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


def test_first_name_placeholder_is_the_display_names_first_word() -> None:
    """`{user_first_name}` is the display name's first word, or the localpart for a blank display name."""
    template = {"agent": {"display_name": "{user_first_name}'s Mind"}}
    assert personal_agent_data(template, _entry(user_display_name="Bas Nijholt"))["display_name"] == "Bas's Mind"
    assert personal_agent_data(template, _entry(user_display_name=" \u00a0"))["display_name"] == "Alice.Smith's Mind"


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
        ("users", {"@bob:x": {"allow_self_config": "true"}}, "allow_self_config"),
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
    assert template.agent_name("mind", "alice") == "alice_mind"
    assert template.rendered_matrix_username("alice") == "alice.mind"
    default = PersonalAgentTemplateConfig.model_validate({"agent": {"display_name": "Mind"}})
    assert default.agent_name("mind", "alice") == "mind_alice"
    assert default.rendered_matrix_username("alice") is None


def test_template_rejects_private_and_self_config_agent() -> None:
    """A template agent cannot be private or self-configurable."""
    for extra in ({"private": {"per": "user"}}, {"allow_self_config": True}):
        with pytest.raises(ValidationError):
            PersonalAgentTemplateConfig.model_validate({"agent": {"display_name": "Mind", **extra}})


def test_roster_round_trip(tmp_path: Path) -> None:
    """The roster is empty before the first entry and reads back what was appended."""
    paths = test_runtime_paths(tmp_path)
    assert read_personal_agent_roster(paths) == []
    _append_personal_agent_roster_entry(paths, _entry())
    assert read_personal_agent_roster(paths) == [_entry()]
    assert personal_agent_roster_path(paths) == tracking_dir(paths) / "personal_agents.json"
    assert personal_agent_roster_lock_path(paths) == tracking_dir(paths) / "personal_agents.lock"


def test_roster_append_keeps_earlier_entries(tmp_path: Path) -> None:
    """Appending rewrites the roster with earlier entries intact."""
    paths = test_runtime_paths(tmp_path)
    first, second = _entry(), _entry("@bob:example.org")
    _append_personal_agent_roster_entry(paths, first)
    _append_personal_agent_roster_entry(paths, second)
    assert read_personal_agent_roster(paths) == [first, second]


BASE: dict[str, Any] = {
    "models": {"default": {"provider": "openai", "id": "m"}},
    "rooms": {"lobby": {}},
    "personal_agents": {"mind": {"agent": {"display_name": "{user_display_name}'s Mind", "model": "default"}}},
    "personal_rooms": {"agent": "mind", "onboarding_rooms": ["lobby"]},
}


def _runtime_config(
    tmp_path: Path,
    data: dict[str, Any] | None = None,
    roster: tuple[PersonalAgentRosterEntry, ...] = (),
) -> Config:
    paths = test_runtime_paths(tmp_path)
    for entry in roster:
        _append_personal_agent_roster_entry(paths, entry)
    return Config.validate_with_runtime(deepcopy(BASE) if data is None else data, paths)


def test_runtime_config_contains_generated_agent(tmp_path: Path) -> None:
    """A roster entry becomes an ordinary agent that only the runtime dump keeps."""
    config = _runtime_config(tmp_path, roster=(_entry(),))
    assert config.agents["mind_alice_smith"].display_name == "Alice's Mind"
    instance = config.personal_agent_instance("mind_alice_smith")
    assert instance is not None
    assert instance.user_id == "@Alice.Smith:example.org"
    assert config.personal_agent_instance("mind") is None
    assert config.personal_agent_name("mind", "@Alice.Smith:example.org") == "mind_alice_smith"
    assert config.personal_agent_name("mind", "@bob:example.org") is None
    assert "mind_alice_smith" not in config.authored_model_dump().get("agents", {})
    assert config.runtime_model_dump()["agents"]["mind_alice_smith"]["display_name"] == "Alice's Mind"
    overlaid = config.with_runtime_knowledge_base_overlay("extra", KnowledgeBaseConfig(path=str(tmp_path)))
    assert overlaid.personal_agent_instance("mind_alice_smith") == instance


def test_generated_agent_never_gets_self_config(tmp_path: Path) -> None:
    """A generated agent opts out of self_config even when the defaults turn it on."""
    data = deepcopy(BASE) | {"defaults": {"allow_self_config": True}}
    config = _runtime_config(tmp_path, data, roster=(_entry(),))
    assert config.agents["mind_alice_smith"].allow_self_config is False


def test_template_key_in_delegate_to_names_each_users_own_agent(tmp_path: Path) -> None:
    """The template key in delegate_to lets each user's agent run copies of itself, and validates before onboarding."""
    data = deepcopy(BASE) | {"agents": {"research": {"display_name": "Research", "model": "default"}}}
    data["personal_agents"]["mind"]["agent"]["delegate_to"] = ["mind", "research"]
    assert "mind" not in _runtime_config(tmp_path / "empty", deepcopy(data)).agents
    config = _runtime_config(tmp_path, data, roster=(_entry(),))
    assert config.agents["mind_alice_smith"].delegate_to == ["mind_alice_smith", "research"]
    assert config.authored_model_dump()["personal_agents"]["mind"]["agent"]["delegate_to"] == ["mind", "research"]


def test_broken_template_fails_without_any_user(tmp_path: Path) -> None:
    """A template that would generate an invalid agent fails before anyone is onboarded."""
    data = deepcopy(BASE)
    # Config does not check agent model names, so use a knowledge base reference it does check.
    data["personal_agents"]["mind"]["agent"]["knowledge_bases"] = ["missing"]
    with pytest.raises(ConfigRuntimeValidationError) as raised:
        _runtime_config(tmp_path, data)
    assert str(raised.value) == (
        "personal_agents would generate an invalid agent for a new user: "
        "config: Agents reference unknown knowledge bases: mind_personal_agent_probe_0 -> missing"
    )


def test_broken_override_for_user_not_onboarded_fails(tmp_path: Path) -> None:
    """A user override that would generate an invalid agent fails before that user is onboarded."""
    data = deepcopy(BASE)
    data["personal_agents"]["mind"]["users"] = {"@carol:example.org": {"knowledge_bases": ["missing"]}}
    with pytest.raises(ConfigRuntimeValidationError, match="personal_agents would generate an invalid agent"):
        _runtime_config(tmp_path, data, roster=(_entry(),))


def test_authored_collision_is_a_config_error(tmp_path: Path) -> None:
    """An authored agent that takes a generated agent's name is a config error naming the template."""
    data = deepcopy(BASE) | {"agents": {"mind_alice_smith": {"display_name": "Mine"}}}
    with pytest.raises(ConfigRuntimeValidationError, match=r"personal_agents\.mind"):
        _runtime_config(tmp_path, data, roster=(_entry(),))


def test_edited_generated_agent_is_rejected_on_save(tmp_path: Path) -> None:
    """Editing a generated agent in place keeps it in the authored dump, so saving collides instead of dropping it."""
    config = _runtime_config(tmp_path, roster=(_entry(),))
    config.agents["mind_alice_smith"].display_name = "Edited"
    authored = config.authored_model_dump()
    assert authored["agents"]["mind_alice_smith"]["display_name"] == "Edited"
    with pytest.raises(ConfigRuntimeValidationError, match=r"agents\.mind_alice_smith collides"):
        Config.validate_with_runtime(authored, test_runtime_paths(tmp_path))


def test_template_key_must_not_reuse_agent_key() -> None:
    """A template key cannot also be an agent key."""
    with pytest.raises(ValidationError, match="personal_agents keys must not equal an agent or team key: mind"):
        Config.model_validate(
            {
                "agents": {"mind": {"display_name": "M"}},
                "personal_agents": {"mind": {"agent": {"display_name": "Mind"}}},
            },
        )


@pytest.mark.parametrize("key", ["router", "bad-key"])
def test_template_key_must_be_a_valid_agent_name(key: str) -> None:
    """A template key follows the agent name rules."""
    with pytest.raises(ValidationError, match=key):
        Config.model_validate({"personal_agents": {key: {"agent": {"display_name": "Mind"}}}})


def test_personal_rooms_may_name_a_template() -> None:
    """Personal rooms accept a template as their agent."""
    assert Config.model_validate(deepcopy(BASE)).personal_rooms.agent == "mind"


def test_pending_entry_generates_no_agent_but_keeps_its_name(tmp_path: Path) -> None:
    """A registration still in flight starts no agent, yet a save that takes its name is rejected."""
    pending = _entry(pending=True)
    config = _runtime_config(tmp_path, roster=(pending,))
    assert "mind_alice_smith" not in config.agents
    assert config.personal_agent_name("mind", pending.user_id) is None
    data = deepcopy(BASE) | {"agents": {"mind_alice_smith": {"display_name": "Mine"}}}
    with pytest.raises(ConfigRuntimeValidationError, match=r"agents\.mind_alice_smith collides"):
        Config.validate_with_runtime(data, test_runtime_paths(tmp_path))


def test_personal_rooms_must_not_name_a_generated_agent(tmp_path: Path) -> None:
    """A generated agent serves personal rooms only through its template, so naming it directly is a config error."""
    data = deepcopy(BASE)
    data["personal_rooms"]["agent"] = "mind_alice_smith"
    with pytest.raises(ConfigRuntimeValidationError, match=r"personal_rooms\.agent .*name the template mind"):
        _runtime_config(tmp_path, data, roster=(_entry(),))


def test_authored_dump_round_trips_template(tmp_path: Path) -> None:
    """Revalidating the authored dump regenerates the same agents from the template."""
    config = _runtime_config(tmp_path, roster=(_entry(),))
    authored = config.authored_model_dump()
    assert "agents" not in authored
    assert authored["personal_agents"] == BASE["personal_agents"]
    revalidated = Config.validate_with_runtime(authored, test_runtime_paths(tmp_path))
    assert revalidated.agents == config.agents


def test_runner_snapshot_includes_generated_agents(tmp_path: Path) -> None:
    """Runners resolve agents from the snapshot, which carries generated agents but never the templates."""
    config = _runtime_config(tmp_path, roster=(_entry(),))
    snapshot = runner_config_snapshot(test_runtime_paths(tmp_path), config)
    assert snapshot is not None
    assert snapshot["agents"]["mind_alice_smith"]["display_name"] == "Alice's Mind"
    assert "personal_agents" not in snapshot


def test_override_on_every_owner_does_not_hide_a_broken_template(tmp_path: Path) -> None:
    """A template default that every existing owner overrides still fails before the next plain user commits it."""
    data = deepcopy(BASE)
    data["personal_agents"]["mind"]["agent"]["knowledge_bases"] = ["missing"]
    data["personal_agents"]["mind"]["users"] = {"@Alice.Smith:example.org": {"knowledge_bases": []}}
    with pytest.raises(ConfigRuntimeValidationError, match="personal_agents would generate an invalid agent"):
        _runtime_config(tmp_path, data, roster=(_entry(),))


@pytest.mark.parametrize("content", ['{"entries": [', '{"entries": [{"template": "mind"}]}'])
def test_invalid_roster_error_names_the_file(tmp_path: Path, content: str) -> None:
    """A corrupt or hand-edited roster fails with an error that names the roster file."""
    paths = test_runtime_paths(tmp_path)
    path = personal_agent_roster_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=f"Personal agent roster {re.escape(str(path))} is invalid: "):
        read_personal_agent_roster(paths)
    with pytest.raises(ConfigRuntimeValidationError, match=r"Personal agent roster .* is invalid"):
        Config.validate_with_runtime(deepcopy(BASE), paths)


_CALLS: dict[str, Any] = {
    "enabled": True,
    "profiles": {
        "voice": {"backend": "live", "model": "gpt-live-1", "credentials_service": "openai-live", "voice": "marin"},
    },
    "agents": {"mind": "voice"},
}


def test_calls_template_key_enables_calls_for_each_generated_agent(tmp_path: Path) -> None:
    """A template key in calls.agents gives every user's agent from that template the profile."""
    data = deepcopy(BASE) | {"calls": deepcopy(_CALLS)}
    config = _runtime_config(tmp_path, data, roster=(_entry(), _entry("@bob:example.org")))
    assert config.calls.agents == {"mind_alice_smith": "voice", "mind_bob": "voice"}
    assert config.calls.resolve_agent_config("mind_bob").backend == "live"


def test_calls_template_key_loads_before_anyone_is_onboarded(tmp_path: Path) -> None:
    """A calls-enabled template with no users yet loads and enables nobody."""
    config = _runtime_config(tmp_path, deepcopy(BASE) | {"calls": deepcopy(_CALLS)})
    assert config.calls.agents == {}


def test_authored_dump_keeps_calls_template_key(tmp_path: Path) -> None:
    """Saving the config keeps the template key, not the generated agents it expanded to."""
    config = _runtime_config(tmp_path, deepcopy(BASE) | {"calls": deepcopy(_CALLS)}, roster=(_entry(),))
    assert config.authored_model_dump()["calls"]["agents"] == {"mind": "voice"}


def test_calls_template_whose_agent_lists_rooms_is_rejected(tmp_path: Path) -> None:
    """Every user's agent would join the same rooms, and a room takes only one calls-enabled agent."""
    data = deepcopy(BASE) | {"calls": deepcopy(_CALLS)}
    data["personal_agents"]["mind"]["agent"]["rooms"] = ["lobby"]
    with pytest.raises(
        ConfigRuntimeValidationError,
        match=r"calls\.agents names personal_agents\.mind, whose agent lists rooms",
    ):
        _runtime_config(tmp_path, data)


def test_explicit_call_profile_for_a_generated_agent_wins_and_is_saved(tmp_path: Path) -> None:
    """An agent named in calls.agents keeps its own profile over its template's, and saving keeps both entries."""
    data = deepcopy(BASE) | {"calls": deepcopy(_CALLS)}
    data["calls"]["profiles"]["other"] = data["calls"]["profiles"]["voice"] | {"voice": "cedar"}
    data["calls"]["agents"]["mind_bob"] = "other"
    config = _runtime_config(tmp_path, data, roster=(_entry(), _entry("@bob:example.org")))
    assert config.calls.agents == {"mind_alice_smith": "voice", "mind_bob": "other"}
    assert config.authored_model_dump()["calls"]["agents"] == {"mind": "voice", "mind_bob": "other"}


def test_calls_template_whose_user_override_lists_rooms_is_rejected(tmp_path: Path) -> None:
    """A per-user override must not put a calls-enabled template's agent into rooms either, onboarded or not."""
    data = deepcopy(BASE) | {"calls": deepcopy(_CALLS)}
    data["personal_agents"]["mind"]["users"] = {"@carol:example.org": {"rooms": ["lobby"]}}
    with pytest.raises(
        ConfigRuntimeValidationError,
        match=r"calls\.agents names personal_agents\.mind, whose agent lists rooms",
    ):
        _runtime_config(tmp_path, data)


def test_unknown_template_call_profile_fails_before_onboarding(tmp_path: Path) -> None:
    """A template's unknown profile fails the load that names it, not the reload that onboards its first user."""
    data = deepcopy(BASE) | {"calls": deepcopy(_CALLS) | {"agents": {"mind": "voiec"}}}
    with pytest.raises(ConfigRuntimeValidationError, match=r"calls\.agents references unknown profile\(s\): voiec"):
        _runtime_config(tmp_path, data)


def test_explicit_call_profile_equal_to_the_template_is_saved(tmp_path: Path) -> None:
    """An authored entry for a generated agent survives saving even when it names the template's profile."""
    data = deepcopy(BASE) | {"calls": deepcopy(_CALLS)}
    data["calls"]["agents"]["mind_bob"] = "voice"
    config = _runtime_config(tmp_path, data, roster=(_entry(), _entry("@bob:example.org")))
    assert config.authored_model_dump()["calls"]["agents"] == {"mind": "voice", "mind_bob": "voice"}


def test_onboarding_under_a_calls_template_keeps_other_agents_cli_catalogs(tmp_path: Path) -> None:
    """Another user's onboarding adds only that user's call entry, which never reaches this agent's toolkits."""
    data = deepcopy(BASE) | {"calls": deepcopy(_CALLS)}
    before = _runtime_config(tmp_path / "before", deepcopy(data), roster=(_entry(),))
    after = _runtime_config(tmp_path / "after", deepcopy(data), roster=(_entry(), _entry("@bob:example.org")))
    assert after.calls.agents != before.calls.agents
    assert _catalog_authority(after, "mind_alice_smith") == _catalog_authority(before, "mind_alice_smith")


def test_blank_template_call_profile_is_a_validation_error(tmp_path: Path) -> None:
    """A template key without a profile is refused like any other non-string profile, not with a TypeError."""
    data = deepcopy(BASE) | {"calls": deepcopy(_CALLS) | {"agents": {"mind": None}}}
    with pytest.raises(ValueError, match=r"calls\.agents\.mind"):
        _runtime_config(tmp_path, data)
