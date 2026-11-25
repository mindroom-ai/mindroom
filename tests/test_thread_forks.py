"""Tests for resolving forked threads as a parent prefix plus their own messages."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom.constants import FORK_CONTENT_KEY
from mindroom.matrix.thread_diagnostics import THREAD_HISTORY_DEGRADED_DIAGNOSTIC, is_thread_history_degraded
from mindroom.matrix.thread_forks import (
    _MAX_FORK_DEPTH,
    CrossRoomForkDecision,
    CrossRoomForkHop,
    ThreadForkReference,
    fork_parent_scopes,
    fork_reference,
    thread_fork_reference,
    with_fork_prefix,
)
from mindroom.matrix.thread_history_result import ThreadHistoryResult, thread_history_result
from tests.conftest import make_visible_message

_ROOM = "!room:localhost"
_ROOM_A = "!roomA:localhost"
_ROOM_B = "!roomB:localhost"
_ROOM_C = "!roomC:localhost"


def _fork_content(
    thread_id: str,
    event_id: str,
    *,
    room_id: object = ...,
) -> dict[str, object]:
    payload: dict[str, object] = {"thread_id": thread_id, "event_id": event_id}
    if room_id is not ...:
        payload["room_id"] = room_id
    return {FORK_CONTENT_KEY: payload}


def _thread(
    root: str,
    replies: list[str],
    fork_of: tuple[str, str] | tuple[str, str, str] | None = None,
    *,
    is_full_history: bool = True,
    diagnostics: dict[str, bool] | None = None,
    room_id: str | None = None,
) -> ThreadHistoryResult:
    content: dict[str, object] | None = None
    if fork_of is not None:
        if len(fork_of) == 2:
            content = _fork_content(fork_of[0], fork_of[1])
        else:
            content = _fork_content(fork_of[1], fork_of[2], room_id=fork_of[0])
    messages = [make_visible_message(event_id=root, content=content, room_id=room_id)]
    messages += [make_visible_message(event_id=e, thread_id=root, room_id=room_id) for e in replies]
    return thread_history_result(messages, is_full_history=is_full_history, diagnostics=diagnostics)


def _reader(
    threads: dict[str, ThreadHistoryResult] | dict[tuple[str, str], ThreadHistoryResult],
    *,
    default_room: str = _ROOM,
) -> AsyncMock:
    normalized: dict[tuple[str, str], ThreadHistoryResult] = {
        (k if isinstance(k, tuple) else (default_room, k)): v for k, v in threads.items()
    }

    async def read(room_id: str, thread_id: str) -> ThreadHistoryResult:
        return normalized.get((room_id, thread_id), thread_history_result([], is_full_history=True))

    return AsyncMock(side_effect=read)


def _ids(history: ThreadHistoryResult) -> list[str]:
    return [m.event_id for m in history]


def test_reference_parses_event_ids_and_validates_optional_room_id() -> None:
    """Reference accepts same-room or valid Matrix room IDs (including v12 serverless) and rejects malformed values."""
    assert thread_fork_reference(_fork_content("$p", "$p2")) == ThreadForkReference(None, "$p", "$p2")
    assert thread_fork_reference(_fork_content("$p", "$p2", room_id="!src:localhost")) == ThreadForkReference(
        "!src:localhost",
        "$p",
        "$p2",
    )
    assert thread_fork_reference(_fork_content("$p", "$p2", room_id="!v12serverlessroom")) == ThreadForkReference(
        "!v12serverlessroom",
        "$p",
        "$p2",
    )

    assert thread_fork_reference({}) is None
    assert thread_fork_reference({FORK_CONTENT_KEY: "$p"}) is None
    assert thread_fork_reference(_fork_content("", "$p2")) is None
    assert thread_fork_reference(_fork_content("$p", "")) is None
    assert thread_fork_reference(_fork_content("p", "$p2")) is None
    assert thread_fork_reference(_fork_content("$p", "p2")) is None
    assert thread_fork_reference(_fork_content("$", "$p2")) is None
    assert thread_fork_reference(_fork_content("$p", "$")) is None

    for bad_room_id in (None, "", "!", "#alias:localhost", "!bad room:localhost", " !room:localhost", "!room\n", 123):
        assert thread_fork_reference(_fork_content("$p", "$p2", room_id=bad_room_id)) is None


@pytest.mark.asyncio
async def test_fork_history_is_parent_prefix_then_own_messages() -> None:
    """Fork history is parent prefix (stamped with room_id) then own messages."""
    read = _reader({"$p": _thread("$p", ["$p1", "$p2", "$p3"])})
    fork = _thread("$f", ["$f1"], ("$p", "$p2"))
    result = await with_fork_prefix(fork, room_id=_ROOM, thread_id="$f", read_thread=read, logger=MagicMock())
    assert _ids(result) == ["$p", "$p1", "$p2", "$f", "$f1"]
    assert result.messages[1].thread_id == "$p"
    assert [m.room_id for m in result.messages] == [_ROOM, _ROOM, _ROOM, None, None]


@pytest.mark.asyncio
async def test_fork_of_fork_resolves_the_chain() -> None:
    """Fork of fork resolves the chain."""
    read = _reader(
        {
            "$g": _thread("$g", ["$g1", "$g2"]),
            "$p": _thread("$p", ["$p1"], ("$g", "$g1")),
        },
    )
    fork = _thread("$f", [], ("$p", "$p1"))
    result = await with_fork_prefix(fork, room_id=_ROOM, thread_id="$f", read_thread=read, logger=MagicMock())
    assert _ids(result) == ["$g", "$g1", "$p", "$p1", "$f"]


@pytest.mark.asyncio
async def test_cross_room_chain_resolves_omitted_room_in_owning_hop_room() -> None:
    """In A -> B -> C where B -> A omits room_id (same-room in A), B -> A resolves in A and checks against C."""
    read = _reader(
        {
            (_ROOM_A, "$a"): _thread("$a", ["$a1", "$a2"]),
            (_ROOM_A, "$b"): _thread("$b", ["$b1"], ("$a", "$a1")),
        },
    )
    fork_c = _thread("$c", ["$c1"], (_ROOM_A, "$b", "$b1"))
    seen_hops: list[CrossRoomForkHop] = []

    async def allow(hop: CrossRoomForkHop) -> CrossRoomForkDecision:
        seen_hops.append(hop)
        return CrossRoomForkDecision.ALLOWED

    result = await with_fork_prefix(
        fork_c,
        room_id=_ROOM_C,
        thread_id="$c",
        read_thread=read,
        authorize_cross_room=allow,
        logger=MagicMock(),
    )

    assert _ids(result) == ["$a", "$a1", "$b", "$b1", "$c", "$c1"]
    assert [m.room_id for m in result.messages] == [_ROOM_A, _ROOM_A, _ROOM_A, _ROOM_A, None, None]
    assert read.await_args_list == [
        ((_ROOM_A, "$b"), {}),
        ((_ROOM_A, "$a"), {}),
    ]
    assert [(h.target_room_id, h.child_room_id, h.parent_room_id, h.fork_root.event_id) for h in seen_hops] == [
        (_ROOM_C, _ROOM_C, _ROOM_A, "$c"),
        (_ROOM_C, _ROOM_A, _ROOM_A, "$b"),
    ]
    assert fork_parent_scopes(result, "$c", room_id=_ROOM_C) == frozenset({(_ROOM_A, "$b"), (_ROOM_A, "$a")})


@pytest.mark.asyncio
async def test_cross_room_cycle_detection_is_keyed_by_room_and_thread() -> None:
    """Two different rooms sharing a thread ID do not false-cycle, while a real (room, thread) cycle stops cleanly."""
    read = _reader(
        {
            (_ROOM_A, "$t"): _thread("$t", ["$a1"]),
            (_ROOM_B, "$t"): _thread("$t", ["$b1"], (_ROOM_A, "$t", "$a1")),
        },
    )
    fork_c = _thread("$c", ["$c1"], (_ROOM_B, "$t", "$b1"))
    allow = AsyncMock(return_value=CrossRoomForkDecision.ALLOWED)
    result = await with_fork_prefix(
        fork_c,
        room_id=_ROOM_C,
        thread_id="$c",
        read_thread=read,
        authorize_cross_room=allow,
        logger=MagicMock(),
    )
    assert [(m.room_id, m.event_id) for m in result.messages] == [
        (_ROOM_A, "$t"),
        (_ROOM_A, "$a1"),
        (_ROOM_B, "$t"),
        (_ROOM_B, "$b1"),
        (None, "$c"),
        (None, "$c1"),
    ]
    assert fork_parent_scopes(result, "$c", room_id=_ROOM_C) == frozenset({(_ROOM_B, "$t"), (_ROOM_A, "$t")})

    # Real cycle across rooms: (!roomA, $x) -> (!roomB, $x) -> (!roomA, $x)
    cyclic_read = _reader(
        {
            (_ROOM_A, "$x"): _thread("$x", ["$ar"], (_ROOM_B, "$x", "$br")),
            (_ROOM_B, "$x"): _thread("$x", ["$br"], (_ROOM_A, "$x", "$ar")),
        },
    )
    cyclic_result = await with_fork_prefix(
        _thread("$x", ["$ar"], (_ROOM_B, "$x", "$br")),
        room_id=_ROOM_A,
        thread_id="$x",
        read_thread=cyclic_read,
        authorize_cross_room=allow,
        logger=MagicMock(),
    )
    assert cyclic_read.await_count == 1
    assert [(m.room_id, m.event_id) for m in cyclic_result.messages] == [
        (_ROOM_B, "$x"),
        (_ROOM_B, "$br"),
        (None, "$x"),
        (None, "$ar"),
    ]


@pytest.mark.asyncio
async def test_cross_room_denied_stops_cleanly_without_reading_or_degrading() -> None:
    """Denied cross-room hop yields zero parent messages, never reads parent, and never degrades child history."""
    read = _reader({(_ROOM_A, "$p"): _thread("$p", ["$p1"])})
    fork = _thread("$f", ["$f1"], (_ROOM_A, "$p", "$p1"))
    deny = AsyncMock(return_value=CrossRoomForkDecision.DENIED)

    denied = await with_fork_prefix(
        fork,
        room_id=_ROOM_B,
        thread_id="$f",
        read_thread=read,
        authorize_cross_room=deny,
        logger=MagicMock(),
    )
    assert _ids(denied) == ["$f", "$f1"]
    assert denied.is_full_history
    assert not is_thread_history_degraded(denied)
    assert fork_parent_scopes(denied, "$f", room_id=_ROOM_B) == frozenset()
    read.assert_not_awaited()

    # Missing authorize_cross_room callback also fails closed without degrading.
    no_gate = await with_fork_prefix(fork, room_id=_ROOM_B, thread_id="$f", read_thread=read, logger=MagicMock())
    assert _ids(no_gate) == ["$f", "$f1"]
    assert no_gate.is_full_history
    assert not is_thread_history_degraded(no_gate)
    read.assert_not_awaited()


@pytest.mark.asyncio
async def test_cross_room_deferred_marks_history_degraded_without_reading() -> None:
    """DEFERRED decision marks the snapshot degraded and incomplete so dispatch upgrades to STRICT."""
    read = _reader({(_ROOM_A, "$p"): _thread("$p", ["$p1"])})
    fork = _thread("$f", ["$f1"], (_ROOM_A, "$p", "$p1"))
    defer = AsyncMock(return_value=CrossRoomForkDecision.DEFERRED)

    result = await with_fork_prefix(
        fork,
        room_id=_ROOM_B,
        thread_id="$f",
        read_thread=read,
        authorize_cross_room=defer,
        logger=MagicMock(),
    )
    assert _ids(result) == ["$f", "$f1"]
    assert is_thread_history_degraded(result)
    assert not result.is_full_history
    read.assert_not_awaited()


@pytest.mark.asyncio
async def test_chain_stops_after_eight_parents() -> None:
    """Chain stops after eight parents."""
    threads = {}
    for i in range(10):
        parent = (f"$t{i + 1}", f"$t{i + 1}r")
        threads[f"$t{i}"] = _thread(f"$t{i}", [f"$t{i}r"], parent if i < 9 else None)
    read = _reader(threads)
    logger = MagicMock()
    result = await with_fork_prefix(threads["$t0"], room_id=_ROOM, thread_id="$t0", read_thread=read, logger=logger)
    assert _MAX_FORK_DEPTH == 8
    assert read.await_count == 8
    assert _ids(result) == [x for i in range(8, 0, -1) for x in (f"$t{i}", f"$t{i}r")] + ["$t0", "$t0r"]
    assert logger.warning.call_count == 1


@pytest.mark.asyncio
async def test_cycle_stops() -> None:
    """Cycle stops."""
    read = _reader(
        {
            "$a": _thread("$a", ["$ar"], ("$b", "$br")),
            "$b": _thread("$b", ["$br"], ("$a", "$ar")),
        },
    )
    result = await with_fork_prefix(
        _thread("$a", ["$ar"], ("$b", "$br")),
        room_id=_ROOM,
        thread_id="$a",
        read_thread=read,
        logger=MagicMock(),
    )
    assert read.await_count == 1
    assert _ids(result) == ["$b", "$br", "$a", "$ar"]


@pytest.mark.asyncio
async def test_missing_parent_or_fork_point_yields_own_messages() -> None:
    """Missing parent or fork point yields own messages."""
    fork = _thread("$f", ["$f1"], ("$p", "$p2"))
    empty = await with_fork_prefix(fork, room_id=_ROOM, thread_id="$f", read_thread=_reader({}), logger=MagicMock())
    assert _ids(empty) == ["$f", "$f1"]
    other = _reader({"$p": _thread("$p", ["$p1"])})
    absent = await with_fork_prefix(fork, room_id=_ROOM, thread_id="$f", read_thread=other, logger=MagicMock())
    assert _ids(absent) == ["$f", "$f1"]


@pytest.mark.asyncio
async def test_completeness_needs_every_part() -> None:
    """Completeness needs every part."""
    fork = _thread("$f", ["$f1"], ("$p", "$p1"))
    partial = _reader({"$p": _thread("$p", ["$p1"], is_full_history=False)})
    result = await with_fork_prefix(fork, room_id=_ROOM, thread_id="$f", read_thread=partial, logger=MagicMock())
    assert not result.is_full_history
    degraded = _reader({"$p": _thread("$p", ["$p1"], diagnostics={THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True})})
    result = await with_fork_prefix(fork, room_id=_ROOM, thread_id="$f", read_thread=degraded, logger=MagicMock())
    assert is_thread_history_degraded(result)
    assert result.is_full_history


@pytest.mark.asyncio
async def test_plain_thread_is_returned_without_reads() -> None:
    """Plain thread is returned without reads."""
    read = _reader({})
    plain = _thread("$f", ["$f1"])
    result = await with_fork_prefix(plain, room_id=_ROOM, thread_id="$f", read_thread=read, logger=MagicMock())
    read.assert_not_awaited()
    assert result is plain


@pytest.mark.asyncio
async def test_incomplete_parent_without_the_fork_point_is_reported_incomplete() -> None:
    """A parent read that may lack the fork point marks the fork incomplete, so dispatch reads it strictly."""
    fork = _thread("$f", ["$f1"], ("$p", "$p2"))
    partial = _reader({"$p": _thread("$p", ["$p1"], is_full_history=False)})
    result = await with_fork_prefix(fork, room_id=_ROOM, thread_id="$f", read_thread=partial, logger=MagicMock())
    assert _ids(result) == ["$f", "$f1"]
    assert not result.is_full_history
    degraded = _reader({"$p": _thread("$p", ["$p1"], diagnostics={THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True})})
    result = await with_fork_prefix(fork, room_id=_ROOM, thread_id="$f", read_thread=degraded, logger=MagicMock())
    assert _ids(result) == ["$f", "$f1"]
    assert is_thread_history_degraded(result)


@pytest.mark.asyncio
async def test_missing_fork_point_is_logged_once_when_the_parent_has_messages() -> None:
    """A parent that has messages but not the fork point logs it; a parent with none stays quiet."""
    fork = _thread("$f", ["$f1"], ("$p", "$p2"))
    logger = MagicMock()
    await with_fork_prefix(
        fork,
        room_id=_ROOM,
        thread_id="$f",
        read_thread=_reader({"$p": _thread("$p", ["$p1"])}),
        logger=logger,
    )
    logger.info.assert_called_once_with(
        "Thread fork point missing from its parent",
        room_id=_ROOM,
        thread_id="$f",
        parent_room_id=_ROOM,
        parent_thread_id="$p",
        fork_point_event_id="$p2",
    )
    quiet = MagicMock()
    await with_fork_prefix(fork, room_id=_ROOM, thread_id="$f", read_thread=_reader({}), logger=quiet)
    quiet.info.assert_not_called()


def test_fork_parent_scopes_and_fork_reference() -> None:
    """fork_parent_scopes tracks (room_id, thread_id) pairs present in resolved history."""
    history = thread_history_result(
        [
            make_visible_message(event_id="$g", room_id=_ROOM_A),
            make_visible_message(event_id="$g1", thread_id="$g", room_id=_ROOM_A),
            make_visible_message(event_id="$p", content=_fork_content("$g", "$g1"), room_id=_ROOM_A),
            make_visible_message(event_id="$f", content=_fork_content("$p", "$p", room_id=_ROOM_A)),
            make_visible_message(event_id="$f1", thread_id="$f"),
        ],
        is_full_history=True,
    )
    assert fork_reference(history, "$f", room_id=_ROOM_B) == ThreadForkReference(_ROOM_A, "$p", "$p")
    assert fork_parent_scopes(history, "$f", room_id=_ROOM_B) == frozenset({(_ROOM_A, "$p"), (_ROOM_A, "$g")})
    assert fork_parent_scopes(_thread("$t", ["$t1"]), "$t", room_id=_ROOM) == frozenset()
    unresolved = _thread("$f", ["$f1"], ("$p", "$p1"))
    assert fork_parent_scopes(unresolved, "$f", room_id=_ROOM) == frozenset()
