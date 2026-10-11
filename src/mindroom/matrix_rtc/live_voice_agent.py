"""GPT-Live speech delegates authorized work to a normal MindRoom agent."""

from __future__ import annotations

import asyncio
import sys
import time
import traceback
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mindroom.background_tasks import wait_for_future_until_complete
from mindroom.logging_config import get_logger
from mindroom.matrix_rtc.call_audio_capture import CallAudioCapture, tap_published_audio
from mindroom.matrix_rtc.call_telemetry import LiveCallTelemetry
from mindroom.matrix_rtc.voice_agent import (
    CallVoiceAgentOptions,
    LiveVoiceAgentOptions,
    LiveVoiceUsage,
    RealtimeVoiceBridge,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from livekit.agents import Agent
    from livekit.agents.llm import ChatContext, RealtimeSessionReconnectedEvent
    from livekit.agents.voice.events import ConversationItemAddedEvent
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

    def __init__(self, options: LiveVoiceAgentOptions, session: GPTLiveSession, telemetry: LiveCallTelemetry) -> None:
        self._options = options
        self._session = session
        self._telemetry = telemetry
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
        self._telemetry.delegation_created(delegation.id)
        messages = tuple(
            (item.id, item.role, item.text_content)
            for item in context.items
            if isinstance(item, ChatMessage) and item.role in {"user", "assistant"} and item.text_content
        )
        task = asyncio.create_task(self._answer(delegation, messages, self._generation))
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        self._telemetry.delegation_enqueued(delegation.id)

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
            self._telemetry.delegation_lock_acquired(delegation.id)
            if self._closed or generation != self._generation:
                return
            prompt, pending = self._prompt(messages, delegation.pending_transcript.strip())
            # An attempted request can complete tools without returning text,
            # or fail after side effects. Never label it as new context again.
            self._message_ids.update(identifier for identifier, _, _ in messages)
            self._pending_transcript = pending
            try:
                response = await self._options.respond(prompt, self._options.on_tools_executed)
                self._telemetry.delegation_response_done(delegation.id, len(response.text))
                if self._closed or generation != self._generation:
                    return
                self._append_commentary(
                    delegation.id,
                    list(_commentary_chunks(response.text or _EMPTY_DELEGATION_RESPONSE)),
                )
            except Exception as error:
                logger.warning(
                    "call_live_delegation_failed",
                    delegation_id=delegation.id,
                    error_type=type(error).__name__,
                    traceback_frames=_exception_frames(error),
                )
                if self._closed or generation != self._generation:
                    return
                if self._options.on_session_error is not None:
                    self._options.on_session_error(_DELEGATION_ERROR)
                self._append_commentary(delegation.id, [_DELEGATION_ERROR])

    def _append_commentary(self, delegation_id: str, chunks: list[str]) -> None:
        for index, chunk in enumerate(chunks):
            self._session.append_commentary(chunk, delegation_id=delegation_id)
            self._telemetry.commentary_appended(delegation_id, chunk_index=index, chunk_count=len(chunks))

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


def _build_live_agent(  # noqa: C901 - SDK class is defined lazily
    options: LiveVoiceAgentOptions,
    instructions: str,
    telemetry: LiveCallTelemetry,
) -> Agent:
    """Load the optional SDK only when a Live call starts."""
    from livekit.agents import Agent  # noqa: PLC0415
    from livekit.plugins.openai.realtime import GPTLiveSession  # noqa: PLC0415

    class LiveCallAgent(Agent):
        def __init__(self) -> None:
            super().__init__(instructions=instructions, tools=[])
            self._delegations: _LiveDelegationRunner | None = None
            self._live_session: GPTLiveSession | None = None
            self._provider_close_task: asyncio.Task[None] | None = None
            self._exiting = False
            self._exit_task: asyncio.Task[None] | None = None
            self._voice_started_at = time.time()
            self._voice_usage: dict[str, LiveVoiceUsage] = {}
            self._failed_usage: dict[str, LiveVoiceUsage] = {}
            self._usage_tasks: set[asyncio.Task[None]] = set()
            self._usage_lock = asyncio.Lock()

        async def on_enter(self) -> None:
            session = self.duplex_session
            if not isinstance(session, GPTLiveSession):
                msg = "GPT-Live requires a GPTLiveSession"
                raise TypeError(msg)
            self._live_session = session
            self._delegations = _LiveDelegationRunner(options, session, telemetry)
            session.on("delegation_created", self._on_delegation)
            session.on("session_reconnected", self._on_reconnected)
            session.on("openai_server_event_received", self._on_server_event)
            # A separate listener: the emitter isolates telemetry failures from call handling.
            session.on("openai_server_event_received", telemetry.on_server_event)
            session.on("openai_client_event_queued", telemetry.on_client_event)
            session.on("input_speech_stopped", telemetry.on_input_speech_stopped)

        def _on_delegation(self, event: GPTLiveDelegation) -> None:
            if self._delegations is not None:
                self._delegations.submit(event, self.chat_ctx)

        def _on_reconnected(self, _event: RealtimeSessionReconnectedEvent) -> None:
            if self._delegations is not None:
                self._delegations.on_reconnected()

        def _on_server_event(self, event: dict[str, Any]) -> None:
            if event.get("type") == "session.started":
                self._voice_started_at = time.time()
            if event.get("type") in {"session.usage.updated", "session.closed"}:
                self._capture_usage(event)
            if event.get("type") != "session.closed" or self._provider_close_task is not None or self._exiting:
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

        def _capture_usage(self, event: dict[str, Any]) -> None:
            session_id = self._live_session.session_id if self._live_session is not None else None
            raw_usage = event.get("usage")
            seconds = raw_usage.get("seconds") if isinstance(raw_usage, dict) else None
            if options.record_usage is None or not session_id:
                return
            if (
                isinstance(seconds, bool)
                or not isinstance(seconds, (int, float))
                or not 0 <= seconds <= sys.float_info.max
            ):
                logger.warning("call_live_usage_invalid")
                return
            previous = self._voice_usage.get(session_id)
            usage = LiveVoiceUsage(
                provider_session_id=session_id,
                model=options.model,
                created_at=previous.created_at if previous is not None else self._voice_started_at,
                duration_seconds=max(float(seconds), previous.duration_seconds if previous is not None else 0.0),
                finalized=event["type"] == "session.closed" or (previous is not None and previous.finalized),
            )
            self._voice_usage[session_id] = usage
            task = asyncio.create_task(self._record_usage(usage))
            self._usage_tasks.add(task)
            task.add_done_callback(self._usage_tasks.discard)

        async def _record_usage(self, usage: LiveVoiceUsage) -> None:
            if options.record_usage is not None:
                async with self._usage_lock:
                    try:
                        await options.record_usage(usage)
                    except Exception as error:
                        self._failed_usage[usage.provider_session_id] = usage
                        logger.warning("call_live_usage_save_failed", error_type=type(error).__name__)
                    else:
                        self._failed_usage.pop(usage.provider_session_id, None)

        async def on_exit(self) -> None:
            self._exiting = True
            if self._exit_task is None:
                self._exit_task = asyncio.create_task(self._close())
            await wait_for_future_until_complete(self._exit_task)

        async def _close(self) -> None:
            if self._delegations is not None:
                await self._delegations.aclose()
            if self._live_session is not None:
                # Keep usage subscribed while the provider drains its final duration.
                # A disconnected SDK session re-raises its already-reported transport error.
                await asyncio.gather(self._live_session.aclose(), return_exceptions=True)
                self._live_session.off("delegation_created", self._on_delegation)
                self._live_session.off("session_reconnected", self._on_reconnected)
                self._live_session.off("openai_server_event_received", self._on_server_event)
                self._live_session.off("openai_server_event_received", telemetry.on_server_event)
                self._live_session.off("openai_client_event_queued", telemetry.on_client_event)
                self._live_session.off("input_speech_stopped", telemetry.on_input_speech_stopped)
                self._live_session = None
            if self._provider_close_task is not None:
                await asyncio.gather(self._provider_close_task, return_exceptions=True)
            if self._usage_tasks:
                await asyncio.gather(*self._usage_tasks)
            for usage in tuple(self._failed_usage.values()):
                await self._record_usage(usage)
            if self._failed_usage:
                logger.error("call_live_usage_unpersisted", sessions=len(self._failed_usage))

    return LiveCallAgent()


def _record_assistant_reply(telemetry: LiveCallTelemetry, event: ConversationItemAddedEvent) -> None:
    """Pass each spoken assistant message to reply routing."""
    from livekit.agents.llm import ChatMessage  # noqa: PLC0415

    item = event.item
    if isinstance(item, ChatMessage) and item.role == "assistant" and (text := item.text_content):
        telemetry.assistant_reply(len(text), started_at=item.created_at, interrupted=item.interrupted)


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
        instructions = await options.get_instructions()
        capture = None if options.audio_capture_dir is None else CallAudioCapture(options.audio_capture_dir)
        telemetry = LiveCallTelemetry(capture)
        self._owned_speech_resource_closers += (telemetry.aclose,)
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
        session.on("conversation_item_added", partial(_record_assistant_reply, telemetry))
        self._live_agent = _build_live_agent(options, instructions, telemetry)
        await self._start_session(
            session,
            self._live_agent,
            options,
            rtc_module=rtc,
            room_io_module=room_io,
            on_input_frame=None if capture is None else capture.mic_in,
        )
        if capture is not None:
            tap_published_audio(session, capture)
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
