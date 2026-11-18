"""Post a voice message's transcript once, from the agent that answers it."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
    SOURCE_KIND_KEY,
    VISIBLE_ROUTER_VOICE_ECHO_KEY,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
    VOICE_TRANSCRIPT_KEY,
)
from mindroom.delivery_gateway import SendTextRequest
from mindroom.dispatch_source import TRUSTED_INTERNAL_RELAY_SOURCE_KIND

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.dispatch_handoff import PreparedIngress
    from mindroom.message_target import MessageTarget
    from mindroom.turn_store import TurnStore

# One lock per voice message for every bot of this process: a bot that does not post the transcript still waits
# here for the bot that does, so no reply precedes it.
_locks: WeakValueDictionary[tuple[str, str], asyncio.Lock] = WeakValueDictionary()


@dataclass(frozen=True)
class VisibleVoiceEchoLifecycle:
    """Post the transcript of a voice message this bot answers, unless a bot already did."""

    runtime: BotRuntimeView
    agent_name: str
    delivery_gateway: DeliveryGateway
    turn_store: TurnStore
    # `EventJournalStore.claim_room_event`: the first bot to claim a voice message posts its transcript.
    claim_room_event: Callable[[str, str, str, str], Awaitable[str]]

    async def publish(
        self,
        room_id: str,
        source_event_id: str,
        target: MessageTarget,
        transcript: PreparedIngress,
    ) -> None:
        """Return once the transcript is in the room, or when none is posted; raise when it could not be sent."""
        content = transcript.source["content"]
        if not self.runtime.config.voice.visible_router_echo or content.get(VOICE_RAW_AUDIO_FALLBACK_KEY) is True:
            return
        lock = _locks.setdefault((room_id, source_event_id), asyncio.Lock())
        async with lock, self.runtime.admission_fence():
            owner = await self.claim_room_event(room_id, source_event_id, "voice_transcript", self.agent_name)
            # Another owner has posted, as it held this lock during its send; this bot's own receipt says it has.
            if owner != self.agent_name or await self.turn_store.visible_echo(source_event_id) is not None:
                return
            event_id = await self.delivery_gateway.send_text(
                SendTextRequest(
                    target=target,
                    response_text=transcript.body,
                    skip_mentions=True,
                    extra_content={
                        SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
                        VISIBLE_ROUTER_VOICE_ECHO_KEY: True,
                        ORIGINAL_SENDER_KEY: transcript.sender,
                        **{key: content[key] for key in (ATTACHMENT_IDS_KEY, VOICE_TRANSCRIPT_KEY) if key in content},
                    },
                ),
            )
            if event_id is None:
                msg = "The voice transcript could not be sent"
                raise RuntimeError(msg)
            await self.turn_store.record_visible_echo(room_id, source_event_id, event_id)
