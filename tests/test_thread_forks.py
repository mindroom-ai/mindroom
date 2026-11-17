"""Tests for resolving forked threads as a parent prefix plus their own messages."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom.constants import FORK_CONTENT_KEY
from mindroom.matrix.thread_diagnostics import THREAD_HISTORY_DEGRADED_DIAGNOSTIC, is_thread_history_degraded
from mindroom.matrix.thread_forks import (
    _MAX_FORK_DEPTH,
    _thread_fork_reference,
    _ThreadForkReference,
    fork_parent_thread_ids,
    fork_roots,
    with_fork_prefix,
)
from mindroom.matrix.thread_history_result import ThreadHistoryResult, thread_history_result
from tests.conftest import make_visible_message

_ROOM = "!room:localhost"


def _fork_content(thread_id: str, event_id: str) -> dict[str, object]:
    return {FORK_CONTENT_KEY: {"thread_id": thread_id, "event_id": event_id}}


def _thread(
    root: str,
    replies: list[str],
    fork_of: tuple[str, str] | None = None,
    *,
    is_full_history: bool = True,
    diagnostics: dict[str, bool] | None = None,
) -> ThreadHistoryResult:
    content = _fork_content(*fork_of) if fork_of else None
    messages = [make_visible_message(event_id=root, content=content)]
    messages += [make_visible_message(event_id=e, thread_id=root) for e in replies]
    return thread_history_result(messages, is_full_history=is_full_history, diagnostics=diagnostics)


def _reader(threads: dict[str, ThreadHistoryResult]) -> AsyncMock:
    async def read(thread_id: str) -> ThreadHistoryResult:
        return threads.get(thread_id, thread_history_result([], is_full_history=True))

    return AsyncMock(side_effect=read)


def _ids(history: ThreadHistoryResult) -> list[str]:
    return [m.event_id for m in history]


def test_reference_parses_only_two_event_ids() -> None:
    """Reference parses only two event ids."""
    assert _thread_fork_reference(_fork_content("$p", "$p2")) == _ThreadForkReference("$p", "$p2")
    assert _thread_fork_reference({}) is None
    assert _thread_fork_reference({FORK_CONTENT_KEY: "$p"}) is None
    assert _thread_fork_reference(_fork_content("", "$p2")) is None
    assert _thread_fork_reference(_fork_content("$p", "")) is None
    assert _thread_fork_reference(_fork_content("p", "$p2")) is None
    assert _thread_fork_reference(_fork_content("$p", "p2")) is None


@pytest.mark.asyncio
async def test_fork_history_is_parent_prefix_then_own_messages() -> None:
    """Fork history is parent prefix then own messages."""
    read = _reader({"$p": _thread("$p", ["$p1", "$p2", "$p3"])})
    fork = _thread("$f", ["$f1"], ("$p", "$p2"))
    result = await with_fork_prefix(fork, room_id=_ROOM, thread_id="$f", read_thread=read, logger=MagicMock())
    assert _ids(result) == ["$p", "$p1", "$p2", "$f", "$f1"]
    assert result.messages[1].thread_id == "$p"


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
    # Each fork root is where one parent's messages end, so each gets its own divider.
    assert fork_roots(result) == {2: ("$p", "$g", "$g1"), 4: ("$f", "$p", "$p1")}


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
        parent_thread_id="$p",
        fork_point_event_id="$p2",
    )
    quiet = MagicMock()
    await with_fork_prefix(fork, room_id=_ROOM, thread_id="$f", read_thread=_reader({}), logger=quiet)
    quiet.info.assert_not_called()


def test_parent_thread_ids_follow_the_parents_in_the_history() -> None:
    """A fork's parent threads are those its references reach whose messages its history holds."""
    history = thread_history_result(
        [
            make_visible_message(event_id="$g"),
            make_visible_message(event_id="$g1", thread_id="$g"),
            make_visible_message(event_id="$p", content=_fork_content("$g", "$g1")),
            make_visible_message(event_id="$f", content=_fork_content("$p", "$p")),
            make_visible_message(event_id="$f1", thread_id="$f"),
        ],
        is_full_history=True,
    )
    assert fork_parent_thread_ids(history, "$f") == {"$p", "$g"}
    assert fork_parent_thread_ids(_thread("$t", ["$t1"]), "$t") == set()
    unresolved = _thread("$f", ["$f1"], ("$p", "$p1"))
    assert fork_parent_thread_ids(unresolved, "$f") == set()
