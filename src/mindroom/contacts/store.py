"""Primary-only storage of contacts in the tracking directory."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - pydantic resolves the field type at runtime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mindroom.constants import tracking_dir
from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

type PartyKind = Literal["agent", "person", "remote_agent"]
type ContactStatus = Literal["pending", "active", "declined", "removed"]
type SideName = Literal["requester", "target"]

_SIDES: tuple[SideName, SideName] = ("requester", "target")


class ContactParty(BaseModel):
    """One end of a contact: a personal agent by name, a person by canonical Matrix ID, or another deployment's agent.

    Another deployment's agent is known by its Matrix ID alone: no deployment can verify who owns another's agent, so
    it has no owner here, while every party of this deployment has one.
    """

    model_config = ConfigDict(extra="forbid")

    kind: PartyKind
    id: str
    owner: str | None

    @model_validator(mode="after")
    def owned_unless_remote(self) -> ContactParty:
        """Refuse an owner on another deployment's agent and a missing owner on any party here."""
        if (self.kind == "remote_agent") != (self.owner is None):
            msg = "Only an agent of another deployment has no owner, and it never has one"
            raise ValueError(msg)
        return self

    @property
    def local_owner(self) -> str:
        """Return the owner of a party of this deployment, which every one of them has."""
        if self.owner is None:
            msg = f"{self.id} is an agent of another deployment and has no owner here"
            raise ValueError(msg)
        return self.owner


class ContactSide(BaseModel):
    """What the owner of one side added to a contact; a person's side stays empty."""

    model_config = ConfigDict(extra="forbid")

    rules: str | None = None
    tools: list[str] | None = None


class ContactRecord(BaseModel):
    """One contact and where its request came from, its notice, its room, and when it was asked and answered."""

    model_config = ConfigDict(extra="forbid")

    id: str
    status: ContactStatus
    requester: ContactParty
    target: ContactParty
    reason: str
    requester_side: ContactSide
    target_side: ContactSide
    # A request another deployment sent was asked in no room here.
    origin_room_id: str | None
    origin_thread_id: str | None
    notice_event_id: str | None = None
    room_id: str | None = None
    # LEGACY_COMPAT: contact records without request and decision dates
    # Legacy format: a record in tracking/contacts.json with no requested_at or decided_at field.
    # Last legacy release: unreleased replacement; v2026.11.113, which added the contacts store, through v2026.11.122,
    # the newest release when this was written, write records without them; the replacement dates every new request
    # and decision.
    # Handling: both read as None, so the Connections page shows no date for such a contact; nothing is migrated.
    # Coverage: tests/contacts/test_store.py::test_a_record_written_before_dates_reads_them_unset
    requested_at: datetime | None = None
    decided_at: datetime | None = None

    @model_validator(mode="after")
    def asked_here_unless_remote(self) -> ContactRecord:
        """Refuse a request of this deployment without the room it was asked in, and one from another with a room."""
        if (self.requester.kind == "remote_agent") != (self.origin_room_id is None):
            msg = "Only a request from another deployment's agent has no origin room, and it never has one"
            raise ValueError(msg)
        return self

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

    def owners(self) -> list[str]:
        """Return the owners of this deployment's parties, requester's first; another deployment's agent has none."""
        return [party.owner for party in (self.requester, self.target) if party.owner is not None]

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


def owner_contacts(runtime_paths: RuntimePaths, owner: str) -> list[tuple[ContactRecord, SideName]]:
    """Return the owner's contacts that are not removed, each with the owner's side, in the order they were asked."""
    return [
        (record, side)
        for record in read_contacts(runtime_paths).contacts
        if (side := record.owner_side(owner)) is not None and record.status != "removed"
    ]


def contact_room_ids(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    *,
    any_status: bool = False,
) -> frozenset[str]:
    """Return the rooms this agent keeps across restarts.

    Those are its active contacts' rooms, and the rooms of its pending requests to agents of other deployments, since
    such a room is the request itself and needs a member left for the other agent to join.

    With ``any_status`` every room its contacts have or had counts, pending and removed ones included, since a room
    is named on a pending contact before anyone is invited and a contact keeps its room once removed.
    """
    return frozenset(
        record.room_id
        for record, side in agent_contacts(config, runtime_paths, agent_name)
        if record.room_id is not None
        and (
            any_status
            or record.status == "active"
            or (record.status == "pending" and side == "requester" and record.target.kind == "remote_agent")
        )
    )


def remove_kept_contacts(runtime_paths: RuntimePaths, agent_name: str) -> list[str]:
    """Mark every contact whose room this agent keeps removed, and return their IDs.

    Those are its active contacts and its pending requests to agents of other deployments. An agent that no longer has
    contacts leaves their rooms, so its contacts end for both sides, and restoring its contacts later revives none of
    them. Nothing is written while it has no such contact.
    """

    def active(record: ContactRecord) -> bool:
        side = record.agent_side(agent_name)
        if side is None:
            return False
        sent_elsewhere = side == "requester" and record.target.kind == "remote_agent"
        return record.status == "active" or (record.status == "pending" and sent_elsewhere)

    if not any(active(record) for record in read_contacts(runtime_paths).contacts):
        return []

    def remove(book: ContactBook) -> list[str]:
        removed = [record for record in book.contacts if active(record)]
        for record in removed:
            record.status = "removed"
        return [record.id for record in removed]

    return update_contacts(runtime_paths, remove)
