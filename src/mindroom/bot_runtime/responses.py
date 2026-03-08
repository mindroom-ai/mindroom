"""AI response orchestration for agent and team bots."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from mindroom import interactive
from mindroom.media_inputs import MediaInputs
from mindroom.memory.auto_flush import (
    mark_auto_flush_dirty_session,
    reprioritize_auto_flush_sessions,
)
from mindroom.streaming import (
    IN_PROGRESS_MARKER,
    ReplacementStreamingResponse,
    StreamingResponse,
)
from mindroom.teams import TeamMode, select_model_for_team
from mindroom.thread_utils import create_session_id
from mindroom.tool_system.runtime_context import tool_runtime_context

from .types import _DispatchPayload, _merge_response_extra_content

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    import nio

    from mindroom.bot import AgentBot
    from mindroom.matrix.identity import MatrixID
    from mindroom.tool_system.events import ToolTraceEntry


async def _generate_team_response_helper(
    self: AgentBot,
    room_id: str,
    reply_to_event_id: str,
    thread_id: str | None,
    team_agents: list[MatrixID],
    team_mode: str,
    thread_history: list[dict],
    requester_user_id: str,
    existing_event_id: str | None = None,
    *,
    payload: _DispatchPayload,
) -> str | None:
    """Generate a team response shared between preformed teams and TeamBot."""
    assert self.client is not None

    model_name = select_model_for_team(self.agent_name, room_id, self.config)
    room_mode = self.config.get_entity_thread_mode(self.agent_name, room_id=room_id) == "room"

    use_streaming = await self.should_use_streaming(
        self.client,
        room_id,
        requester_user_id=requester_user_id,
        enable_streaming=self.enable_streaming,
    )

    mode = TeamMode.COORDINATE if team_mode == "coordinate" else TeamMode.COLLABORATE

    agent_names = [mid.agent_name(self.config) or mid.username for mid in team_agents]
    include_matrix_prompt_context = any(self._agent_has_matrix_messaging_tool(name) for name in agent_names)
    model_message = self._append_matrix_prompt_context(
        payload.prompt,
        room_id=room_id,
        thread_id=thread_id,
        reply_to_event_id=reply_to_event_id,
        include_context=include_matrix_prompt_context,
    )
    tool_context = self._build_tool_runtime_context(
        room_id=room_id,
        thread_id=thread_id,
        reply_to_event_id=reply_to_event_id,
        user_id=requester_user_id,
        attachment_ids=payload.attachment_ids,
    )
    orchestrator = self.orchestrator
    if orchestrator is None:
        msg = "Orchestrator is not set"
        raise RuntimeError(msg)

    client = self.client

    async def generate_team_response(message_id: str | None) -> None:
        if use_streaming and not existing_event_id:
            async with self.typing_indicator(client, room_id):
                with tool_runtime_context(tool_context):
                    response_stream = self.team_response_stream(
                        agent_ids=team_agents,
                        message=model_message,
                        orchestrator=orchestrator,
                        mode=mode,
                        thread_history=thread_history,
                        model_name=model_name,
                        media=payload.media,
                        show_tool_calls=self.show_tool_calls,
                    )

                    event_id, accumulated = await self.send_streaming_response(
                        client,
                        room_id,
                        reply_to_event_id,
                        thread_id,
                        self.matrix_id.domain,
                        self.config,
                        response_stream,
                        streaming_cls=ReplacementStreamingResponse,
                        header=None,
                        show_tool_calls=self.show_tool_calls,
                        existing_event_id=message_id,
                        room_mode=room_mode,
                    )

            await self._handle_interactive_question(
                event_id,
                accumulated,
                room_id,
                thread_id,
                reply_to_event_id,
                agent_name="team",
            )
        else:
            async with self.typing_indicator(client, room_id):
                with tool_runtime_context(tool_context):
                    response_text = await self.team_response(
                        agent_names=agent_names,
                        mode=mode,
                        message=model_message,
                        orchestrator=orchestrator,
                        thread_history=thread_history,
                        model_name=model_name,
                        media=payload.media,
                    )

            if message_id:
                await self._edit_message(room_id, message_id, response_text, thread_id)
            else:
                event_id = await self._send_response(
                    room_id,
                    reply_to_event_id,
                    response_text,
                    thread_id,
                )
                if event_id:
                    await self._handle_interactive_question(
                        event_id,
                        response_text,
                        room_id,
                        thread_id,
                        reply_to_event_id,
                        agent_name="team",
                    )

    thinking_msg = None if existing_event_id else "🤝 Team Response: Thinking..."
    return await self._run_cancellable_response(
        room_id=room_id,
        reply_to_event_id=reply_to_event_id,
        thread_id=thread_id,
        response_function=generate_team_response,
        thinking_message=thinking_msg,
        existing_event_id=existing_event_id,
        user_id=requester_user_id,
    )


async def _run_cancellable_response(
    self: AgentBot,
    room_id: str,
    reply_to_event_id: str,
    thread_id: str | None,
    response_function: Callable[[str | None], Coroutine[object, object, None]],
    thinking_message: str | None = None,
    existing_event_id: str | None = None,
    user_id: str | None = None,
) -> str | None:
    """Run a response generation function with cancellation support."""
    assert self.client is not None
    assert not (thinking_message and existing_event_id), "thinking_message and existing_event_id are mutually exclusive"

    initial_message_id = None
    if thinking_message:
        initial_message_id = await self._send_response(
            room_id,
            reply_to_event_id,
            f"{thinking_message} {IN_PROGRESS_MARKER}",
            thread_id,
        )

    message_id = existing_event_id or initial_message_id
    task: asyncio.Task[None] = asyncio.create_task(response_function(message_id))

    message_to_track = existing_event_id or initial_message_id
    show_stop_button = False

    if message_to_track:
        self.stop_manager.set_current(message_to_track, room_id, task, None)

        show_stop_button = self.config.defaults.show_stop_button
        if show_stop_button and user_id:
            user_is_online = await self.is_user_online(self.client, user_id)
            show_stop_button = user_is_online
            self.logger.info(
                "Stop button decision",
                message_id=message_to_track,
                user_online=user_is_online,
                show_button=show_stop_button,
            )

        if show_stop_button:
            self.logger.info("Adding stop button", message_id=message_to_track)
            await self.stop_manager.add_stop_button(self.client, room_id, message_to_track)

    try:
        await task
    except asyncio.CancelledError:
        self.logger.info("Response cancelled by user", message_id=message_to_track)
    except Exception as exc:
        self.logger.exception("Error during response generation", error=str(exc))
        raise
    finally:
        if message_to_track:
            tracked = self.stop_manager.tracked_messages.get(message_to_track)
            button_already_removed = tracked is None or tracked.reaction_event_id is None

            self.stop_manager.clear_message(
                message_to_track,
                client=self.client,
                remove_button=show_stop_button and not button_already_removed,
            )

    return initial_message_id


async def _process_and_respond(
    self: AgentBot,
    room_id: str,
    prompt: str,
    reply_to_event_id: str,
    thread_id: str | None,
    thread_history: list[dict],
    existing_event_id: str | None = None,
    user_id: str | None = None,
    media: MediaInputs | None = None,
    attachment_ids: list[str] | None = None,
) -> str | None:
    """Process a message and send a response without streaming."""
    assert self.client is not None
    if not prompt.strip():
        return None

    media_inputs = media or MediaInputs()
    session_id = create_session_id(room_id, thread_id)
    knowledge = self._knowledge_for_agent(self.agent_name)
    model_prompt = self._append_matrix_prompt_context(
        prompt,
        room_id=room_id,
        thread_id=thread_id,
        reply_to_event_id=reply_to_event_id,
        include_context=self._agent_has_matrix_messaging_tool(self.agent_name),
    )
    tool_context = self._build_tool_runtime_context(
        room_id=room_id,
        thread_id=thread_id,
        reply_to_event_id=reply_to_event_id,
        user_id=user_id,
        attachment_ids=attachment_ids,
    )
    tool_trace: list[ToolTraceEntry] = []
    run_metadata_content: dict[str, Any] = {}

    try:
        async with self.typing_indicator(self.client, room_id):
            with tool_runtime_context(tool_context):
                response_text = await self.ai_response(
                    agent_name=self.agent_name,
                    prompt=model_prompt,
                    session_id=session_id,
                    storage_path=self.storage_path,
                    config=self.config,
                    thread_history=thread_history,
                    room_id=room_id,
                    knowledge=knowledge,
                    user_id=user_id,
                    media=media_inputs,
                    reply_to_event_id=reply_to_event_id,
                    show_tool_calls=self.show_tool_calls,
                    tool_trace_collector=tool_trace,
                    run_metadata_collector=run_metadata_content,
                )
    except asyncio.CancelledError:
        self.logger.info("Non-streaming response cancelled by user", message_id=existing_event_id)
        if existing_event_id:
            cancelled_text = "**[Response cancelled by user]**"
            await self._edit_message(room_id, existing_event_id, cancelled_text, thread_id)
        raise

    response_extra_content = _merge_response_extra_content(run_metadata_content, attachment_ids)
    if existing_event_id:
        await self._edit_message(
            room_id,
            existing_event_id,
            response_text,
            thread_id,
            tool_trace=tool_trace if self.show_tool_calls else None,
            extra_content=response_extra_content,
        )
        return existing_event_id

    response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)
    event_id = await self._send_response(
        room_id,
        reply_to_event_id,
        response.formatted_text,
        thread_id,
        tool_trace=tool_trace if self.show_tool_calls else None,
        extra_content=response_extra_content,
    )
    if event_id and response.option_map and response.options_list:
        thread_root_for_registration = self._resolve_reply_thread_id(
            thread_id,
            reply_to_event_id,
            room_id=room_id,
        )
        interactive.register_interactive_question(
            event_id,
            room_id,
            thread_root_for_registration,
            response.option_map,
            self.agent_name,
        )
        await interactive.add_reaction_buttons(self.client, room_id, event_id, response.options_list)

    return event_id


async def _send_skill_command_response(
    self: AgentBot,
    *,
    room_id: str,
    reply_to_event_id: str,
    thread_id: str | None,
    thread_history: list[dict],
    prompt: str,
    agent_name: str,
    user_id: str | None,
    reply_to_event: nio.RoomMessageText | None = None,
) -> str | None:
    """Send a skill command response using a specific agent."""
    assert self.client is not None
    if not prompt.strip():
        return None

    session_id = create_session_id(room_id, thread_id)
    reprioritize_auto_flush_sessions(
        self.storage_path,
        self.config,
        agent_name=agent_name,
        active_session_id=session_id,
    )
    knowledge = self._knowledge_for_agent(agent_name)
    model_prompt = self._append_matrix_prompt_context(
        prompt,
        room_id=room_id,
        thread_id=thread_id,
        reply_to_event_id=reply_to_event_id,
        include_context=self._agent_has_matrix_messaging_tool(agent_name),
    )
    tool_context = self._build_tool_runtime_context(
        room_id=room_id,
        thread_id=thread_id,
        reply_to_event_id=reply_to_event_id,
        user_id=user_id,
        agent_name=agent_name,
    )
    show_tool_calls = self._show_tool_calls_for_agent(agent_name)
    tool_trace: list[ToolTraceEntry] = []
    run_metadata_content: dict[str, Any] = {}

    async with self.typing_indicator(self.client, room_id):
        with tool_runtime_context(tool_context):
            response_text = await self.ai_response(
                agent_name=agent_name,
                prompt=model_prompt,
                session_id=session_id,
                storage_path=self.storage_path,
                config=self.config,
                thread_history=thread_history,
                room_id=room_id,
                knowledge=knowledge,
                reply_to_event_id=reply_to_event_id,
                show_tool_calls=show_tool_calls,
                tool_trace_collector=tool_trace,
                run_metadata_collector=run_metadata_content,
            )

    response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)
    event_id = await self._send_response(
        room_id,
        reply_to_event_id,
        response.formatted_text,
        thread_id,
        reply_to_event=reply_to_event,
        skip_mentions=True,
        tool_trace=tool_trace if show_tool_calls else None,
        extra_content=run_metadata_content or None,
    )

    if event_id and response.option_map and response.options_list:
        thread_root_for_registration = self._resolve_reply_thread_id(
            thread_id,
            reply_to_event_id,
            room_id=room_id,
        )
        interactive.register_interactive_question(
            event_id,
            room_id,
            thread_root_for_registration,
            response.option_map,
            agent_name,
        )
        await interactive.add_reaction_buttons(
            self.client,
            room_id,
            event_id,
            response.options_list,
        )

    try:
        mark_auto_flush_dirty_session(
            self.storage_path,
            self.config,
            agent_name=agent_name,
            session_id=session_id,
            room_id=room_id,
            thread_id=thread_id,
        )
        if self.config.get_agent_memory_backend(agent_name) == "mem0":
            self.create_background_task(
                self.store_conversation_memory(
                    prompt,
                    agent_name,
                    self.storage_path,
                    session_id,
                    self.config,
                    room_id,
                    thread_history,
                    user_id,
                ),
                name=f"memory_save_{agent_name}_{session_id}",
            )
    except Exception:  # pragma: no cover
        self.logger.debug("Skipping memory storage due to configuration error")

    return event_id


async def _handle_interactive_question(
    self: AgentBot,
    event_id: str | None,
    content: str,
    room_id: str,
    thread_id: str | None,
    reply_to_event_id: str,
    agent_name: str | None = None,
) -> None:
    """Handle interactive question registration and reactions if present."""
    if not event_id or not self.client:
        return

    if interactive.should_create_interactive_question(content):
        response = interactive.parse_and_format_interactive(content, extract_mapping=True)
        if response.option_map and response.options_list:
            thread_root_for_registration = self._resolve_reply_thread_id(
                thread_id,
                reply_to_event_id,
                room_id=room_id,
            )
            interactive.register_interactive_question(
                event_id,
                room_id,
                thread_root_for_registration,
                response.option_map,
                agent_name or self.agent_name,
            )
            await interactive.add_reaction_buttons(
                self.client,
                room_id,
                event_id,
                response.options_list,
            )


async def _process_and_respond_streaming(
    self: AgentBot,
    room_id: str,
    prompt: str,
    reply_to_event_id: str,
    thread_id: str | None,
    thread_history: list[dict],
    existing_event_id: str | None = None,
    user_id: str | None = None,
    media: MediaInputs | None = None,
    attachment_ids: list[str] | None = None,
) -> str | None:
    """Process a message and send a response with streaming."""
    assert self.client is not None
    if not prompt.strip():
        return None

    media_inputs = media or MediaInputs()
    session_id = create_session_id(room_id, thread_id)
    knowledge = self._knowledge_for_agent(self.agent_name)
    room_mode = self.config.get_entity_thread_mode(self.agent_name, room_id=room_id) == "room"
    model_prompt = self._append_matrix_prompt_context(
        prompt,
        room_id=room_id,
        thread_id=thread_id,
        reply_to_event_id=reply_to_event_id,
        include_context=self._agent_has_matrix_messaging_tool(self.agent_name),
    )
    tool_context = self._build_tool_runtime_context(
        room_id=room_id,
        thread_id=thread_id,
        reply_to_event_id=reply_to_event_id,
        user_id=user_id,
        attachment_ids=attachment_ids,
    )
    run_metadata_content: dict[str, Any] = {}

    try:
        async with self.typing_indicator(self.client, room_id):
            with tool_runtime_context(tool_context):
                response_stream = self.stream_agent_response(
                    agent_name=self.agent_name,
                    prompt=model_prompt,
                    session_id=session_id,
                    storage_path=self.storage_path,
                    config=self.config,
                    thread_history=thread_history,
                    room_id=room_id,
                    knowledge=knowledge,
                    user_id=user_id,
                    media=media_inputs,
                    reply_to_event_id=reply_to_event_id,
                    show_tool_calls=self.show_tool_calls,
                    run_metadata_collector=run_metadata_content,
                )
                response_extra_content = _merge_response_extra_content(run_metadata_content, attachment_ids)

                event_id, accumulated = await self.send_streaming_response(
                    self.client,
                    room_id,
                    reply_to_event_id,
                    thread_id,
                    self.matrix_id.domain,
                    self.config,
                    response_stream,
                    streaming_cls=StreamingResponse,
                    existing_event_id=existing_event_id,
                    room_mode=room_mode,
                    show_tool_calls=self.show_tool_calls,
                    extra_content=response_extra_content,
                )

        await self._handle_interactive_question(
            event_id,
            accumulated,
            room_id,
            thread_id,
            reply_to_event_id,
        )
    except asyncio.CancelledError:
        self.logger.info("Streaming cancelled by user", message_id=existing_event_id)
        raise
    except Exception as exc:
        self.logger.exception("Error in streaming response", error=str(exc))
        return None
    return event_id


async def _generate_response(
    self: AgentBot,
    room_id: str,
    prompt: str,
    reply_to_event_id: str,
    thread_id: str | None,
    thread_history: list[dict],
    existing_event_id: str | None = None,
    user_id: str | None = None,
    media: MediaInputs | None = None,
    attachment_ids: list[str] | None = None,
) -> str | None:
    """Generate and send or edit a response using AI."""
    assert self.client is not None
    media_inputs = media or MediaInputs()

    session_id = create_session_id(room_id, thread_id)
    reprioritize_auto_flush_sessions(
        self.storage_path,
        self.config,
        agent_name=self.agent_name,
        active_session_id=session_id,
    )

    use_streaming = await self.should_use_streaming(
        self.client,
        room_id,
        requester_user_id=user_id,
        enable_streaming=self.enable_streaming,
    )

    async def generate(message_id: str | None) -> None:
        if use_streaming:
            await self._process_and_respond_streaming(
                room_id,
                prompt,
                reply_to_event_id,
                thread_id,
                thread_history,
                message_id,
                user_id=user_id,
                media=media_inputs,
                attachment_ids=attachment_ids,
            )
        else:
            await self._process_and_respond(
                room_id,
                prompt,
                reply_to_event_id,
                thread_id,
                thread_history,
                message_id,
                user_id=user_id,
                media=media_inputs,
                attachment_ids=attachment_ids,
            )

    thinking_msg = None if existing_event_id else "Thinking..."
    event_id = await self._run_cancellable_response(
        room_id=room_id,
        reply_to_event_id=reply_to_event_id,
        thread_id=thread_id,
        response_function=generate,
        thinking_message=thinking_msg,
        existing_event_id=existing_event_id,
        user_id=user_id,
    )

    try:
        mark_auto_flush_dirty_session(
            self.storage_path,
            self.config,
            agent_name=self.agent_name,
            session_id=session_id,
            room_id=room_id,
            thread_id=thread_id,
        )
        if self.config.get_agent_memory_backend(self.agent_name) == "mem0":
            self.create_background_task(
                self.store_conversation_memory(
                    prompt,
                    self.agent_name,
                    self.storage_path,
                    session_id,
                    self.config,
                    room_id,
                    thread_history,
                    user_id,
                ),
                name=f"memory_save_{self.agent_name}_{session_id}",
            )
    except Exception:
        self.logger.exception(
            "Failed to queue memory persistence after response",
            agent_name=self.agent_name,
            session_id=session_id,
            room_id=room_id,
            thread_id=thread_id,
        )

    return event_id
