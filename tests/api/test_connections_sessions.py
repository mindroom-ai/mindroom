"""Tests for signed Connections portal sessions."""

import pytest

from mindroom.api import connections_sessions
from mindroom.api.connections_sessions import (
    CONNECTIONS_SESSION_SECONDS,
    connections_session_user,
    issue_connections_session,
)

ALICE = "@alice:example.org"
NOW = 1000.0


def test_issued_session_resolves_to_its_user() -> None:
    """A session this process issued names its user."""
    token = issue_connections_session(ALICE, now=NOW)
    assert connections_session_user(token, now=NOW) == ALICE


def test_session_expires_after_one_hour() -> None:
    """The lifetime is fixed from issue and never renewed."""
    token = issue_connections_session(ALICE, now=NOW)
    assert connections_session_user(token, now=NOW + CONNECTIONS_SESSION_SECONDS - 1) == ALICE
    assert connections_session_user(token, now=NOW + CONNECTIONS_SESSION_SECONDS) is None


def test_session_naming_another_user_is_rejected() -> None:
    """Swapping the user or the expiry in the payload breaks the signature."""
    _payload, _, signature = issue_connections_session(ALICE, now=NOW).rpartition(".")
    for forged in (f"{int(NOW) + CONNECTIONS_SESSION_SECONDS}|@mallory:example.org", f"{int(NOW) + 10**9}|{ALICE}"):
        payload = forged.encode().hex()
        assert connections_session_user(f"{payload}.{signature}", now=NOW) is None


def test_session_from_another_process_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each process signs with its own key, so a restart signs every portal user out."""
    token = issue_connections_session(ALICE, now=NOW)
    monkeypatch.setattr(connections_sessions, "_SESSION_KEY", b"another process")
    assert connections_session_user(token, now=NOW) is None


@pytest.mark.parametrize("token", ["", "garbage", ".", "a.b.c", "é.é"])
def test_malformed_session_is_rejected(token: str) -> None:
    """A cookie value this process did not issue names nobody."""
    assert connections_session_user(token, now=NOW) is None
