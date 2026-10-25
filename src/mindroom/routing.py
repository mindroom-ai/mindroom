"""Simple AI routing for MindRoom responders."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from mindroom.agent_descriptions import describe_agent
from mindroom.agent_loop.helper import complete_prompt
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import entity_identity_registry
from mindroom.helper_usage import record_system_usage
from mindroom.logging_config import get_logger
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage, replace_visible_message
from mindroom.matrix.identity import MatrixID
from mindroom.providers import registry as provider_registry
from mindroom.routing_judgment import ResponderSelection, judge_responder
from mindroom.tool_system.events import body_without_tool_markers

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)


class _RoutingSuggestion(BaseModel):
    """Structured output for routing decisions."""

    entity_name: str = Field(description="The name of the agent or team that should respond")
    reasoning: str = Field(description="Brief explanation of why this agent or team was chosen")


async def suggest_responder(
    message: str,
    available_entity_names: list[str],
    config: Config,
    runtime_paths: RuntimePaths,
    thread_context: Sequence[ResolvedVisibleMessage] | None = None,
    *,
    room_id: str | None = None,
    thread_id: str | None = None,
) -> ResponderSelection | None:
    """Use AI to suggest which of ``available_entity_names``, plain agent or team names, should answer a message.

    This is the core routing logic, independent of any transport layer. ``thread_context`` holds recent messages by
    their visible sender and body; ``room_id`` names the room whose runtime model default applies and ``thread_id``
    the thread whose model override takes precedence. Returns an accepted selection (``entity_name=None`` means no
    fit), or None if routing fails.
    """
    try:
        if config.router.judgment is not None:
            selection = await judge_responder(message, available_entity_names, config, runtime_paths, thread_context)
            if selection is not None:
                return selection
        agents_info = "\n\n".join(
            f"{entity_name}:\n  {describe_agent(entity_name, config)}" for entity_name in available_entity_names
        )
        prompt = config.render_prompt(
            "ROUTER_AGENT_SELECTION_PROMPT_TEMPLATE",
            agents_info=agents_info,
            message=message,
        )

        if thread_context:
            recent = "".join(
                f"{msg.sender}: {body_without_tool_markers(msg.body, msg.content)[:100]}\n"
                for msg in thread_context[-3:]
            )
            prompt = f"{config.get_prompt('ROUTER_THREAD_CONTEXT_HEADER')}\n{recent}\n{prompt}"

        router_model_name = config.resolve_runtime_model(
            entity_name=ROUTER_AGENT_NAME,
            room_id=room_id,
            thread_id=thread_id,
            runtime_paths=runtime_paths,
        ).model_name

        provider = provider_registry.build_model_provider(config, runtime_paths, router_model_name)
        logger.info("using_router_model", model_name=router_model_name, model_id=provider.identity.id)

        result = await complete_prompt(
            provider,
            prompt,
            role="Route messages to appropriate agents or teams",
            schema=_RoutingSuggestion,
        )
        await record_system_usage(result.record, runtime_paths=runtime_paths, kind="routing")
        suggestion = result.content
        if not isinstance(suggestion, _RoutingSuggestion):
            logger.warning(
                "Unexpected response type from AI routing",
                expected="_RoutingSuggestion",
                actual=type(suggestion).__name__,
            )
            return None

        if suggestion.entity_name not in available_entity_names:
            logger.warning(
                "AI suggested invalid entity",
                suggested=suggestion.entity_name,
                available=available_entity_names,
            )
            return None

        logger.info("Routing decision", entity=suggestion.entity_name, reason=suggestion.reasoning)
    except Exception as e:
        logger.exception("Routing failed", error=str(e))
        return None
    else:
        return ResponderSelection(suggestion.entity_name)


async def suggest_responder_for_message(
    message: str,
    available_entities: list[MatrixID],
    config: Config,
    runtime_paths: RuntimePaths,
    thread_context: Sequence[ResolvedVisibleMessage] | None = None,
    *,
    room_id: str | None = None,
    thread_id: str | None = None,
) -> str | None:
    """Use AI to suggest which configured responder should answer a message.

    Matrix-aware wrapper around suggest_responder() that converts MatrixID
    objects to plain responder names and resolves sender identities in
    thread context.
    """
    registry = entity_identity_registry(config, runtime_paths)
    entity_names = [
        name
        for mid in available_entities
        if (name := registry.current_entity_name_for_user_id(mid.full_id, include_router=False)) is not None
    ]

    # Resolve Matrix sender IDs to readable names for thread context
    resolved_context = None
    if thread_context:
        resolved_context = []
        for msg in thread_context:
            sender = msg.sender
            if sender.startswith("@") and ":" in sender:
                sender_name = registry.current_entity_name_for_user_id(sender)
                # TODO(simplify): A human sender reaches the router as its homeserver domain, so every human on one
                # server reads as the same speaker. Keep the sender's Matrix ID instead. Models routing would notice.
                sender = sender_name if sender_name is not None else MatrixID.parse(sender).domain
            resolved_context.append(replace_visible_message(msg, sender=sender))

    selection = await suggest_responder(
        message,
        entity_names,
        config,
        runtime_paths,
        resolved_context,
        room_id=room_id,
        thread_id=thread_id,
    )

    return selection.entity_name if selection is not None else None


__all__ = [
    "ResponderSelection",
    "suggest_responder",
    "suggest_responder_for_message",
]
