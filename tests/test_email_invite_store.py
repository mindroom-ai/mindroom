"""Tests for the email-invite SQLite store."""

from __future__ import annotations

import sqlite3
import threading
from typing import TYPE_CHECKING

import pytest

from mindroom.email_invites import store
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

NOW = 1_800_000_000
DAY = 86400
ROOM = "!room:example.org"


def _request(
    paths: RuntimePaths,
    *,
    email: str = "alice@example.com",
    room_id: str = ROOM,
    requested_by: str = "@bas:example.org",
    now: int = NOW,
) -> store.EmailInvite:
    return store.create_request(
        paths,
        store.Request(room_id, "$thread", email, requested_by, "general"),
        expiry_days=7,
        now=now,
    )


def _issue(paths: RuntimePaths, *, email: str = "alice@example.com", now: int = NOW) -> tuple[store.EmailInvite, str]:
    request = store.Request(ROOM, "$thread", email, "@bas:example.org", "general")
    return store.issue_code(paths, request, expiry_days=7, now=now)


def _sent(paths: RuntimePaths, *, email: str = "alice@example.com", now: int = NOW) -> tuple[store.EmailInvite, str]:
    _request(paths, email=email, now=now)
    return _issue(paths, email=email, now=now)


def _db_path(paths: RuntimePaths) -> Path:
    return paths.storage_root / "tracking" / "email_invites.sqlite"


def test_code_is_stored_hashed_only(tmp_path: Path) -> None:
    """The raw code never reaches the database file."""
    paths = test_runtime_paths(tmp_path)
    invite, code = _sent(paths)
    assert code.encode() not in _db_path(paths).read_bytes()
    found = store.find_by_code(paths, code, now=NOW)
    assert found is not None
    assert found.id == invite.id
    assert found.inviter_id == "@bas:example.org"
    assert found.sent_at == NOW


def test_open_invite_is_unique_per_room_and_address(tmp_path: Path) -> None:
    """A second open request for the same room and address is refused."""
    paths = test_runtime_paths(tmp_path)
    first = _request(paths)
    with pytest.raises(ValueError, match="open"):
        _request(paths)
    other = _request(paths, room_id="!other:example.org")
    assert other.id != first.id
    open_row = store.open_invite(paths, room_id=ROOM, email="alice@example.com", now=NOW)
    assert open_row is not None
    assert open_row.id == first.id


def test_asker_cap(tmp_path: Path) -> None:
    """One asker may make five requests per 24 hours."""
    paths = test_runtime_paths(tmp_path)
    for index in range(5):
        _request(paths, email=f"user{index}@example.com")
    with pytest.raises(store.EmailInviteLimitError) as caught:
        _request(paths, email="user5@example.com")
    assert caught.value.limit == "asker"
    _request(paths, email="user5@example.com", now=NOW + DAY + 1)


def test_room_cap(tmp_path: Path) -> None:
    """A room holds at most twenty open requests."""
    paths = test_runtime_paths(tmp_path)
    invites = [
        _request(paths, email=f"user{index}@example.com", requested_by=f"@asker{index // 5}:example.org")
        for index in range(20)
    ]
    with pytest.raises(store.EmailInviteLimitError) as caught:
        _request(paths, email="extra@example.com", requested_by="@other:example.org")
    assert caught.value.limit == "room"
    assert store.revoke(paths, invites[0].id, now=NOW) is True
    _request(paths, email="extra@example.com", requested_by="@other:example.org")


def test_address_and_deployment_caps(tmp_path: Path) -> None:
    """Sends are capped per address and per deployment, revoked sends included."""
    paths = test_runtime_paths(tmp_path)
    for index in range(3):
        invite, _code = _sent(paths, now=NOW + index)
        # A failed send revokes the request, but it still counts.
        assert store.revoke(paths, invite.id, now=NOW + index) is True
    blocked = _request(paths, now=NOW + 3)
    with pytest.raises(store.EmailInviteLimitError) as caught:
        _issue(paths, now=NOW + 3)
    assert caught.value.limit == "address"
    store.revoke(paths, blocked.id, now=NOW + 3)

    for index in range(48):
        email = f"user{index}@example.com"
        _request(paths, email=email, requested_by=f"@asker{index}:example.org", now=NOW + 10)
        if index < 47:
            invite, _code = _issue(paths, email=email, now=NOW + 10)
            store.revoke(paths, invite.id, now=NOW + 10)
    with pytest.raises(store.EmailInviteLimitError) as caught:
        _issue(paths, email="user47@example.com", now=NOW + 10)
    assert caught.value.limit == "deployment"


def test_issue_code_extends_expiry(tmp_path: Path) -> None:
    """Issuing the code restarts the expiry from that moment, and uses the waiting request."""
    paths = test_runtime_paths(tmp_path)
    invite = _request(paths)
    assert invite.expires_at == NOW + 7 * DAY
    issued, code = _issue(paths, now=NOW + 3 * DAY)
    assert issued.id == invite.id
    assert issued.requested_by == "@bas:example.org"
    found = store.find_by_code(paths, code, now=NOW + 3 * DAY)
    assert found is not None
    assert found.expires_at == issued.expires_at == NOW + 10 * DAY


def test_reissue_replaces_and_a_refused_cap_changes_nothing(tmp_path: Path) -> None:
    """A second send replaces the sent request; a send a cap refuses leaves the sent link working."""
    paths = test_runtime_paths(tmp_path)
    first, first_code = _sent(paths)
    second, second_code = _issue(paths, now=NOW + 1)
    assert second.id != first.id
    assert store.find_by_code(paths, first_code, now=NOW + 1) is None
    _issue(paths, now=NOW + 2)
    with pytest.raises(store.EmailInviteLimitError):
        _issue(paths, now=NOW + 3)
    (still_open,) = store.open_invites(paths, ROOM, now=NOW + 3)
    assert still_open.sent_at == NOW + 2
    assert second_code


def test_revoke_wins_once_and_frees_the_address(tmp_path: Path) -> None:
    """Revoke wins only once and not after use; a revoked link no longer binds, and a new one may be sent."""
    paths = test_runtime_paths(tmp_path)
    invite, code = _sent(paths)
    assert store.bind_claim(paths, invite.id, "@alice:example.org", now=NOW + 1)
    assert store.revoke(paths, invite.id, now=NOW + 2) is True
    assert store.revoke(paths, invite.id, now=NOW + 2) is False
    assert store.bind_claim(paths, invite.id, "@alice:example.org", now=NOW + 2) is False
    assert store.find_by_code(paths, code, now=NOW + 2) is None
    fresh, _fresh_code = _issue(paths, now=NOW + 3)
    assert fresh.id != invite.id
    assert store.mark_invited(paths, fresh.id, now=NOW + 4) is True
    assert store.revoke(paths, fresh.id, now=NOW + 5) is False


def test_one_send_per_link_and_sending_is_terminal(tmp_path: Path) -> None:
    """Only one call moves a link to sending; it can no longer be revoked or reopened, and is revoked once if lost."""
    paths = test_runtime_paths(tmp_path)
    invite, code = _sent(paths)
    assert store.bind_claim(paths, invite.id, "@alice:example.org", now=NOW + 1)
    assert store.start_sending(paths, invite.id, "@mallory:example.org", now=NOW + 1) is False
    assert store.start_sending(paths, invite.id, "@alice:example.org", now=NOW + 1) is True
    assert store.start_sending(paths, invite.id, "@alice:example.org", now=NOW + 1) is False
    assert store.revoke(paths, invite.id, now=NOW + 2) is False
    assert store.open_invites(paths, ROOM, now=NOW + 2) == []
    assert [row.id for row in store.abandon_sending(paths)] == [invite.id]
    assert store.abandon_sending(paths) == []
    assert store.find_by_code(paths, code, now=NOW + 2) is None
    fresh, _fresh_code = _issue(paths, now=NOW + 3)
    assert fresh.id != invite.id


def test_two_claimants_one_winner(tmp_path: Path) -> None:
    """Two accounts binding the same code at once: exactly one wins."""
    paths = test_runtime_paths(tmp_path)
    invite, _code = _sent(paths)
    barrier = threading.Barrier(2)
    results: dict[str, bool] = {}

    def claim(claimant: str) -> None:
        barrier.wait()
        results[claimant] = store.bind_claim(paths, invite.id, claimant, now=NOW + 1)

    threads = [threading.Thread(target=claim, args=(claimant,)) for claimant in ("@a:x", "@b:x")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results.values()) == [False, True]
    winner = next(claimant for claimant, won in results.items() if won)
    loser = next(claimant for claimant, won in results.items() if not won)
    assert store.bind_claim(paths, invite.id, winner, now=NOW + 2) is True
    assert store.bind_claim(paths, invite.id, loser, now=NOW + 2) is False


def test_claim_fails_when_revoked_or_expired(tmp_path: Path) -> None:
    """Neither a revoked nor an expired code can be found or bound."""
    paths = test_runtime_paths(tmp_path)
    revoked, revoked_code = _sent(paths)
    store.revoke(paths, revoked.id, now=NOW)
    assert store.find_by_code(paths, revoked_code, now=NOW + 1) is None
    assert store.bind_claim(paths, revoked.id, "@alice:example.org", now=NOW + 1) is False

    expired, expired_code = _sent(paths, email="bob@example.com")
    later = NOW + 7 * DAY + 1
    assert store.find_by_code(paths, expired_code, now=later) is None
    assert store.bind_claim(paths, expired.id, "@bob:example.org", now=later) is False


def test_revoke_after_use_changes_nothing(tmp_path: Path) -> None:
    """A used invite cannot be revoked and stays findable for repeat claims."""
    paths = test_runtime_paths(tmp_path)
    invite, code = _sent(paths)
    assert store.bind_claim(paths, invite.id, "@alice:example.org", now=NOW + 1) is True
    store.mark_invited(paths, invite.id, now=NOW + 2)
    assert store.revoke(paths, invite.id, now=NOW + 3) is False
    found = store.find_by_code(paths, code, now=NOW + 8 * DAY)
    assert found is not None
    assert found.invited_at == NOW + 2
    assert found.revoked is False
    assert store.open_invite(paths, room_id=ROOM, email="alice@example.com", now=NOW + 3) is None
    used = store.used_invite(paths, room_id=ROOM, email="alice@example.com")
    assert used is not None
    assert used.claimed_by == "@alice:example.org"
    assert used.claimed_at == NOW + 1


def test_open_invites_lists_room_rows(tmp_path: Path) -> None:
    """Only open rows of the room are listed."""
    paths = test_runtime_paths(tmp_path)
    waiting = _request(paths)
    sent, _code = _sent(paths, email="bob@example.com")
    _request(paths, email="carol@example.com", room_id="!other:example.org")
    revoked = _request(paths, email="dave@example.com")
    store.revoke(paths, revoked.id, now=NOW)
    assert [invite.id for invite in store.open_invites(paths, ROOM, now=NOW)] == [waiting.id, sent.id]


def test_purge(tmp_path: Path) -> None:
    """Rows a day past expiry are deleted on the next write."""
    paths = test_runtime_paths(tmp_path)
    old = _request(paths)
    _request(paths, email="bob@example.com", now=NOW + 8 * DAY + 1)
    with sqlite3.connect(_db_path(paths)) as connection:
        ids = [row[0] for row in connection.execute("SELECT id FROM email_invites")]
    assert old.id not in ids
    assert len(ids) == 1


def test_file_modes(tmp_path: Path) -> None:
    """The store directory and file are private."""
    paths = test_runtime_paths(tmp_path)
    _request(paths)
    path = _db_path(paths)
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
