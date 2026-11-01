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
    created_at: float

    def party(self, side: SideName) -> ContactParty:
        """Return the party on one side."""
        return self.requester if side == "requester" else self.target

    def side(self, side: SideName) -> ContactSide:
        """Return what the owner of one side set."""
        return self.requester_side if side == "requester" else self.target_side

    def other(self, side: SideName) -> SideName:
        """Return the opposite side."""
        return "target" if side == "requester" else "requester"

    def agent_side(self, agent_name: str) -> SideName | None:
        """Return the side whose party is this agent."""
        for side in _SIDES:
            party = self.party(side)
            if party.kind == "agent" and party.id == agent_name:
                return side
        return None

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


def _contacts_lock_path(runtime_paths: RuntimePaths) -> Path:
    return tracking_dir(runtime_paths) / "contacts.lock"


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
    with advisory_file_lock(_contacts_lock_path(runtime_paths)):
        try:
            book = _parse(path, path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            book = ContactBook()
        result = change(book)
        write_json_file_durable(path, book.model_dump(mode="json"), strict_atomic_replace=True)
        # A write that lands within the previous write's timestamp tick would otherwise read as unchanged.
        _BOOKS.pop(path, None)
    return result


def _contact_in_room(runtime_paths: RuntimePaths, room_id: str) -> ContactRecord | None:
    """Return the active contact whose room this is."""
    return next(
        (
            record
            for record in read_contacts(runtime_paths).contacts
            if record.status == "active" and record.room_id == room_id
        ),
        None,
    )


def contact_room_ids(runtime_paths: RuntimePaths, agent_name: str) -> frozenset[str]:
    """Return the rooms of the active contacts this agent is a party of, which it keeps across restarts."""
    return frozenset(
        record.room_id
        for record in read_contacts(runtime_paths).contacts
        if record.status == "active" and record.room_id is not None and record.agent_side(agent_name) is not None
    )


def contact_party_in_room(
    runtime_paths: RuntimePaths,
    room_id: str,
    agent_name: str,
) -> tuple[ContactRecord, SideName] | None:
    """Return the contact of this room and the side this agent is on, or None when the agent is no party of it."""
    record = _contact_in_room(runtime_paths, room_id)
    if record is None:
        return None
    side = record.agent_side(agent_name)
    return None if side is None else (record, side)
