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
from mindroom.delivery_replay import DeliveryReplayStoreError
from mindroom.logging_config import get_logger
from mindroom.matrix.state import resolve_room_id
from mindroom.slack.api import SLACK_CHANNEL_ID_RE, SlackApiError, neutralize_broadcasts, slack_call
from mindroom.slack.app import load_slack_app, slack_bot_identity
from mindroom.slack.events import project_thread_key
from mindroom.slack.turns import failure_reason, project_scope, slack_replay_store
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
_HOOK_SOURCE = re.compile(rf"slack/({SLACK_CHANNEL_ID_RE.pattern})/(\d+\.\d+)", re.ASCII)
# Slack's limit for one markdown block, counted in UTF-16 code units.
_BLOCK_LIMIT = 12_000
_ELLIPSIS = "…"


def is_slack_project_turn(envelope: MessageEnvelope) -> bool:
    """Return whether a turn may be a Slack project turn, from the turn alone, before anything is looked up."""
    return _slack_thread(envelope) is not None


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


def _utf16(text: str) -> bytes:
    # A model's reply can hold half of a surrogate pair, which must not raise here.
    return text.encode("utf-16-le", errors="surrogatepass")


def _visible_text(response_text: str) -> str:
    """Return the reply without its tool markers and broadcast mentions, cut to fit one markdown block.

    The cut keeps whole characters: a character outside the basic plane takes two UTF-16 code units, and a cut between
    them drops the half.
    A lone half of a surrogate pair, which Slack cannot take, is dropped too.
    """
    units = _utf16(neutralize_broadcasts(strip_visible_tool_markers(response_text).strip()))
    if len(units) <= 2 * _BLOCK_LIMIT:
        return units.decode("utf-16-le", errors="ignore")
    kept = units[: 2 * _BLOCK_LIMIT - len(_utf16(_ELLIPSIS))]
    return kept.decode("utf-16-le", errors="ignore") + _ELLIPSIS


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
                slack_replay_store(control_state_root).bound_thread_root,
                project_scope(envelope.agent_name),
                project_thread_key(identity.team_id, channel_id, root_ts),
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
        logger.warning("Slack reply not mirrored", channel_id=channel_id, reason=failure_reason(exc))
