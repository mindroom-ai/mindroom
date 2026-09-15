"""Opaque computer capability expiry, isolation, and revocation."""

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
