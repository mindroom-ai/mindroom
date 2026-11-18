"""Post a voice message's transcript once, from the agent that answers it."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5
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
from mindroom.event_journal import turn_rows
from mindroom.matrix.guarded_rooms import send_refused

if TYPE_CHECKING:
    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.dispatch_handoff import PreparedIngress
    from mindroom.event_journal import EventJournalStore
    from mindroom.message_target import MessageTarget
    from mindroom.turn_store import TurnStore

# How long a bot waits for the receipt of another bot that posts the transcript and has failed or not run yet.
_OWNER_RECEIPT_GRACE_SECONDS = 0.25
# One lock per voice message for every bot of this process: a bot that does not post the transcript still waits
# here for the bot that does, so no reply precedes it.
_posting: WeakValueDictionary[tuple[str, str], asyncio.Condition] = WeakValueDictionary()


@dataclass(frozen=True)
class VisibleVoiceEchoLifecycle:
    """Post the transcript of a voice message this bot answers, unless a bot already did."""

    runtime: BotRuntimeView
    agent_name: str
    delivery_gateway: DeliveryGateway
    turn_store: TurnStore
    # Holds the claim every bot shares, whose first writer posts the transcript, and each bot's receipt.
    journal: EventJournalStore

    async def publish(
        self,
        room_id: str,
        source_event_id: str,
        target: MessageTarget,
        transcript: PreparedIngress,
    ) -> None:
        """Return once the transcript is in the room, or when none is posted; raise when it could not be sent.

        The caller holds the reply decision's admission, so the config read here stays the one it decided on.
        """
        content = transcript.source["content"]
        # A contact room takes only the sends its message policy approved, and no transcript is one.
        unposted = content.get(VOICE_RAW_AUDIO_FALLBACK_KEY) is True or send_refused(room_id)
        if unposted or not self.runtime.config.voice.visible_router_echo:
            return
        posting = _posting.setdefault((room_id, source_event_id), asyncio.Condition())
        async with posting:
            owner = await self.journal.claim_room_event(room_id, source_event_id, "voice_transcript", self.agent_name)
            if owner != self.agent_name:
                # The owner held this lock during its send, so it has no receipt only when that send failed or it
                # has not run yet. Its retry gets a short grace, which releases the lock; then this bot answers.
                if await self.journal.turn_records(owner).read(turn_rows.visible_echo, source_event_id) is None:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(posting.wait(), _OWNER_RECEIPT_GRACE_SECONDS)
                return
            if await self.turn_store.visible_echo(source_event_id) is not None:
                return
            event_id = await self.delivery_gateway.send_text(
                SendTextRequest(
                    target=target,
                    response_text=transcript.body,
                    skip_mentions=True,
                    # A retry after a lost acknowledgement or receipt gets the event the homeserver already accepted.
                    transaction_id=str(uuid5(NAMESPACE_URL, f"mindroom-voice-transcript:{room_id}:{source_event_id}")),
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
            await self.turn_store.record_finalized_visible_echo(source_event_id, event_id, is_fallback=False)
            posting.notify_all()
