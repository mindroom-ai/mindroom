"""Tests for the in-memory Connections portal session store."""

from fastapi import FastAPI

from mindroom.api.config_lifecycle import app_state, ensure_app_state
from mindroom.api.connections_sessions import (
    CONNECTIONS_SESSION_SECONDS,
    ConnectionsSessionStore,
)

ALICE = "@alice:example.org"


class _Clock:
    """Advanceable fake monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_create_then_resolve_returns_user() -> None:
    """A freshly created token resolves to its Matrix user; unknown tokens do not."""
    store = ConnectionsSessionStore()

    assert store.resolve(store.create(ALICE)) == ALICE
    assert store.resolve("unknown") is None


def test_session_expires_after_one_hour() -> None:
    """The lifetime is fixed: live just before one hour, gone at one hour."""
    clock = _Clock()
    store = ConnectionsSessionStore(clock=clock)
    token = store.create(ALICE)

    clock.now += CONNECTIONS_SESSION_SECONDS - 1
    assert store.resolve(token) == ALICE
    clock.now += 1
    assert store.resolve(token) is None


def test_per_user_cap_evicts_oldest() -> None:
    """A user holds at most 16 live sessions; the earliest-created one is evicted."""
    store = ConnectionsSessionStore()
    tokens = [store.create(ALICE) for _ in range(17)]

    assert store.resolve(tokens[0]) is None
    assert all(store.resolve(token) == ALICE for token in tokens[1:])


def test_per_user_cap_does_not_evict_other_users() -> None:
    """The per-user cap only counts the creating user's sessions."""
    store = ConnectionsSessionStore(max_per_user=1)
    bob_token = store.create("@bob:example.org")
    store.create(ALICE)
    store.create(ALICE)

    assert store.resolve(bob_token) == "@bob:example.org"


def test_global_cap_evicts_oldest() -> None:
    """The store holds at most max_sessions live sessions; the earliest-created is evicted."""
    store = ConnectionsSessionStore(max_sessions=2)
    first = store.create("@a:example.org")
    second = store.create("@b:example.org")
    third = store.create("@c:example.org")

    assert store.resolve(first) is None
    assert store.resolve(second) == "@b:example.org"
    assert store.resolve(third) == "@c:example.org"


def test_expired_sessions_do_not_count_toward_caps() -> None:
    """Expired sessions are pruned before caps are enforced, so live ones survive."""
    clock = _Clock()
    store = ConnectionsSessionStore(max_sessions=2, max_per_user=2, clock=clock)
    store.create(ALICE)
    store.create(ALICE)
    clock.now += CONNECTIONS_SESSION_SECONDS
    survivor = store.create("@bob:example.org")
    newest = store.create(ALICE)

    assert store.resolve(survivor) == "@bob:example.org"
    assert store.resolve(newest) == ALICE


def test_tokens_are_unique_and_unguessable_length() -> None:
    """Every session gets a distinct URL-safe token with real entropy."""
    store = ConnectionsSessionStore()
    tokens = {store.create(ALICE) for _ in range(8)}

    assert len(tokens) == 8
    assert all(len(token) >= 32 for token in tokens)


def test_tokens_are_not_stored_in_plaintext() -> None:
    """The store keeps only token digests so a memory dump does not leak live cookies."""
    store = ConnectionsSessionStore()
    token = store.create(ALICE)

    assert token not in repr(store.__dict__)


def test_each_app_gets_its_own_session_store() -> None:
    """App state carries a store per app, so sessions never leak between apps."""
    first, second = FastAPI(), FastAPI()
    token = ensure_app_state(first).connections_sessions.create(ALICE)

    assert app_state(first).connections_sessions.resolve(token) == ALICE
    assert ensure_app_state(second).connections_sessions.resolve(token) is None
