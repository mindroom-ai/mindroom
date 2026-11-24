"""Shells in a pty that keep running, with their scrollback, while viewers come and go."""

from __future__ import annotations

import asyncio
import os
import re
import signal
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from mindroom.terminal import session as session_module
from mindroom.terminal.session import TerminalError, TerminalLaunch, TerminalManager
from tests.conftest import requires_linux

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.terminal.session import TerminalEvent, TerminalSession, TerminalViewer


def _launch(tmp_path: Path) -> TerminalLaunch:
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "TERM": "xterm-256color", "PS1": "$ "}
    return TerminalLaunch(("bash", "--noprofile", "--norc", "-i"), env, str(tmp_path))


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def manager() -> AsyncIterator[TerminalManager]:
    """Keep each test's terminals, and end those it leaves running."""
    terminals = TerminalManager()
    yield terminals
    await terminals.close_all()


async def _read_until(viewer: TerminalViewer, marker: bytes, seen: bytes = b"") -> bytes:
    """Return ``seen`` and what ``viewer`` gets until ``marker``, which a command's echo never holds."""
    async with asyncio.timeout(10):
        while marker not in seen:
            event = await viewer.next()
            assert event.type == "output", event
            seen += event.data
    return seen


async def _exit_code(viewer: TerminalViewer) -> int | None:
    async with asyncio.timeout(10):
        while (event := await viewer.next()).type != "exit":
            assert event.type == "output", event
    return event.code


async def _queued(viewer: TerminalViewer) -> TerminalEvent | None:
    """Return the event waiting for ``viewer``, if any: a waiting one returns without yielding to the timeout."""
    try:
        async with asyncio.timeout(0):
            return await viewer.next()
    except TimeoutError:
        return None


async def _ended(session: TerminalSession) -> None:
    async with asyncio.timeout(10):
        while session.alive:  # noqa: ASYNC110 - a session's end has no event to await
            await asyncio.sleep(0.01)


async def test_a_viewer_gets_the_scrollback_then_live_output_with_no_gap_and_nothing_twice(
    tmp_path: Path,
    manager: TerminalManager,
) -> None:
    """A viewer that attaches while a command prints sees every line once, in order; a detached one sees no more."""
    session = manager.open("owner", _launch(tmp_path), cols=80, rows=24)
    _, first = session.attach()
    session.write(b"for i in $(seq 1 3000); do echo n$i; done; echo $((40+2))end\n")
    await _read_until(first, b"n50\r\n")
    scrollback, second = session.attach()
    session.detach(first)
    seen = await _read_until(second, b"42end", scrollback)

    assert re.findall(rb"\bn(\d+)\r\n", seen) == [str(i).encode() for i in range(1, 3001)]
    while await _queued(first) is not None:
        pass
    session.write(b"echo $((6*7))after\n")
    await _read_until(second, b"42after")
    assert await _queued(first) is None


async def test_input_and_resize_reach_the_shell(tmp_path: Path, manager: TerminalManager) -> None:
    """Typed keys run in the shell, and a new size is the one its programs read."""
    session = manager.open("owner", _launch(tmp_path), cols=80, rows=24)
    _, viewer = session.attach()
    session.write(b"stty size; echo $((1+1))sized\n")
    assert b"24 80" in await _read_until(viewer, b"2sized")

    session.resize(132, 40)
    session.write(b"stty size; echo $((2+1))sized\n")

    assert b"40 132" in await _read_until(viewer, b"3sized")
    assert (session.info().cols, session.info().rows) == (132, 40)


async def test_a_session_ends_when_its_shell_exits_and_keeps_its_scrollback(
    tmp_path: Path,
    manager: TerminalManager,
) -> None:
    """Viewers get the exit code and all output before it, a later viewer gets the scrollback and the exit."""
    session = manager.open("owner", _launch(tmp_path), cols=80, rows=24)
    _, viewer = session.attach()
    session.write(b"head -c 200000 /dev/zero | tr '\\0' z; exit 7\n")

    assert await _exit_code(viewer) == 7
    info = session.info()
    assert (info.alive, info.exit_code) == (False, 7)
    scrollback, late = session.attach()
    assert b"z" * 200000 in scrollback
    assert await _exit_code(late) == 7
    with pytest.raises(TerminalError) as refused:
        session.write(b"echo again\n")
    assert refused.value.status_code == 409
    assert manager.sessions("owner") == [session]


@requires_linux(reason="relies on a Linux pty holding more unread output than one read returns")
async def test_output_that_arrives_with_the_exit_is_kept(tmp_path: Path, manager: TerminalManager) -> None:
    """Output still unread when the shell's exit is noticed, more than one read takes, reaches viewers first."""
    session = manager.open("owner", _launch(tmp_path), cols=80, rows=24)
    _, viewer = session.attach()
    # The first prompt shows the loop has run since the session started watching for the shell's exit.
    await _read_until(viewer, b"$ ")
    session.write(b"head -c 6000 /dev/zero | tr '\\0' q; exit 5\n")
    # Block the loop until the shell has exited, so its last output and its exit wait together.
    os.waitid(os.P_PID, session.pid, os.WEXITED | os.WNOWAIT)  # noqa: ASYNC222 - blocks the loop on purpose

    assert await _exit_code(viewer) == 5
    scrollback, _ = session.attach()
    assert b"q" * 6000 in scrollback


async def test_without_pidfds_a_session_still_ends(
    tmp_path: Path,
    manager: TerminalManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where the platform has no pidfds, the shell's exit is still noticed."""
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.setattr(session_module, "_EXIT_POLL_SECONDS", 0.01)
    session = manager.open("owner", _launch(tmp_path), cols=80, rows=24)
    _, viewer = session.attach()
    session.write(b"exit 4\n")

    assert await _exit_code(viewer) == 4


async def test_an_ended_session_is_kept_for_the_idle_time_after_its_shell_exits(
    tmp_path: Path,
    manager: TerminalManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shell that exits late in its idle time keeps its scrollback and exit code for the whole idle time after."""
    monkeypatch.setattr(session_module, "_IDLE_SECONDS", 1.0)
    loop = asyncio.get_running_loop()
    session = manager.open("owner", _launch(tmp_path), cols=80, rows=24)
    session.write(b"sleep 0.6; exit 3\n")

    await _ended(session)
    ended_at = loop.time()
    async with asyncio.timeout(10):
        while manager.sessions("owner"):  # noqa: ASYNC110 - forgetting has no event to await
            await asyncio.sleep(0.01)

    assert session.exit_code == 3
    assert loop.time() - ended_at >= 0.95


async def test_a_shell_with_no_input_and_no_viewer_is_hung_up_and_forgotten(
    tmp_path: Path,
    manager: TerminalManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idle time counts only while nobody types and nobody watches; an ended session is forgotten after it too."""
    monkeypatch.setattr(session_module, "_IDLE_SECONDS", 0.5)
    loop = asyncio.get_running_loop()
    watched = manager.open("owner", _launch(tmp_path), cols=80, rows=24)
    _, viewer = watched.attach()
    typed = manager.open("owner", _launch(tmp_path), cols=80, rows=24)
    ended = manager.open("owner", _launch(tmp_path), cols=80, rows=24)
    ended.write(b"exit 3\n")
    await asyncio.sleep(0.3)
    typed.write(b"true\n")
    typed_at = loop.time()

    await _ended(typed)
    assert loop.time() - typed_at >= 0.5
    await _ended(ended)
    async with asyncio.timeout(10):
        while manager.sessions("owner") != [watched]:  # noqa: ASYNC110 - forgetting has no event to await
            await asyncio.sleep(0.05)
    assert watched.alive

    watched.detach(viewer)
    detached_at = loop.time()
    await _ended(watched)
    assert loop.time() - detached_at >= 0.5
    assert manager.sessions("owner") == []


async def test_a_viewer_that_falls_too_far_behind_is_dropped(
    tmp_path: Path,
    manager: TerminalManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A viewer that reads nothing is dropped once its backlog passes the limit; one that keeps up stays."""
    # Above one read of the pty, so a viewer that reads each one as it comes never falls behind.
    monkeypatch.setattr(session_module, "_MAX_VIEWER_BACKLOG_BYTES", 2 * session_module._READ_CHUNK)
    session = manager.open("owner", _launch(tmp_path), cols=80, rows=24)
    _, stalled = session.attach()
    _, reading = session.attach()
    session.write(b"head -c 1000000 /dev/zero | tr '\\0' x; echo; echo $((5*5))done\n")

    await _read_until(reading, b"25done")
    assert stalled.dropped
    assert not reading.dropped
    kinds = []
    while (event := await _queued(stalled)) is not None:
        kinds.append(event.type)
    assert kinds[-1] == "dropped"
    assert "dropped" not in kinds[:-1]
    session.write(b"echo $((4*4))still\n")
    await _read_until(reading, b"16still")
    assert await _queued(stalled) is None


async def test_a_session_answers_only_its_owner_and_each_owner_has_a_limit(
    tmp_path: Path,
    manager: TerminalManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another owner can't find a session; a full owner first forgets its oldest ended session, then refuses.

    The runner as a whole holds a limited number of sessions too.
    """
    monkeypatch.setattr(session_module, "_MAX_SESSIONS_PER_OWNER", 2)
    first = manager.open("alice", _launch(tmp_path), cols=80, rows=24)
    second = manager.open("alice", _launch(tmp_path), cols=80, rows=24)

    with pytest.raises(TerminalError) as hidden:
        manager.get("bob", first.id)
    assert hidden.value.status_code == 404
    assert manager.sessions("bob") == []
    with pytest.raises(TerminalError) as full:
        manager.open("alice", _launch(tmp_path), cols=80, rows=24)
    assert full.value.status_code == 429

    first.write(b"exit\n")
    await _ended(first)
    third = manager.open("alice", _launch(tmp_path), cols=80, rows=24)
    assert manager.sessions("alice") == [second, third]
    with pytest.raises(TerminalError) as invalid:
        manager.open("alice/../x", _launch(tmp_path), cols=80, rows=24)
    assert invalid.value.status_code == 400
    monkeypatch.setattr(session_module, "_MAX_SESSIONS", 2)
    with pytest.raises(TerminalError) as crowded:
        manager.open("bob", _launch(tmp_path), cols=80, rows=24)
    assert crowded.value.status_code == 429


async def test_typing_is_refused_when_too_much_comes_at_once_or_the_shell_stops_reading(
    tmp_path: Path,
    manager: TerminalManager,
) -> None:
    """One write holds at most the input limit, and typing the shell does not read piles up only so far."""
    session = manager.open("owner", _launch(tmp_path), cols=80, rows=24)
    _, viewer = session.attach()
    # In canonical mode the kernel takes, and drops, a line that never ends however long it grows, so only how fast it
    # drains would decide whether typing piles up; without it, the terminal takes a few KiB and then stops reading.
    session.write(b"stty -icanon; echo $((2*2))asleep; sleep 600\n")
    await _read_until(viewer, b"4asleep")

    with pytest.raises(TerminalError) as too_much:
        session.write(b"x" * (session_module._MAX_INPUT_BYTES + 1))
    assert too_much.value.status_code == 413

    def type_past_the_unread_limit() -> None:
        for _ in range(session_module._MAX_PENDING_INPUT_BYTES // session_module._MAX_INPUT_BYTES + 2):
            session.write(b"x" * session_module._MAX_INPUT_BYTES)

    with pytest.raises(TerminalError) as unread:
        type_past_the_unread_limit()
    assert unread.value.status_code == 429


async def test_a_shell_that_ignores_the_hangup_is_killed(
    tmp_path: Path,
    manager: TerminalManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing kills a shell that is still running after the hangup's grace time."""
    monkeypatch.setattr(session_module, "_HANGUP_GRACE_SECONDS", 0.2)
    session = manager.open("owner", _launch(tmp_path), cols=80, rows=24)
    _, viewer = session.attach()
    session.write(b"trap '' HUP; echo $((3+4))trapped\n")
    await _read_until(viewer, b"7trapped")

    await manager.close("owner", session.id)

    assert session.exit_code == -signal.SIGKILL


async def test_closing_hangs_up_the_shell_and_its_foreground_job(tmp_path: Path, manager: TerminalManager) -> None:
    """Closing ends the shell even while a command runs, and forgets the session; closing all ends the rest."""
    running = manager.open("owner", _launch(tmp_path), cols=80, rows=24)
    _, viewer = running.attach()
    running.write(b"echo $((7*6))sleeping; sleep 600\n")
    await _read_until(viewer, b"42sleeping")
    other = manager.open("owner", _launch(tmp_path), cols=80, rows=24)

    await manager.close("owner", running.id)

    assert running.exit_code == -signal.SIGHUP
    assert manager.sessions("owner") == [other]
    await manager.close_all()
    assert not other.alive
    assert manager.sessions("owner") == []


async def test_scrollback_keeps_the_last_bytes_from_a_line_start(
    tmp_path: Path,
    manager: TerminalManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scrollback holds at most its limit, cut where a line starts, so a replay starts with a whole line."""
    monkeypatch.setattr(session_module, "_SCROLLBACK_BYTES", 2048)
    session = manager.open("owner", _launch(tmp_path), cols=80, rows=24)
    _, viewer = session.attach()
    session.write(b"for i in $(seq 1 1000); do echo line$i; done; echo $((8*8))end\n")
    await _read_until(viewer, b"64end")

    scrollback, _ = session.attach()

    assert len(scrollback) <= 2048
    assert scrollback.startswith(b"line")
    assert b"64end" in scrollback
