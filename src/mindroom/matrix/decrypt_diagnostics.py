"""Runtime-owned diagnostics for ciphertext whose recovery belongs to Nio."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import nio

from mindroom.authorization import is_sender_allowed_for_agent_reply_in_room
from mindroom.background_tasks import create_background_task
from mindroom.matrix.decrypt_failure import handle_decrypt_failure

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from contextlib import AbstractAsyncContextManager

    from nio.durable import SyncRecord

    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import RoomMembershipPosition
    from mindroom.runtime_protocols import SupportsClientConfigMemberships


@dataclass
class DecryptionDiagnostics:
    """Coalesce best-effort notices without claiming an application event ID."""

    agent_name: str
    runtime: SupportsClientConfigMemberships
    runtime_paths: RuntimePaths
    read_position: Callable[[str], Awaitable[RoomMembershipPosition | None]]
    admit_response: Callable[[], AbstractAsyncContextManager[None]]
    notice_is_fenced: Callable[[str], bool]
    _pending: set[tuple[str, str, int]] = field(default_factory=set, init=False)

    def schedule(self, record: SyncRecord) -> None:
        """Queue actionable diagnostics without delaying batch acknowledgement."""
        if record.provenance not in {nio.TimelineEventProvenance.LIVE, nio.TimelineEventProvenance.RECOVERED}:
            return
        client = self.runtime.client
        if client is None or record.room_id is None or record.membership_epoch is None:
            return
        event = nio.Event.parse_event(dict(record.source))
        if not isinstance(event, nio.MegolmEvent):
            return
        event.room_id = record.room_id
        assert event.session_id is not None
        key = (record.room_id, event.session_id, record.membership_epoch)
        if key in self._pending:
            return
        self._pending.add(key)
        task = create_background_task(
            self._run(client, record.room_id, record.membership_epoch, event),
            name=f"decrypt_diagnostic_{self.agent_name}",
            owner=self.runtime,
        )
        task.add_done_callback(lambda _task: self._pending.discard(key))

    async def _is_current(
        self,
        client: nio.AsyncClient,
        room_id: str,
        membership_epoch: int,
        event: nio.MegolmEvent,
    ) -> bool:
        position = await self.read_position(room_id)
        return (
            self.runtime.client is client
            and position is not None
            and (position.membership, position.membership_epoch) == ("join", membership_epoch)
            and is_sender_allowed_for_agent_reply_in_room(
                event.sender,
                self.agent_name,
                self.runtime.config,
                room_id,
                self.runtime_paths,
                self.runtime.agent_reply_memberships,
            )
        )

    async def _run(
        self,
        client: nio.AsyncClient,
        room_id: str,
        membership_epoch: int,
        event: nio.MegolmEvent,
    ) -> None:
        async with self.admit_response():
            if not await self._is_current(client, room_id, membership_epoch, event):
                return

            async def can_notify() -> bool:
                return await self._is_current(client, room_id, membership_epoch, event) and not self.notice_is_fenced(
                    room_id,
                )

            await handle_decrypt_failure(
                client,
                nio.MatrixRoom(room_id, client.user_id),
                event,
                agent_name=self.agent_name,
                runtime_paths=self.runtime_paths,
                can_notify=can_notify,
            )
