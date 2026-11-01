"""Who a turn of an agent party in its contact room runs as."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.contacts.parties import party_matrix_ids
from mindroom.contacts.store import contact_party_in_room
from mindroom.requester_identity import resolve_human_requester_alias

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


@dataclass(frozen=True)
class ContactAdmission:
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
) -> ContactAdmission | None:
    """Return who a turn of this agent in its contact room runs as, or None when the room is no contact room of it.

    Every such turn runs as the agent's owner, but only the counterparty and the owner may start one.
    ``sender_ids`` are every identity the event speaks for: its sender, a relayed original sender, and an acting
    requester; any one of them that is the counterparty or the owner admits it.
    """
    found = contact_party_in_room(runtime_paths, room_id, agent_name)
    if found is None:
        return None
    record, side = found
    owner = record.party(side).owner
    allowed = party_matrix_ids(record.party(record.other(side)), runtime_paths) | {owner}
    resolved = {resolve_human_requester_alias(sender_id, config, runtime_paths) for sender_id in sender_ids}
    return ContactAdmission(record.id, owner if not allowed.isdisjoint(resolved) else None)
