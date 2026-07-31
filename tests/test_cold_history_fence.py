"""Matrix-continuity admission for cold callback windows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import nio
import pytest

from mindroom.cold_history_fence import ColdHistoryFence
from mindroom.dispatch_obligations import (
    DispatchCallbackKind,
    DispatchObligationRunner,
    DispatchObligationStore,
    _DispatchCallbackResult,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable
    from pathlib import Path


@dataclass
class _PendingObligations:
    pending: set[tuple[str, DispatchCallbackKind]] = field(default_factory=set)
    reads: list[tuple[str, DispatchCallbackKind]] = field(default_factory=list)

    @classmethod
    def with_keys(
        cls,
        keys: Iterable[tuple[str, DispatchCallbackKind]],
    ) -> _PendingObligations:
        return cls(pending=set(keys))

    def has_pending(
        self,
        source_event_id: str,
        callback_kind: DispatchCallbackKind,
    ) -> bool:
        self.reads.append((source_event_id, callback_kind))
        return (source_event_id, callback_kind) in self.pending


def _message(event_id: str) -> nio.RoomMessageText:
    event = nio.RoomMessageText.from_dict(
        {
            "type": "m.room.message",
            "event_id": event_id,
            "sender": "@user:example.org",
            "origin_server_ts": 1,
            "content": {"msgtype": "m.text", "body": "hello"},
        },
    )
    assert isinstance(event, nio.RoomMessageText)
    return event


def _runner(
    store: DispatchObligationStore,
    fence: ColdHistoryFence,
    callback: Callable[
        [nio.MatrixRoom, nio.Event],
        Awaitable[_DispatchCallbackResult],
    ],
) -> DispatchObligationRunner:
    return DispatchObligationRunner(
        store=store,
        callbacks={DispatchCallbackKind.MESSAGE: callback},
        room_for_id=lambda room_id: nio.MatrixRoom(room_id, "@code:example.org"),
        turn_is_terminal=lambda _event_id: False,
        source_admission=lambda _room_id, event_id, callback_kind: fence.admit(
            event_id,
            callback_kind,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", [None, "", " \t"])
async def test_missing_startup_continuation_suppresses_arbitrary_callbacks(
    continuation: str | None,
) -> None:
    """Tokenless startup must fail closed for arbitrary callback work."""
    obligations = _PendingObligations()
    fence = ColdHistoryFence(obligations)

    fence.start(trusted_continuation=continuation)

    assert not await fence.admit("$history", DispatchCallbackKind.MESSAGE)
    assert fence.is_cold
    assert obligations.reads == [("$history", DispatchCallbackKind.MESSAGE)]


@pytest.mark.asyncio
async def test_cold_window_admits_only_exact_pending_event_and_kind() -> None:
    """A pending event cannot license another event or callback kind."""
    obligations = _PendingObligations.with_keys(
        {("$obligated", DispatchCallbackKind.REACTION)},
    )
    fence = ColdHistoryFence(obligations)
    fence.start(trusted_continuation=None)

    assert await fence.admit("$obligated", DispatchCallbackKind.REACTION)
    assert not await fence.admit("$obligated", DispatchCallbackKind.MESSAGE)
    assert not await fence.admit("$other", DispatchCallbackKind.REACTION)


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", ["s_after_cold", "pos_after_cold"])
async def test_matrix_continuation_opens_ordinary_dispatch(continuation: str) -> None:
    """A Matrix-issued continuation opens ordinary callback dispatch."""
    obligations = _PendingObligations()
    fence = ColdHistoryFence(obligations)
    fence.start(trusted_continuation=None)

    fence.observe_continuation(continuation)

    assert await fence.admit("$ordinary", DispatchCallbackKind.MESSAGE)
    assert not fence.is_cold
    assert obligations.reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", [None, "", " "])
async def test_missing_response_continuation_keeps_fence_cold(
    continuation: str | None,
) -> None:
    """An unusable response continuation cannot open a cold fence."""
    fence = ColdHistoryFence(_PendingObligations())
    fence.start(trusted_continuation=None)

    fence.observe_continuation(continuation)

    assert not await fence.admit("$ordinary", DispatchCallbackKind.MESSAGE)
    assert fence.is_cold


@pytest.mark.asyncio
async def test_missing_response_continuation_rearms_open_fence() -> None:
    """Losing the response continuation must rearm exact-only admission."""
    fence = ColdHistoryFence(_PendingObligations())
    fence.start(trusted_continuation="s_before_missing")

    fence.observe_continuation(None)

    assert not await fence.admit("$ordinary", DispatchCallbackKind.MESSAGE)
    assert fence.is_cold


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", ["s_before_reset", "pos_before_reset"])
async def test_continuity_reset_rearms_exact_obligation_admission(
    continuation: str,
) -> None:
    """Continuity rejection closes ordinary work while preserving exact retries."""
    obligations = _PendingObligations.with_keys(
        {("$retry", DispatchCallbackKind.REDACTION)},
    )
    fence = ColdHistoryFence(obligations)
    fence.start(trusted_continuation=continuation)
    assert await fence.admit("$ordinary", DispatchCallbackKind.REDACTION)

    fence.reset()

    assert not await fence.admit("$ordinary", DispatchCallbackKind.REDACTION)
    assert await fence.admit("$retry", DispatchCallbackKind.REDACTION)


@pytest.mark.asyncio
@pytest.mark.parametrize("obligated", [False, True])
async def test_opposite_origin_timestamp_skew_cannot_change_admission(
    obligated: bool,
) -> None:
    """Federated event clocks must not influence callback admission."""
    kind = DispatchCallbackKind.MESSAGE
    pending = {("$same-event", kind)} if obligated else set()
    fence = ColdHistoryFence(_PendingObligations.with_keys(pending))
    fence.start(trusted_continuation=None)
    sources = (
        {"event_id": "$same-event", "origin_server_ts": 0},
        {"event_id": "$same-event", "origin_server_ts": 9_000_000_000_000},
    )

    decisions = [await fence.admit(str(source["event_id"]), kind) for source in sources]

    assert decisions == [obligated, obligated]


@pytest.mark.asyncio
@pytest.mark.parametrize("callback_kind", list(DispatchCallbackKind))
async def test_each_callback_family_uses_exact_pending_admission(
    callback_kind: DispatchCallbackKind,
) -> None:
    """Every correctness callback kind follows the same exact-key rule."""
    obligations = _PendingObligations.with_keys({("$exact", callback_kind)})
    fence = ColdHistoryFence(obligations)
    fence.start(trusted_continuation=None)

    assert not await fence.admit("$absent", callback_kind)
    assert await fence.admit("$exact", callback_kind)


@pytest.mark.asyncio
async def test_edit_uses_message_obligation_without_timestamp_admission() -> None:
    """Matrix edits use exact MESSAGE obligations without a clock boundary."""
    obligations = _PendingObligations.with_keys(
        {("$edit", DispatchCallbackKind.MESSAGE)},
    )
    fence = ColdHistoryFence(obligations)
    fence.start(trusted_continuation=None)
    edit_source = {
        "event_id": "$edit",
        "origin_server_ts": 0,
        "content": {
            "m.relates_to": {
                "rel_type": "m.replace",
                "event_id": "$original",
            },
        },
    }

    assert await fence.admit(
        str(edit_source["event_id"]),
        DispatchCallbackKind.MESSAGE,
    )


@pytest.mark.asyncio
async def test_runner_checks_cold_admission_before_creating_current_obligation(
    tmp_path: Path,
) -> None:
    """Cold replay cannot create the obligation that would admit itself."""
    store = DispatchObligationStore(
        tracking_path=tmp_path,
        principal_id="@code:example.org",
        entity_name="code",
    )
    fence = ColdHistoryFence(store)
    fence.start(trusted_continuation=None)
    attempts = 0

    async def callback(
        _room: nio.MatrixRoom,
        _event: nio.Event,
    ) -> _DispatchCallbackResult:
        nonlocal attempts
        attempts += 1
        return _DispatchCallbackResult.SUCCEEDED

    runner = _runner(store, fence, callback)

    await runner.dispatch(
        nio.MatrixRoom("!room:example.org", "@code:example.org"),
        _message("$history"),
        DispatchCallbackKind.MESSAGE,
    )

    assert attempts == 0
    assert not store.has_pending("$history", DispatchCallbackKind.MESSAGE)


@pytest.mark.asyncio
async def test_runner_preserves_current_event_after_server_continuation(
    tmp_path: Path,
) -> None:
    """A live event after Matrix establishes continuity still dispatches."""
    store = DispatchObligationStore(
        tracking_path=tmp_path,
        principal_id="@code:example.org",
        entity_name="code",
    )
    fence = ColdHistoryFence(store)
    fence.start(trusted_continuation=None)
    fence.observe_continuation("s_live")
    seen: list[str] = []

    async def callback(
        _room: nio.MatrixRoom,
        event: nio.Event,
    ) -> _DispatchCallbackResult:
        seen.append(event.event_id)
        return _DispatchCallbackResult.SUCCEEDED

    await _runner(store, fence, callback).dispatch(
        nio.MatrixRoom("!room:example.org", "@code:example.org"),
        _message("$live"),
        DispatchCallbackKind.MESSAGE,
    )

    assert seen == ["$live"]
    assert not store.has_pending("$live", DispatchCallbackKind.MESSAGE)


@pytest.mark.asyncio
async def test_runner_admits_exact_pending_obligation_after_reset(
    tmp_path: Path,
) -> None:
    """An exact pending callback remains runnable after continuity resets."""
    store = DispatchObligationStore(
        tracking_path=tmp_path,
        principal_id="@code:example.org",
        entity_name="code",
    )
    fence = ColdHistoryFence(store)
    fence.start(trusted_continuation="s_warm")
    attempts = 0

    async def callback(
        _room: nio.MatrixRoom,
        _event: nio.Event,
    ) -> _DispatchCallbackResult:
        nonlocal attempts
        attempts += 1
        return _DispatchCallbackResult.SUCCEEDED

    runner = _runner(store, fence, callback)
    room = nio.MatrixRoom("!room:example.org", "@code:example.org")
    event = _message("$retry")
    obligation = await runner.persist(
        room,
        event,
        DispatchCallbackKind.MESSAGE,
    )
    assert obligation is not None
    fence.reset()

    await runner.dispatch(room, event, DispatchCallbackKind.MESSAGE)

    assert attempts == 1
    assert not store.has_pending("$retry", DispatchCallbackKind.MESSAGE)


@pytest.mark.asyncio
async def test_direct_recovery_bypasses_cold_source_admission(
    tmp_path: Path,
) -> None:
    """Direct restart recovery must not depend on a new Matrix continuation."""
    store = DispatchObligationStore(
        tracking_path=tmp_path,
        principal_id="@code:example.org",
        entity_name="code",
    )
    warm_fence = ColdHistoryFence(store)
    warm_fence.start(trusted_continuation="s_warm")

    async def failing_callback(
        _room: nio.MatrixRoom,
        _event: nio.Event,
    ) -> _DispatchCallbackResult:
        msg = "callback failed"
        raise RuntimeError(msg)

    room = nio.MatrixRoom("!room:example.org", "@code:example.org")
    event = _message("$failed")
    with pytest.raises(RuntimeError, match="callback failed"):
        await _runner(store, warm_fence, failing_callback).dispatch(
            room,
            event,
            DispatchCallbackKind.MESSAGE,
        )
    assert store.has_pending("$failed", DispatchCallbackKind.MESSAGE)

    cold_fence = ColdHistoryFence(store)
    cold_fence.start(trusted_continuation=None)
    recovered: list[str] = []

    async def succeeding_callback(
        _room: nio.MatrixRoom,
        recovered_event: nio.Event,
    ) -> _DispatchCallbackResult:
        recovered.append(recovered_event.event_id)
        return _DispatchCallbackResult.SUCCEEDED

    await _runner(store, cold_fence, succeeding_callback).recover_pending()

    assert recovered == ["$failed"]
    assert not store.has_pending("$failed", DispatchCallbackKind.MESSAGE)
