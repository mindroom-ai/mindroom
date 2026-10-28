"""The free-text failure reasons failing paused replies stored become their failure kind when the journal opens."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.event_journal import ApprovalFailure
from mindroom.reply_lifecycle import ApprovalFailureKind
from tests.test_legacy_approval_records import recorded_journal as _recorded_journal

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.test_legacy_approval_records import _Journal

pytestmark = pytest.mark.asyncio

recorded_journal = _recorded_journal
_RESPONDER = "code@@mindroom_code:example.org"
_RECORDING = "06_failing.sql"


_CONVERTED = {
    "stopped": ApprovalFailure(ApprovalFailureKind.USER_STOP),
    "sync-restart": ApprovalFailure(ApprovalFailureKind.RESTART),
    "interrupted": ApprovalFailure(ApprovalFailureKind.INTERRUPTED),
    "shutdown": ApprovalFailure(ApprovalFailureKind.SHUTDOWN),
    "errored": ApprovalFailure(
        ApprovalFailureKind.ERROR,
        "Tool approval resume failed: the model provider returned HTTP 502.",
    ),
}


async def test_each_stored_failure_reason_becomes_its_kind(recorded_journal: Callable[[str], _Journal]) -> None:
    """Each failing paused reply keeps failing, with the kind its stored reason named and an error's text as detail."""
    journal = recorded_journal(_RECORDING)
    assert "failure_reason" in journal.columns("approval_continuations")
    store = journal.open()
    try:
        approvals = store.principal(_RESPONDER).approvals
        for approval_id, failure in _CONVERTED.items():
            continuation = await approvals.approval_continuation(approval_id)
            assert continuation is not None
            assert (continuation.state, continuation.failure) == ("failing", failure)
    finally:
        await store.close()
    assert {"failure_kind", "failure_detail"} <= journal.columns("approval_continuations")
    assert "failure_reason" not in journal.columns("approval_continuations")


async def test_the_conversion_runs_once(recorded_journal: Callable[[str], _Journal]) -> None:
    """A journal the conversion already upgraded opens unchanged."""
    journal = recorded_journal(_RECORDING)
    await journal.open().close()
    converted = journal.columns("approval_continuations")
    store = journal.open()
    try:
        continuation = await store.principal(_RESPONDER).approvals.approval_continuation("errored")
        assert continuation is not None
        assert continuation.failure == _CONVERTED["errored"]
    finally:
        await store.close()
    assert journal.columns("approval_continuations") == converted
