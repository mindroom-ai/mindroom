"""Fakes shared by the Slack delivery and events route tests: Slack, the Matrix send seam, and agent bots."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx

from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.entity_resolution import entity_identity_registry
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.matrix.state import MatrixState
from mindroom.response_admission import ResponseAdmissionGate
from mindroom.slack.delivery import SlackRuntime
from mindroom.slack.oauth import slack_oauth_provider
from mindroom.slack.watches import SlackWatch, SlackWatchStore, watch_id_for
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
from tests.oauth_test_utils import publish_oauth_credentials

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    import nio

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

TEAM = "T0123ABCD"
BOT_USER = "U0BOT0000"
CHANNEL = "C0123ABCD"
DM = "D0123ABCD"
ROOT_TS = "1700000000.000100"
REPLY_TS = "1700000100.000200"
NOW = 1_700_000_500
WORKSPACE_URL = "https://example.slack.com/"
BOT_TOKEN = "xoxb-bot-token"  # noqa: S105
APP_TOKEN = "xapp-app-token"  # noqa: S105
SIGNING_SECRET = "signing-secret"  # noqa: S105
EVENT_CONTEXT = "4-event-context"
LAUNCH_ROOM = "!launch:example.org"
WATCH_ROOM = "!room:example.org"
ADA = "@ada:example.org"
CAROL = "@carol:example.org"
ALICE = "@alice:example.org"
BOB = "@bob:example.org"
SLACK_IDS = {"ada": "U0ADA0000", "carol": "U0CAROL00", "erin": "U0ERIN000", "dana": "U0DANA000", "alice": "U0ALICE00"}
AUTH_TEST = {"ok": True, "team_id": TEAM, "user_id": BOT_USER, "url": WORKSPACE_URL}
CANT_ANSWER = "MindRoom can't answer you here."
UNRECEIVED = "MindRoom couldn't receive your message; try again"

type Answer = dict[str, Any] | Callable[[httpx.Request], Awaitable[dict[str, Any]]]


def slack_user(name: str, **flags: object) -> dict[str, Any]:
    """Return what `users.info` says about one person of our team, whose email is `<name>@example.com`."""
    profile = {"display_name": name.title(), "email": f"{name}@example.com"}
    return {"id": SLACK_IDS[name], "team_id": TEAM, "name": name, "profile": profile, **flags}


@dataclass
class FakeSlack:
    """Answer Slack Web API methods by name, recording every request; an answer may be a coroutine.

    `users.info` answers from `users` unless `answers` has an entry for it.
    """

    answers: dict[str, Answer] = field(default_factory=dict)
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method = request.url.path.removeprefix("/api/")
        if method == "users.info" and method not in self.answers:
            user = self.users.get(form(request)["user"])
            return httpx.Response(
                200,
                json={"ok": True, "user": user} if user else {"ok": False, "error": "user_not_found"},
            )
        answer = self.answers.get(method, {"ok": True})
        return httpx.Response(200, json=answer if isinstance(answer, dict) else await answer(request))

    def client(self) -> httpx.AsyncClient:
        """Return an HTTP client whose Slack calls reach this fake."""
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle), follow_redirects=False, timeout=10.0)

    def calls(self, method: str) -> list[dict[str, str]]:
        """Return the form of every call of one method."""
        return [form(request) for request in self.requests if request.url.path == f"/api/{method}"]


def form(request: httpx.Request) -> dict[str, str]:
    """Return a Slack call's form fields."""
    return {key: values[0] for key, values in parse_qs(request.content.decode()).items()}


@dataclass
class FakeMatrix:
    """Stand in for `send_matrix_message`: record sends, fail the next `fail` of them, and hold each until `gate` opens."""

    sent: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail: int = 0
    gate: asyncio.Event | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)

    async def send(self, client: object, room_id: str, content: dict[str, Any]) -> DeliveredMatrixEvent | None:
        """Record one Matrix send, or fail it."""
        del client
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            self.fail -= 1
            return None
        self.sent.append((room_id, content))
        return DeliveredMatrixEvent(event_id=f"$sent{len(self.sent)}", content_sent=content)


@dataclass
class FakeBot:
    """The parts of a running agent bot that Slack delivery uses."""

    client: nio.AsyncClient | AsyncMock
    _conversation_reader: AsyncMock = field(default_factory=AsyncMock)
    running: bool = True
    admission_gate: ResponseAdmissionGate = field(default_factory=ResponseAdmissionGate)


def runtime_for(
    bots: Mapping[str, FakeBot],
    memberships: AgentReplyMembershipIndex,
    slack: FakeSlack,
) -> SlackRuntime:
    """Return a Slack runtime over fake bots whose Slack calls reach `slack`."""
    return SlackRuntime(cast("Mapping[str, AgentBot | TeamBot]", bots), memberships, http_client=slack.client)


def config_data(
    *,
    assistant_tools: tuple[str, ...] = ("slack",),
    launch_users: tuple[str, ...] = (ADA, CAROL),
) -> dict[str, object]:
    """Return a config with a project agent answering `CHANNEL` in `launch` and a personal agent for watches."""
    return {
        "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
        "router": {"model": "default"},
        "agents": {
            "launch": {
                "display_name": "Launch",
                "rooms": ["launch"],
                "access": {"users": list(launch_users), "members_of_rooms": []},
                "tools": [{"slack": {"channels": [CHANNEL], "room": "launch"}}],
            },
            "assistant": {"display_name": "Assistant", "tools": list(assistant_tools)},
            "other_agent": {"display_name": "Other"},
        },
        "rooms": {"launch": {"display_name": "Launch"}},
        "slack": {"email_domain": "example.com", "matrix_user_id_template": "@{localpart}:example.org"},
    }


def install_slack_state(runtime_paths: RuntimePaths, *, app_token: str | None = APP_TOKEN) -> None:
    """Store the Slack app and its OAuth client, and the `launch` room's Matrix ID."""
    manager = get_runtime_credentials_manager(runtime_paths)
    app = {"bot_token": BOT_TOKEN, "signing_secret": SIGNING_SECRET}
    manager.save_credentials("slack_app", app if app_token is None else {**app, "app_token": app_token})
    manager.save_credentials("slack_oauth_client", {"client_id": "slack-client", "client_secret": "slack-secret"})
    state = MatrixState.load(runtime_paths=runtime_paths)
    state.add_room("launch", LAUNCH_ROOM, "#launch:example.org", "Launch")
    state.save(runtime_paths=runtime_paths)


def entity_id(config: Config, runtime_paths: RuntimePaths, name: str) -> str:
    """Return an agent's Matrix user ID."""
    return entity_identity_registry(config, runtime_paths).current_id(name).full_id


def alice_watch(runtime_paths: RuntimePaths) -> SlackWatch:
    """Store and return Alice's watch with `assistant` in `WATCH_ROOM`."""
    watch = SlackWatch(
        watch_id=watch_id_for(ALICE, "assistant", WATCH_ROOM),
        owner_id=ALICE,
        agent_name="assistant",
        room_id=WATCH_ROOM,
        team_id=TEAM,
        slack_user_id=SLACK_IDS["alice"],
        mentions=True,
        direct_messages=True,
        channels=(),
        created_at=NOW - 100,
    )
    assert runtime_paths.control_state_root is not None
    SlackWatchStore(runtime_paths.control_state_root).put(watch)
    return watch


def connect_alice(runtime_paths: RuntimePaths, *, slack_user_id: str = SLACK_IDS["alice"]) -> None:
    """Store Alice's own Slack connection, as the `slack` toolkit reads it."""
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="assistant",
        requester_id=ALICE,
        room_id=WATCH_ROOM,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    publish_oauth_credentials(
        slack_oauth_provider(),
        {
            "token": "xoxp-alice-token",
            "client_id": "slack-client",
            "scopes": ["search:read"],
            "_source": "oauth",
            "_oauth_provider": "slack",
            "_oauth_claims": {"sub": slack_user_id, "team_id": TEAM},
            "_oauth_claims_verified": True,
        },
        credentials_manager=get_runtime_credentials_manager(runtime_paths),
        worker_target=resolve_worker_target("user", "assistant", execution_identity=identity),
    )


def event_payload(
    event_type: str = "app_mention",
    *,
    author: str = "ada",
    text: str | None = None,
    ts: str = ROOT_TS,
    thread_ts: str | None = None,
    channel: str = CHANNEL,
    channel_type: str = "channel",
    team: str = TEAM,
) -> dict[str, Any]:
    """Return an Events API envelope for one message by `author`; an app mention mentions the app by default."""
    event: dict[str, Any] = {
        "type": event_type,
        "user": SLACK_IDS[author],
        "channel": channel,
        "ts": ts,
        "text": text if text is not None else f"<@{BOT_USER}> what's blocking the beta?",
    }
    if event_type == "message":
        event["channel_type"] = channel_type
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    return {
        "type": "event_callback",
        "team_id": team,
        "event_id": f"Ev{event_type}{ts}",
        "event_context": EVENT_CONTEXT,
        "event": event,
    }


def signed_headers(body: bytes, *, timestamp: int, secret: str = SIGNING_SECRET) -> dict[str, str]:
    """Return the headers Slack signs a request body with."""
    base = b"v0:" + str(timestamp).encode() + b":" + body
    signature = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return {
        "X-Slack-Request-Timestamp": str(timestamp),
        "X-Slack-Signature": signature,
        "Content-Type": "application/json",
    }


def body_of(payload: object) -> bytes:
    """Return a JSON request body."""
    return json.dumps(payload).encode()
