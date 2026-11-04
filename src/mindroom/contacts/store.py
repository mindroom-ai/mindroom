"""Primary-only storage of contacts in the tracking directory."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from mindroom.constants import tracking_dir
from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

type _PartyKind = Literal["agent", "person"]
type _ContactStatus = Literal["pending", "active", "declined", "removed"]
type SideName = Literal["requester", "target"]

_SIDES: tuple[SideName, SideName] = ("requester", "target")


class ContactParty(BaseModel):
    """One end of a contact: a personal agent by name, or a person by canonical Matrix ID."""

    model_config = ConfigDict(extra="forbid")

    kind: _PartyKind
    id: str
    owner: str


class ContactSide(BaseModel):
    """What the owner of one side added to a contact; a person's side stays empty."""

    model_config = ConfigDict(extra="forbid")

    rules: str | None = None
    tools: list[str] | None = None


class ContactRecord(BaseModel):
    """One contact and where its request came from, its notice, and its room."""

    model_config = ConfigDict(extra="forbid")

    id: str
    status: _ContactStatus
    requester: ContactParty
    target: ContactParty
    reason: str
    requester_side: ContactSide
    target_side: ContactSide
    origin_room_id: str
    origin_thread_id: str | None
    notice_event_id: str | None = None
    room_id: str | None = None

    def party(self, side: SideName) -> ContactParty:
        """Return the party on one side."""
        return self.requester if side == "requester" else self.target

    def side(self, side: SideName) -> ContactSide:
        """Return what the owner of one side set."""
        return self.requester_side if side == "requester" else self.target_side

    def counterparty(self, side: SideName) -> ContactParty:
        """Return the party on the opposite side."""
        return self.target if side == "requester" else self.requester

    def agent_side(self, agent_name: str) -> SideName | None:
        """Return the side whose party is this agent."""
        return next(
            (side for side in _SIDES if (party := self.party(side)).kind == "agent" and party.id == agent_name),
            None,
        )

    def owner_side(self, user_id: str) -> SideName | None:
        """Return the side whose party this user owns."""
        return next((side for side in _SIDES if self.party(side).owner == user_id), None)


class ContactBook(BaseModel):
    """Every contact the primary knows."""

    model_config = ConfigDict(extra="forbid")

    contacts: list[ContactRecord] = Field(default_factory=list)


# Parsed books by file path, each tagged with the modification time it was read at.
_BOOKS: dict[Path, tuple[int, ContactBook]] = {}


def _contacts_path(runtime_paths: RuntimePaths) -> Path:
    """Return the primary-only file of contacts, which no worker mounts."""
    return tracking_dir(runtime_paths) / "contacts.json"


def _parse(path: Path, text: str) -> ContactBook:
    try:
        return ContactBook.model_validate_json(text)
    except ValueError as exc:  # JSON and pydantic errors are both ValueErrors.
        msg = f"Contacts file {path} is invalid: {exc}"
        raise ValueError(msg) from exc


def read_contacts(runtime_paths: RuntimePaths) -> ContactBook:
    """Return the cached book, or an empty one before the first contact; callers must not change it."""
    path = _contacts_path(runtime_paths)
    try:
        mtime_ns = path.stat().st_mtime_ns
    except FileNotFoundError:
        return ContactBook()
    cached = _BOOKS.get(path)
    if cached is not None and cached[0] == mtime_ns:
        return cached[1]
    book = _parse(path, path.read_text(encoding="utf-8"))
    _BOOKS[path] = (mtime_ns, book)
    return book


def update_contacts[T](runtime_paths: RuntimePaths, change: Callable[[ContactBook], T]) -> T:
    """Apply one change to the stored book under the file lock, store the result, and return what the change returns.

    A change that raises stores nothing.
    """
    path = _contacts_path(runtime_paths)
    with advisory_file_lock(tracking_dir(runtime_paths) / "contacts.lock"):
        try:
            book = _parse(path, path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            book = ContactBook()
        result = change(book)
        write_json_file_durable(path, book.model_dump(mode="json"), strict_atomic_replace=True)
        # A write that lands within the previous write's timestamp tick would otherwise read as unchanged.
        _BOOKS.pop(path, None)
    return result


def agent_contacts(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
) -> list[tuple[ContactRecord, SideName]]:
    """Return every contact this agent is a party of, in any status, with its side.

    Only a personal agent can be a party, so only it reads the store, and a corrupt store stops no other agent.
    """
    if config.personal_agent_instance(agent_name) is None:
        return []
    return [
        (record, side)
        for record in read_contacts(runtime_paths).contacts
        if (side := record.agent_side(agent_name)) is not None
    ]


def contact_room_ids(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    *,
    any_status: bool = False,
) -> frozenset[str]:
    """Return the rooms of the active contacts this agent is a party of, which it keeps across restarts.

    With ``any_status`` every room its contacts have or had counts, pending and removed ones included, since a room
    is named on a pending contact before anyone is invited and a contact keeps its room once removed.
    """
    return frozenset(
        record.room_id
        for record, _side in agent_contacts(config, runtime_paths, agent_name)
        if (any_status or record.status == "active") and record.room_id is not None
    )


def remove_active_contacts(runtime_paths: RuntimePaths, agent_name: str) -> list[str]:
    """Mark every active contact this agent is a party of removed, and return their IDs.

    An agent that no longer has contacts leaves their rooms, so its contacts end for both sides, and restoring its
    contacts later revives none of them. Nothing is written while it has no active contact.
    """

    def active(record: ContactRecord) -> bool:
        return record.status == "active" and record.agent_side(agent_name) is not None

    if not any(active(record) for record in read_contacts(runtime_paths).contacts):
        return []

    def remove(book: ContactBook) -> list[str]:
        removed = [record for record in book.contacts if active(record)]
        for record in removed:
            record.status = "removed"
        return [record.id for record in removed]

    return update_contacts(runtime_paths, remove)
