"""Opaque computer capability expiry, isolation, and revocation."""

import asyncio
from dataclasses import replace

import pytest

from mindroom.worker_computer.sessions import ComputerError, ComputerSessionStore, ComputerTarget
from mindroom.workers.models import WorkerSpec


def authorized_target() -> ComputerTarget:
    """Return one already authorized isolated scope."""
    return ComputerTarget(
        "@alice:example.org",
        "!room:example.org",
        "@agent:example.org",
        WorkerSpec("worker"),
        "config",
    )


def test_stream_ticket_is_single_use_and_bound_to_session() -> None:
    """Stream ticket is single use and bound to session."""
    store = ComputerSessionStore(clock=lambda: 100.0)
    session = store.create(authorized_target())
    ticket = store.issue_stream_ticket(session.session_id)
    with pytest.raises(ComputerError):
        store.consume_stream_ticket("another-session", ticket.ticket)
    assert store.consume_stream_ticket(session.session_id, ticket.ticket) is session
    with pytest.raises(ComputerError):
        store.consume_stream_ticket(session.session_id, ticket.ticket)


def test_expiry_capacity_and_revocation_close_streams() -> None:
    """Expiry capacity and revocation close streams."""
    now = [100.0]
    store = ComputerSessionStore(clock=lambda: now[0], capacity=1)
    session = store.create(authorized_target())
    with pytest.raises(ComputerError, match="capacity"):
        store.create(authorized_target())
    ticket = store.issue_stream_ticket(session.session_id)
    now[0] = 131.0
    with pytest.raises(ComputerError):
        store.consume_stream_ticket(session.session_id, ticket.ticket)
    with pytest.raises(ComputerError):
        store.authenticate(session.session_id, "wrong")
    assert store.authenticate(session.session_id, session.session_token) is session
    now[0] = 3700.0
    store.create(authorized_target())
    assert session.closed.is_set()
    with pytest.raises(ComputerError):
        store.authenticate(session.session_id, session.session_token)


def test_replacement_ticket_invalidates_previous_ticket_and_close_revokes_bearer() -> None:
    """Replacement ticket invalidates previous ticket and close revokes bearer."""
    store = ComputerSessionStore()
    session = store.create(authorized_target())
    previous = store.issue_stream_ticket(session.session_id)
    current = store.issue_stream_ticket(session.session_id)
    with pytest.raises(ComputerError):
        store.consume_stream_ticket(session.session_id, previous.ticket)
    assert store.consume_stream_ticket(session.session_id, current.ticket) is session
    store.close(session.session_id)
    with pytest.raises(ComputerError):
        store.authenticate(session.session_id, session.session_token)


def test_requester_quota_preserves_other_viewers_and_reclaims_slots() -> None:
    """A single requester cannot consume global capacity; close and expiry restore admission."""
    now = [100.0]
    store = ComputerSessionStore(clock=lambda: now[0])
    target = authorized_target()
    sessions = [store.create(target) for _ in range(8)]
    with pytest.raises(ComputerError) as denied:
        store.create(replace(target, room_id="!other:example.org"))
    assert denied.value.status_code == 429
    other = store.create(replace(target, requester_id="@bob:example.org"))
    assert all(store.get(session.session_id) is session for session in sessions)
    store.close(sessions[0].session_id)
    replacement = store.create(target)
    now[0] = 3700.0
    assert store.create(target).target == target
    assert replacement.closed.is_set()
    assert other.closed.is_set()


def test_live_worker_snapshot_excludes_absent_closed_and_expired_streams() -> None:
    """Only the current live stream keeps a worker alive during maintenance."""
    now = [100.0]
    store = ComputerSessionStore(clock=lambda: now[0])
    session = store.create(authorized_target())
    assert store.active_worker_keys() == frozenset()
    old = asyncio.Event()
    session.stream = old
    assert store.active_worker_keys() == frozenset({"worker"})
    old.set()
    assert store.active_worker_keys() == frozenset()
    session.stream = asyncio.Event()
    assert store.active_worker_keys() == frozenset({"worker"})
    now[0] = 3700.0
    assert store.active_worker_keys() == frozenset()


def test_global_capacity_remains_bounded_across_requesters() -> None:
    """Distinct requesters cannot bypass the process-wide limit."""
    store = ComputerSessionStore()
    for index in range(256):
        store.create(replace(authorized_target(), requester_id=f"@user{index}:example.org"))
    with pytest.raises(ComputerError) as denied:
        store.create(replace(authorized_target(), requester_id="@next:example.org"))
    assert denied.value.status_code == 429
