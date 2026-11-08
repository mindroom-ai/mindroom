"""Messages a plugin route posts to one room as the router: notices, and relays for the route's signed-in user."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from mindroom.api.app_state import ensure_app_state
from mindroom.constants import HOOK_MESSAGE_RECEIVED_DEPTH_KEY, ORIGINAL_SENDER_KEY, ROUTER_AGENT_NAME
from mindroom.entity_directory import entity_directory
from mindroom.hooks.types import format_hook_source
from mindroom.matrix.reserved_content import MINDROOM_RESERVED_PREFIXES, reserved_content_keys

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from fastapi import FastAPI

    from mindroom.bot import AgentBot

# What a route's message names after its plugin in its hook source, where a hook's names the hook's event.
_ROUTE_SOURCE_EVENT = "http"


async def post_notice(
    app: FastAPI,
    plugin_name: str,
    room_id: str,
    text: str,
    *,
    thread_id: str | None,
    content: Mapping[str, object] | None,
) -> str:
    """Post ``text`` as the router's ``m.notice``, which mentions no one and claims no requester; return its event ID."""
    extra = _plugin_content(content)
    extra["msgtype"] = "m.notice"
    return await _send(_running_router(app), plugin_name, room_id, text, thread_id, extra, mentions=())


async def post_relay(
    app: FastAPI,
    plugin_name: str,
    room_id: str,
    text: str,
    *,
    thread_id: str | None,
    content: Mapping[str, object] | None,
    user_id: str,
    members: Mapping[str, object],
    mentions: Sequence[str],
) -> str:
    """Post ``text`` as the router for ``user_id``, mentioning exactly ``mentions``; return its event ID.

    The message names the user as its original sender, so each agent it wakes answers them under its access rules, and
    its text says it is sent for them. ``mentions`` must name at least one agent or team and only agents and teams
    among ``members``, the room's joined users, or the relay answers 400.
    """
    extra = _plugin_content(content)
    extra[ORIGINAL_SENDER_KEY] = user_id
    router = _running_router(app)
    directory = entity_directory(router.config, router.runtime_paths)
    if not mentions:
        raise HTTPException(400, "A relay mentions at least one agent or team.")
    for mentioned in mentions:
        entity = directory.get(mentioned)
        if entity is None or entity.kind == "router" or mentioned not in members:
            raise HTTPException(400, f"A relay mentions only agents and teams in this room, not {mentioned}.")
    return await _send(router, plugin_name, room_id, f"Sent for {user_id}: {text}", thread_id, extra, mentions)


def _plugin_content(content: Mapping[str, object] | None) -> dict[str, Any]:
    """Return a copy of a plugin's ``content``, answering 400 when it sets a MindRoom key at any depth."""
    extra = dict(content or {})
    if reserved := reserved_content_keys(extra, MINDROOM_RESERVED_PREFIXES):
        raise HTTPException(400, f"content must not set MindRoom-reserved keys ({', '.join(reserved)}).")
    return extra


def _running_router(app: FastAPI) -> AgentBot:
    """Return the router bot running now, so a restarted router posts with its new client, or answer 503."""
    embedded = ensure_app_state(app).embedded
    router = None if embedded is None else embedded.agent_bot(ROUTER_AGENT_NAME)
    if router is None or not router.running:
        raise HTTPException(503, "MindRoom's router is not running; try again.")
    return router


async def _send(
    router: AgentBot,
    plugin_name: str,
    room_id: str,
    text: str,
    thread_id: str | None,
    extra: dict[str, Any],
    mentions: Sequence[str],
) -> str:
    # One hop into the synthetic chain, as a hook's own send outside a message is, so hooks reacting to it cannot loop.
    extra[HOOK_MESSAGE_RECEIVED_DEPTH_KEY] = 1
    event_id = await router.hook_context.send_message(
        room_id,
        text,
        thread_id,
        format_hook_source(plugin_name, _ROUTE_SOURCE_EVENT),
        extra,
        mentions=list(mentions),
    )
    if event_id is None:
        raise HTTPException(503, "The message could not be sent; try again.")
    return event_id
