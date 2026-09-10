"""GPT-Live speech delegates authorized work to a normal MindRoom agent."""

from __future__ import annotations

import asyncio
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mindroom.logging_config import get_logger
from mindroom.matrix_rtc.voice_agent import CallVoiceAgentOptions, LiveVoiceAgentOptions, RealtimeVoiceBridge

if TYPE_CHECKING:
    from collections.abc import Iterator

    from livekit.agents import Agent
    from livekit.agents.llm import ChatContext, RealtimeSessionReconnectedEvent
    from livekit.plugins.openai.realtime import GPTLiveDelegation, GPTLiveSession

logger = get_logger(__name__)
_DELEGATION_ERROR = "Voice call error: the agent could not complete the delegated request. Please try again."
_EMPTY_DELEGATION_RESPONSE = "No response is available from the agent for this request."


def _exception_frames(error: BaseException) -> list[str]:
    """Retain stack locations without exception text, source lines, or locals."""
    return [
        f"{Path(frame.filename).name}:{frame.lineno} ({frame.name})"
        for frame in traceback.extract_tb(error.__traceback__)
    ]


def _commentary_chunks(text: str) -> Iterator[str]:
    """Stay below Live's 500-token append limit using a conservative byte cap."""
    start = 0
    size = 0
    for index, character in enumerate(text):
        character_size = len(character.encode("utf-8"))
        if size + character_size > 480:
            yield text[start:index]
            start = index
            size = 0
        size += character_size
    if start < len(text):
        yield text[start:]


class _LiveDelegationRunner:
    """Own and serialize client delegations, including authorization admission."""

    def __init__(self, options: LiveVoiceAgentOptions, session: GPTLiveSession) -> None:
        self._options = options
        self._session = session
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._delegation_ids: set[str] = set()
        self._message_ids: set[str] = set()
        self._pending_transcript = ""
        self._generation = 0
        self._closed = False

    def submit(self, delegation: GPTLiveDelegation, context: ChatContext) -> None:
        """Snapshot new speech synchronously before the SDK finalizes pending text."""
        from livekit.agents.llm import ChatMessage  # noqa: PLC0415

        if self._closed or delegation.id in self._delegation_ids:
            return
        self._delegation_ids.add(delegation.id)
        messages = tuple(
            (item.id, item.role, item.text_content)
            for item in context.items
            if isinstance(item, ChatMessage) and item.role in {"user", "assistant"} and item.text_content
        )
        task = asyncio.create_task(self._answer(delegation, messages, self._generation))
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _prompt(self, messages: tuple[tuple[str, str, str], ...], pending: str) -> tuple[str, str]:
        """Compute a delta against requests already dispatched to the delegate."""
        lines: list[str] = []
        previous_pending = self._pending_transcript
        for identifier, role, content in messages:
            if identifier in self._message_ids:
                continue
            text = content
            if role == "user" and previous_pending and text.startswith(previous_pending):
                text = text[len(previous_pending) :].strip()
                previous_pending = ""
            if text:
                lines.append(f"{role}: {text}")
        new_pending = pending
        if previous_pending and new_pending.startswith(previous_pending):
            new_pending = new_pending[len(previous_pending) :].strip()
        if new_pending and (not lines or lines[-1] != f"user: {new_pending}"):
            lines.append(f"user: {new_pending}")
        prompt = (
            "Handle the current voice request using your normal instructions and tools. "
            "The following is new conversation context since the previous delegation; "
            "earlier context is already in this call's history. Return a concise result for the voice assistant.\n\n"
            + "\n".join(lines)
        )
        return prompt, pending or previous_pending

    async def _answer(
        self,
        delegation: GPTLiveDelegation,
        messages: tuple[tuple[str, str, str], ...],
        generation: int,
    ) -> None:
        async with self._lock:
            if self._closed or generation != self._generation:
                return
            prompt, pending = self._prompt(messages, delegation.pending_transcript.strip())
            # An attempted request can complete tools without returning text,
            # or fail after side effects. Never label it as new context again.
            self._message_ids.update(identifier for identifier, _, _ in messages)
            self._pending_transcript = pending
            try:
                response = await self._options.respond(prompt, self._options.on_tools_executed)
                if self._closed or generation != self._generation:
                    return
                for chunk in _commentary_chunks(response.text or _EMPTY_DELEGATION_RESPONSE):
                    self._session.append_commentary(chunk, delegation_id=delegation.id)
            except Exception as error:
                logger.warning(
                    "call_live_delegation_failed",
                    error_type=type(error).__name__,
                    traceback_frames=_exception_frames(error),
                )
                if self._closed or generation != self._generation:
                    return
                if self._options.on_session_error is not None:
                    self._options.on_session_error(_DELEGATION_ERROR)
                self._session.append_commentary(_DELEGATION_ERROR, delegation_id=delegation.id)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.warning(
                "call_live_delegation_delivery_failed",
                error_type=type(error).__name__,
                traceback_frames=_exception_frames(error),
            )

    def on_reconnected(self) -> None:
        """Invalidate connection-scoped IDs and cancel their unfinished work."""
        self._generation += 1
        self._delegation_ids.clear()
        for task in self._tasks:
            task.cancel()

    def stop(self) -> None:
        """Reject new work immediately when the provider session ends."""
        self._closed = True
        self.on_reconnected()

    async def aclose(self) -> None:
        """Cancel and settle delegation work before releasing the agent cache."""
        self.stop()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


def _build_live_agent(options: LiveVoiceAgentOptions) -> Agent:  # noqa: C901 - SDK class is defined lazily
    """Load the optional SDK only when a Live call starts."""
    from livekit.agents import Agent  # noqa: PLC0415
    from livekit.plugins.openai.realtime import GPTLiveSession  # noqa: PLC0415

    class LiveCallAgent(Agent):
        def __init__(self) -> None:
            super().__init__(instructions=options.instructions, tools=[])
            self._delegations: _LiveDelegationRunner | None = None
            self._live_session: GPTLiveSession | None = None
            self._provider_close_task: asyncio.Task[None] | None = None

        async def on_enter(self) -> None:
            session = self.duplex_session
            if not isinstance(session, GPTLiveSession):
                msg = "GPT-Live requires a GPTLiveSession"
                raise TypeError(msg)
            self._live_session = session
            self._delegations = _LiveDelegationRunner(options, session)
            session.on("delegation_created", self._on_delegation)
            session.on("session_reconnected", self._on_reconnected)
            session.on("openai_server_event_received", self._on_server_event)

        def _on_delegation(self, event: GPTLiveDelegation) -> None:
            if self._delegations is not None:
                self._delegations.submit(event, self.chat_ctx)

        def _on_reconnected(self, _event: RealtimeSessionReconnectedEvent) -> None:
            if self._delegations is not None:
                self._delegations.on_reconnected()

        def _on_server_event(self, event: dict[str, Any]) -> None:
            if event.get("type") != "session.closed" or self._provider_close_task is not None:
                return
            if self._delegations is not None:
                self._delegations.stop()
            if self._live_session is not None:
                # Even with max_retry=0, the SDK reconnects after a graceful
                # server close. Close its queue before that loop resumes so
                # old delegation commentary cannot enter a fresh session.
                self._provider_close_task = asyncio.create_task(self._live_session.aclose())
            if options.on_session_terminated is not None:
                options.on_session_terminated(True)

        async def on_exit(self) -> None:
            if self._live_session is not None:
                self._live_session.off("delegation_created", self._on_delegation)
                self._live_session.off("session_reconnected", self._on_reconnected)
                self._live_session.off("openai_server_event_received", self._on_server_event)
                self._live_session = None
            if self._delegations is not None:
                await self._delegations.aclose()
            if self._provider_close_task is not None:
                await asyncio.gather(self._provider_close_task, return_exceptions=True)

    return LiveCallAgent()


class LiveVoiceBridge(RealtimeVoiceBridge):
    """MatrixRTC media and GPT-Live speech with client-side delegation."""

    def __init__(self, *, local_identity: str, e2ee_enabled: bool) -> None:
        super().__init__(local_identity=local_identity, e2ee_enabled=e2ee_enabled)
        self._live_agent: Agent | None = None

    async def start_agent(self, options: CallVoiceAgentOptions) -> None:
        """Start GPT-Live with the same authorized media path as other calls."""
        from livekit import rtc  # noqa: PLC0415
        from livekit.agents import AgentSession, APIConnectOptions, room_io  # noqa: PLC0415
        from livekit.plugins.openai.realtime import GPTLiveModel  # noqa: PLC0415

        if self._room is None:
            msg = "connect() must succeed before start_agent()"
            raise RuntimeError(msg)
        if not isinstance(options, LiveVoiceAgentOptions):
            msg = "LiveVoiceBridge requires live agent options"
            raise TypeError(msg)
        if options.close_responder is not None:
            self._owned_speech_resource_closers += (options.close_responder,)
        model = GPTLiveModel(
            model=options.model,
            api_key=options.api_key,
            base_url="https://api.openai.com/v1",
            voice=options.voice,
            delegation="client",
            # CallManager retries with a fresh provider queue and the same
            # agent history. SDK retries can replay connection-scoped IDs.
            conn_options=APIConnectOptions(max_retry=0),
        )
        self._owned_speech_resource_closers += (model.aclose,)
        session = AgentSession(llm=model)
        self._live_agent = _build_live_agent(options)
        await self._start_session(session, self._live_agent, options, rtc_module=rtc, room_io_module=room_io)
        if options.greeting_instructions:
            session.generate_reply(instructions=options.greeting_instructions)

    async def aclose(self) -> None:
        """Settle delegates before the parent closes the normal agent cache."""
        agent, self._live_agent = self._live_agent, None
        try:
            if agent is not None:
                await agent.on_exit()
        finally:
            await super().aclose()
