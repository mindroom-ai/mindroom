"""One silent turn: a listening agent goes over what it heard in a room, acts only through its allowlist, and posts nothing.

The turn is the agent itself, with its identity, instructions, and memory, but it offers only the tools ``listen.tools``
names, hides approval-gated tools and interactive questions, and drops its text. Its runs keep a session of their own
beside the room's conversations, and its usage counts as listening, since nobody asked for it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom.ai import ai_response
from mindroom.config.resolution import entity_timezone
from mindroom.message_target import MessageTarget
from mindroom.prompt_message_tags import render_msg_tag
from mindroom.response_turn import ResponseTurnContext, TurnSinks
from mindroom.session_ids import listening_session_id
from mindroom.timestamp_formatting import format_timestamp_ms
from mindroom.tool_system.runtime_context import tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.bot import AgentBot
    from mindroom.config.listening import ListenConfig
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage


def _prompt(room_id: str, heard: Sequence[ResolvedVisibleMessage], timezone: str) -> str:
    """Return what the turn is told: that it only listens and cannot post, then what it heard, oldest first."""
    messages = (
        render_msg_tag(
            sender=message.sender,
            body=message.body,
            event_id=message.event_id,
            ts=format_timestamp_ms(message.timestamp, timezone=timezone),
            thread_id=message.thread_id,
        )
        for message in heard
    )
    return "\n".join(
        (
            f"You are listening in room {room_id}. Nobody asked you anything.",
            "You cannot post: nothing you write here is sent to the room or shown to anyone.",
            "Use your tools only if something below is worth remembering or reacting to; otherwise do nothing.",
            "What others said in the room since you last listened, oldest first:",
            "<heard>",
            *messages,
            "</heard>",
        ),
    )


async def run_silent_turn(
    *,
    bot: AgentBot,
    agent_name: str,
    room_id: str,
    heard: Sequence[ResolvedVisibleMessage],
    listen: ListenConfig,
    config: Config,
    runtime_paths: RuntimePaths,
) -> None:
    """Run one silent turn over ``heard`` in ``room_id``; a turn that fails raises, so its messages are heard again."""
    session_id = listening_session_id(room_id)
    target = MessageTarget(
        room_id=room_id,
        source_thread_id=None,
        resolved_thread_id=None,
        reply_to_event_id=None,
        session_id=session_id,
    )
    # No requester asked for the turn, so its tools run as the agent's own account in the room.
    context = bot.tool_runtime_support.build_turn_context(target, user_id=None, active_model_name=listen.model)
    turn = ResponseTurnContext(
        entity_label=agent_name,
        session_id=session_id,
        run_id=None,
        correlation_id=uuid4().hex,
        reply_to_event_id=None,
        room_id=room_id,
        thread_id=None,
        requester_id=None,
        matrix_run_metadata=None,
        active_model_name=listen.model,
        # No reply owns a call's outcome afterwards, so every call finishes inside the turn.
        inline_tools=True,
        tool_names=tuple(listen.tools),
        usage_kind="listening",
        prompt=_prompt(room_id, heard, entity_timezone(config, agent_name)),
        show_tool_calls=False,
        execution_identity=context.execution_identity,
        include_interactive_questions=False,
        supports_native_tool_approval=False,
    )
    with tool_runtime_context(context):
        # An interrupted turn is heard again, so its partial record is not kept.
        result = await ai_response(turn, runtime_paths, config, sinks=TurnSinks(persist_interrupted=False))
    if result.failed is not None:
        raise result.failed.reply.error
