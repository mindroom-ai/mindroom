"""Making MindRoom's own messages readable before the server echoes them.

Sync remains the only authoritative source of conversation content, including
this bot's own messages. This exists for the window before that: a turn that
speaks and then reads, and a turn no room event triggered at all -- a
scheduled task, a todo poke -- would otherwise read a room it has already
spoken in as one it has not.

What is written here is a placeholder for ordering purposes only. The send
response carries an event ID and nothing else, so the timestamp is this
machine's clock, and the projection replaces the whole row when the echo
arrives.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.event_journal import ProjectedEvent, thread_root

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.event_journal import PrincipalStore


@dataclass(frozen=True, slots=True)
class OutboundProjection:
    """Seeds one principal's projection from its own accepted sends."""

    store: PrincipalStore
    sender: str

    async def record_sent(
        self,
        *,
        room_id: str,
        event_id: str,
        content: Mapping[str, object],
    ) -> None:
        """Record a message this bot sent and Matrix accepted."""
        await self.store.seed_outbound_message(
            ProjectedEvent(
                event_id=event_id,
                room_id=room_id,
                # Taken from the content rather than the caller's target: this
                # is the thread the message actually claims to be in, which is
                # what the echo will say too.
                thread_id=thread_root(content),
                sender=self.sender,
                origin_server_ts=int(time.time() * 1_000),
                content=content,
                replaces_event_id=None,
                redacts_event_id=None,
            ),
        )
