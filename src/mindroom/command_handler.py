"""Command handling helpers extracted from bot dispatch logic."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import config_confirmation
from .authorization import get_available_agents_for_sender
from .commands import Command, CommandType, get_command_help, handle_widget_command
from .config_commands import handle_config_command
from .constants import ROUTER_AGENT_NAME
from .logging_config import get_logger
from .matrix.event_info import EventInfo
from .scheduling import (
    cancel_all_scheduled_tasks,
    cancel_scheduled_task,
    edit_scheduled_task,
    list_scheduled_tasks,
    schedule_task,
)
from .thread_utils import check_agent_mentioned, get_configured_agents_for_room
from .tool_system.metadata import get_tool_by_name
from .tool_system.skills import resolve_skill_command_spec

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    import nio
    import structlog
    from agno.tools.function import Function
    from agno.tools.toolkit import Toolkit

    from .config.main import Config
    from .matrix.identity import MatrixID
    from .response_tracker import ResponseTracker

logger = get_logger(__name__)


@dataclass(frozen=True)
class CommandHandlerContext:
    """Dependencies required by command handling."""

    client: nio.AsyncClient
    config: Config
    logger: structlog.stdlib.BoundLogger
    response_tracker: ResponseTracker
    derive_conversation_context: Callable[[str, EventInfo], Awaitable[tuple[bool, str | None, list[dict[str, Any]]]]]
    requester_user_id_for_event: Callable[[nio.RoomMessageText], str]
    resolve_reply_thread_id: Callable[[str | None, str | None], str | None]
    send_response: Callable[..., Awaitable[str | None]]
    send_skill_command_response: Callable[..., Awaitable[str | None]]


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


def _generate_welcome_message(room_id: str, config: Config) -> str:
    """Generate the welcome message text for a room."""
    # Get list of configured agents for this room
    configured_agents = get_configured_agents_for_room(room_id, config)

    # Build agent list for the welcome message
    agent_list = []
    for agent_id in configured_agents:
        agent_name = agent_id.agent_name(config)
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
        "• Agents respond in threads; plain replies still continue the same conversation\n"
        "• Multiple agents can collaborate when you mention them together\n"
        "• 🎤 Voice messages are automatically transcribed and work perfectly!\n\n"
        "⚡ **Quick commands:**\n"
        "• `!hi` - Show this welcome message again\n"
        "• `!widget` - Add configuration widget to this room\n"
        "• `!schedule <time> <message>` - Schedule tasks and reminders\n"
        "• `!help [topic]` - Get detailed help\n\n"
        "✨ Feel free to ask any agent for help or start a conversation!"
    )

    return welcome_msg


def _build_skill_command_prompt(skill_name: str, args_text: str) -> str:
    args = args_text.strip()
    args_section = args if args else "(no arguments provided)"
    return (
        "You were invoked via the !skill command.\n"
        f"Skill: {skill_name}\n"
        f"User input:\n{args_section}\n\n"
        "Load the skill instructions with get_skill_instructions and follow them."
    )


def _resolve_skill_command_agent(  # noqa: C901
    skill_name: str,
    *,
    config: Config,
    room: nio.MatrixRoom,
    mentioned_agents: list[MatrixID],
    requester_user_id: str,
) -> tuple[str | None, str | None]:
    requested = skill_name.strip().lower()
    mentioned_names: list[str] = []
    for mid in mentioned_agents:
        name = mid.agent_name(config)
        if not name or name == ROUTER_AGENT_NAME:
            continue
        mentioned_names.append(name)
    unique_mentions = list(dict.fromkeys(mentioned_names))
    if len(unique_mentions) > 1:
        return None, f"❌ Multiple agents mentioned: {', '.join(unique_mentions)}. Mention only one."

    agents_in_room = get_available_agents_for_sender(room, requester_user_id, config)
    candidate_names: list[str] = []
    for mid in agents_in_room:
        name = mid.agent_name(config)
        if not name:
            continue
        if name not in config.agents:
            continue
        allowlist = {skill.lower() for skill in config.get_agent(name).skills}
        if requested in allowlist:
            candidate_names.append(name)
    candidate_names = list(dict.fromkeys(candidate_names))

    if unique_mentions:
        target = unique_mentions[0]
        if target not in candidate_names:
            return None, f"❌ Agent '{target}' does not have skill '{skill_name}' enabled in this room."
        return target, None

    if len(candidate_names) == 1:
        return candidate_names[0], None

    if not candidate_names:
        return None, f"❌ No agents in this room have skill '{skill_name}' enabled."

    return None, (
        f"❌ Multiple agents have skill '{skill_name}': {', '.join(candidate_names)}. "
        "Mention one with @mindroom_<agent>."
    )


def _collect_agent_toolkits(config: Config, agent_name: str) -> list[tuple[str, Toolkit]]:
    sandbox_tools = config.get_agent_sandbox_tools(agent_name)
    toolkits: list[tuple[str, Toolkit]] = []
    for tool_name in config.get_agent_tools(agent_name):
        try:
            toolkits.append((tool_name, get_tool_by_name(tool_name, sandbox_tools_override=sandbox_tools)))
        except ValueError as exc:
            logger.warning(
                "Failed to load tool for skill dispatch",
                tool=tool_name,
                agent=agent_name,
                error=str(exc),
            )
    return toolkits


def _resolve_tool_dispatch_target(  # noqa: C901, PLR0911, PLR0912
    toolkits: list[tuple[str, Toolkit]],
    command_tool: str,
) -> tuple[Function | None, Toolkit | None, str | None]:
    if not command_tool:
        return None, None, "Missing command-tool for tool dispatch."

    if "." in command_tool:
        toolkit_name, function_name = command_tool.split(".", 1)
        for registered_name, toolkit in toolkits:
            if registered_name != toolkit_name:
                continue
            function = toolkit.functions.get(function_name) or toolkit.async_functions.get(function_name)
            if function:
                return function, toolkit, None
        return None, None, f"Tool '{toolkit_name}' does not expose '{function_name}'."

    matches: list[tuple[Function, Toolkit, str]] = []
    for registered_name, toolkit in toolkits:
        function = toolkit.functions.get(command_tool) or toolkit.async_functions.get(command_tool)
        if function:
            matches.append((function, toolkit, registered_name))

    if len(matches) == 1:
        function, toolkit, _ = matches[0]
        return function, toolkit, None

    if len(matches) > 1:
        toolkit_names = ", ".join(sorted({name for _, _, name in matches}))
        return None, None, f"Command tool '{command_tool}' is ambiguous across toolkits: {toolkit_names}."

    for registered_name, toolkit in toolkits:
        if registered_name != command_tool:
            continue
        functions = {**toolkit.functions, **toolkit.async_functions}
        if not functions:
            return None, None, f"Tool '{command_tool}' has no callable functions."
        if len(functions) == 1:
            return next(iter(functions.values())), toolkit, None
        return None, None, f"Tool '{command_tool}' exposes multiple functions; specify one."

    return None, None, f"Tool '{command_tool}' not found for this agent."


@dataclass(frozen=True)
class _ToolCallArguments:
    """Prepared arguments for a tool call."""

    args: tuple[object, ...]
    kwargs: dict[str, object]
    error: str | None = None


def _prepare_tool_call_arguments(  # noqa: PLR0911
    entrypoint: Callable[..., object] | None,
    base_args: Mapping[str, object],
) -> _ToolCallArguments:
    if entrypoint is None:
        return _ToolCallArguments((), {}, "Tool entrypoint is missing.")

    signature = inspect.signature(entrypoint)
    params = list(signature.parameters.values())
    has_var_kw = any(param.kind == param.VAR_KEYWORD for param in params)
    if has_var_kw:
        return _ToolCallArguments((), dict(base_args), None)

    kwargs = {key: value for key, value in base_args.items() if key in signature.parameters}
    if kwargs:
        missing = [
            param.name
            for param in params
            if param.default is param.empty
            and param.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
            and param.name not in kwargs
        ]
        if missing:
            return _ToolCallArguments((), {}, f"Tool requires parameters: {', '.join(missing)}.")
        return _ToolCallArguments((), kwargs, None)

    if not params:
        return _ToolCallArguments((), {}, None)

    if len(params) == 1 and params[0].kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    ):
        return _ToolCallArguments((base_args.get("command", ""),), {}, None)

    missing = [
        param.name
        for param in params
        if param.default is param.empty
        and param.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    ]
    if missing:
        return _ToolCallArguments((), {}, f"Tool requires parameters: {', '.join(missing)}.")
    return _ToolCallArguments((), {}, None)


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


async def _run_skill_command_tool(
    *,
    config: Config,
    agent_name: str,
    command_tool: str,
    skill_name: str,
    args_text: str,
    command_name: str = "skill",
) -> str:
    toolkits = _collect_agent_toolkits(config, agent_name)
    function, toolkit, error = _resolve_tool_dispatch_target(toolkits, command_tool)
    if error:
        return f"❌ {error}"
    assert function is not None

    base_args = {
        "command": args_text,
        "commandName": command_name,
        "skillName": skill_name,
    }
    entrypoint = function.entrypoint
    call_args = _prepare_tool_call_arguments(entrypoint, base_args)
    if call_args.error:
        return f"❌ {call_args.error}"
    assert entrypoint is not None

    try:
        if toolkit and getattr(toolkit, "_requires_connect", False):
            connect = getattr(toolkit, "connect", None)
            close = getattr(toolkit, "close", None)
            if connect:
                await _maybe_await(connect())
            try:
                result = await _maybe_await(entrypoint(*call_args.args, **call_args.kwargs))
            finally:
                if close:
                    await _maybe_await(close())
        else:
            result = await _maybe_await(entrypoint(*call_args.args, **call_args.kwargs))
    except Exception as exc:
        logger.warning(
            "Skill command tool dispatch failed",
            agent=agent_name,
            tool=command_tool,
            error=str(exc),
        )
        return f"❌ Tool '{command_tool}' failed: {exc}"

    if result is None or result == "":
        return "✅ Tool completed."
    return str(result)


async def handle_command(  # noqa: C901, PLR0912, PLR0915
    *,
    context: CommandHandlerContext,
    room: nio.MatrixRoom,
    event: nio.RoomMessageText,
    command: Command,
) -> None:
    """Dispatch chat commands using injected bot context."""
    context.logger.info("Handling command", command_type=command.type.value)

    event_info = EventInfo.from_event(event.source)
    _, thread_id, thread_history = await context.derive_conversation_context(room.room_id, event_info)
    requester_user_id = context.requester_user_id_for_event(event)

    # Widget command modifies room state, so it doesn't need a thread
    if command.type == CommandType.WIDGET:
        url = command.args.get("url")
        response_text = await handle_widget_command(client=context.client, room_id=room.room_id, url=url)
        # Send response in thread if in thread, otherwise in main room
        await context.send_response(room.room_id, event.event_id, response_text, thread_id)
        return

    # Commands/tools that persist conversation context should use the same
    # thread-root policy as outgoing replies.
    effective_thread_id = context.resolve_reply_thread_id(thread_id, event.event_id)

    response_text = ""

    if command.type == CommandType.HELP:
        topic = command.args.get("topic")
        response_text = get_command_help(topic)

    elif command.type == CommandType.HI:
        # Generate the welcome message for this room
        response_text = _generate_welcome_message(room.room_id, context.config)

    elif command.type == CommandType.SCHEDULE:
        full_text = command.args["full_text"]

        # Get mentioned agents from the command text
        mentioned_agents, _, _ = check_agent_mentioned(event.source, None, context.config)

        _, response_text = await schedule_task(
            client=context.client,
            room_id=room.room_id,
            thread_id=effective_thread_id,
            scheduled_by=requester_user_id,
            full_text=full_text,
            config=context.config,
            room=room,
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
            client=context.client,
            room_id=room.room_id,
            task_id=task_id,
            full_text=full_text,
            scheduled_by=requester_user_id,
            config=context.config,
            room=room,
            thread_id=effective_thread_id,
        )

    elif command.type == CommandType.CONFIG:
        # Handle config command
        args_text = command.args.get("args_text", "")
        response_text, change_info = await handle_config_command(args_text)

        # If we have change_info, this is a config set that needs confirmation
        if change_info:
            # Send the preview message
            event_id = await context.send_response(
                room.room_id,
                event.event_id,
                response_text,
                effective_thread_id,
                reply_to_event=event,
                skip_mentions=True,
            )

            if event_id:
                # Register the pending change
                config_confirmation.register_pending_change(
                    event_id=event_id,
                    room_id=room.room_id,
                    thread_id=effective_thread_id,
                    config_path=change_info["config_path"],
                    old_value=change_info["old_value"],
                    new_value=change_info["new_value"],
                    requester=requester_user_id,
                )

                # Get the pending change we just registered
                pending_change = config_confirmation.get_pending_change(event_id)

                # Store in Matrix state for persistence
                if pending_change:
                    await config_confirmation.store_pending_change_in_matrix(
                        context.client,
                        event_id,
                        pending_change,
                    )

                # Add reaction buttons
                await config_confirmation.add_confirmation_reactions(context.client, room.room_id, event_id)

            context.response_tracker.mark_responded(event.event_id)
            return  # Exit early since we've handled the response

    elif command.type == CommandType.SKILL:
        skill_name = command.args.get("skill_name")
        args_text = command.args.get("args_text", "")
        if not skill_name:
            response_text = "Usage: !skill <name> [args]"
        else:
            mentioned_agents, _, _ = check_agent_mentioned(event.source, None, context.config)
            target_agent, error = _resolve_skill_command_agent(
                skill_name,
                config=context.config,
                room=room,
                mentioned_agents=mentioned_agents,
                requester_user_id=requester_user_id,
            )
            if error:
                response_text = error
            else:
                assert target_agent is not None
                spec = resolve_skill_command_spec(skill_name, context.config, target_agent)
                if spec is None:
                    response_text = f"❌ Skill '{skill_name}' not found or not enabled for agent '{target_agent}'."
                elif not spec.user_invocable:
                    response_text = f"❌ Skill '{spec.name}' is not user-invocable."
                elif spec.dispatch and spec.dispatch.kind == "tool":
                    response_text = await _run_skill_command_tool(
                        config=context.config,
                        agent_name=target_agent,
                        command_tool=spec.dispatch.tool_name,
                        skill_name=spec.name,
                        args_text=args_text,
                    )
                elif spec.disable_model_invocation:
                    response_text = (
                        f"❌ Skill '{spec.name}' is configured to skip model invocation and has no tool dispatch."
                    )
                else:
                    prompt = _build_skill_command_prompt(spec.name, args_text)
                    event_id = await context.send_skill_command_response(
                        room_id=room.room_id,
                        reply_to_event_id=event.event_id,
                        thread_id=effective_thread_id,
                        thread_history=thread_history,
                        prompt=prompt,
                        agent_name=target_agent,
                        user_id=requester_user_id,
                        reply_to_event=event,
                    )
                    if event_id:
                        context.response_tracker.mark_responded(event.event_id, event_id)
                    return

    elif command.type == CommandType.UNKNOWN:
        # Handle unknown commands
        response_text = "❌ Unknown command. Try !help for available commands."

    if response_text:
        await context.send_response(
            room.room_id,
            event.event_id,
            response_text,
            effective_thread_id,
            reply_to_event=event,
            skip_mentions=True,
        )
        context.response_tracker.mark_responded(event.event_id)
