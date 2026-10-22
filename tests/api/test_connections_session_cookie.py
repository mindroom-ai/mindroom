"""Tests for signed Connections portal sessions."""

import pytest

from mindroom.api import connections_session_cookie
from mindroom.api.connections_session_cookie import (
    CONNECTIONS_SESSION_SECONDS,
    issue_connections_session,
    read_connections_session,
)

ALICE = "@alice:example.org"
NOW = 1000.0


def test_issued_session_resolves_to_its_user() -> None:
    """A session this process issued names its user."""
    token = issue_connections_session(ALICE, now=NOW, dashboard=False)
    assert read_connections_session(token, now=NOW) == (ALICE, False)


def test_session_keeps_the_dashboard_bit() -> None:
    """A session issued for the dashboard reads back as one."""
    token = issue_connections_session(ALICE, now=NOW, dashboard=True)
    assert read_connections_session(token, now=NOW) == (ALICE, True)


def test_previous_format_cookie_reads_as_signed_out() -> None:
    """A correctly signed payload without the dashboard field names nobody."""
    payload = f"{int(NOW) + CONNECTIONS_SESSION_SECONDS}|{ALICE}".encode().hex()
    token = f"{payload}.{connections_session_cookie._signature(payload)}"
    assert read_connections_session(token, now=NOW) is None


def test_session_expires_after_one_hour() -> None:
    """The lifetime is fixed from issue and never renewed."""
    token = issue_connections_session(ALICE, now=NOW, dashboard=False)
    assert read_connections_session(token, now=NOW + CONNECTIONS_SESSION_SECONDS - 1) is not None
    assert read_connections_session(token, now=NOW + CONNECTIONS_SESSION_SECONDS) is None


def test_session_naming_another_user_is_rejected() -> None:
    """Swapping the user, the expiry, or the dashboard bit in the payload breaks the signature."""
    expires = int(NOW) + CONNECTIONS_SESSION_SECONDS
    _payload, _, signature = issue_connections_session(ALICE, now=NOW, dashboard=False).rpartition(".")
    for forged in (f"{expires}|0|@mallory:example.org", f"{int(NOW) + 10**9}|0|{ALICE}", f"{expires}|1|{ALICE}"):
        payload = forged.encode().hex()
        assert read_connections_session(f"{payload}.{signature}", now=NOW) is None


def test_session_from_another_process_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each process signs with its own key, so a restart signs every portal user out."""
    token = issue_connections_session(ALICE, now=NOW, dashboard=False)
    monkeypatch.setattr(connections_session_cookie, "_SESSION_KEY", b"another process")
    assert read_connections_session(token, now=NOW) is None


@pytest.mark.parametrize("token", ["", "garbage", ".", "a.b.c", "é.é"])
def test_malformed_session_is_rejected(token: str) -> None:
    """A cookie value this process did not issue names nobody."""
    assert read_connections_session(token, now=NOW) is None
