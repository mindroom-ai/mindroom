"""Post an agent's final reply to a Slack project turn back into its Slack thread.

The reply is already delivered in Matrix and the caller runs this in the background, so a failure is only logged.
A turn counts as a Slack turn only when MindRoom itself sent it: a managed account's hook dispatch whose Slack thread
the replay store, written by delivery, binds to this room and Matrix thread.
A person's own Matrix message that names a Slack thread meets neither condition.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING

import httpx

from mindroom.config.slack import slack_answering_agents
from mindroom.delivery_replay import DeliveryReplayStore, DeliveryReplayStoreError
from mindroom.logging_config import get_logger
from mindroom.matrix.state import resolve_room_id
from mindroom.slack.api import SlackApiError, slack_call
from mindroom.slack.app import load_slack_app, slack_bot_identity
from mindroom.tool_system.events import strip_visible_tool_markers
from mindroom.turn_origin import SenderKind, TurnIntent

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.hooks import MessageEnvelope

logger = get_logger(__name__)

# The hook source of a project turn, as delivery writes it: the channel, then the thread's root `ts`.
_HOOK_SOURCE = re.compile(r"slack/([CG][A-Z0-9]{8,})/(\d+\.\d+)", re.ASCII)
# Slack's limit for one markdown block.
_BLOCK_LIMIT = 12_000
_ELLIPSIS = "…"


def _slack_thread(envelope: MessageEnvelope) -> tuple[str, str] | None:
    """Return the channel and root `ts` of the Slack thread a trusted project turn came from."""
    match = _HOOK_SOURCE.fullmatch(envelope.hook_source or "")
    origin = envelope.origin
    if (
        match is None
        or origin.intent is not TurnIntent.HOOK_DISPATCH
        or origin.sender_kind is not SenderKind.MANAGED_ENTITY
    ):
        return None
    return match[1], match[2]


def _answers_here(envelope: MessageEnvelope, channel_id: str, config: Config, runtime_paths: RuntimePaths) -> bool:
    """Return whether the turn's agent answers this channel in the room the turn is in."""
    return any(
        answering.agent == envelope.agent_name
        and channel_id in answering.channels
        and resolve_room_id(answering.room, runtime_paths) == envelope.target.room_id
        for answering in slack_answering_agents(config)
    )


def _visible_text(response_text: str) -> str:
    """Return the reply without its tool markers, cut to fit one markdown block."""
    text = strip_visible_tool_markers(response_text).strip()
    return text if len(text) <= _BLOCK_LIMIT else text[: _BLOCK_LIMIT - len(_ELLIPSIS)] + _ELLIPSIS


async def mirror_reply_to_slack(
    envelope: MessageEnvelope,
    response_text: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager,
    client_factory: Callable[[], httpx.AsyncClient],
) -> None:
    """Post the final reply of a Slack project turn into its Slack thread, as the app.

    Anything else, including a turn that only claims to come from Slack, posts nothing.
    A Slack or replay store failure is logged and not raised.
    """
    thread = _slack_thread(envelope)
    if thread is None:
        return
    channel_id, root_ts = thread
    text = _visible_text(response_text)
    control_state_root = runtime_paths.control_state_root
    if not text or control_state_root is None or not _answers_here(envelope, channel_id, config, runtime_paths):
        return
    app = await asyncio.to_thread(load_slack_app, credentials_manager)
    if app is None:
        return
    try:
        async with client_factory() as client:
            identity = await slack_bot_identity(client, app)
            bound = await asyncio.to_thread(
                DeliveryReplayStore(control_state_root / "slack" / "replay").bound_thread_root,
                f"slack:agent:{envelope.agent_name}",
                f"{identity.team_id}:{channel_id}:{root_ts}",
                now=int(time.time()),
            )
            if bound != (envelope.target.room_id, envelope.target.resolved_thread_id):
                logger.info("Slack reply not mirrored: its thread is not bound here", channel_id=channel_id)
                return
            await slack_call(
                client,
                "chat.postMessage",
                app.bot_token,
                channel=channel_id,
                thread_ts=root_ts,
                text=text,
                blocks=[{"type": "markdown", "text": text}],
            )
    except (SlackApiError, httpx.HTTPError, DeliveryReplayStoreError) as exc:
        reason = exc.error if isinstance(exc, SlackApiError) else type(exc).__name__
        logger.warning("Slack reply not mirrored", channel_id=channel_id, reason=reason)
