"""LiveKit media bridge running an OpenAI realtime voice agent in a call.

This is the media plane of a MatrixRTC call: it connects to the LiveKit SFU
with the credentials minted by the MatrixRTC Authorization Service, applies
per-participant frame-encryption keys, and drives a ``livekit-agents``
``AgentSession`` backed by an OpenAI speech-to-speech realtime model.

The heavy ``livekit`` / ``livekit-agents`` dependencies are optional (the
``matrix_calls`` extra), so all imports happen inside functions.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.matrix_rtc.focus import SfuGrant

logger = get_logger(__name__)

#: Frame-crypto settings mirroring Element Call's ``MatrixKeyProvider``
#: (``keyringSize: 256`` fits the 0-255 key indices, ``ratchetWindowSize: 10``).
_KEY_RING_SIZE = 256
_RATCHET_WINDOW_SIZE = 10


def matrix_calls_dependencies_available() -> bool:
    """Whether the optional ``matrix_calls`` extra is installed."""
    return (
        importlib.util.find_spec("livekit.rtc") is not None
        and importlib.util.find_spec("livekit.agents") is not None
        and importlib.util.find_spec("livekit.plugins.openai") is not None
    )


@dataclass(frozen=True)
class VoiceAgentOptions:
    """Everything the realtime voice agent needs to join and speak."""

    instructions: str
    model: str
    api_key: str
    voice: str | None = None
    greeting_instructions: str | None = None


class RealtimeVoiceBridge:
    """One LiveKit connection with an OpenAI realtime agent on top."""

    def __init__(self, *, local_identity: str, e2ee_enabled: bool) -> None:
        self._local_identity = local_identity
        self._e2ee_enabled = e2ee_enabled
        self._room: Any = None
        self._session: Any = None

    async def connect(self, grant: SfuGrant) -> None:
        """Connect to the SFU, enabling frame encryption when required."""
        from livekit import rtc  # noqa: PLC0415

        options = rtc.RoomOptions(auto_subscribe=True)
        if self._e2ee_enabled:
            options = rtc.RoomOptions(
                auto_subscribe=True,
                e2ee=rtc.E2EEOptions(
                    key_provider_options=rtc.KeyProviderOptions(
                        ratchet_window_size=_RATCHET_WINDOW_SIZE,
                        key_ring_size=_KEY_RING_SIZE,
                    ),
                ),
            )
        room = rtc.Room()
        await room.connect(grant.url, grant.jwt, options)
        self._room = room
        logger.info("call_sfu_connected", url=grant.url, identity=self._local_identity)

    def set_frame_key(self, participant_identity: str, key: bytes, key_index: int) -> None:
        """Install a media frame key for one participant (or ourselves)."""
        if self._room is None or not self._e2ee_enabled:
            return
        self._room.e2ee_manager.key_provider.set_key(participant_identity, key, key_index)

    async def start_agent(self, options: VoiceAgentOptions) -> None:
        """Start the realtime agent session on the connected room."""
        from livekit.agents import Agent, AgentSession  # noqa: PLC0415
        from livekit.plugins.openai import realtime  # noqa: PLC0415

        if self._room is None:
            msg = "connect() must succeed before start_agent()"
            raise RuntimeError(msg)
        if options.voice:
            model = realtime.RealtimeModel(model=options.model, api_key=options.api_key, voice=options.voice)
        else:
            model = realtime.RealtimeModel(model=options.model, api_key=options.api_key)
        session = AgentSession(llm=model)
        self._session = session
        await session.start(Agent(instructions=options.instructions), room=self._room)
        if options.greeting_instructions:
            session.generate_reply(instructions=options.greeting_instructions)

    async def aclose(self) -> None:
        """Tear down the agent session and leave the SFU."""
        session = self._session
        self._session = None
        if session is not None:
            await session.aclose()
        room = self._room
        self._room = None
        if room is not None:
            await room.disconnect()
