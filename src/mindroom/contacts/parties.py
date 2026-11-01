"""Who can be a party of a contact, and how a name or Matrix ID resolves to one."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.contacts.store import ContactParty
from mindroom.entity_resolution import current_entity_id, prepared_entity_names_by_user_id
from mindroom.matrix.personal_room_store import personal_room_record_path, read_personal_room
from mindroom.matrix_identifiers import is_concrete_matrix_user_id
from mindroom.personal_agents import is_mindroom_account_id
from mindroom.requester_identity import resolve_human_requester_alias

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


def agent_party(config: Config, agent_name: str) -> ContactParty | None:
    """Return the party of a personal agent that has contacts, or None for any other agent."""
    agent = config.agents.get(agent_name)
    instance = config.personal_agent_instance(agent_name)
    if agent is None or agent.contacts is None or instance is None:
        return None
    return ContactParty(kind="agent", id=agent_name, owner=instance.user_id)


def person_party(config: Config, runtime_paths: RuntimePaths, user_id: str) -> ContactParty:
    """Return the party of a person by their canonical Matrix ID, so a bridge alias reaches the same person."""
    canonical = resolve_human_requester_alias(user_id, config, runtime_paths)
    if is_mindroom_account_id(canonical, runtime_paths):
        msg = f"{user_id} is a MindRoom account, not a person"
        raise ValueError(msg)
    return ContactParty(kind="person", id=canonical, owner=canonical)


def resolve_target(config: Config, runtime_paths: RuntimePaths, target: str) -> ContactParty:
    """Resolve an agent name, an agent's Matrix ID, or a person's Matrix ID to the party it names."""
    agent_name = (
        target if target in config.agents else prepared_entity_names_by_user_id(config, runtime_paths).get(target)
    )
    if agent_name is None:
        if not is_concrete_matrix_user_id(target):
            msg = f"{target} cannot be contacted: it is neither a personal agent nor a Matrix user ID"
            raise ValueError(msg)
        return person_party(config, runtime_paths, target)
    if config.personal_agent_instance(agent_name) is None:
        msg = f"{target} cannot be contacted: only personal agents and people can be contacted"
        raise ValueError(msg)
    if (party := agent_party(config, agent_name)) is None:
        msg = f"{target} cannot be contacted: contacts are not enabled for it"
        raise ValueError(msg)
    return party


def party_name(config: Config, party: ContactParty) -> str:
    """Return how people see a party named: an agent's display name, or a person's Matrix ID."""
    return party.id if party.kind == "person" else config.agents[party.id].display_name


def party_label(config: Config, party: ContactParty) -> str:
    """Return a party as an agent reads it: a person's Matrix ID, or an agent's name, ID, and owner."""
    if party.kind == "person":
        return party.id
    return f"{party_name(config, party)} ({party.id}), the agent of {party.owner}"


def party_matrix_ids(party: ContactParty, runtime_paths: RuntimePaths) -> frozenset[str]:
    """Return the Matrix accounts that speak for a party: an agent's own account and its owner, or the person."""
    if party.kind == "person":
        return frozenset({party.id})
    return frozenset({current_entity_id(party.id, runtime_paths).full_id, party.owner})


def personal_room_agent(config: Config, user_id: str) -> str | None:
    """Return the personal agent that serves a user's personal room, once the user has one."""
    if config.personal_rooms is None:
        return None
    return config.personal_agent_name(config.personal_rooms.agent, user_id)


def personal_room_of(config: Config, runtime_paths: RuntimePaths, user_id: str) -> tuple[str, str] | None:
    """Return the personal agent and room of a user's personal room, once it exists."""
    agent_name = personal_room_agent(config, user_id)
    if agent_name is None:
        return None
    record = read_personal_room(personal_room_record_path(runtime_paths, agent_name, user_id))
    return (agent_name, record.room_id) if record is not None and record.room_id is not None else None
