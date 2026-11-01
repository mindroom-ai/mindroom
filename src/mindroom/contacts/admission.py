"""Who a turn of an agent party in its contact room runs as."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.contacts.parties import agent_party, party_matrix_ids
from mindroom.contacts.store import contact_of_room
from mindroom.requester_identity import resolve_human_requester_alias

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


@dataclass(frozen=True)
class _ContactAdmission:
    """The contact of the room an event arrived in, and the owner its turn runs as, or None when nobody may start it."""

    contact_id: str
    requester_id: str | None


def contact_admission(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    room_id: str,
    agent_name: str,
    sender_ids: Iterable[str],
) -> _ContactAdmission | None:
    """Return who a turn of this agent in its contact room runs as, or None when the room is no contact room of it.

    Every such turn runs as the agent's owner, but only the counterparty and the owner may start one.
    ``sender_ids`` are every identity the event speaks for: its sender, a relayed original sender, and an acting
    requester; any one of them that is the counterparty or the owner admits it.
    A removed contact's room admits nobody, so a message there starts no turn even when the agent's leave failed.
    Only an agent that can be a party reads the contacts store.
    """
    if agent_party(config, agent_name) is None:
        return None
    found = contact_of_room(runtime_paths, room_id, agent_name)
    if found is None:
        return None
    record, side = found
    if record.status != "active":
        return _ContactAdmission(record.id, None)
    owner = record.party(side).owner
    allowed = party_matrix_ids(record.party(record.other(side)), runtime_paths) | {owner}
    resolved = {resolve_human_requester_alias(sender_id, config, runtime_paths) for sender_id in sender_ids}
    return _ContactAdmission(record.id, owner if not allowed.isdisjoint(resolved) else None)
