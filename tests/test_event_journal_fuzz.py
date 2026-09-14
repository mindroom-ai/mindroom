"""Shrinkable mixed-mutation coverage for the current durable journal."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from tests.event_journal_fuzz import Action, JournalFuzzRunner

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.event_journal import EventJournalStore


_EVENTS = (
    "root",
    "message",
    "edit_a",
    "edit_old",
    "edit_z",
    "forged_edit",
    "edit_of_edit",
    "root_edit",
    "reaction",
    "redact_message",
    "redact_edit_a",
    "redact_edit_z",
    "redact_root",
    "redact_root_edit",
    "redact_reaction",
)
_ACTIONS = st.one_of(
    st.builds(
        Action,
        kind=st.sampled_from(("admit", "admit", "settle", "conflicting_duplicate", "concurrent_duplicate")),
        principal=st.integers(0, 1),
        source=st.integers(0, 5),
        event=st.sampled_from(_EVENTS),
    ),
    st.just(Action("reopen")),
    st.just(Action("refresh")),
)


@pytest.mark.asyncio
@pytest.mark.timeout(300)
@settings(
    max_examples=60,
    deadline=None,
    print_blob=True,
    # Each example owns fresh principal namespaces, including after reopen.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    actions=st.one_of(
        st.lists(_ACTIONS, min_size=1, max_size=45),
        st.permutations(
            [Action("admit", event=event) for event in _EVENTS] + [Action("reopen"), Action("refresh")],
        ).map(list),
    ),
)
@example(
    actions=[
        Action("admit", event="edit_a"),
        Action("admit", event="edit_z"),
        Action("admit", event="redact_edit_z"),
        Action("reopen"),
        Action("admit"),
    ],
)
@example(
    actions=[
        Action("admit", event="edit_z"),
        Action("admit", event="forged_edit"),
        Action("reopen"),
        Action("admit"),
        Action("admit", event="edit_a"),
        Action("admit", event="redact_edit_z"),
        Action("reopen"),
        Action("refresh"),
        Action("settle"),
        Action("concurrent_duplicate"),
        Action("conflicting_duplicate"),
        Action("admit", event="redact_message"),
        Action("reopen"),
        Action("admit"),
    ],
)
@example(
    actions=[
        Action("admit", event="redact_message"),
        Action("reopen"),
        Action("admit"),
        Action("admit", principal=1),
        Action("admit", source=1),
        Action("admit", source=2),
        Action("admit", source=4),
        Action("admit", event="root"),
        Action("admit", event="root_edit"),
        Action("admit", event="redact_root_edit"),
        Action("reopen"),
        Action("refresh"),
    ],
)
async def test_generated_journal_mutations_preserve_history_and_pending_work(
    journal_database: Callable[[], EventJournalStore],
    actions: list[Action],
) -> None:
    """Mixed delivery order cannot lose, duplicate, resurrect, or misroute state."""
    runner = JournalFuzzRunner(journal_database)
    try:
        await runner.run(actions)
    finally:
        await runner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["missing", "body", "revision", "thread", "pending", "tombstone"])
async def test_journal_fuzz_oracle_detects_corruption(
    journal_database: Callable[[], EventJournalStore],
    corruption: str,
) -> None:
    """A fuzzer must fail when stored history, pending work, or deletion proof lies."""
    runner = JournalFuzzRunner(journal_database)
    try:
        await runner.run([Action("admit"), Action("admit", event="edit_z"), Action("admit", event="redact_root")])
        statements = {
            "missing": "DELETE FROM visible_messages",
            "body": 'UPDATE visible_messages SET content_json = \'{"msgtype":"m.text","body":"wrong"}\'',
            "revision": "UPDATE visible_messages SET revision_event_id = '$wrong'",
            "thread": "UPDATE visible_messages SET thread_id = '$wrong'",
            "pending": "DELETE FROM journal_events",
            "tombstone": "DELETE FROM redaction_tombstones",
        }
        await runner.store.backend.write(lambda tx: tx.execute(statements[corruption]))
        with pytest.raises(AssertionError):
            await runner.check()
    finally:
        await runner.close()
