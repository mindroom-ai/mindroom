"""Bounded process-local Computer capabilities; no secrets belong in public URLs."""

import asyncio
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from mindroom.workers.models import WorkerHandle, WorkerSpec


class ComputerError(Exception):
    """A bounded public error with a deliberate HTTP status."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class ComputerTarget:
    """Current authorization and canonical worker scope, never client-selected."""

    requester_id: str
    room_id: str
    agent_user_id: str
    spec: WorkerSpec
    config_identity: str


@dataclass(frozen=True)
class _ComputerTicket:
    """Short-lived single-use upgrade capability."""

    ticket: str = field(repr=False)
    expires_at: float


@dataclass
class ComputerSession:
    """One viewer's authority and stream invalidation signal."""

    session_id: str
    session_token: str = field(repr=False)
    target: ComputerTarget
    expires_at: float
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    handle: WorkerHandle | None = field(default=None, repr=False)
    generation: str | None = None
    stream: asyncio.Event | None = None
    ticket: _ComputerTicket | None = field(default=None, repr=False)


class ComputerSessionStore:
    """Own expiring viewer credentials and at most one ticket per viewer."""

    def __init__(self, *, clock: Callable[[], float] = time.time, capacity: int = 256) -> None:
        self.clock = clock
        self.capacity = capacity
        self._sessions: dict[str, ComputerSession] = {}

    def _prune(self) -> None:
        """Revoke expired sessions and notify their active stream."""
        for session in tuple(self._sessions.values()):
            if session.expires_at <= self.clock():
                self.close(session.session_id)

    def create(self, target: ComputerTarget) -> ComputerSession:
        """Reserve a bounded viewer slot before starting any worker resources."""
        self._prune()
        if len(self._sessions) >= self.capacity:
            raise ComputerError(429, "Computer session capacity reached.")
        session = ComputerSession(secrets.token_urlsafe(24), secrets.token_urlsafe(32), target, self.clock() + 3600)
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> ComputerSession:
        """Resolve only live capabilities."""
        self._prune()
        session = self._sessions.get(session_id)
        if session is None:
            raise ComputerError(401, "Invalid or expired computer session.")
        return session

    def authenticate(self, session_id: str, token: str) -> ComputerSession:
        """Authenticate a bearer without data-dependent string comparison."""
        session = self.get(session_id)
        if not secrets.compare_digest(session.session_token.encode(), token.encode()):
            raise ComputerError(401, "Invalid or expired computer session.")
        return session

    def issue_stream_ticket(self, session_id: str) -> _ComputerTicket:
        """Replace any unused ticket so ticket storage is bounded by sessions."""
        session = self.get(session_id)
        ticket = _ComputerTicket(secrets.token_urlsafe(32), min(self.clock() + 30, session.expires_at))
        session.ticket = ticket
        return ticket

    def consume_stream_ticket(self, session_id: str, token: str) -> ComputerSession:
        """Consume exactly one session-bound upgrade capability."""
        session = self.get(session_id)
        ticket = session.ticket
        if (
            ticket is None
            or ticket.expires_at <= self.clock()
            or not secrets.compare_digest(ticket.ticket.encode(), token.encode())
        ):
            raise ComputerError(401, "Invalid or expired computer stream ticket.")
        session.ticket = None
        return session

    def close(self, session_id: str) -> None:
        """Revoke all capabilities and close a viewer's active stream."""
        session = self._sessions.pop(session_id, None)
        if session is not None:
            session.ticket = None
            session.closed.set()
            if session.stream is not None:
                session.stream.set()

    def close_all(self) -> None:
        """Revoke process-local state when authorization runtime is unbound."""
        for session_id in tuple(self._sessions):
            self.close(session_id)

    def active_worker_keys(self) -> frozenset[str]:
        """Snapshot live streams for the existing threaded worker maintenance seam."""
        return frozenset(
            session.target.spec.worker_key
            for session in tuple(self._sessions.values())
            if session.stream is not None and not session.stream.is_set() and session.expires_at > self.clock()
        )
