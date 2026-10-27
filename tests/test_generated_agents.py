"""Tests for generated agents as a base agent plus the owner's edits."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from mindroom.config.agent import AgentConfig
from mindroom.config.generated_agents import generated_agent_data, tool_ceiling
from mindroom.config.main import Config
from mindroom.config.personal_agents import (
    CreatedAgentEntry,
    GeneratedAgentEdits,
    PersonalAgentRosterEntry,
    personal_agent_roster_path,
    read_personal_agent_roster,
    read_personal_agent_roster_file,
)
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

OWNER = "@alice:localhost"
CO_OWNER = "@bob:localhost"

BASE: dict[str, Any] = {
    "models": {"default": {"provider": "openai", "id": "m"}},
    "defaults": {"tools": [{"shell": {"extra_env_passthrough": ["TOKEN"]}}]},
    "agents": {
        "creator": {
            "display_name": "Creator",
            "role": "Makes agents",
            "instructions": ["Be useful"],
            "model": "default",
            "tools": ["calculator", {"shell": {"shell_path_prepend": ["/opt/bin"]}}],
            "worker_tools": ["shell"],
            "rooms": ["lobby"],
            "delegate_to": ["creator"],
            "allow_self_config": True,
            "created_agents_per_owner": 2,
        },
    },
    "rooms": {"lobby": {}},
}
MERGED_SHELL = {"extra_env_passthrough": ["TOKEN"], "shell_path_prepend": ["/opt/bin"]}


def _config(tmp_path: Path) -> Config:
    return Config.validate_with_runtime(deepcopy(BASE), test_runtime_paths(tmp_path))


def _created(**kwargs: object) -> CreatedAgentEntry:
    fields: dict[str, object] = {
        "agent_name": "creator_research",
        "creator": "creator",
        "owner": OWNER,
        "co_owners": [CO_OWNER],
        "edits": GeneratedAgentEdits(display_name="Research"),
    }
    return CreatedAgentEntry.model_validate(fields | kwargs)


def _validated_child(data: dict[str, Any], tmp_path: Path) -> Config:
    raw = deepcopy(BASE)
    raw["agents"]["child"] = data
    return Config.validate_with_runtime(raw, test_runtime_paths(tmp_path))


def test_the_tool_ceiling_is_the_effective_tool_names(tmp_path: Path) -> None:
    """The ceiling lists the agent's own tools and the defaults, in effective order."""
    assert tool_ceiling(_config(tmp_path), "creator") == ("calculator", "shell")


def test_chosen_tools_are_copied_whole_from_the_effective_list(tmp_path: Path) -> None:
    """A chosen tool keeps the creator's merged settings and the child no longer merges defaults."""
    data, dropped = generated_agent_data(_config(tmp_path), "creator", GeneratedAgentEdits(tools=["shell"]))
    assert dropped == []
    assert data["tools"] == [{"name": "shell", "overrides": MERGED_SHELL, "defer": False, "initial": False}]
    assert data["include_default_tools"] is False


def test_chosen_tools_turn_defaults_off(tmp_path: Path) -> None:
    """Choosing only calculator leaves no shell from defaults.tools in the validated child."""
    config = _config(tmp_path)
    data, _ = generated_agent_data(config, "creator", GeneratedAgentEdits(tools=["calculator"]))
    child = _validated_child(data, tmp_path)
    assert [entry.name for entry in child._get_agent_authored_tool_configs("child")] == ["calculator"]
    assert tool_ceiling(child, "child") == ("calculator",)


def test_unknown_tool_names_are_dropped(tmp_path: Path) -> None:
    """A name outside the effective list is reported and not kept."""
    data, dropped = generated_agent_data(
        _config(tmp_path),
        "creator",
        GeneratedAgentEdits(tools=["calculator", "browser"]),
    )
    assert dropped == ["browser"]
    assert [entry["name"] for entry in data["tools"]] == ["calculator"]


def test_untouched_tools_keep_the_base(tmp_path: Path) -> None:
    """Without a tools edit the base's tool entries and default merging are unchanged."""
    config = _config(tmp_path)
    base = config.agents["creator"].model_dump(mode="json", exclude_unset=True)
    for edits in (None, GeneratedAgentEdits(role="Other")):
        data, dropped = generated_agent_data(config, "creator", edits)
        assert dropped == []
        assert data["tools"] == base["tools"]
        assert data.get("include_default_tools") == base.get("include_default_tools")


def test_text_edits_replace_the_base_values(tmp_path: Path) -> None:
    """Display name, role and instructions replace the base; the base is left unchanged."""
    config = _config(tmp_path)
    edits = GeneratedAgentEdits(display_name="Scout", role="Finds things", instructions=["Search first"])
    data, _ = generated_agent_data(config, "creator", edits)
    assert (data["display_name"], data["role"], data["instructions"]) == ("Scout", "Finds things", ["Search first"])
    assert config.agents["creator"].display_name == "Creator"
    assert config.agents["creator"].instructions == ["Be useful"]


def test_a_created_agent_replaces_identity_fields(tmp_path: Path) -> None:
    """A created agent belongs to its owners alone and cannot create, delegate or self-configure."""
    config = _config(tmp_path)
    data, _ = generated_agent_data(config, "creator", _created().edits, created=_created())
    owners = [OWNER, CO_OWNER]
    assert data["access"] == {"users": owners, "members_of_rooms": []}
    assert data["credential_managers"] == owners
    assert data["accept_invites"] == owners
    assert data["rooms"] == []
    assert data["automations"] is None
    assert data["delegate_to"] == []
    assert data["allow_self_config"] is False
    assert data["created_agents_per_owner"] == 0
    assert data["display_name"] == "Research"
    assert data["model"] == "default"
    assert data["worker_tools"] == ["shell"]
    AgentConfig.model_validate(data)
    assert config.agents["creator"].rooms == ["lobby"]


def test_the_roster_file_reads_both_lists(tmp_path: Path) -> None:
    """The roster file holds personal entries with edits and created agents."""
    paths = test_runtime_paths(tmp_path)
    entry = {
        "template": "mind",
        "user_id": OWNER,
        "username": "alice",
        "agent_name": "mind_alice",
        "matrix_username": None,
        "user_display_name": "Alice",
        "source_room_id": "!lobby:localhost",
        "edits": {"role": "Scout", "tools": ["calculator"]},
    }
    created = _created(pending=True).model_dump(mode="json")
    path = personal_agent_roster_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"entries": [entry], "created": [created]}), encoding="utf-8")

    roster = read_personal_agent_roster_file(paths)
    assert roster.entries == [PersonalAgentRosterEntry.model_validate(entry)]
    assert roster.entries[0].edits == GeneratedAgentEdits(role="Scout", tools=["calculator"])
    assert roster.created == [_created(pending=True)]
    assert read_personal_agent_roster(paths) == roster.entries


def test_the_roster_file_defaults_to_empty_lists(tmp_path: Path) -> None:
    """Before the first write, and for a file without created agents, both lists are empty."""
    paths = test_runtime_paths(tmp_path)
    assert read_personal_agent_roster_file(paths).model_dump() == {"entries": [], "created": []}
    path = personal_agent_roster_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"entries": []}), encoding="utf-8")
    assert read_personal_agent_roster_file(paths).created == []


def test_edits_and_created_entries_reject_unknown_fields() -> None:
    """Only the four editable fields can be stored."""
    with pytest.raises(ValidationError):
        GeneratedAgentEdits.model_validate({"model": "opus"})
    with pytest.raises(ValidationError):
        CreatedAgentEntry.model_validate(
            {"agent_name": "a", "creator": "c", "owner": OWNER, "edits": {}, "access": {}},
        )


def test_created_agents_per_owner_defaults_off_and_is_not_negative() -> None:
    """Agents cannot create agents until an admin sets a positive number."""
    assert AgentConfig(display_name="A").created_agents_per_owner == 0
    with pytest.raises(ValidationError):
        AgentConfig(display_name="A", created_agents_per_owner=-1)
