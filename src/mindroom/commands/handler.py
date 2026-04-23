"""Command handling helpers extracted from bot dispatch logic."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from mindroom.commands import config_confirmation
from mindroom.commands.config_commands import handle_config_command
from mindroom.commands.parsing import Command, CommandType, get_command_help
from mindroom.constants import ROUTER_AGENT_NAME, RuntimePaths
from mindroom.handled_turns import HandledTurnState
from mindroom.logging_config import get_logger
from mindroom.matrix.event_info import EventInfo
from mindroom.scheduling import (
    SchedulingRuntime,
    cancel_all_scheduled_tasks,
    cancel_scheduled_task,
    edit_scheduled_task,
    list_scheduled_tasks,
    schedule_task,
)
from mindroom.thread_utils import check_agent_mentioned, get_configured_agents_for_room

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    import nio
    import structlog

    from mindroom.config.main import Config
    from mindroom.hooks.types import HookMatrixAdmin
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol, ConversationEventCache
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


class CommandEvent(Protocol):
    """Minimal canonical text-event shape required by command handling."""

    sender: str
    event_id: str
    body: str
    source: dict[str, Any]


class DeriveConversationContext(Protocol):
    """Callable signature for deriving conversation thread context."""

    async def __call__(
        self,
        room_id: str,
        event_info: EventInfo,
        *,
        event_id: str | None = None,
    ) -> tuple[bool, str | None, Sequence[ResolvedVisibleMessage]]:
        """Return whether one event is threaded plus its thread id and history."""


@dataclass(frozen=True)
class CommandHandlerContext:
    """Dependencies required by command handling."""

    client: nio.AsyncClient
    config: Config
    runtime_paths: RuntimePaths
    logger: structlog.stdlib.BoundLogger
    derive_conversation_context: DeriveConversationContext
    conversation_cache: ConversationCacheProtocol
    event_cache: ConversationEventCache
    build_message_target: Callable[..., MessageTarget]
    record_handled_turn: Callable[[HandledTurnState], None]
    send_response: Callable[..., Awaitable[str | None]]
    reload_plugins: Callable[[], Awaitable[PluginReloadResult]] | None = None
    matrix_admin: HookMatrixAdmin | None = None


def _format_agent_description(agent_name: str, config: Config) -> str:
    """Format a concise agent description for the welcome message."""
    if agent_name in config.agents:
        agent_config = config.agents[agent_name]
        tool_names = config.get_agent_tools(agent_name)
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
        team_desc = f"Team of {len(team_config.agents)} agents"
        if team_config.role:
            return f"{team_config.role} ({team_desc})"
        return team_desc

    return ""


def _generate_welcome_message(room_id: str, config: Config, runtime_paths: RuntimePaths) -> str:
    """Generate the welcome message text for a room."""
    # Get list of configured agents for this room
    configured_agents = get_configured_agents_for_room(room_id, config, runtime_paths)

    # Build agent list for the welcome message
    agent_list = []
    for agent_id in configured_agents:
        agent_name = agent_id.agent_name(config, runtime_paths)
        if not agent_name or agent_name == ROUTER_AGENT_NAME:
            continue

        description = _format_agent_description(agent_name, config)
        # Always show the agent, with or without description
        # Use the username with mindroom_ prefix (but without domain) for proper mention parsing
        agent_entry = f"• **@{agent_id.username}**"
        if description:
            agent_entry += f": {description}"
        agent_list.append(agent_entry)

    # Create welcome message
    welcome_msg = (
        "🎉 **Welcome to MindRoom!**\n\n"
        "I'm your routing assistant, here to help coordinate our team of specialized AI agents. 🤖\n\n"
    )

    if agent_list:
        welcome_msg += "🧠 **Available agents in this room:**\n"
        welcome_msg += "\n".join(agent_list)
        welcome_msg += "\n\n"

    welcome_msg += (
        "💬 **How to interact:**\n"
        "• Mention an agent with @ to get their attention (e.g., @mindroom_assistant)\n"
        "• Use `!help` to see available commands\n"
        "• Agents stay in existing Matrix threads, including compatible plain replies from bridges and non-thread clients\n"
        "• Multiple agents can collaborate when you mention them together\n"
        "• 🎤 Voice messages are automatically transcribed and work perfectly!\n\n"
        "⚡ **Quick commands:**\n"
        "• `!hi` - Show this welcome message again\n"
        "• `!schedule <time> <message>` - Schedule tasks and reminders\n"
        "• `!help [topic]` - Get detailed help\n\n"
        "✨ Feel free to ask any agent for help or start a conversation!"
    )

    return welcome_msg


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
    event: CommandEvent,
    command: Command,
    requester_user_id: str,
) -> None:
    """Dispatch chat commands using injected bot context."""
    context.logger.info("Handling command", command_type=command.type.value)

    event_info = EventInfo.from_event(event.source)
    _, thread_id, _thread_history = await context.derive_conversation_context(
        room.room_id,
        event_info,
        event_id=event.event_id,
    )

    # Commands/tools that persist conversation context should use the same
    # thread-root policy as outgoing replies.
    effective_thread_id = context.build_message_target(
        room_id=room.room_id,
        thread_id=thread_id,
        reply_to_event_id=event.event_id,
        event_source=event.source,
    ).resolved_thread_id

    response_text = ""
    side_effect_committed = False

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
                side_effect_committed = True
            except Exception as exc:
                context.logger.exception("Plugin reload command failed", error=str(exc))
                response_text = f"❌ Plugin reload failed: {exc}"

    elif command.type == CommandType.HI:
        # Generate the welcome message for this room
        response_text = _generate_welcome_message(room.room_id, context.config, context.runtime_paths)

    elif command.type == CommandType.SCHEDULE:
        full_text = command.args["full_text"]

        # Get mentioned agents from the command text
        mentioned_agents, _, _ = check_agent_mentioned(event.source, None, context.config, context.runtime_paths)

        task_id, response_text = await schedule_task(
            runtime=_scheduling_runtime(context, room),
            room_id=room.room_id,
            thread_id=effective_thread_id,
            scheduled_by=requester_user_id,
            full_text=full_text,
            mentioned_agents=mentioned_agents,
        )
        side_effect_committed = task_id is not None

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
        side_effect_committed = response_text.startswith("✅")

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
        side_effect_committed = response_text.startswith("✅")

    elif command.type == CommandType.CONFIG:
        # Handle config command
        args_text = command.args.get("args_text", "")
        response_text, change_info = await handle_config_command(
            args_text,
            runtime_paths=context.runtime_paths,
        )

        # If we have change_info, this is a config set that needs confirmation
        if change_info:
            # Send the preview message
            response_event_id = _normalized_response_event_id(
                await context.send_response(
                    room.room_id,
                    event.event_id,
                    response_text,
                    effective_thread_id,
                    reply_to_event=event,
                    skip_mentions=True,
                ),
            )
            if response_event_id:
                context.record_handled_turn(
                    HandledTurnState.from_source_event_id(
                        event.event_id,
                        response_event_id=response_event_id,
                    ),
                )
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
                )
            return  # Exit early since we've handled the response

    elif command.type == CommandType.UNKNOWN:
        # Handle unknown commands
        response_text = "❌ Unknown command. Try !help for available commands."

    if response_text:
        response_event_id = _normalized_response_event_id(
            await context.send_response(
                room.room_id,
                event.event_id,
                response_text,
                effective_thread_id,
                reply_to_event=event,
                skip_mentions=True,
            ),
        )
        if response_event_id is not None:
            context.record_handled_turn(
                HandledTurnState.from_source_event_id(
                    event.event_id,
                    response_event_id=response_event_id,
                ),
            )
        elif side_effect_committed:
            context.record_handled_turn(HandledTurnState.from_source_event_id(event.event_id))
