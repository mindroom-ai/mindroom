"""Tests for posting an agent's final reply to a Slack project turn back into its Slack thread."""

# ruff: noqa: D102, D103

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import nio
import pytest
import pytest_asyncio
from structlog.testing import capture_logs

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.main import Config
from mindroom.conversation_resolver import ConversationResolver, ConversationResolverDeps
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.dispatch_source import HOOK_DISPATCH_SOURCE_KIND, MESSAGE_SOURCE_KIND
from mindroom.entity_resolution import entity_identity_registry
from mindroom.hooks import MessageEnvelope
from mindroom.ingress_validation import IngressValidator, IngressValidatorDeps
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget
from mindroom.slack import app as slack_app
from mindroom.slack import identity as slack_identity
from mindroom.slack import reply_mirror, turns
from mindroom.slack.app import load_slack_app
from mindroom.slack.delivery import deliver_slack_event
from mindroom.slack.reply_mirror import is_slack_project_turn, mirror_reply_to_slack
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    message_origin,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.runtime_fakes import ready_runtime
from tests.slack.delivery_fakes import (
    ADA,
    AUTH_TEST,
    BOT_TOKEN,
    CAROL,
    CHANNEL,
    LAUNCH_ROOM,
    ROOT_TS,
    FakeBot,
    FakeMatrix,
    FakeSlack,
    config_data,
    entity_id,
    event_payload,
    install_slack_state,
    runtime_for,
    slack_user,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths

# Who may wake a project agent is part of the turn these tests start.
pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

PRIVATE_CHANNEL = "G0123ABCD"
REPLY = "The beta is blocked on the docs review."
TOOL_MARKER = "🔧 `search` [1]"
BLOCK_LIMIT = 12_000


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


@dataclass
class World:
    """A runtime-bound config with its Slack app, in which `launch` answered a Slack mention that opened a thread."""

    config: Config
    paths: RuntimePaths
    slack: FakeSlack
    channel: str
    thread_root: str
    turn_content: dict[str, Any]

    def ingress_envelope(self) -> MessageEnvelope:
        """Return the envelope `launch` builds for the Matrix event delivery posted, as it does when it receives it."""
        launch_id = entity_identity_registry(self.config, self.paths).current_id("launch")
        event = nio.RoomMessageText.from_dict(
            {
                "event_id": self.thread_root,
                "sender": launch_id.full_id,
                "origin_server_ts": 1_700_000_000_000,
                "room_id": LAUNCH_ROOM,
                "type": "m.room.message",
                "content": self.turn_content,
            },
        )
        runtime = BotRuntimeState(orchestrator=ready_runtime(self.config, self.paths))
        validator = IngressValidator(
            IngressValidatorDeps(
                runtime=runtime,
                runtime_paths=self.paths,
                agent_name="launch",
                matrix_id=launch_id,
                turn_store=MagicMock(),
                turn_policy=MagicMock(),
                settle_ignored_sources=AsyncMock(),
            ),
        )
        resolver = ConversationResolver(
            ConversationResolverDeps(
                runtime=runtime,
                logger=get_logger(__name__),
                runtime_paths=self.paths,
                agent_name="launch",
                matrix_id=launch_id,
                conversation_reader=make_conversation_reader_mock(),
                relations=make_relation_lookup(),
            ),
        )
        target = resolver.build_message_target(
            room_id=LAUNCH_ROOM,
            thread_id=None,
            reply_to_event_id=event.event_id,
            event_source=event.source,
        )
        envelope = resolver.build_envelope(event=event, facts=validator.classify(event), target=target)
        assert envelope.requester_id == ADA
        return envelope

    def envelope(
        self,
        *,
        agent: str = "launch",
        human: bool = False,
        source_kind: str = HOOK_DISPATCH_SOURCE_KIND,
        room_id: str = LAUNCH_ROOM,
        thread_id: str | None = None,
        hook_source: str | None = None,
    ) -> MessageEnvelope:
        """Return the envelope of the agent's turn for that mention, with whatever a test changes.

        MindRoom's own `launch` account sent the turn, unless `human` says a person did.
        """
        sender_id = CAROL if human else entity_id(self.config, self.paths, "launch")
        return MessageEnvelope(
            source_event_id=self.thread_root,
            target=MessageTarget.resolve(room_id, thread_id or self.thread_root, self.thread_root),
            body="Slack message from Ada",
            attachment_ids=(),
            mentioned_agents=(agent,),
            agent_name=agent,
            origin=message_origin(
                sender_id=sender_id,
                requester_id=ADA,
                sender_entity_name=None if human else "launch",
                source_kind=source_kind,
                original_sender=ADA,
            ),
            hook_source=hook_source or f"slack/{self.channel}/{ROOT_TS}",
        )

    async def mirror(
        self,
        envelope: MessageEnvelope,
        text: str = REPLY,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        await mirror_reply_to_slack(
            envelope,
            text,
            config=self.config,
            runtime_paths=self.paths,
            credentials_manager=get_runtime_credentials_manager(self.paths),
            client_factory=client_factory or self.slack.client,
        )

    def posts(self) -> list[dict[str, str]]:
        return self.slack.calls("chat.postMessage")


@pytest.fixture(autouse=True)
def _empty_slack_caches() -> Iterator[None]:
    slack_app._IDENTITIES.clear()
    slack_identity._PROFILES.clear()
    slack_identity._CHANNELS.clear()
    yield
    slack_app._IDENTITIES.clear()
    slack_identity._PROFILES.clear()
    slack_identity._CHANNELS.clear()


async def _start_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, channel: str) -> World:
    """Deliver a Slack mention to `launch` for real, so its thread is bound the way delivery binds it."""
    data = config_data()
    data["agents"]["launch"]["tools"] = [{"slack": {"channels": [channel], "room": "launch"}}]  # type: ignore[index]
    config = bind_runtime_paths(Config.model_validate(data), test_runtime_paths(tmp_path))
    paths = runtime_paths_for(config)
    install_slack_state(paths)
    slack = FakeSlack(answers={"auth.test": AUTH_TEST}, users={"U0ADA0000": slack_user("ada")})
    matrix = FakeMatrix()
    monkeypatch.setattr(turns, "send_matrix_message", matrix.send)
    runtime = runtime_for({"launch": FakeBot(AsyncMock())}, AgentReplyMembershipIndex(), slack)
    manager = get_runtime_credentials_manager(paths)
    app = load_slack_app(manager)
    assert app is not None
    async with slack.client() as client:
        result = await deliver_slack_event(
            event_payload(channel=channel),
            runtime=runtime,
            config=config,
            runtime_paths=paths,
            credentials_manager=manager,
            app=app,
            client=client,
            now=int(time.time()),
        )
    assert result == "delivered"
    ((room_id, turn_content),) = matrix.sent
    assert room_id == LAUNCH_ROOM
    await wait_for_background_tasks(5.0, owner=runtime)
    slack.requests.clear()
    return World(config, paths, slack, channel, thread_root="$sent1", turn_content=turn_content)


@pytest_asyncio.fixture
async def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return await _start_thread(tmp_path, monkeypatch, CHANNEL)


@pytest.mark.asyncio
async def test_mirrors_final_reply_for_slack_turn(world: World) -> None:
    """The envelope is the one the agent builds from its own Slack post, so delivery and the mirror agree on the turn."""
    await world.mirror(world.ingress_envelope(), f"{TOOL_MARKER}\n\n{REPLY}")

    (post,) = world.posts()
    assert post == {
        "channel": CHANNEL,
        "thread_ts": ROOT_TS,
        "text": REPLY,
        "blocks": json.dumps([{"type": "markdown", "text": REPLY}]),
    }
    (request,) = [request for request in world.slack.requests if request.url.path == "/api/chat.postMessage"]
    assert request.headers["authorization"] == f"Bearer {BOT_TOKEN}"


@pytest.mark.asyncio
async def test_mirrors_g_prefixed_channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    private = await _start_thread(tmp_path, monkeypatch, PRIVATE_CHANNEL)

    await private.mirror(private.envelope())

    (post,) = private.posts()
    assert (post["channel"], post["thread_ts"]) == (PRIVATE_CHANNEL, ROOT_TS)


_FORGERIES: dict[str, dict[str, Any]] = {
    "a person who copies the hook source": {"human": True, "source_kind": MESSAGE_SOURCE_KIND},
    "a person who also claims a hook dispatch": {"human": True},
    "an ordinary message of a managed account": {"source_kind": MESSAGE_SOURCE_KIND},
    "a Slack thread nothing was delivered to": {"hook_source": f"slack/{CHANNEL}/1700000999.000100"},
    "a Matrix thread other than the bound one": {"thread_id": "$elsewhere"},
    "a room other than the bound one": {"room_id": "!elsewhere:example.org"},
    "a channel the agent does not answer": {"hook_source": f"slack/C0OTHER000/{ROOT_TS}"},
    "an agent that does not answer the channel": {"agent": "assistant"},
}


@pytest.mark.parametrize("forgery", _FORGERIES.values(), ids=_FORGERIES.keys())
@pytest.mark.asyncio
async def test_forged_hook_source_is_not_mirrored(world: World, forgery: dict[str, Any]) -> None:
    """Review Focus 3: only a trusted hook dispatch whose Slack thread is bound to this room and Matrix thread posts."""
    await world.mirror(world.envelope(**forgery))

    assert world.posts() == []


@pytest.mark.parametrize(
    "launch_tools",
    [
        [{"slack": {"channels": ["C0OTHER000"], "room": "launch"}}],
        [{"slack": {"channels": [CHANNEL], "room": "elsewhere"}}],
    ],
    ids=["channel dropped from the agent", "agent answers from another room"],
)
@pytest.mark.asyncio
async def test_revoked_access_is_not_mirrored(world: World, launch_tools: list[dict[str, Any]]) -> None:
    """A bound thread outlives a config change, but the reply follows the current config."""
    data = config_data()
    data["agents"]["launch"] |= {"tools": launch_tools, "rooms": ["launch", "elsewhere"]}  # type: ignore[index]
    data["rooms"]["elsewhere"] = {"display_name": "Elsewhere"}  # type: ignore[index]
    world.config = bind_runtime_paths(Config.model_validate(data), world.paths)

    await world.mirror(world.envelope())

    assert world.posts() == []


@pytest.mark.asyncio
async def test_expired_binding_is_not_mirrored(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Slack thread idle for more than a week is not bound any more, so its reply stays in Matrix."""
    week_later = time.time() + 8 * 86_400
    monkeypatch.setattr(reply_mirror, "time", SimpleNamespace(time=lambda: week_later))

    await world.mirror(world.envelope())

    assert world.posts() == []


@pytest.mark.parametrize(
    "hook_source",
    [
        "slack-watch/0123456789abcdef",
        "automation/exploration",
        f"slack/{CHANNEL}",
        f"slack/{CHANNEL}/{ROOT_TS}/x",
        f"slack/D0123ABCD/{ROOT_TS}",
    ],
)
@pytest.mark.asyncio
async def test_non_slack_turn_untouched(world: World, hook_source: str) -> None:
    for envelope in (world.envelope(hook_source=hook_source), replace(world.envelope(), hook_source=None)):
        await world.mirror(envelope)

    assert world.slack.requests == []


def test_only_a_trusted_hook_dispatch_naming_a_slack_thread_may_be_a_project_turn(world: World) -> None:
    """The cheap check that runs before any task starts: what the turn itself says, with no lookup."""
    assert is_slack_project_turn(world.envelope())
    assert is_slack_project_turn(world.ingress_envelope())
    assert not is_slack_project_turn(world.envelope(human=True))
    assert not is_slack_project_turn(world.envelope(source_kind=MESSAGE_SOURCE_KIND))
    assert not is_slack_project_turn(world.envelope(hook_source="slack-watch/0123456789abcdef"))
    assert not is_slack_project_turn(replace(world.envelope(), hook_source=None))


def _unreachable_slack() -> httpx.AsyncClient:
    def refuse(request: httpx.Request) -> httpx.Response:
        message = "no route"
        raise httpx.ConnectError(message, request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(refuse), follow_redirects=False, timeout=10.0)


@pytest.mark.parametrize("failure", ["channel_not_found", "ConnectError"])
@pytest.mark.asyncio
async def test_mirror_failure_is_logged_not_raised(world: World, failure: str) -> None:
    world.slack.answers["chat.postMessage"] = {"ok": False, "error": "channel_not_found"}
    client_factory = world.slack.client if failure == "channel_not_found" else _unreachable_slack

    with capture_logs() as logs:
        await world.mirror(world.envelope(), client_factory=client_factory)

    (entry,) = [entry for entry in logs if entry["log_level"] == "warning"]
    assert entry["channel_id"] == CHANNEL
    assert entry["reason"] == failure
    # Neither the reply nor the token reaches a log.
    assert REPLY not in repr(logs)
    assert BOT_TOKEN not in repr(logs)


@pytest.mark.asyncio
async def test_long_reply_truncated(world: World) -> None:
    await world.mirror(world.envelope(), "x" * (BLOCK_LIMIT + 5_000))

    (post,) = world.posts()
    (block,) = json.loads(post["blocks"])
    assert block == {"type": "markdown", "text": "x" * (BLOCK_LIMIT - 1) + "…"}
    assert post["text"] == block["text"]


@pytest.mark.asyncio
async def test_reply_cannot_notify_a_whole_channel(world: World) -> None:
    """Broadcast mentions in an agent's reply reach Slack defused, in the text and in the markdown block."""
    await world.mirror(world.envelope(), "<!channel> <!here> <!everyone> <!subteam^S0123ABCD> the beta is blocked")

    (post,) = world.posts()
    assert all("<!" not in value for value in post.values())
    (block,) = json.loads(post["blocks"])
    assert block["text"] == post["text"]
    assert post["text"].endswith(" the beta is blocked")


@pytest.mark.asyncio
async def test_broadcast_neutralizing_happens_before_truncation(world: World) -> None:
    """Each defused `<!` is one character longer, so a reply of broadcasts still fits the block."""
    await world.mirror(world.envelope(), "<!here>" * 3_000)

    (post,) = world.posts()
    assert "<!" not in post["text"]
    assert utf16_length(post["text"]) <= BLOCK_LIMIT


@pytest.mark.asyncio
async def test_long_emoji_reply_is_cut_by_utf16_length(world: World) -> None:
    """Slack counts UTF-16 code units, so an emoji outside the basic plane counts twice and is never split."""
    await world.mirror(world.envelope(), "😀" * 7_000)

    (post,) = world.posts()
    (block,) = json.loads(post["blocks"])
    assert block["text"] == "😀" * 5_999 + "…"
    assert utf16_length(block["text"]) == BLOCK_LIMIT - 1
    assert post["text"] == block["text"]


@pytest.mark.parametrize(("emoji", "kept"), [(6_000, 6_000), (6_001, 5_999)])
@pytest.mark.asyncio
async def test_emoji_reply_at_the_utf16_limit(world: World, emoji: int, kept: int) -> None:
    await world.mirror(world.envelope(), "😀" * emoji)

    (post,) = world.posts()
    assert post["text"] == "😀" * kept + ("" if kept == emoji else "…")


@pytest.mark.parametrize(
    ("reply", "posted"),
    [
        ("before \ud800 after", "before  after"),
        ("\udc00" + "x" * (BLOCK_LIMIT + 10), "x" * (BLOCK_LIMIT - 2) + "…"),
    ],
    ids=["short", "cut"],
)
@pytest.mark.asyncio
async def test_lone_surrogate_in_a_reply_is_dropped(world: World, reply: str, posted: str) -> None:
    """A model's reply can hold half of a surrogate pair, which Slack cannot take; the rest of the reply is posted."""
    await world.mirror(world.envelope(), reply)

    (post,) = world.posts()
    assert post["text"] == posted


@pytest.mark.asyncio
async def test_reply_at_the_limit_is_not_truncated(world: World) -> None:
    await world.mirror(world.envelope(), "y" * BLOCK_LIMIT)

    (post,) = world.posts()
    assert post["text"] == "y" * BLOCK_LIMIT


@pytest.mark.asyncio
async def test_reply_of_only_tool_markers_posts_nothing(world: World) -> None:
    await world.mirror(world.envelope(), f"{TOOL_MARKER}\n")

    assert world.posts() == []
