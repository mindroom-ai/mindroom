"""Multi-agent bot facade where each agent has its own Matrix user account."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Any

from mindroom.matrix import image_handler
from mindroom.matrix.client import (
    _latest_thread_event_id,
    fetch_thread_history,
    get_joined_rooms,
    get_latest_thread_event_id_if_needed,
    join_room,
    send_message,
)
from mindroom.matrix.identity import MatrixID, extract_agent_name
from mindroom.matrix.media import extract_media_caption
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.presence import is_user_online, should_use_streaming
from mindroom.matrix.reply_chain import ReplyChainCaches
from mindroom.matrix.rooms import is_dm_room, leave_non_dm_rooms
from mindroom.matrix.state import MatrixState
from mindroom.matrix.typing import typing_indicator
from mindroom.matrix.users import AgentMatrixUser, create_agent_user
from mindroom.memory import store_conversation_memory
from mindroom.stop import StopManager
from mindroom.thread_utils import (
    check_agent_mentioned,
    create_session_id,
    get_agents_in_thread,
    get_all_mentioned_agents_in_thread,
    has_multiple_non_agent_users_in_thread,
    has_user_responded_after_message,
    should_agent_respond,
)

from . import interactive, voice_handler
from .agents import create_agent, create_session_storage, remove_run_by_event_id
from .ai import ai_response, stream_agent_response
from .attachment_media import resolve_attachment_media
from .attachments import (
    register_file_or_video_attachment,
    register_image_attachment,
    resolve_thread_attachment_ids,
)
from .authorization import (
    filter_agents_by_sender_permissions,
    get_available_agents_for_sender,
    is_authorized_sender,
)
from .background_tasks import create_background_task, wait_for_background_tasks
from .bot_runtime.context import (
    _agent_has_matrix_messaging_tool as _agent_has_matrix_messaging_tool_method,
)
from .bot_runtime.context import (
    _append_matrix_prompt_context as _append_matrix_prompt_context_method,
)
from .bot_runtime.context import (
    _build_tool_runtime_context as _build_tool_runtime_context_method,
)
from .bot_runtime.context import (
    _cached_room as _cached_room_method,
)
from .bot_runtime.context import (
    _can_reply_to_sender as _can_reply_to_sender_method,
)
from .bot_runtime.context import (
    _decide_team_for_sender as _decide_team_for_sender_method,
)
from .bot_runtime.context import (
    _derive_conversation_context as _derive_conversation_context_method,
)
from .bot_runtime.context import (
    _extract_message_context as _extract_message_context_method,
)
from .bot_runtime.context import (
    _precheck_event as _precheck_event_method,
)
from .bot_runtime.context import (
    _prepare_dispatch as _prepare_dispatch_method,
)
from .bot_runtime.context import (
    _requester_user_id_for_event as _requester_user_id_for_event_method,
)
from .bot_runtime.context import (
    _resolve_response_action as _resolve_response_action_method,
)
from .bot_runtime.context import (
    _should_skip_mentions,
)
from .bot_runtime.dispatch import (
    _dispatch_text_message as _dispatch_text_message_method,
)
from .bot_runtime.dispatch import (
    _execute_dispatch_action as _execute_dispatch_action_method,
)
from .bot_runtime.dispatch import (
    _handle_command as _handle_command_method,
)
from .bot_runtime.dispatch import (
    _on_message as _on_message_method,
)
from .bot_runtime.dispatch import (
    _on_reaction as _on_reaction_method,
)
from .bot_runtime.dispatch import (
    _resolve_dispatch_action as _resolve_dispatch_action_method,
)
from .bot_runtime.edits import _handle_message_edit as _handle_message_edit_method
from .bot_runtime.lifecycle import (
    _create_task_wrapper,
)
from .bot_runtime.lifecycle import (
    _on_invite as _on_invite_method,
)
from .bot_runtime.lifecycle import (
    _post_join_room_setup as _post_join_room_setup_method,
)
from .bot_runtime.lifecycle import (
    _send_welcome_message_if_empty as _send_welcome_message_if_empty_method,
)
from .bot_runtime.lifecycle import (
    _set_avatar_if_available as _set_avatar_if_available_method,
)
from .bot_runtime.lifecycle import (
    _set_presence_with_model_info as _set_presence_with_model_info_method,
)
from .bot_runtime.lifecycle import (
    cleanup as cleanup_method,
)
from .bot_runtime.lifecycle import (
    ensure_rooms as ensure_rooms_method,
)
from .bot_runtime.lifecycle import (
    ensure_user_account as ensure_user_account_method,
)
from .bot_runtime.lifecycle import (
    join_configured_rooms as join_configured_rooms_method,
)
from .bot_runtime.lifecycle import (
    leave_unconfigured_rooms as leave_unconfigured_rooms_method,
)
from .bot_runtime.lifecycle import (
    start as start_method,
)
from .bot_runtime.lifecycle import (
    stop as stop_method,
)
from .bot_runtime.lifecycle import (
    sync_forever as sync_forever_method,
)
from .bot_runtime.lifecycle import (
    try_start as try_start_method,
)
from .bot_runtime.media import (
    _build_dispatch_payload_with_attachments as _build_dispatch_payload_with_attachments_method,
)
from .bot_runtime.media import (
    _maybe_send_visible_voice_echo as _maybe_send_visible_voice_echo_method,
)
from .bot_runtime.media import (
    _on_audio_media_message as _on_audio_media_message_method,
)
from .bot_runtime.media import (
    _on_media_message as _on_media_message_method,
)
from .bot_runtime.media import (
    _register_routed_attachment as _register_routed_attachment_method,
)
from .bot_runtime.outbound import (
    _edit_message as _edit_message_method,
)
from .bot_runtime.outbound import (
    _resolve_reply_thread_id as _resolve_reply_thread_id_method,
)
from .bot_runtime.outbound import (
    _send_response as _send_response_method,
)
from .bot_runtime.responses import (
    _generate_response as _generate_response_method,
)
from .bot_runtime.responses import (
    _generate_team_response_helper as _generate_team_response_helper_method,
)
from .bot_runtime.responses import (
    _handle_interactive_question as _handle_interactive_question_method,
)
from .bot_runtime.responses import (
    _process_and_respond as _process_and_respond_method,
)
from .bot_runtime.responses import (
    _process_and_respond_streaming as _process_and_respond_streaming_method,
)
from .bot_runtime.responses import (
    _run_cancellable_response as _run_cancellable_response_method,
)
from .bot_runtime.responses import (
    _send_skill_command_response as _send_skill_command_response_method,
)
from .bot_runtime.router import (
    _handle_ai_routing as _handle_ai_routing_method,
)
from .bot_runtime.router import (
    _handle_router_dispatch as _handle_router_dispatch_method,
)
from .bot_runtime.types import (
    _DispatchPayload,
    _MessageContext,
)
from .commands import config_confirmation
from .constants import MATRIX_HOMESERVER, ROUTER_AGENT_NAME
from .knowledge.utils import MultiKnowledgeVectorDb, resolve_agent_knowledge
from .logging_config import emoji, get_logger
from .matrix.rooms import resolve_room_aliases
from .matrix.users import login_agent_user
from .media_inputs import MediaInputs
from .response_tracker import ResponseTracker
from .routing import suggest_agent_for_message
from .scheduling import cancel_all_running_scheduled_tasks, restore_scheduled_tasks
from .streaming import send_streaming_response
from .teams import decide_team_formation, team_response, team_response_stream

if TYPE_CHECKING:
    from pathlib import Path

    import nio
    import structlog
    from agno.agent import Agent
    from agno.knowledge.knowledge import Knowledge

    from mindroom.config.main import Config
    from mindroom.orchestrator import MultiAgentOrchestrator

logger = get_logger(__name__)
type _BotCollaborator = Any

__all__ = [
    "AgentBot",
    "MultiKnowledgeVectorDb",
    "TeamBot",
    "_DispatchPayload",
    "_MessageContext",
    "_create_task_wrapper",
    "_should_skip_mentions",
    "interactive",
    "voice_handler",
]


def create_bot_for_entity(
    entity_name: str,
    agent_user: AgentMatrixUser,
    config: Config,
    storage_path: Path,
) -> AgentBot | TeamBot:
    """Create the appropriate bot instance for an entity."""
    enable_streaming = config.defaults.enable_streaming

    if entity_name == ROUTER_AGENT_NAME:
        all_room_aliases = config.get_all_configured_rooms()
        rooms = resolve_room_aliases(list(all_room_aliases))
        return AgentBot(agent_user, storage_path, config, rooms, enable_streaming=enable_streaming)

    if entity_name in config.teams:
        team_config = config.teams[entity_name]
        rooms = resolve_room_aliases(team_config.rooms)
        team_matrix_ids = [MatrixID.from_agent(agent_name, config.domain) for agent_name in team_config.agents]
        return TeamBot(
            agent_user=agent_user,
            storage_path=storage_path,
            config=config,
            rooms=rooms,
            team_agents=team_matrix_ids,
            team_mode=team_config.mode,
            team_model=team_config.model,
            enable_streaming=enable_streaming,
        )

    if entity_name in config.agents:
        agent_config = config.agents[entity_name]
        rooms = resolve_room_aliases(agent_config.rooms)
        return AgentBot(agent_user, storage_path, config, rooms, enable_streaming=enable_streaming)

    msg = f"Entity '{entity_name}' not found in configuration."
    raise ValueError(msg)


@dataclass
class AgentBot:
    """Represents a single agent bot with its own Matrix account."""

    _MATRIX_PROMPT_CONTEXT_MARKER = "[Matrix metadata for tool calls]"

    agent_user: AgentMatrixUser
    storage_path: Path
    config: Config
    rooms: list[str] = field(default_factory=list)

    client: nio.AsyncClient | None = field(default=None, init=False)
    running: bool = field(default=False, init=False)
    enable_streaming: bool = field(default=True)
    orchestrator: MultiAgentOrchestrator | None = field(default=None, init=False)
    _reply_chain: ReplyChainCaches = field(default_factory=ReplyChainCaches, init=False)

    @property
    def agent_name(self) -> str:
        """Get the agent name from the backing user account."""
        return self.agent_user.agent_name

    @cached_property
    def logger(self) -> structlog.stdlib.BoundLogger:
        """Get a logger with agent context bound."""
        return logger.bind(agent=emoji(self.agent_name))

    @cached_property
    def matrix_id(self) -> MatrixID:
        """Get the Matrix ID for this agent bot."""
        return self.agent_user.matrix_id

    @property
    def show_tool_calls(self) -> bool:
        """Whether to show tool call details inline in responses."""
        return self._show_tool_calls_for_agent(self.agent_name)

    def _show_tool_calls_for_agent(self, agent_name: str) -> bool:
        """Resolve tool-call visibility for a specific agent."""
        agent_config = self.config.agents.get(agent_name)
        if agent_config and agent_config.show_tool_calls is not None:
            return agent_config.show_tool_calls
        return self.config.defaults.show_tool_calls

    @property
    def is_authorized_sender(self) -> _BotCollaborator:
        """Return the current authorization predicate."""
        return is_authorized_sender

    @property
    def extract_agent_name(self) -> _BotCollaborator:
        """Return the current Matrix sender-to-agent resolver."""
        return extract_agent_name

    @property
    def fetch_thread_history(self) -> _BotCollaborator:
        """Return the current thread-history fetcher."""
        return fetch_thread_history

    @property
    def check_agent_mentioned(self) -> _BotCollaborator:
        """Return the current mention parser."""
        return check_agent_mentioned

    @property
    def is_dm_room(self) -> _BotCollaborator:
        """Return the current DM-room detector."""
        return is_dm_room

    @property
    def should_agent_respond(self) -> _BotCollaborator:
        """Return the current should-respond predicate."""
        return should_agent_respond

    @property
    def get_agents_in_thread(self) -> _BotCollaborator:
        """Return the current thread agent extractor."""
        return get_agents_in_thread

    @property
    def get_all_mentioned_agents_in_thread(self) -> _BotCollaborator:
        """Return the current mentioned-agent-in-thread extractor."""
        return get_all_mentioned_agents_in_thread

    @property
    def get_available_agents_for_sender(self) -> _BotCollaborator:
        """Return the current sender-visible room agent resolver."""
        return get_available_agents_for_sender

    @property
    def filter_agents_by_sender_permissions(self) -> _BotCollaborator:
        """Return the current sender-visible agent filter."""
        return filter_agents_by_sender_permissions

    @property
    def has_multiple_non_agent_users_in_thread(self) -> _BotCollaborator:
        """Return the current thread participant classifier."""
        return has_multiple_non_agent_users_in_thread

    @property
    def has_user_responded_after_message(self) -> _BotCollaborator:
        """Return the current interactive-question staleness check."""
        return has_user_responded_after_message

    @property
    def should_use_streaming(self) -> _BotCollaborator:
        """Return the current presence-based streaming selector."""
        return should_use_streaming

    @property
    def is_user_online(self) -> _BotCollaborator:
        """Return the current presence lookup helper."""
        return is_user_online

    @property
    def ai_response(self) -> _BotCollaborator:
        """Return the current non-streaming AI runner."""
        return ai_response

    @property
    def stream_agent_response(self) -> _BotCollaborator:
        """Return the current streaming AI runner."""
        return stream_agent_response

    @property
    def send_streaming_response(self) -> _BotCollaborator:
        """Return the current streaming response sender."""
        return send_streaming_response

    @property
    def typing_indicator(self) -> _BotCollaborator:
        """Return the current typing-indicator context manager."""
        return typing_indicator

    @property
    def get_joined_rooms(self) -> _BotCollaborator:
        """Return the current joined-room lookup helper."""
        return get_joined_rooms

    @property
    def join_room(self) -> _BotCollaborator:
        """Return the current room join helper."""
        return join_room

    @property
    def restore_scheduled_tasks(self) -> _BotCollaborator:
        """Return the current scheduled-task restore helper."""
        return restore_scheduled_tasks

    @property
    def cancel_all_running_scheduled_tasks(self) -> _BotCollaborator:
        """Return the current scheduled-task cancellation helper."""
        return cancel_all_running_scheduled_tasks

    @property
    def wait_for_background_tasks(self) -> _BotCollaborator:
        """Return the current background-task drain helper."""
        return wait_for_background_tasks

    @property
    def create_background_task(self) -> _BotCollaborator:
        """Return the current background-task creator."""
        return create_background_task

    @property
    def store_conversation_memory(self) -> _BotCollaborator:
        """Return the current memory persistence helper."""
        return store_conversation_memory

    @property
    def login_agent_user(self) -> _BotCollaborator:
        """Return the current Matrix login helper."""
        return login_agent_user

    @property
    def create_agent_user(self) -> _BotCollaborator:
        """Return the current Matrix account creation helper."""
        return create_agent_user

    @property
    def matrix_homeserver(self) -> str:
        """Return the configured Matrix homeserver endpoint."""
        return MATRIX_HOMESERVER

    @property
    def decide_team_formation(self) -> _BotCollaborator:
        """Return the current team-formation policy function."""
        return decide_team_formation

    @property
    def suggest_agent_for_message(self) -> _BotCollaborator:
        """Return the current router suggestion function."""
        return suggest_agent_for_message

    @property
    def resolve_thread_attachment_ids(self) -> _BotCollaborator:
        """Return the current thread attachment resolver."""
        return resolve_thread_attachment_ids

    @property
    def resolve_attachment_media(self) -> _BotCollaborator:
        """Return the current attachment materializer."""
        return resolve_attachment_media

    @property
    def extract_media_caption(self) -> _BotCollaborator:
        """Return the current media caption extractor."""
        return extract_media_caption

    @property
    def image_handler(self) -> _BotCollaborator:
        """Return the current Matrix image handler module."""
        return image_handler

    @property
    def register_file_or_video_attachment(self) -> _BotCollaborator:
        """Return the current file/video attachment registrar."""
        return register_file_or_video_attachment

    @property
    def register_image_attachment(self) -> _BotCollaborator:
        """Return the current image attachment registrar."""
        return register_image_attachment

    @property
    def create_session_storage(self) -> _BotCollaborator:
        """Return the current Agno session storage factory."""
        return create_session_storage

    @property
    def remove_run_by_event_id(self) -> _BotCollaborator:
        """Return the current stale-run remover."""
        return remove_run_by_event_id

    @property
    def get_latest_thread_event_id_if_needed(self) -> _BotCollaborator:
        """Return the current latest-thread-event resolver for replies."""
        return get_latest_thread_event_id_if_needed

    @property
    def latest_thread_event_id(self) -> _BotCollaborator:
        """Return the current latest-thread-event resolver for edits."""
        return _latest_thread_event_id

    @property
    def send_message(self) -> _BotCollaborator:
        """Return the current Matrix send_message helper."""
        return send_message

    @property
    def format_message_with_mentions(self) -> _BotCollaborator:
        """Return the current Matrix message formatter."""
        return format_message_with_mentions

    @property
    def config_confirmation(self) -> _BotCollaborator:
        """Return the current config-confirmation workflow module."""
        return config_confirmation

    @property
    def leave_non_dm_rooms(self) -> _BotCollaborator:
        """Return the current room-leave helper for non-DM rooms."""
        return leave_non_dm_rooms

    @property
    def matrix_state_cls(self) -> _BotCollaborator:
        """Return the current Matrix state storage class."""
        return MatrixState

    @property
    def team_response(self) -> _BotCollaborator:
        """Return the current non-streaming team response runner."""
        return team_response

    @property
    def team_response_stream(self) -> _BotCollaborator:
        """Return the current streaming team response runner."""
        return team_response_stream

    def _get_shared_knowledge(self, base_id: str) -> Knowledge | None:
        """Get shared knowledge instance for a configured knowledge base."""
        orchestrator = self.orchestrator
        if orchestrator is None:
            return None
        manager = orchestrator.knowledge_managers.get(base_id)
        if manager is None:
            return None
        return manager.get_knowledge()

    def _knowledge_for_agent(self, agent_name: str) -> Knowledge | None:
        """Return shared knowledge for agents assigned to one or more knowledge bases."""
        return resolve_agent_knowledge(
            agent_name,
            self.config,
            self._get_shared_knowledge,
            on_missing_bases=lambda missing_base_ids: self.logger.warning(
                "Knowledge bases not available for agent",
                agent_name=agent_name,
                knowledge_bases=missing_base_ids,
            ),
        )

    @property
    def agent(self) -> Agent:
        """Get the Agno Agent instance for this bot."""
        knowledge = self._knowledge_for_agent(self.agent_name)
        return create_agent(
            agent_name=self.agent_name,
            config=self.config,
            storage_path=self.storage_path,
            knowledge=knowledge,
        )

    @cached_property
    def response_tracker(self) -> ResponseTracker:
        """Get or create the response tracker for this agent."""
        tracking_dir = self.storage_path / "tracking"
        return ResponseTracker(self.agent_name, base_path=tracking_dir)

    @cached_property
    def stop_manager(self) -> StopManager:
        """Get or create the StopManager for this agent."""
        return StopManager()

    _resolve_reply_thread_id = _resolve_reply_thread_id_method
    _send_response = _send_response_method
    _edit_message = _edit_message_method

    join_configured_rooms = join_configured_rooms_method
    _post_join_room_setup = _post_join_room_setup_method
    leave_unconfigured_rooms = leave_unconfigured_rooms_method
    ensure_user_account = ensure_user_account_method
    _set_avatar_if_available = _set_avatar_if_available_method
    _set_presence_with_model_info = _set_presence_with_model_info_method
    ensure_rooms = ensure_rooms_method
    start = start_method
    try_start = try_start_method
    cleanup = cleanup_method
    stop = stop_method
    _send_welcome_message_if_empty = _send_welcome_message_if_empty_method
    sync_forever = sync_forever_method
    _on_invite = _on_invite_method

    _derive_conversation_context = _derive_conversation_context_method
    _requester_user_id_for_event = _requester_user_id_for_event_method
    _precheck_event = _precheck_event_method
    _prepare_dispatch = _prepare_dispatch_method
    _resolve_response_action = _resolve_response_action_method
    _decide_team_for_sender = _decide_team_for_sender_method
    _extract_message_context = _extract_message_context_method
    _cached_room = _cached_room_method
    _build_tool_runtime_context = _build_tool_runtime_context_method
    _agent_has_matrix_messaging_tool = _agent_has_matrix_messaging_tool_method
    _append_matrix_prompt_context = _append_matrix_prompt_context_method
    _can_reply_to_sender = _can_reply_to_sender_method

    _dispatch_text_message = _dispatch_text_message_method
    _on_message = _on_message_method
    _on_reaction = _on_reaction_method
    _resolve_dispatch_action = _resolve_dispatch_action_method
    _execute_dispatch_action = _execute_dispatch_action_method
    _handle_command = _handle_command_method

    _build_dispatch_payload_with_attachments = _build_dispatch_payload_with_attachments_method
    _on_audio_media_message = _on_audio_media_message_method
    _maybe_send_visible_voice_echo = _maybe_send_visible_voice_echo_method
    _on_media_message = _on_media_message_method
    _register_routed_attachment = _register_routed_attachment_method

    _handle_router_dispatch = _handle_router_dispatch_method
    _handle_ai_routing = _handle_ai_routing_method

    _generate_team_response_helper = _generate_team_response_helper_method
    _run_cancellable_response = _run_cancellable_response_method
    _process_and_respond = _process_and_respond_method
    _send_skill_command_response = _send_skill_command_response_method
    _handle_interactive_question = _handle_interactive_question_method
    _process_and_respond_streaming = _process_and_respond_streaming_method
    _generate_response = _generate_response_method

    _handle_message_edit = _handle_message_edit_method


@dataclass
class TeamBot(AgentBot):
    """A bot that represents a team of agents working together."""

    team_agents: list[MatrixID] = field(default_factory=list)
    team_mode: str = field(default="coordinate")
    team_model: str | None = field(default=None)

    @cached_property
    def agent(self) -> Agent | None:
        """Teams don't have individual agents, return None."""
        return None

    async def _generate_response(
        self,
        room_id: str,
        prompt: str,
        reply_to_event_id: str,
        thread_id: str | None,
        thread_history: list[dict],
        existing_event_id: str | None = None,
        user_id: str | None = None,
        media: MediaInputs | None = None,
        attachment_ids: list[str] | None = None,
    ) -> None:
        """Generate a team response instead of an individual agent response."""
        if not prompt.strip():
            return

        assert self.client is not None

        session_id = create_session_id(room_id, thread_id)
        agent_names = [mid.agent_name(self.config) or mid.username for mid in self.team_agents]
        create_background_task(
            store_conversation_memory(
                prompt,
                agent_names,
                self.storage_path,
                session_id,
                self.config,
                room_id,
                thread_history,
                user_id,
            ),
            name=f"memory_save_team_{session_id}",
        )
        self.logger.info(f"Storing memory for team: {agent_names}")

        media_inputs = media or MediaInputs()

        await self._generate_team_response_helper(
            room_id=room_id,
            reply_to_event_id=reply_to_event_id,
            thread_id=thread_id,
            payload=_DispatchPayload(
                prompt=prompt,
                media=media_inputs,
                attachment_ids=attachment_ids,
            ),
            team_agents=self.team_agents,
            team_mode=self.team_mode,
            thread_history=thread_history,
            requester_user_id=user_id or "",
            existing_event_id=existing_event_id,
        )
