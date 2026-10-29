"""In-memory reply records for driving the streamer directly in tests.

The streamer writes every reply through the reply's records: its first visible
create and its terminal update are the reply's rows, and each direct progress
edit is written ahead before it is sent. ``FakeReplyRecords`` stands in for
those records. It records each write, delivers a create or terminal update
through a Matrix transport, the client's own by default, and answers each
progress write-ahead from a script of permissions, so a test can make any write
fail or refuse it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from mindroom.matrix.client_delivery import edit_message_result, send_message_result
from mindroom.streaming import ProgressPermission

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio

    from mindroom.matrix.client import DeliveredMatrixEvent
    from mindroom.streaming import ProgressDelivered, ProgressState, ProgressWriteAhead, TerminalEdit, TerminalSend

type SendTransport = Callable[[nio.AsyncClient, str, dict[str, Any]], Awaitable[DeliveredMatrixEvent | None]]
type EditTransport = Callable[
    [nio.AsyncClient, str, str, dict[str, Any], str],
    Awaitable[DeliveredMatrixEvent | None],
]


class ProgressHooks(TypedDict):
    """The hooks a progress-only stream writes its progress edits through."""

    progress_write_ahead: ProgressWriteAhead
    progress_delivered: ProgressDelivered


class ReplyStreamHooks(ProgressHooks):
    """Every hook a stream that owns its reply's create and terminal update writes through."""

    initial_send: TerminalSend
    terminal_send: TerminalSend
    terminal_edit: TerminalEdit


@dataclass(frozen=True)
class ReplyWrite:
    """One write the streamer asked the reply's records for.

    ``progress`` asks to send a progress edit and ``progress_delivered``
    reports one that landed on ``event_id``. A ``terminal`` write edits
    ``event_id``, or creates the reply when it is ``None``.
    """

    kind: Literal["create", "progress", "progress_delivered", "terminal"]
    progress: ProgressState
    content: dict[str, Any] | None = None
    display_text: str | None = None
    event_id: str | None = None


@dataclass
class FakeReplyRecords:
    """Reply records that keep each write in memory and deliver creates and terminal updates through a transport."""

    send: SendTransport = send_message_result
    edit: EditTransport = edit_message_result
    # Answers to successive progress write-aheads; once used up, every edit may be sent.
    progress_permissions: list[ProgressPermission] = field(default_factory=list)
    writes: list[ReplyWrite] = field(default_factory=list)

    async def initial_send(
        self,
        client: nio.AsyncClient,
        room_id: str,
        content: dict[str, Any],
        display_text: str,
        *,
        progress: ProgressState,
    ) -> DeliveredMatrixEvent | None:
        """Record the reply's create, then send it."""
        self.writes.append(ReplyWrite("create", progress, dict(content), display_text))
        return await self.send(client, room_id, content)

    async def terminal_send(
        self,
        client: nio.AsyncClient,
        room_id: str,
        content: dict[str, Any],
        display_text: str,
        *,
        progress: ProgressState,
    ) -> DeliveredMatrixEvent | None:
        """Record a terminal update that is the reply's first visible event, then send it."""
        self.writes.append(ReplyWrite("terminal", progress, dict(content), display_text))
        return await self.send(client, room_id, content)

    async def terminal_edit(
        self,
        client: nio.AsyncClient,
        room_id: str,
        event_id: str,
        content: dict[str, Any],
        display_text: str,
        *,
        progress: ProgressState,
    ) -> DeliveredMatrixEvent | None:
        """Record the terminal update of an existing event, then edit it."""
        self.writes.append(ReplyWrite("terminal", progress, dict(content), display_text, event_id))
        return await self.edit(client, room_id, event_id, content, display_text)

    async def progress_write_ahead(self, progress: ProgressState) -> ProgressPermission:
        """Record one progress edit and answer it from the scripted permissions."""
        self.writes.append(ReplyWrite("progress", progress))
        return self.progress_permissions.pop(0) if self.progress_permissions else ProgressPermission.SEND

    def progress_delivered(self, progress: ProgressState, event_id: str) -> None:
        """Record the progress edit that landed."""
        self.writes.append(ReplyWrite("progress_delivered", progress, event_id=event_id))

    def progress_hooks(self) -> ProgressHooks:
        """Return the hooks for a progress-only stream."""
        return {"progress_write_ahead": self.progress_write_ahead, "progress_delivered": self.progress_delivered}

    def hooks(self) -> ReplyStreamHooks:
        """Return every hook for a stream that owns its reply's create and terminal update."""
        return {
            "progress_write_ahead": self.progress_write_ahead,
            "progress_delivered": self.progress_delivered,
            "initial_send": self.initial_send,
            "terminal_send": self.terminal_send,
            "terminal_edit": self.terminal_edit,
        }


def reply_hooks() -> ReplyStreamHooks:
    """Return the hooks of fresh reply records whose writes no test inspects."""
    return FakeReplyRecords().hooks()
