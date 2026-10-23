"""A scope context alone names the session a run of its scope is saved into."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.types import HistoryScope
from mindroom.session_storage.store import ConversationStore
from mindroom.session_storage.types import ConversationSession
from mindroom.teams.storage import scope_storage

if TYPE_CHECKING:
    from mindroom.session_storage.types import SessionKind


def _context(kind: SessionKind, session: ConversationSession | None) -> ScopeSessionContext:
    return ScopeSessionContext(
        scope=HistoryScope(kind=kind, scope_id="crew"),
        storage=Mock(spec=ConversationStore),
        session=session,
        session_id="session-1",
    )


@pytest.mark.parametrize("kind", ["agent", "team"])
def test_a_scope_without_a_stored_session_starts_one_of_its_kind_under_the_id_it_was_opened_for(
    kind: SessionKind,
) -> None:
    """The scope's first session takes the context's id, the scope's owner, and the scope's kind."""
    session = _context(kind, None).stored_or_first_session()

    assert (session.session_id, session.kind, session.owner_id) == ("session-1", kind, "crew")


@pytest.mark.parametrize("kind", ["agent", "team"])
def test_the_stored_session_is_the_one_a_run_is_saved_into(kind: SessionKind) -> None:
    """A stored session is saved into as itself, never as a copy."""
    stored = ConversationSession(session_id="session-1", kind=kind, owner_id="crew", created_at=1)

    assert _context(kind, stored).stored_or_first_session() is stored


def test_a_team_run_is_stored_in_the_stored_session_of_its_scope_context() -> None:
    """A team run takes its session from the scope context alone."""
    stored = ConversationSession(session_id="session-1", kind="team", owner_id="crew", created_at=1)
    context = _context("team", stored)

    storage = scope_storage(context)

    assert storage.session is stored
    assert (storage.store, storage.scope) == (context.storage, context.scope)


def test_a_team_scope_without_a_stored_session_stores_its_runs_in_its_first_one() -> None:
    """A team scope with no stored session gives its first run the team session under the context's id."""
    storage = scope_storage(_context("team", None))

    assert (storage.session.session_id, storage.session.kind, storage.session.owner_id) == ("session-1", "team", "crew")
