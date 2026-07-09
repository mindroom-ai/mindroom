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
    from collections.abc import Callable

    from livekit.agents import AgentSession
    from livekit.agents.voice.events import ConversationItemAddedEvent, FunctionToolsExecutedEvent

    from mindroom.matrix_rtc.focus import SfuGrant

logger = get_logger(__name__)

#: Frame-crypto settings mirroring Element Call's ``MatrixKeyProvider``
#: (``keyringSize: 256`` fits the 0-255 key indices, ``ratchetWindowSize: 10``).
_KEY_RING_SIZE = 256
_RATCHET_WINDOW_SIZE = 10


def matrix_calls_dependencies_available() -> bool:
    """Whether the optional ``matrix_calls`` extra is installed."""
    # find_spec("livekit.rtc") raises (rather than returning None) when the
    # parent "livekit" package itself is missing.
    try:
        return (
            importlib.util.find_spec("livekit.rtc") is not None
            and importlib.util.find_spec("livekit.agents") is not None
            and importlib.util.find_spec("livekit.plugins.openai") is not None
        )
    except ModuleNotFoundError:
        return False


@dataclass(frozen=True)
class VoiceAgentOptions:
    """Everything the realtime voice agent needs to join and speak."""

    instructions: str
    model: str
    api_key: str
    voice: str | None = None
    greeting_instructions: str | None = None
    #: livekit function tools exposed to the realtime model (see call_tools.py).
    tools: tuple[Any, ...] = ()
    #: Called with (speaker, text) for every finalized conversation turn.
    on_conversation_turn: Callable[[str, str], None] | None = None
    #: Called with the executed tool names after each tool round.
    on_tools_executed: Callable[[list[str]], None] | None = None


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
        self._register_session_listeners(session, options)
        agent = Agent(instructions=options.instructions, tools=list(options.tools))
        await session.start(agent, room=self._room)
        if options.greeting_instructions:
            session.generate_reply(instructions=options.greeting_instructions)

    def _register_session_listeners(self, session: AgentSession, options: VoiceAgentOptions) -> None:
        from livekit.agents.llm import ChatMessage  # noqa: PLC0415

        on_turn = options.on_conversation_turn
        if on_turn is not None:

            def _on_item_added(event: ConversationItemAddedEvent) -> None:
                item = event.item
                if not isinstance(item, ChatMessage):
                    return
                text = item.text_content
                if text:
                    on_turn(str(item.role), text)

            session.on("conversation_item_added", _on_item_added)
        on_tools = options.on_tools_executed
        if on_tools is not None:

            def _on_tools_executed(event: FunctionToolsExecutedEvent) -> None:
                on_tools([call.name for call in event.function_calls])

            session.on("function_tools_executed", _on_tools_executed)

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
