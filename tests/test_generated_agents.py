"""Tests for generated agents as a base agent plus the owner's edits."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from mindroom.config import generated_agents
from mindroom.config.agent import AgentConfig
from mindroom.config.generated_agents import GeneratedAgentInfo, generated_agent_data, tool_ceiling
from mindroom.config.main import Config
from mindroom.config.personal_agents import (
    CreatedAgentEntry,
    GeneratedAgentEdits,
    PersonalAgentRoster,
    PersonalAgentRosterEntry,
    personal_agent_roster_path,
    read_personal_agent_roster,
    read_personal_agent_roster_file,
)
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Sequence
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


PERSONAL_TEMPLATE: dict[str, Any] = {
    "agent": {
        "display_name": "{user_display_name}'s Mind",
        "model": "default",
        "tools": ["calculator"],
    },
}


def _personal(user_id: str = OWNER, **kwargs: object) -> PersonalAgentRosterEntry:
    username = user_id.removeprefix("@").partition(":")[0]
    fields: dict[str, object] = {
        "template": "mind",
        "user_id": user_id,
        "username": username,
        "agent_name": f"mind_{username}",
        "matrix_username": None,
        "user_display_name": username.title(),
        "source_room_id": "!lobby:localhost",
    }
    return PersonalAgentRosterEntry.model_validate(fields | kwargs)


def _load(
    tmp_path: Path,
    data: dict[str, Any] | None = None,
    *,
    entries: Sequence[PersonalAgentRosterEntry] = (),
    created: Sequence[CreatedAgentEntry] = (),
) -> Config:
    paths = test_runtime_paths(tmp_path)
    path = personal_agent_roster_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        PersonalAgentRoster(entries=list(entries), created=list(created)).model_dump_json(),
        encoding="utf-8",
    )
    return Config.validate_with_runtime(deepcopy(BASE) if data is None else data, paths)


def _with_template() -> dict[str, Any]:
    return deepcopy(BASE) | {"personal_agents": {"mind": deepcopy(PERSONAL_TEMPLATE)}}


def _skipped(logs: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(log["agent_name"], log["reason"]) for log in logs if log["event"] == "created_agent_skipped"]


def test_a_created_agent_loads_from_its_creator(tmp_path: Path) -> None:
    """An active created entry becomes an agent with its chosen tools, owned by the requester."""
    config = _load(tmp_path, created=[_created(edits=GeneratedAgentEdits(display_name="Research", tools=["shell"]))])
    agent = config.agents["creator_research"]
    assert agent.display_name == "Research"
    assert agent.model == "default"
    assert agent.access.users == [OWNER, CO_OWNER]
    assert [entry.name for entry in config._get_agent_authored_tool_configs("creator_research")] == ["shell"]
    assert config.generated_agent("creator_research") == GeneratedAgentInfo(
        base="creator",
        owner=OWNER,
        co_owners=(CO_OWNER,),
        created=True,
        allowed_tools=("calculator", "shell"),
    )
    assert config.generated_agent("creator") is None
    assert config.tool_ceiling("creator_research") == ("calculator", "shell")
    assert config.tool_ceiling("creator") == ("calculator", "shell")


def test_personal_agent_edits_apply_at_load(tmp_path: Path) -> None:
    """Owner edits replace the template's values, and the template stays the ceiling."""
    edits = GeneratedAgentEdits(display_name="Scout", tools=["calculator"])
    config = _load(tmp_path, _with_template(), entries=[_personal(edits=edits), _personal(CO_OWNER)])
    alice = config.agents["mind_alice"]
    assert alice.display_name == "Scout"
    assert [entry.name for entry in config._get_agent_authored_tool_configs("mind_alice")] == ["calculator"]
    # The owner may add back shell from defaults.tools, which the edit removed.
    assert config.tool_ceiling("mind_alice") == ("calculator", "shell")
    assert config.generated_agent("mind_alice") == GeneratedAgentInfo(
        base="mind_alice",
        owner=OWNER,
        co_owners=(),
        created=False,
        allowed_tools=("calculator", "shell"),
    )
    # Every personal agent is a generated agent, edited or not.
    assert config.agents["mind_bob"].display_name == "Bob's Mind"
    assert config.generated_agent("mind_bob") is not None
    assert config.personal_agent_name("mind", OWNER) == "mind_alice"


def test_a_created_agent_chooses_from_its_creators_granted_tools(tmp_path: Path) -> None:
    """A personal creator that removed shell from itself can still give shell to the agent it created."""
    data = _with_template()
    data["personal_agents"]["mind"]["agent"]["created_agents_per_owner"] = 1
    config = _load(
        tmp_path,
        data,
        entries=[_personal(edits=GeneratedAgentEdits(tools=["calculator"]))],
        created=[
            _created(
                creator="mind_alice",
                agent_name="mind_alice_research",
                co_owners=[],
                edits=GeneratedAgentEdits(display_name="Research", tools=["shell"]),
            ),
        ],
    )
    assert [entry.name for entry in config._get_agent_authored_tool_configs("mind_alice_research")] == ["shell"]
    assert config.tool_ceiling("mind_alice_research") == ("calculator", "shell")


def test_a_tool_the_creator_lost_is_dropped_at_load(tmp_path: Path) -> None:
    """A stored tool the creator no longer has is left out, and loading stays healthy."""
    data = deepcopy(BASE)
    del data["defaults"]
    data["agents"]["creator"]["tools"] = ["calculator"]
    data["agents"]["creator"]["worker_tools"] = []
    entry = _created(edits=GeneratedAgentEdits(display_name="Research", tools=["calculator", "shell"]))
    with capture_logs() as logs:
        config = _load(tmp_path, data, created=[entry])
    assert [entry.name for entry in config._get_agent_authored_tool_configs("creator_research")] == ["calculator"]
    dropped = [log for log in logs if log["event"] == "generated_agent_tool_dropped"]
    assert [(log["agent_name"], log["tools"]) for log in dropped] == [("creator_research", ["shell"])]


def test_the_creator_cap_limits_created_agents_at_load(tmp_path: Path) -> None:
    """Only the first active entries up to the creator's current cap load, per owner, in roster order."""
    entries = [
        _created(agent_name="creator_draft", pending=True),
        _created(agent_name="creator_research"),
        _created(agent_name="creator_writer"),
        _created(agent_name="creator_other", owner=CO_OWNER, co_owners=[]),
    ]
    for cap, loaded in ((1, {"creator_research", "creator_other"}), (0, set())):
        data = deepcopy(BASE)
        data["agents"]["creator"]["created_agents_per_owner"] = cap
        with capture_logs() as logs:
            config = _load(tmp_path / str(cap), data, created=entries)
        assert set(config.agents) - {"creator"} == loaded
        assert ("creator_draft", "pending") in _skipped(logs)
        assert ("creator_writer", "over_cap") in _skipped(logs)


def test_a_created_agent_whose_creator_is_gone_is_skipped(tmp_path: Path) -> None:
    """Removing the creator removes the agents it created at the next load."""
    with capture_logs() as logs:
        config = _load(tmp_path, created=[_created(creator="gone", agent_name="gone_research")])
    assert "gone_research" not in config.agents
    assert config.generated_agent("gone_research") is None
    assert _skipped(logs) == [("gone_research", "creator_missing")]


def test_a_created_name_taken_by_an_authored_agent_is_skipped(tmp_path: Path) -> None:
    """An authored agent keeps its name and config when a created entry claims it."""
    data = deepcopy(BASE)
    data["agents"]["creator_research"] = {"display_name": "Authored"}
    with capture_logs() as logs:
        config = _load(tmp_path, data, created=[_created()])
    assert config.agents["creator_research"].display_name == "Authored"
    assert config.generated_agent("creator_research") is None
    assert config.authored_model_dump()["agents"]["creator_research"] == {"display_name": "Authored"}
    assert _skipped(logs) == [("creator_research", "name_taken")]


def test_an_invalid_pass_two_falls_back_to_pass_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A generated agent that fails validation leaves the pass-one config, so loading never breaks."""
    monkeypatch.setattr(generated_agents, "generated_agent_data", lambda *_args, **_kwargs: ({"display_name": 5}, []))
    with capture_logs() as logs:
        config = _load(
            tmp_path,
            _with_template(),
            entries=[_personal(edits=GeneratedAgentEdits(display_name="Scout"))],
            created=[_created()],
        )
    assert "creator_research" not in config.agents
    assert config.generated_agent("creator_research") is None
    assert config.agents["mind_alice"].display_name == "Alice's Mind"
    # The owner keeps the tools to fix the edit.
    assert config.generated_agent("mind_alice") is not None
    assert [log["event"] for log in logs].count("generated_agents_invalid") == 1


def test_authored_dump_leaves_out_created_agents(tmp_path: Path) -> None:
    """Saving never writes created or edited generated agents into the authored config."""
    config = _load(
        tmp_path,
        _with_template(),
        entries=[_personal(edits=GeneratedAgentEdits(display_name="Scout"))],
        created=[_created()],
    )
    authored = config.authored_model_dump()
    assert set(authored["agents"]) == {"creator"}
    assert {"creator_research", "mind_alice"} < set(config.runtime_model_dump()["agents"])
    revalidated = Config.validate_with_runtime(authored, test_runtime_paths(tmp_path))
    assert revalidated.agents == config.agents
