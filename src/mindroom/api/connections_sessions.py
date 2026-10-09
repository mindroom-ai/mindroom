"""Bounded process-local sessions for the Connections portal; only token digests are stored."""

import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

CONNECTIONS_SESSION_COOKIE = "__Host-mindroom_connections_session"
CONNECTIONS_SESSION_SECONDS = 3600


@dataclass(frozen=True)
class _ConnectionsSession:
    """One signed-in Matrix user and the fixed instant their session ends."""

    matrix_user_id: str
    expires_at: float


def _digest(token: str) -> str:
    """Index sessions by token digest so a memory dump does not reveal live cookies."""
    return hashlib.sha256(token.encode()).hexdigest()


class ConnectionsSessionStore:
    """Own expiring portal sessions with global and per-user bounds.

    Lifetime is fixed from creation and never renewed by `resolve`.
    The dict preserves creation order, so the first live entry is always the oldest.
    Every method is synchronous and never awaits, so event-loop callers cannot interleave inside one.
    """

    def __init__(
        self,
        *,
        max_sessions: int = 1024,
        max_per_user: int = 16,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_sessions = max_sessions
        self.max_per_user = max_per_user
        self.clock = clock
        self._sessions: dict[str, _ConnectionsSession] = {}

    def _prune(self) -> None:
        """Drop expired sessions so they never count toward a cap."""
        now = self.clock()
        for digest, session in tuple(self._sessions.items()):
            if session.expires_at <= now:
                del self._sessions[digest]

    def create(self, matrix_user_id: str) -> str:
        """Start a session for `matrix_user_id`, evicting the oldest ones to stay within both caps."""
        self._prune()
        user_digests = [
            digest for digest, session in self._sessions.items() if session.matrix_user_id == matrix_user_id
        ]
        for digest in user_digests[: max(0, len(user_digests) - self.max_per_user + 1)]:
            del self._sessions[digest]
        while len(self._sessions) >= self.max_sessions:
            del self._sessions[next(iter(self._sessions))]
        token = secrets.token_urlsafe(32)
        self._sessions[_digest(token)] = _ConnectionsSession(matrix_user_id, self.clock() + CONNECTIONS_SESSION_SECONDS)
        return token

    def resolve(self, token: str) -> str | None:
        """Return the Matrix user for a live token, else `None`."""
        self._prune()
        session = self._sessions.get(_digest(token))
        return None if session is None else session.matrix_user_id
