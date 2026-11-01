"""Tests for generated agents as a base agent plus the owner's edits."""

from __future__ import annotations

import asyncio
import json
import re
import threading
from copy import deepcopy
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from mindroom import personal_agents
from mindroom.config import generated_agents
from mindroom.config.agent import AgentConfig
from mindroom.config.generated_agents import GeneratedAgentInfo, _generated_agent_data
from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.config.models import _DEFER_PROHIBITED_CONTROL_TOOLS
from mindroom.config.personal_agents import (
    CreatedAgentEntry,
    GeneratedAgentEdits,
    PersonalAgentRoster,
    PersonalAgentRosterEntry,
    personal_agent_roster_path,
    read_personal_agent_roster,
    read_personal_agent_roster_file,
)
from mindroom.config.resolution import authored_tool_entries
from mindroom.config.slack import slack_answering_agents
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import AgentMatrixUser
from mindroom.personal_agents import (
    _append_personal_agent_roster_entry,
    _GeneratedAgentError,
    _reserve_created_agent,
    _settle_roster_entry,
    _taken_agent_names,
    delete_created_agent,
    generated_agent_settings,
    provision_created_agent,
    update_generated_agent,
)
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.workers.runtime import build_worker_launch_config
from tests.conftest import test_runtime_paths
from tests.matrix_state_helpers import save_matrix_state
from tests.test_plugins import _publish, _write_broken_tool_plugin

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.constants import RuntimePaths

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
    assert _config(tmp_path).tool_ceiling("creator") == ("calculator", "shell")


def test_chosen_tools_are_copied_whole_from_the_effective_list(tmp_path: Path) -> None:
    """A chosen tool keeps the creator's merged settings and lazy-loading flags, and the child no longer merges defaults."""
    data, dropped = _generated_agent_data(_config(tmp_path), "creator", GeneratedAgentEdits(tools=["shell"]))
    assert dropped == []
    assert data["tools"] == [{"name": "shell", "overrides": MERGED_SHELL}]
    assert data["include_default_tools"] is False
    raw = deepcopy(BASE)
    raw["agents"]["creator"]["tools"][0] = {"calculator": {"defer": True, "initial": True}}
    config = Config.validate_with_runtime(raw, test_runtime_paths(tmp_path))
    data, _ = _generated_agent_data(config, "creator", GeneratedAgentEdits(tools=["calculator"]))
    assert data["tools"] == [{"name": "calculator", "overrides": {}, "defer": True, "initial": True}]


def test_chosen_tools_turn_defaults_off(tmp_path: Path) -> None:
    """Choosing only calculator leaves no shell from defaults.tools in the validated child."""
    config = _config(tmp_path)
    data, _ = _generated_agent_data(config, "creator", GeneratedAgentEdits(tools=["calculator"]))
    child = _validated_child(data, tmp_path)
    assert [entry.name for entry in authored_tool_entries(child, "child")] == ["calculator"]
    assert child.tool_ceiling("child") == ("calculator",)


def test_unknown_tool_names_are_dropped(tmp_path: Path) -> None:
    """A name outside the effective list is reported and not kept."""
    data, dropped = _generated_agent_data(
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
        data, dropped = _generated_agent_data(config, "creator", edits)
        assert dropped == []
        assert data["tools"] == base["tools"]
        assert data.get("include_default_tools") == base.get("include_default_tools")


def test_text_edits_replace_the_base_values(tmp_path: Path) -> None:
    """Display name, role and instructions replace the base; the base is left unchanged."""
    config = _config(tmp_path)
    edits = GeneratedAgentEdits(display_name="Scout", role="Finds things", instructions=["Search first"])
    data, _ = _generated_agent_data(config, "creator", edits)
    assert (data["display_name"], data["role"], data["instructions"]) == ("Scout", "Finds things", ["Search first"])
    assert config.agents["creator"].display_name == "Creator"
    assert config.agents["creator"].instructions == ["Be useful"]


def test_a_created_agent_replaces_identity_fields(tmp_path: Path) -> None:
    """A created agent is its owners' and cannot create, delegate, self-configure or have contacts; credentials stay the creator's."""
    raw = deepcopy(BASE)
    raw["agents"]["creator"]["credential_managers"] = ["@carol:localhost"]
    raw["agents"]["creator"]["contacts"] = {}
    config = Config.validate_with_runtime(raw, test_runtime_paths(tmp_path))
    data, _ = _generated_agent_data(config, "creator", _created().edits, created=_created())
    owners = [OWNER, CO_OWNER]
    assert data["access"] == {"users": owners, "members_of_rooms": []}
    assert data["credential_managers"] == ["@carol:localhost"]
    assert data["accept_invites"] == owners
    assert data["rooms"] == []
    assert data["automations"] is None
    assert data["contacts"] is None
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


def _slack_creator() -> dict[str, Any]:
    """A config whose creator answers Slack mentions in its room and may create agents."""
    data = deepcopy(BASE)
    data["slack"] = {"email_domain": "example.com", "matrix_user_id_template": "@{localpart}:localhost"}
    data["agents"]["creator"]["tools"].append({"slack": {"channels": ["C0123ABCD"], "room": "lobby"}})
    data["agents"]["creator"]["created_agents_per_owner"] = 3
    return data


def _slack_overrides(config: Config, agent_name: str) -> dict[str, Any]:
    (entry,) = (entry for entry in authored_tool_entries(config, agent_name) if entry.name == "slack")
    return entry.tool_config_overrides


def _skipped(logs: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(log["agent_name"], log["reason"]) for log in logs if log["event"] == "created_agent_skipped"]


def test_a_created_agent_loads_from_its_creator(tmp_path: Path) -> None:
    """An active created entry becomes an agent with its chosen tools, owned by the requester."""
    config = _load(tmp_path, created=[_created(edits=GeneratedAgentEdits(display_name="Research", tools=["shell"]))])
    agent = config.agents["creator_research"]
    assert agent.display_name == "Research"
    assert agent.model == "default"
    assert agent.access.users == [OWNER, CO_OWNER]
    assert [entry.name for entry in authored_tool_entries(config, "creator_research")] == ["shell"]
    assert config.generated_agent("creator_research") == GeneratedAgentInfo(
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
    assert [entry.name for entry in authored_tool_entries(config, "mind_alice")] == ["calculator"]
    # The owner may add back shell from defaults.tools, which the edit removed.
    assert config.tool_ceiling("mind_alice") == ("calculator", "shell")
    assert config.generated_agent("mind_alice") == GeneratedAgentInfo(
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
    assert [entry.name for entry in authored_tool_entries(config, "mind_alice_research")] == ["shell"]
    assert config.tool_ceiling("mind_alice_research") == ("calculator", "shell")


def test_a_tool_that_brings_others_keeps_the_creators_settings_for_them(tmp_path: Path) -> None:
    """matrix_message brings attachments, so choosing it alone still gives the creator's restricted attachments."""
    data = deepcopy(BASE)
    restricted = {"include_tools": ["list_attachments"]}
    data["agents"]["creator"]["tools"] = ["matrix_message", {"attachments": restricted}]
    edits = GeneratedAgentEdits(display_name="Research", tools=["matrix_message"])
    config = _load(tmp_path, data, created=[_created(edits=edits)])
    for name in ("creator", "creator_research"):
        candidates = resolve_agent_tools(config, name, builtin_tool_registry()).candidates()
        assert {entry.name: entry.tool_config_overrides for entry in candidates}["attachments"] == restricted


def test_a_tool_the_creator_lost_is_dropped_at_load(tmp_path: Path) -> None:
    """A stored tool the creator no longer has is left out, and loading stays healthy."""
    data = deepcopy(BASE)
    del data["defaults"]
    data["agents"]["creator"]["tools"] = ["calculator"]
    data["agents"]["creator"]["worker_tools"] = []
    entry = _created(edits=GeneratedAgentEdits(display_name="Research", tools=["calculator", "shell"]))
    with capture_logs() as logs:
        config = _load(tmp_path, data, created=[entry])
    assert [entry.name for entry in authored_tool_entries(config, "creator_research")] == ["calculator"]
    dropped = [log for log in logs if log["event"] == "generated_agent_tool_dropped"]
    assert [(log["agent_name"], log["tools"]) for log in dropped] == [("creator_research", ["shell"])]


def test_a_created_agent_keeps_a_broken_plugin_s_tool_and_gets_it_back_once_the_plugin_loads(tmp_path: Path) -> None:
    """While a plugin fails to load its tool stays chosen through every load, and the agent offers it once it loads."""
    plugin_root = tmp_path / "plugins" / "p"
    _write_broken_tool_plugin(plugin_root, tool_name="p_tool")
    data = deepcopy(BASE)
    data["plugins"] = ["./plugins/p"]
    data["agents"]["creator"]["tools"].append("p_tool")
    entry = _created(edits=GeneratedAgentEdits(display_name="Research", tools=["calculator", "p_tool"]))
    paths = test_runtime_paths(tmp_path)
    roster_path = personal_agent_roster_path(paths)
    roster_path.parent.mkdir(parents=True, exist_ok=True)
    roster_path.write_text(PersonalAgentRoster(created=[entry]).model_dump_json(), encoding="utf-8")

    def load(role: str) -> Config:
        data["agents"]["creator"]["role"] = role
        return Config.validate_with_runtime(deepcopy(data), paths, tolerate_plugin_load_errors=True)

    def offered(config: Config) -> tuple[str, ...]:
        return resolve_agent_tools(config, "creator_research", config_plugins(config, paths).tools).tool_names

    with capture_logs() as logs:
        config = load("Makes agents")
        _publish(config, paths)
        assert "p_tool" not in offered(config)

        edited = load("Makes other agents")
        assert [tool.name for tool in authored_tool_entries(edited, "creator_research")] == ["calculator", "p_tool"]
        assert "p_tool" not in offered(edited)

        (plugin_root / "tools.py").write_text(
            (plugin_root / "tools.py").read_text(encoding="utf-8").replace("raise ImportError", "# raise ImportError"),
            encoding="utf-8",
        )
        _publish(edited, paths)
        assert "p_tool" in offered(edited)
    assert not [log for log in logs if log["event"] == "generated_agent_tool_dropped"]


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


def test_a_created_entry_with_a_reserved_name_is_skipped_alone(tmp_path: Path) -> None:
    """A hand-edited entry named router is skipped by itself, so the other created agents still load."""
    with capture_logs() as logs:
        config = _load(tmp_path, created=[_created(agent_name="router"), _created(agent_name="creator_writer")])
    assert "creator_writer" in config.agents
    assert config.generated_agent("router") is None
    assert _skipped(logs) == [("router", "name_taken")]
    assert "generated_agents_invalid" not in [log["event"] for log in logs]


def test_a_stored_agent_from_a_slack_answering_creator_loads_without_the_creators_room(tmp_path: Path) -> None:
    """A roster entry copies the creator's Slack channels but not its room, so it loads beside the other created agents."""
    created = [
        _created(),
        _created(agent_name="creator_writer", edits=GeneratedAgentEdits(tools=["slack"])),
        _created(agent_name="creator_reader", edits=GeneratedAgentEdits(tools=["calculator"])),
    ]
    with capture_logs() as logs:
        config = _load(tmp_path, _slack_creator(), created=created)
    assert "generated_agents_invalid" not in [log["event"] for log in logs]
    assert {"creator_research", "creator_writer", "creator_reader"} <= set(config.agents)
    for name in ("creator_research", "creator_writer"):
        assert _slack_overrides(config, name) == {"channels": ["C0123ABCD"]}
    assert _slack_overrides(config, "creator") == {"channels": ["C0123ABCD"], "room": "lobby"}
    assert [answering.agent for answering in slack_answering_agents(config)] == ["creator"]


def test_an_invalid_pass_two_falls_back_to_pass_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A generated agent that fails validation leaves the pass-one config, so loading never breaks."""
    monkeypatch.setattr(generated_agents, "_generated_agent_data", lambda *_args, **_kwargs: ({"display_name": 5}, []))
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


def test_dedicated_workers_launch_with_the_generated_agents_of_the_applied_config(tmp_path: Path) -> None:
    """Docker and Kubernetes workers project created agents from the config the runtime applied, not the file."""
    config = _load(tmp_path, created=[_created()])

    agents = build_worker_launch_config(config, tool_validation_snapshot={}).config_data["agents"]

    assert {"creator", "creator_research"} <= set(agents)


@pytest.mark.parametrize("change", ["edit", "remove"])
def test_saving_an_in_place_change_to_a_created_agent_is_refused(tmp_path: Path, change: str) -> None:
    """A created agent lives only in the roster, so a config save that changed it in place would lose the change."""
    config = _load(tmp_path, created=[_created()])
    if change == "edit":
        config.agents["creator_research"].display_name = "Edited"
    else:
        del config.agents["creator_research"]
    with pytest.raises(ConfigRuntimeValidationError, match="its owners change it with update_agent"):
        config.authored_model_dump()


STRANGER = "@mallory:localhost"
ALLOWED = "Not among the tools this agent may use: browser; allowed: calculator, shell"


def _register(seen: list[bool] | None = None) -> AsyncMock:
    def register(_homeserver: str, agent_name: str, display_name: str, paths: RuntimePaths) -> AgentMatrixUser:
        if seen is not None:
            seen.extend(entry.pending for entry in read_personal_agent_roster_file(paths).created)
        return AgentMatrixUser(
            agent_name=agent_name,
            user_id=f"@mindroom_{agent_name}:localhost",
            display_name=display_name,
            password=None,
        )

    return AsyncMock(side_effect=register)


async def _create(
    config: Config,
    paths: RuntimePaths,
    register: AsyncMock,
    *,
    name: str = "Research",
    requester: str = OWNER,
    tools: list[str] | None = None,
) -> str:
    with patch("mindroom.personal_agents.create_agent_user", new=register):
        return await provision_created_agent(
            config,
            paths,
            creator="creator",
            requester_id=requester,
            name=name,
            display_name="Research",
            role="Finds things",
            instructions=None,
            tools=tools,
        )


def _update(
    config: Config,
    paths: RuntimePaths,
    changes: dict[str, object],
    *,
    requester: str = OWNER,
    agent_name: str = "creator_research",
) -> None:
    update_generated_agent(config, paths, agent_name=agent_name, requester_id=requester, changes=changes)


def _delete(
    config: Config,
    paths: RuntimePaths,
    *,
    requester: str = OWNER,
    agent_name: str = "creator_research",
) -> None:
    delete_created_agent(config, paths, agent_name=agent_name, requester_id=requester)


def _add_account(paths: RuntimePaths, agent_name: str) -> None:
    state = MatrixState.load(runtime_paths=paths)
    state.add_account(f"agent_{agent_name}", f"mindroom_{agent_name}", None, domain="localhost")
    save_matrix_state(state, paths)


def test_only_owners_may_change_a_created_agent(tmp_path: Path) -> None:
    """Anyone but the owner and co-owners is refused with the owner's name, and nothing is written."""
    config = _load(tmp_path, created=[_created()])
    paths = test_runtime_paths(tmp_path)
    with pytest.raises(_GeneratedAgentError, match=re.escape(OWNER)):
        _update(config, paths, {"role": "Spy"}, requester=STRANGER)
    with pytest.raises(_GeneratedAgentError, match=re.escape(OWNER)):
        generated_agent_settings(config, paths, "creator_research", requester_id=STRANGER)
    with pytest.raises(_GeneratedAgentError, match=re.escape(OWNER)):
        _delete(config, paths, requester=STRANGER)
    with pytest.raises(_GeneratedAgentError, match="creator is not a generated agent"):
        _update(config, paths, {"role": "Spy"}, agent_name="creator")
    assert read_personal_agent_roster_file(paths).created == [_created()]


def test_co_owners_cannot_change_co_owners(tmp_path: Path) -> None:
    """A co-owner may edit the agent, but only the owner decides who co-owns it; a bridge alias is stored as its person."""
    data = deepcopy(BASE) | {"authorization": {"aliases": {"@carol:localhost": ["@carol_bridge:localhost"]}}}
    config = _load(tmp_path, data, created=[_created()])
    paths = test_runtime_paths(tmp_path)
    _update(config, paths, {"role": "Scout"}, requester=CO_OWNER)
    with pytest.raises(_GeneratedAgentError, match=f"Only {re.escape(OWNER)} may change co_owners"):
        _update(config, paths, {"co_owners": []}, requester=CO_OWNER)
    _update(config, paths, {"co_owners": ["@carol_bridge:localhost"]})
    [entry] = read_personal_agent_roster_file(paths).created
    assert entry.edits == GeneratedAgentEdits(display_name="Research", role="Scout")
    assert entry.co_owners == ["@carol:localhost"]
    _update(config, paths, {"role": "Helper"}, requester="@carol_bridge:localhost")
    assert read_personal_agent_roster_file(paths).created[0].edits.role == "Helper"
    _update(config, paths, {"co_owners": None})
    assert read_personal_agent_roster_file(paths).created[0].co_owners == []


def test_co_owners_must_be_concrete_people(tmp_path: Path) -> None:
    """Globs, brackets, the owner again and MindRoom accounts would break loading or hand the agent to an agent."""
    config = _load(tmp_path, created=[_created()])
    paths = test_runtime_paths(tmp_path)
    _add_account(paths, "helper")
    for co_owners, refusal in (
        (["@*:localhost"], "concrete Matrix user IDs"),
        (["@b[o]b:localhost"], "brackets"),
        ([OWNER], "Duplicate"),
        (["@mindroom_helper:localhost"], "MindRoom account"),
        ("@bob:localhost", "co_owners"),
    ):
        with pytest.raises(ValueError, match=re.escape(refusal)):
            _update(config, paths, {"co_owners": co_owners})
    assert read_personal_agent_roster_file(paths).created == [_created()]


def test_update_refuses_tools_outside_the_ceiling_with_the_allowed_names(tmp_path: Path) -> None:
    """A tool the base does not offer is refused, naming the tools it does."""
    config = _load(tmp_path, created=[_created()])
    paths = test_runtime_paths(tmp_path)
    with pytest.raises(_GeneratedAgentError, match=ALLOWED):
        _update(config, paths, {"tools": ["calculator", "browser"]})
    _update(config, paths, {"tools": ["calculator"]})
    assert read_personal_agent_roster_file(paths).created[0].edits.tools == ["calculator"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["openclaw_compat", *sorted(_DEFER_PROHIBITED_CONTROL_TOOLS)])
async def test_a_tool_that_cannot_be_deferred_can_be_chosen(tmp_path: Path, tool: str) -> None:
    """A preset or control-plane tool is copied without defer or initial, so created and edited agents still load."""
    data = deepcopy(BASE)
    data["agents"]["creator"]["tools"] = ["calculator", tool]
    config = _load(tmp_path, data, created=[_created()])
    paths = test_runtime_paths(tmp_path)
    _update(config, paths, {"tools": ["calculator", tool]})
    await _create(config, paths, _register(), name="writer", tools=[tool])
    with capture_logs() as logs:
        reloaded = Config.validate_with_runtime(deepcopy(data), paths)
    assert "generated_agents_invalid" not in [log["event"] for log in logs]
    for name in ("creator_research", "creator_writer"):
        assert tool in [entry.name for entry in authored_tool_entries(reloaded, name)]


@pytest.mark.asyncio
async def test_a_change_the_next_load_would_reject_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Such a change would leave out every generated agent at the next load, so it is refused and nothing is stored."""
    config = _load(tmp_path, created=[_created()])
    paths = test_runtime_paths(tmp_path)
    real = generated_agents._generated_agent_data

    def broken_for_some(*args: Any, created: CreatedAgentEntry | None = None) -> tuple[dict[str, Any], list[str]]:  # noqa: ANN401
        data, dropped = real(*args, created=created)
        broken = data.get("role") == "Broken" or (created is not None and created.agent_name == "creator_writer")
        return ({**data, "display_name": 5} if broken else data), dropped

    monkeypatch.setattr(generated_agents, "_generated_agent_data", broken_for_some)
    with pytest.raises(_GeneratedAgentError, match="would not load") as raised:
        _update(config, paths, {"role": "Broken"})
    assert str(raised.value) == (
        "The agent would not load with this change, so nothing was stored: "
        "agents → creator_research → display_name: Input should be a valid string"
    )
    register = _register()
    with pytest.raises(_GeneratedAgentError, match="would not load"):
        await _create(config, paths, register, name="writer")
    register.assert_not_awaited()
    assert read_personal_agent_roster_file(paths).created == [_created()]
    _update(config, paths, {"role": "Fine"})
    assert read_personal_agent_roster_file(paths).created[0].edits.role == "Fine"


def test_update_refuses_fields_that_are_not_editable(tmp_path: Path) -> None:
    """Only the edit fields, plus co_owners on a created agent, change, and only to values of the right type."""
    roster = PersonalAgentRoster(entries=[_personal()], created=[_created()])
    config = _load(tmp_path, _with_template(), entries=roster.entries, created=roster.created)
    paths = test_runtime_paths(tmp_path)
    with pytest.raises(
        _GeneratedAgentError,
        match="model cannot be changed; allowed: display_name, role, instructions, tools, co_owners",
    ):
        _update(config, paths, {"model": "opus"})
    with pytest.raises(_GeneratedAgentError, match="co_owners cannot be changed"):
        _update(config, paths, {"co_owners": [CO_OWNER]}, agent_name="mind_alice")
    with pytest.raises(ValueError, match="instructions"):
        _update(config, paths, {"instructions": "Be brief"})
    assert read_personal_agent_roster_file(paths) == roster


def test_null_restores_the_base_value(tmp_path: Path) -> None:
    """A None value drops the stored edit, so the next load uses the creator's value."""
    config = _load(tmp_path, created=[_created(edits=GeneratedAgentEdits(display_name="Research", role="Scout"))])
    paths = test_runtime_paths(tmp_path)
    _update(config, paths, {"role": None})
    assert read_personal_agent_roster_file(paths).created[0].edits == GeneratedAgentEdits(display_name="Research")
    assert Config.validate_with_runtime(deepcopy(BASE), paths).agents["creator_research"].role == "Makes agents"


def test_settings_show_the_current_values_and_the_allowed_tools(tmp_path: Path) -> None:
    """Owners see the current editable values and the tools to choose from; created agents also show their owners."""
    config = _load(tmp_path, _with_template(), entries=[_personal()], created=[_created()])
    paths = test_runtime_paths(tmp_path)
    assert generated_agent_settings(config, paths, "creator_research", requester_id=CO_OWNER) == {
        "agent_name": "creator_research",
        "edits": {
            "display_name": "Research",
            "role": "Makes agents",
            "instructions": ["Be useful"],
            "tools": ["calculator", "shell"],
        },
        "allowed_tools": ["calculator", "shell"],
        "owner": OWNER,
        "co_owners": [CO_OWNER],
    }
    settings = generated_agent_settings(config, paths, "mind_alice", requester_id=OWNER)
    assert settings["edits"]["display_name"] == "Alice's Mind"
    assert settings["allowed_tools"] == ["calculator", "shell"]
    assert set(settings) == {"agent_name", "edits", "allowed_tools"}


def test_personal_agent_owner_may_update_but_not_delete(tmp_path: Path) -> None:
    """The owner of a personal agent edits it within its template, and nobody else does."""
    config = _load(tmp_path, _with_template(), entries=[_personal()])
    paths = test_runtime_paths(tmp_path)
    _update(config, paths, {"display_name": "Scout", "tools": ["shell"]}, agent_name="mind_alice")
    with pytest.raises(_GeneratedAgentError, match=re.escape(OWNER)):
        _update(config, paths, {"role": "Spy"}, requester=CO_OWNER, agent_name="mind_alice")
    with pytest.raises(_GeneratedAgentError, match="mind_alice is a personal agent and cannot be deleted"):
        _delete(config, paths, agent_name="mind_alice")
    edits = GeneratedAgentEdits(display_name="Scout", tools=["shell"])
    assert read_personal_agent_roster_file(paths).entries == [_personal(edits=edits)]
    assert Config.validate_with_runtime(_with_template(), paths).agents["mind_alice"].display_name == "Scout"


@pytest.mark.asyncio
async def test_create_names_the_agent_after_its_creator_and_registers_it(tmp_path: Path) -> None:
    """The entry is pending while the account registers, then active and loaded with the requester as owner."""
    config = _config(tmp_path)
    paths = test_runtime_paths(tmp_path)
    pending: list[bool] = []
    register = _register(pending)

    user_id = await _create(config, paths, register, name=" Research! ", tools=["shell"])

    assert user_id == "@mindroom_creator_research:localhost"
    register.assert_awaited_once_with("http://localhost:8008", "creator_research", "Research", paths)
    assert pending == [True]
    assert read_personal_agent_roster_file(paths).created == [
        CreatedAgentEntry(
            agent_name="creator_research",
            creator="creator",
            owner=OWNER,
            edits=GeneratedAgentEdits(display_name="Research", role="Finds things", tools=["shell"]),
        ),
    ]
    reloaded = Config.validate_with_runtime(deepcopy(BASE), paths)
    assert reloaded.generated_agent("creator_research") is not None
    assert [entry.name for entry in authored_tool_entries(reloaded, "creator_research")] == ["shell"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tools", [None, ["slack"]])
async def test_a_slack_answering_creator_creates_an_agent_without_its_room(
    tmp_path: Path,
    tools: list[str] | None,
) -> None:
    """The created agent reads the creator's Slack channels, has no room to answer in, and loads."""
    config = Config.validate_with_runtime(_slack_creator(), test_runtime_paths(tmp_path))
    paths = test_runtime_paths(tmp_path)

    await _create(config, paths, _register(), tools=tools)

    assert [entry.agent_name for entry in read_personal_agent_roster_file(paths).created] == ["creator_research"]
    reloaded = Config.validate_with_runtime(_slack_creator(), paths)
    assert reloaded.generated_agent("creator_research") is not None
    assert _slack_overrides(reloaded, "creator_research") == {"channels": ["C0123ABCD"]}
    assert _slack_overrides(reloaded, "creator") == {"channels": ["C0123ABCD"], "room": "lobby"}
    assert _slack_overrides(config, "creator") == {"channels": ["C0123ABCD"], "room": "lobby"}


@pytest.mark.asyncio
async def test_create_refuses_at_the_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Active agents and those still registering count against each owner's cap; other owners keep theirs."""
    config = _load(tmp_path, created=[_created(co_owners=[]), _created(agent_name="creator_draft", pending=True)])
    paths = test_runtime_paths(tmp_path)
    monkeypatch.setattr(personal_agents, "_REGISTERING", {"creator_draft"})
    register = _register()
    with pytest.raises(_GeneratedAgentError, match=re.escape(f"{OWNER} already has 2")):
        await _create(config, paths, register, name="writer")
    register.assert_not_awaited()
    await _create(config, paths, register, name="writer", requester=CO_OWNER)
    assert [entry.owner for entry in read_personal_agent_roster_file(paths).created] == [OWNER, OWNER, CO_OWNER]


@pytest.mark.asyncio
async def test_a_pending_entry_left_by_a_crash_frees_its_name_and_place(tmp_path: Path) -> None:
    """A pending entry that this runtime, the roster's only writer, is not registering is removed by the next creation."""
    data = deepcopy(BASE)
    data["agents"]["creator"]["created_agents_per_owner"] = 1
    config = _load(tmp_path, data, created=[_created(pending=True)])
    paths = test_runtime_paths(tmp_path)
    await _create(config, paths, _register(), name="research")
    [entry] = read_personal_agent_roster_file(paths).created
    assert (entry.agent_name, entry.co_owners, entry.pending) == ("creator_research", [], False)
    assert set() == personal_agents._REGISTERING


@pytest.mark.asyncio
async def test_create_refuses_a_taken_name(tmp_path: Path) -> None:
    """Another entry, an agent saved but not loaded, a left account and a name without letters are refused."""
    config = _load(tmp_path, created=[_created(owner=CO_OWNER, co_owners=[])])
    paths = test_runtime_paths(tmp_path)
    paths.config_path.write_text("agents:\n  creator_saved:\n    display_name: Saved\n", encoding="utf-8")
    _add_account(paths, "creator_old")
    register = _register()
    for name in ("research", "Saved", "old"):
        with pytest.raises(_GeneratedAgentError, match=f"creator_{name.lower()} is taken; choose another name"):
            await _create(config, paths, register, name=name)
    with pytest.raises(_GeneratedAgentError, match="choose another name"):
        await _create(config, paths, register, name="!!!")
    register.assert_not_awaited()
    assert len(read_personal_agent_roster_file(paths).created) == 1


def test_taken_names_include_reserved_and_created_names(tmp_path: Path) -> None:
    """Personal and created agents never take a reserved name or a created agent's name."""
    taken = _taken_agent_names(
        _config(tmp_path),
        test_runtime_paths(tmp_path),
        PersonalAgentRoster(created=[_created(pending=True)]),
    )
    assert {"router", "creator", "creator_research"} <= taken


@pytest.mark.asyncio
async def test_create_refuses_a_mindroom_account_requester(tmp_path: Path) -> None:
    """An agent's account, or an ID with brackets, never owns a created agent."""
    config = _config(tmp_path)
    paths = test_runtime_paths(tmp_path)
    _add_account(paths, "mind_bob")
    register = _register()
    with pytest.raises(_GeneratedAgentError, match="MindRoom account"):
        await _create(config, paths, register, requester="@mindroom_mind_bob:localhost")
    with pytest.raises(_GeneratedAgentError, match="brackets"):
        await _create(config, paths, register, requester="@al[i]ce:localhost")
    register.assert_not_awaited()
    assert read_personal_agent_roster_file(paths).created == []


@pytest.mark.asyncio
async def test_create_refuses_tools_the_creator_lacks(tmp_path: Path) -> None:
    """A created agent's tools come from its creator's effective list, and a refusal names that list."""
    register = _register()
    with pytest.raises(_GeneratedAgentError, match=ALLOWED):
        await _create(_config(tmp_path), test_runtime_paths(tmp_path), register, tools=["browser"])
    register.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_registration_leaves_no_entry(tmp_path: Path) -> None:
    """A failed or cancelled registration removes the pending entry, so it holds no name and no cap slot."""
    config = _config(tmp_path)
    paths = test_runtime_paths(tmp_path)
    with pytest.raises(_GeneratedAgentError, match="M_EXCLUSIVE"):
        await _create(config, paths, AsyncMock(side_effect=RuntimeError("M_EXCLUSIVE")))
    assert read_personal_agent_roster_file(paths).created == []
    with pytest.raises(asyncio.CancelledError):
        await _create(config, paths, AsyncMock(side_effect=asyncio.CancelledError()))
    assert read_personal_agent_roster_file(paths).created == []


@pytest.mark.asyncio
async def test_a_cancelled_reservation_leaves_no_entry(tmp_path: Path) -> None:
    """Cancelling while the pending entry is written finishes the write, then removes the entry before registering."""
    config = _config(tmp_path)
    paths = test_runtime_paths(tmp_path)
    writing, release = threading.Event(), threading.Event()

    def slow_reserve(*args: Any) -> None:  # noqa: ANN401
        writing.set()
        release.wait(5)
        _reserve_created_agent(*args)

    register = _register()
    with patch("mindroom.personal_agents._reserve_created_agent", new=slow_reserve):
        task = asyncio.create_task(_create(config, paths, register))
        try:
            assert await asyncio.to_thread(writing.wait, 5)
            task.cancel()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    register.assert_not_awaited()
    assert read_personal_agent_roster_file(paths).created == []


def test_delete_removes_the_entry_for_its_owner_only(tmp_path: Path) -> None:
    """Only the owner deletes a created agent, and the other entries stay."""
    config = _load(tmp_path, created=[_created(), _created(agent_name="creator_writer")])
    paths = test_runtime_paths(tmp_path)
    with pytest.raises(_GeneratedAgentError, match=f"Only {re.escape(OWNER)} may delete creator_research"):
        _delete(config, paths, requester=CO_OWNER)
    _delete(config, paths)
    assert [entry.agent_name for entry in read_personal_agent_roster_file(paths).created] == ["creator_writer"]


@pytest.mark.asyncio
async def test_writes_keep_the_other_roster_list(tmp_path: Path) -> None:
    """Writing created agents keeps personal entries, and writing personal entries keeps created agents."""
    config = _load(tmp_path, _with_template(), entries=[_personal()], created=[_created()])
    paths = test_runtime_paths(tmp_path)
    _update(config, paths, {"role": "Scout"})
    await _create(config, paths, _register(), name="writer")
    _delete(config, paths)
    assert read_personal_agent_roster_file(paths).entries == [_personal()]

    created = read_personal_agent_roster_file(paths).created
    assert [entry.agent_name for entry in created] == ["creator_writer"]
    _update(config, paths, {"role": "Scout"}, agent_name="mind_alice")
    _append_personal_agent_roster_entry(paths, _personal(CO_OWNER, pending=True))
    _settle_roster_entry(paths, _personal(CO_OWNER, pending=True), _personal(CO_OWNER))
    assert read_personal_agent_roster_file(paths) == PersonalAgentRoster(
        entries=[_personal(edits=GeneratedAgentEdits(role="Scout")), _personal(CO_OWNER)],
        created=created,
    )
