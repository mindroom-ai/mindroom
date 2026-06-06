"""Command handling helpers extracted from bot dispatch logic."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from mindroom.authorization import responder_candidate_entities_for_room
from mindroom.commands import config_confirmation
from mindroom.commands.config_commands import handle_config_command
from mindroom.commands.parsing import Command, CommandType, get_command_help, get_compact_command_entries
from mindroom.entity_resolution import configured_routable_entity_ids_for_room, entity_identity_registry
from mindroom.handled_turns import HandledTurnState
from mindroom.logging_config import get_logger
from mindroom.scheduling import (
    SchedulingRuntime,
    cancel_all_scheduled_tasks,
    cancel_scheduled_task,
    edit_scheduled_task,
    list_scheduled_tasks,
    schedule_task,
)
from mindroom.thread_utils import check_agent_mentioned

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    import nio
    import structlog

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import HookMatrixAdmin
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol, ConversationEventCache
    from mindroom.matrix.identity import MatrixID
    from mindroom.message_target import MessageTarget
    from mindroom.tool_system.plugins import PluginReloadResult

logger = get_logger(__name__)


def _scheduling_runtime(context: CommandHandlerContext, room: nio.MatrixRoom) -> SchedulingRuntime:
    """Collapse active scheduling collaborators into one explicit live runtime object."""
    return SchedulingRuntime(
        client=context.client,
        config=context.config,
        runtime_paths=context.runtime_paths,
        room=room,
        conversation_cache=context.conversation_cache,
        event_cache=context.event_cache,
        matrix_admin=context.matrix_admin,
    )


class _CommandEvent(Protocol):
    """Minimal canonical text-event shape required by command handling."""

    sender: str
    event_id: str
    body: str
    source: dict[str, Any]


class _CommandResponseSender(Protocol):
    """Send one command response to the stable command target."""

    def __call__(
        self,
        response_text: str,
        *,
        skip_mentions: bool = False,
    ) -> Awaitable[str | None]:
        """Send a command response."""


@dataclass(frozen=True)
class CommandHandlerContext:
    """Dependencies required by command handling."""

    client: nio.AsyncClient
    config: Config
    runtime_paths: RuntimePaths
    logger: structlog.stdlib.BoundLogger
    conversation_cache: ConversationCacheProtocol
    event_cache: ConversationEventCache
    stable_target: MessageTarget
    record_handled_turn: Callable[[HandledTurnState], None]
    send_response: _CommandResponseSender
    reload_plugins: Callable[[], Awaitable[PluginReloadResult]] | None = None
    matrix_admin: HookMatrixAdmin | None = None
    responder_candidates_for_room: Callable[[nio.MatrixRoom, str], Awaitable[list[MatrixID]]] | None = None


def _format_agent_description(agent_name: str, config: Config) -> str:
    """Format a concise agent description for the welcome message."""
    if agent_name in config.agents:
        agent_config = config.agents[agent_name]
        tool_names = config.get_agent_available_tools(agent_name)
        desc_parts = []

        # Add role first
        if agent_config.role:
            desc_parts.append(agent_config.role)

        # Add tools with better formatting
        if tool_names:
            # Wrap each tool name in backticks
            formatted_tools = [f"`{tool}`" for tool in tool_names[:3]]
            tools_str = ", ".join(formatted_tools)
            if len(tool_names) > 3:
                tools_str += f" +{len(tool_names) - 3} more"
            desc_parts.append(f"(🔧 {tools_str})")

        return " ".join(desc_parts) if desc_parts else ""

    if agent_name in config.teams:
        team_config = config.teams[agent_name]
        agent_count = len(team_config.agents)
        noun = "agent" if agent_count == 1 else "agents"
        team_desc = f"Team of {agent_count} {noun}"
        if team_config.role:
            return f"{team_config.role} ({team_desc})"
        return team_desc

    return ""


def _format_welcome_message(
    candidate_entities: Iterable[MatrixID],
    config: Config,
    runtime_paths: RuntimePaths,
) -> str:
    """Generate the welcome message text for resolved responder candidates."""
    entity_list = []
    candidate_entity_ids = list(candidate_entities)
    registry = entity_identity_registry(config, runtime_paths) if candidate_entity_ids else None
    for entity_id in candidate_entity_ids:
        assert registry is not None
        entity_name = registry.current_entity_name_for_user_id(entity_id.full_id, include_router=False)
        if entity_name is None:
            continue
        description = _format_agent_description(entity_name, config)
        entity_entry = f"• **@{entity_name}**"
        if description:
            entity_entry += f": {description}"
        entity_list.append(entity_entry)

    welcome_msg = (
        "🎉 **Welcome to MindRoom!**\n\n"
        "I'm your routing assistant, here to help coordinate our team of specialized AI agents. 🤖\n\n"
    )

    if entity_list:
        welcome_msg += "🧠 **Available agents and teams in this room:**\n"
        welcome_msg += "\n".join(entity_list)
        welcome_msg += "\n\n"

    quick_commands = "\n".join(get_compact_command_entries(format_code=True))
    welcome_msg += (
        "💬 **How to interact:**\n"
        "• Mention an agent or team with @ to get their attention using its configured alias\n"
        "• Use `!help` to see available commands\n"
        "• Agents stay in existing Matrix threads, including compatible plain replies from bridges and non-thread clients\n"
        "• Multiple agents can collaborate when you mention them together; mention a team directly for its team workflow\n"
        "• 🎤 Voice messages are automatically transcribed and work perfectly!\n\n"
        "⚡ **Quick commands:**\n"
        f"{quick_commands}\n\n"
        "✨ Feel free to ask any agent or team for help or start a conversation!"
    )

    return welcome_msg


async def generate_welcome_message_for_room(
    client: nio.AsyncClient | None,
    room: nio.MatrixRoom,
    sender_id: str | None,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str:
    """Generate a welcome message for callers without a live turn-policy candidate source."""
    if sender_id is None:
        candidate_entities = configured_routable_entity_ids_for_room(config, room.room_id, runtime_paths)
    else:
        candidate_entities = await responder_candidate_entities_for_room(client, room, sender_id, config, runtime_paths)
    return _format_welcome_message(candidate_entities, config, runtime_paths)


def _normalized_response_event_id(raw_response_event_id: str | None) -> str | None:
    """Normalize Matrix send helpers that may return empty strings or None."""
    return raw_response_event_id if isinstance(raw_response_event_id, str) and raw_response_event_id else None


def _format_plugin_reload_summary(result: PluginReloadResult) -> str:
    """Return a short user-facing summary for one plugin reload."""
    plugin_count = len(result.active_plugin_names)
    task_label = "task" if result.cancelled_task_count == 1 else "tasks"
    plugin_label = "plugin" if plugin_count == 1 else "plugins"
    active_plugins = ", ".join(result.active_plugin_names) if result.active_plugin_names else "none"
    return f"✅ Reloaded {plugin_count} {plugin_label}; cancelled {result.cancelled_task_count} {task_label}; active: {active_plugins}"


async def handle_command(  # noqa: C901, PLR0912, PLR0915
    *,
    context: CommandHandlerContext,
    room: nio.MatrixRoom,
    event: _CommandEvent,
    command: Command,
    requester_user_id: str,
) -> None:
    """Dispatch chat commands using injected bot context."""
    context.logger.info("Handling command", command_type=command.type.value)

    effective_thread_id = context.stable_target.resolved_thread_id

    response_text = ""

    if command.type == CommandType.HELP:
        topic = command.args.get("topic")
        response_text = get_command_help(topic)

    elif command.type == CommandType.RELOAD_PLUGINS:
        resolved_requester_user_id = context.config.authorization.resolve_alias(requester_user_id)
        if resolved_requester_user_id not in context.config.authorization.global_users:
            response_text = "❌ Admin only."
        elif context.reload_plugins is None:
            response_text = "❌ Plugin reload unavailable."
        else:
            try:
                response_text = _format_plugin_reload_summary(await context.reload_plugins())
            except Exception as exc:
                context.logger.exception("Plugin reload command failed", error=str(exc))
                response_text = f"❌ Plugin reload failed: {exc}"

    elif command.type == CommandType.HI:
        if context.responder_candidates_for_room is None:
            response_text = await generate_welcome_message_for_room(
                context.client,
                room,
                requester_user_id,
                context.config,
                context.runtime_paths,
            )
        else:
            candidate_entities = await context.responder_candidates_for_room(room, requester_user_id)
            response_text = _format_welcome_message(candidate_entities, context.config, context.runtime_paths)

    elif command.type == CommandType.SCHEDULE:
        full_text = command.args["full_text"]

        mentioned_agents, _, _ = check_agent_mentioned(event.source, None, context.config, context.runtime_paths)

        _, response_text = await schedule_task(
            runtime=_scheduling_runtime(context, room),
            room_id=room.room_id,
            thread_id=effective_thread_id,
            scheduled_by=requester_user_id,
            full_text=full_text,
            mentioned_agents=mentioned_agents,
        )

    elif command.type == CommandType.LIST_SCHEDULES:
        response_text = await list_scheduled_tasks(
            client=context.client,
            room_id=room.room_id,
            thread_id=effective_thread_id,
            config=context.config,
        )

    elif command.type == CommandType.CANCEL_SCHEDULE:
        cancel_all = command.args.get("cancel_all", False)

        if cancel_all:
            # Cancel all scheduled tasks
            response_text = await cancel_all_scheduled_tasks(
                client=context.client,
                room_id=room.room_id,
            )
        else:
            # Cancel specific task
            task_id = command.args["task_id"]
            response_text = await cancel_scheduled_task(
                client=context.client,
                room_id=room.room_id,
                task_id=task_id,
            )

    elif command.type == CommandType.EDIT_SCHEDULE:
        task_id = command.args["task_id"]
        full_text = command.args["full_text"]
        response_text = await edit_scheduled_task(
            runtime=_scheduling_runtime(context, room),
            room_id=room.room_id,
            task_id=task_id,
            full_text=full_text,
            scheduled_by=requester_user_id,
            thread_id=effective_thread_id,
        )

    elif command.type == CommandType.CONFIG:
        authorization = context.config.authorization
        resolved_requester_user_id = authorization.resolve_alias(requester_user_id)
        if not authorization.config_command_enabled:
            response_text = "❌ Config command disabled."
        elif resolved_requester_user_id not in authorization.global_users:
            response_text = "❌ Admin only."
        else:
            # Handle config command
            args_text = command.args.get("args_text", "")
            response_text, change_info = await handle_config_command(
                args_text,
                runtime_paths=context.runtime_paths,
            )

            # If we have change_info, this is a config set that needs confirmation
            if change_info:
                # Send the preview message
                raw_response_event_id = await context.send_response(
                    response_text,
                    skip_mentions=True,
                )
                response_event_id = _normalized_response_event_id(raw_response_event_id)
                handled_turn = HandledTurnState.from_source_event_id(
                    event.event_id,
                    response_event_id=response_event_id,
                )

                if response_event_id:
                    context.record_handled_turn(handled_turn)
                    # Register the pending change
                    config_confirmation.register_pending_change(
                        event_id=response_event_id,
                        room_id=room.room_id,
                        thread_id=effective_thread_id,
                        config_path=change_info["config_path"],
                        old_value=change_info["old_value"],
                        new_value=change_info["new_value"],
                        requester=requester_user_id,
                    )

                    # Get the pending change we just registered
                    pending_change = config_confirmation.get_pending_change(response_event_id)

                    # Store in Matrix state for persistence
                    if pending_change:
                        await config_confirmation.store_pending_change_in_matrix(
                            context.client,
                            response_event_id,
                            pending_change,
                        )

                    # Add reaction buttons
                    await config_confirmation.add_confirmation_reactions(
                        context.client,
                        room.room_id,
                        response_event_id,
                        config=context.config,
                    )

                if response_event_id is None:
                    context.record_handled_turn(handled_turn)
                return  # Exit early since we've handled the response

    elif command.type == CommandType.UNKNOWN:
        # Handle unknown commands
        response_text = "❌ Unknown command. Try !help for available commands."

    if response_text:
        raw_response_event_id = await context.send_response(
            response_text,
            skip_mentions=True,
        )
        context.record_handled_turn(
            HandledTurnState.from_source_event_id(
                event.event_id,
                response_event_id=_normalized_response_event_id(raw_response_event_id),
            ),
        )
