"""Tests for the contacts store."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from mindroom.contacts.store import (
    ContactBook,
    ContactParty,
    ContactRecord,
    ContactSide,
    _contacts_path,
    contact_of_room,
    read_contacts,
    update_contacts,
)
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

ALICE = "@alice:localhost"
BOB = "@bob:localhost"


def _record(contact_id: str = "c1", **kwargs: object) -> ContactRecord:
    fields: dict[str, object] = {
        "id": contact_id,
        "status": "pending",
        "requester": ContactParty(kind="agent", id="mind_alice", owner=ALICE),
        "target": ContactParty(kind="agent", id="mind_bob", owner=BOB),
        "reason": "Plan a dinner",
        "requester_side": ContactSide(),
        "target_side": ContactSide(),
        "origin_room_id": "!alice:localhost",
        "origin_thread_id": "$thread",
        "created_at": 1.0,
    }
    return ContactRecord.model_validate(fields | kwargs)


@pytest.fixture
def paths(tmp_path: Path) -> RuntimePaths:
    """Runtime paths whose tracking directory holds the contacts file."""
    return test_runtime_paths(tmp_path)


def test_a_missing_file_is_an_empty_book(paths: RuntimePaths) -> None:
    """Before the first contact, the book is empty and no file is created by reading."""
    assert read_contacts(paths) == ContactBook()
    assert not _contacts_path(paths).exists()
    assert contact_of_room(paths, "!room:localhost", "mind_bob") is None


def test_update_contacts_round_trips_and_read_sees_new_mtime(paths: RuntimePaths) -> None:
    """A read after each update returns what that update wrote, even though the earlier read was cached."""
    assert update_contacts(paths, lambda book: book.contacts.append(_record()) or "added") == "added"
    first = read_contacts(paths)
    assert [record.id for record in first.contacts] == ["c1"]
    assert first.contacts[0].status == "pending"
    assert read_contacts(paths) is first

    def activate(book: ContactBook) -> ContactRecord:
        record = book.contacts[0]
        record.status = "active"
        record.room_id = "!room:localhost"
        return record

    assert update_contacts(paths, activate).status == "active"
    second = read_contacts(paths)
    assert second.contacts[0].status == "active"
    assert second.contacts[0].room_id == "!room:localhost"
    assert first.contacts[0].status == "pending"


def test_update_contacts_leaves_the_file_alone_when_the_change_raises(paths: RuntimePaths) -> None:
    """A refused change stores nothing, and the next read sees the earlier book."""
    update_contacts(paths, lambda book: book.contacts.append(_record()))

    def refuse(book: ContactBook) -> None:
        book.contacts.clear()
        msg = "refused"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="refused"):
        update_contacts(paths, refuse)
    assert [record.id for record in read_contacts(paths).contacts] == ["c1"]


def test_invalid_contacts_file_raises(paths: RuntimePaths) -> None:
    """A corrupt file is reported with its path instead of reading as no contacts."""
    path = _contacts_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match=f"^Contacts file {re.escape(str(path))} is invalid: "):
        read_contacts(paths)
    with pytest.raises(ValueError, match=r"^Contacts file"):
        update_contacts(paths, lambda book: book.contacts.append(_record()))


def test_contact_files_reject_unknown_fields(paths: RuntimePaths) -> None:
    """A file with a field this version does not know is invalid."""
    path = _contacts_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"contacts": [], "extra": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match=r"^Contacts file"):
        read_contacts(paths)


def test_a_contact_keeps_its_room_once_removed(paths: RuntimePaths) -> None:
    """A room belongs to its contact in any status, and only to the agents that are its parties."""
    removed = _record("c1", status="removed", room_id="!removed:localhost")
    active = _record("c2", status="active", room_id="!room:localhost")
    update_contacts(paths, lambda book: book.contacts.extend([_record("c0"), removed, active]))

    assert contact_of_room(paths, "!removed:localhost", "mind_bob") == (removed, "target")
    assert contact_of_room(paths, "!room:localhost", "mind_bob") == (active, "target")
    assert contact_of_room(paths, "!room:localhost", "mind_alice") == (active, "requester")
    assert contact_of_room(paths, "!room:localhost", "other") is None
    assert contact_of_room(paths, "!elsewhere:localhost", "mind_bob") is None


def test_record_helpers_name_the_side_of_an_agent_or_owner() -> None:
    """Sides resolve by agent name or owner, and a person party has no agent side."""
    record = _record(
        target=ContactParty(kind="person", id="@carol:localhost", owner="@carol:localhost"),
        requester_side=ContactSide(rules="Be brief", tools=["calculator"]),
    )
    assert record.party("requester").id == "mind_alice"
    assert record.party("target").kind == "person"
    assert record.side("requester") == ContactSide(rules="Be brief", tools=["calculator"])
    assert record.side("target") == ContactSide()
    assert record.other("requester") == "target"
    assert record.other("target") == "requester"
    assert record.agent_side("mind_alice") == "requester"
    assert record.agent_side("@carol:localhost") is None
    assert record.agent_side("nobody") is None
    assert record.owner_side(ALICE) == "requester"
    assert record.owner_side("@carol:localhost") == "target"
    assert record.owner_side(BOB) is None
