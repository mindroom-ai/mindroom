"""Tests for resolving the parties of a contact."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.contacts.parties import (
    agent_party,
    party_matrix_ids,
    person_party,
    personal_room_of,
    resolve_target,
)
from mindroom.contacts.store import ContactParty
from mindroom.entity_resolution import current_entity_id
from mindroom.matrix.personal_room_store import (
    PersonalRoomRecord,
    personal_room_record_path,
    write_personal_room,
)
from tests.conftest import test_runtime_paths
from tests.identity_helpers import persist_entity_accounts
from tests.test_generated_agents import CO_OWNER, OWNER, _load, _personal, _with_template

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

CAROL = "@carol:localhost"
CAROL_BRIDGE = "@telegram_carol:localhost"


def _data(*, contacts: dict[str, Any] | None = None) -> dict[str, Any]:
    """The generated-agents config with a personal template, optionally opted into contacts."""
    data = _with_template()
    data["agents"]["creator"]["contacts"] = {}
    if contacts is not None:
        data["personal_agents"]["mind"]["agent"]["contacts"] = deepcopy(contacts)
    data["authorization"] = {"aliases": {CAROL: [CAROL_BRIDGE]}}
    return data


def _setup(tmp_path: Path, *, contacts: dict[str, Any] | None = None) -> tuple[Config, RuntimePaths]:
    config = _load(tmp_path, _data(contacts=contacts), entries=[_personal(), _personal(CO_OWNER)])
    paths = test_runtime_paths(tmp_path)
    persist_entity_accounts(config, paths)
    return config, paths


def test_agent_party_is_a_personal_agent_with_contacts(tmp_path: Path) -> None:
    """Only a generated personal agent with a contacts object is a party, and its owner is the roster user."""
    config, _ = _setup(tmp_path, contacts={})
    assert agent_party(config, "mind_alice") == ContactParty(kind="agent", id="mind_alice", owner=OWNER)
    assert agent_party(config, "mind_bob") == ContactParty(kind="agent", id="mind_bob", owner=CO_OWNER)
    assert config.agents["creator"].contacts is not None
    assert agent_party(config, "creator") is None
    assert agent_party(config, "missing") is None


def test_agent_party_needs_contacts_set(tmp_path: Path) -> None:
    """A personal agent whose template sets no contacts is not a party."""
    config, _ = _setup(tmp_path)
    assert agent_party(config, "mind_alice") is None


def test_resolve_target_accepts_an_agent_name(tmp_path: Path) -> None:
    """A personal agent's name resolves to an agent party carrying its owner."""
    config, paths = _setup(tmp_path, contacts={})
    assert resolve_target(config, paths, "mind_bob") == ContactParty(kind="agent", id="mind_bob", owner=CO_OWNER)


def test_resolve_target_accepts_an_agent_matrix_id(tmp_path: Path) -> None:
    """The agent's own Matrix account resolves to the same party as its name."""
    config, paths = _setup(tmp_path, contacts={})
    matrix_id = current_entity_id("mind_bob", paths).full_id
    assert resolve_target(config, paths, matrix_id) == resolve_target(config, paths, "mind_bob")


def test_resolve_target_refuses_authored_agents(tmp_path: Path) -> None:
    """An authored agent stays on access, by name or by Matrix ID, even when it sets contacts."""
    config, paths = _setup(tmp_path, contacts={})
    for target in ("creator", current_entity_id("creator", paths).full_id):
        with pytest.raises(ValueError, match=f"^{target} cannot be contacted: only personal agents"):
            resolve_target(config, paths, target)


def test_resolve_target_refuses_agents_without_contacts(tmp_path: Path) -> None:
    """A personal agent whose template has no contacts cannot be contacted."""
    config, paths = _setup(tmp_path)
    with pytest.raises(ValueError, match=r"^mind_bob cannot be contacted: contacts are not enabled for it"):
        resolve_target(config, paths, "mind_bob")


def test_resolve_target_refuses_the_router(tmp_path: Path) -> None:
    """The router is no personal agent, so its Matrix ID names no party."""
    config, paths = _setup(tmp_path, contacts={})
    router_id = current_entity_id("router", paths).full_id
    with pytest.raises(ValueError, match=f"^{router_id} cannot be contacted"):
        resolve_target(config, paths, router_id)


def test_resolve_target_accepts_a_person(tmp_path: Path) -> None:
    """A person's Matrix ID resolves to a person party that owns itself."""
    config, paths = _setup(tmp_path, contacts={})
    assert resolve_target(config, paths, CAROL) == ContactParty(kind="person", id=CAROL, owner=CAROL)


def test_a_bridge_alias_resolves_to_the_canonical_person(tmp_path: Path) -> None:
    """A person reached through a bridge alias is the canonical user."""
    config, paths = _setup(tmp_path, contacts={})
    assert person_party(config, paths, CAROL_BRIDGE) == ContactParty(kind="person", id=CAROL, owner=CAROL)
    assert resolve_target(config, paths, CAROL_BRIDGE) == ContactParty(kind="person", id=CAROL, owner=CAROL)


def test_a_person_party_is_never_a_mindroom_account(tmp_path: Path) -> None:
    """Agents and the router are not people, so their Matrix IDs are refused."""
    config, paths = _setup(tmp_path, contacts={})
    for entity in ("mind_alice", "router"):
        matrix_id = current_entity_id(entity, paths).full_id
        with pytest.raises(ValueError, match=f"{matrix_id} is a MindRoom account"):
            person_party(config, paths, matrix_id)


def test_resolve_target_refuses_what_is_no_matrix_id(tmp_path: Path) -> None:
    """An unknown name is neither an agent nor a Matrix ID, so it is not taken for a person."""
    config, paths = _setup(tmp_path, contacts={})
    with pytest.raises(ValueError, match=r"^nobody cannot be contacted: it is neither a personal agent nor a Matrix"):
        resolve_target(config, paths, "nobody")


def test_party_matrix_ids_name_the_account_and_the_owner(tmp_path: Path) -> None:
    """An agent party answers for its account and its owner, and a person only for themself."""
    config, paths = _setup(tmp_path, contacts={})
    agent = resolve_target(config, paths, "mind_alice")
    assert party_matrix_ids(agent, paths) == frozenset({current_entity_id("mind_alice", paths).full_id, OWNER})
    assert party_matrix_ids(person_party(config, paths, CAROL), paths) == frozenset({CAROL})


def test_personal_room_of_reads_the_users_room_with_their_agent(tmp_path: Path) -> None:
    """A user's personal room is the one their template-generated agent recorded, once it exists."""
    data = _data(contacts={})
    data["personal_rooms"] = {"agent": "mind", "onboarding_rooms": ["lobby"]}
    config = _load(tmp_path, data, entries=[_personal(), _personal(CO_OWNER)])
    paths = test_runtime_paths(tmp_path)
    assert personal_room_of(config, paths, OWNER) is None
    record = PersonalRoomRecord(user_id=OWNER, alias="personal-alice", source_room_id="!lobby:localhost")
    write_personal_room(personal_room_record_path(paths, "mind_alice", OWNER), record)
    assert personal_room_of(config, paths, OWNER) is None
    record.room_id = "!personal:localhost"
    write_personal_room(personal_room_record_path(paths, "mind_alice", OWNER), record)
    assert personal_room_of(config, paths, OWNER) == ("mind_alice", "!personal:localhost")
    assert personal_room_of(config, paths, CO_OWNER) is None
    assert personal_room_of(config, paths, CAROL) is None


def test_personal_room_of_is_none_without_personal_rooms(tmp_path: Path) -> None:
    """Without the personal_rooms setting, no user has a personal room."""
    config, paths = _setup(tmp_path, contacts={})
    assert personal_room_of(config, paths, OWNER) is None
