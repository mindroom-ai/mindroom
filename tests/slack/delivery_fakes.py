"""Fakes shared by the Slack delivery and events route tests: Slack, the Matrix send seam, and agent bots."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
from fastapi import FastAPI

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.api.app_state import ensure_app_state
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.main import Config
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.delivery_replay import DeliveryReplayStore
from mindroom.entity_resolution import entity_identity_registry
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.matrix.state import MatrixState
from mindroom.reactive_triggers import dispatch as reactive_dispatch
from mindroom.reactive_triggers.models import ExternalEventSourceSpec, ReactiveTriggerRecord, TriggerTargetSpec
from mindroom.reactive_triggers.service import ReactiveTriggerService
from mindroom.reactive_triggers.store import ReactiveTriggerStore
from mindroom.response_admission import ResponseAdmissionGate
from mindroom.slack import turns
from mindroom.slack.app import SlackAppCredentials, load_slack_app
from mindroom.slack.delivery import SlackRuntime, deliver_slack_event
from mindroom.slack.event_source import SLACK_EVENT_SOURCE_ID, SlackEventSource
from mindroom.slack.integration import SLACK_INTEGRATION, bound_slack_runtime
from mindroom.slack.oauth import slack_oauth_provider
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.matrix_state_helpers import save_matrix_state
from tests.oauth_test_utils import publish_oauth_credentials
from tests.slack.room_state import room_state_client

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from pathlib import Path

    import nio
    import pytest

    from mindroom.bot import AgentBot
    from mindroom.constants import RuntimePaths
    from mindroom.event_sources import EventSourceEmitter
    from mindroom.slack.turns import DeliveryResult

TEAM = "T0123ABCD"
BOT_USER = "U0BOT0000"
BOT_NAME = "mindroom"
CHANNEL = "C0123ABCD"
CHANNEL_NAME = "launch-eng"
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
AUTH_TEST = {"ok": True, "team_id": TEAM, "user_id": BOT_USER, "user": BOT_NAME, "url": WORKSPACE_URL}
CANT_ANSWER = "MindRoom can't answer you here."
UNRECEIVED = "MindRoom couldn't receive your message; try again"

type Answer = dict[str, Any] | Callable[[httpx.Request], Awaitable[dict[str, Any] | httpx.Response]]


def slack_user(name: str, **flags: object) -> dict[str, Any]:
    """Return what `users.info` says about one person of our team, whose email is `<name>@example.com`."""
    profile = {"display_name": name.title(), "email": f"{name}@example.com"}
    return {
        "id": SLACK_IDS[name],
        "team_id": TEAM,
        "name": name,
        "profile": profile,
        "is_email_confirmed": True,
        **flags,
    }


@dataclass
class FakeSlack:
    """Answer Slack Web API methods by name, recording every request.

    An answer may be a coroutine, which may answer a whole HTTP response.
    `users.info` answers from `users`, and `conversations.info` that the app is in the channel, unless `answers` has an
    entry for them.
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
        if method == "conversations.info" and method not in self.answers:
            channel = {"id": form(request)["channel"], "is_member": True}
            return httpx.Response(200, json={"ok": True, "channel": channel})
        answer = self.answers.get(method, {"ok": True})
        body = answer if isinstance(answer, dict) else await answer(request)
        return body if isinstance(body, httpx.Response) else httpx.Response(200, json=body)

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

    async def send(
        self,
        client: object,
        room_id: str,
        content: dict[str, Any],
        *,
        transaction_id: str | None = None,
    ) -> DeliveredMatrixEvent | None:
        """Record one Matrix send, or fail it."""
        del client, transaction_id
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            self.fail -= 1
            return None
        self.sent.append((room_id, content))
        return DeliveredMatrixEvent(event_id=f"$sent{len(self.sent)}", content_sent=content)


@dataclass
class FakeRuntime:
    """The runtime part Slack delivery reads: its response admission gate."""

    response_admission_gate: ResponseAdmissionGate = field(default_factory=ResponseAdmissionGate)


@dataclass
class FakeConversationReader:
    """Minimal conversation reader stub for reactive trigger dispatch."""

    latest_thread_event_id: AsyncMock = field(default_factory=lambda: AsyncMock(return_value="$latest"))


@dataclass
class FakeBot:
    """The parts of a running agent bot that Slack delivery uses."""

    client: nio.AsyncClient | AsyncMock
    latest_thread_event_id_if_needed: AsyncMock = field(default_factory=AsyncMock)
    running: bool = True
    orchestrator: FakeRuntime = field(default_factory=FakeRuntime)
    conversation_reader: FakeConversationReader = field(default_factory=FakeConversationReader)


def runtime_for(
    bots: Mapping[str, FakeBot],
    memberships: AgentReplyMembershipIndex,
    slack: FakeSlack,
    *,
    reactive_triggers: ReactiveTriggerService | None = None,
    event_source: SlackEventSource | None = None,
    event_source_emitter: EventSourceEmitter | None = None,
) -> SlackRuntime:
    """Return a Slack runtime over fake bots whose Slack calls reach `slack`."""
    return SlackRuntime(
        cast("Mapping[str, AgentBot]", bots),
        memberships,
        http_client=slack.client,
        reactive_triggers=reactive_triggers,
        event_source=event_source,
        event_source_emitter=event_source_emitter,
    )


@dataclass
class Harness:
    """A runtime-bound config, its Slack app, fake bots, and the fakes they reach."""

    config: Config
    paths: RuntimePaths
    slack: FakeSlack
    matrix: FakeMatrix
    bots: dict[str, FakeBot]
    memberships: AgentReplyMembershipIndex
    register_source: bool = True
    trigger_store: ReactiveTriggerStore = field(init=False)
    reactive_triggers: ReactiveTriggerService = field(init=False)
    runtime: SlackRuntime = field(init=False)

    def __post_init__(self) -> None:
        """Wire reactive trigger service and optional SlackEventSource registration."""
        self.trigger_store = ReactiveTriggerStore(self.paths)
        self.reactive_triggers = ReactiveTriggerService(
            config_provider=lambda: self.config,
            runtime_paths=self.paths,
            bots=lambda: cast("dict[str, AgentBot]", self.bots),
            memberships=self.memberships,
            store=self.trigger_store,
        )
        slack_source: SlackEventSource | None = None
        slack_emitter: EventSourceEmitter | None = None
        if self.register_source:
            slack_source = SlackEventSource()
            slack_emitter = self.reactive_triggers.event_sources.register(slack_source)
        self.runtime = runtime_for(
            self.bots,
            self.memberships,
            self.slack,
            reactive_triggers=self.reactive_triggers,
            event_source=slack_source,
            event_source_emitter=slack_emitter,
        )

    def bind_integration(self) -> tuple[FastAPI, SlackEventSource]:
        """Bind `SLACK_INTEGRATION` to a fresh FastAPI app and wire its source/emitter onto this harness."""
        app_api = FastAPI()
        ensure_app_state(app_api)
        SLACK_INTEGRATION.bind(
            app_api,
            cast("dict[str, AgentBot]", self.bots),
            self.memberships,
            self.reactive_triggers,
        )
        bound = bound_slack_runtime(app_api)
        assert bound is not None
        registered = self.reactive_triggers.event_sources.get(SLACK_EVENT_SOURCE_ID)
        assert isinstance(registered, SlackEventSource)
        self.runtime = runtime_for(
            self.bots,
            self.memberships,
            self.slack,
            reactive_triggers=self.reactive_triggers,
            event_source=bound.event_source,
            event_source_emitter=bound.event_source_emitter,
        )
        return app_api, registered

    async def drain_triggers(self) -> None:
        """Drain queued reactive trigger firings through ReactiveTriggerService."""
        await self.reactive_triggers.drain_due_firings_once()

    async def deliver(
        self,
        payload: dict[str, Any],
        *,
        now: int = NOW,
        app: SlackAppCredentials | None = None,
    ) -> DeliveryResult:
        """Deliver as the route does, with the Slack app it loaded for this event."""
        manager = get_runtime_credentials_manager(self.paths)
        loaded = app or load_slack_app(manager)
        assert loaded is not None
        async with self.slack.client() as client:
            return await deliver_slack_event(
                payload,
                runtime=self.runtime,
                config=self.config,
                runtime_paths=self.paths,
                app=loaded,
                client=client,
                now=now,
            )

    async def settle(self) -> None:
        """Wait for the reactions and notices delivery started in the background."""
        assert await wait_for_background_tasks(5.0, owner=self.runtime)

    def store(self) -> DeliveryReplayStore:
        """Return the Slack delivery replay store for this harness."""
        assert self.paths.control_state_root is not None
        return DeliveryReplayStore(self.paths.control_state_root / "slack" / "replay")

    def agent_id(self, name: str) -> str:
        """Return an agent Matrix user ID."""
        return entity_id(self.config, self.paths, name)

    def bearer(self, method: str) -> list[str]:
        """Return bearer tokens sent to one Slack Web API method."""
        return [
            request.headers["authorization"].removeprefix("Bearer ")
            for request in self.slack.requests
            if request.url.path == f"/api/{method}"
        ]


def make_delivery_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config: Config | None = None,
    slack_answers: dict[str, Answer] | None = None,
    register_source: bool = True,
    connect_alice_oauth: bool = False,
) -> Harness:
    """Build a ready-to-deliver Slack Harness with monkeypatched Matrix sends."""
    bound_config = (
        config
        if config is not None
        else bind_runtime_paths(Config.model_validate(config_data()), test_runtime_paths(tmp_path))
    )
    paths = runtime_paths_for(bound_config)
    install_slack_state(paths)
    if connect_alice_oauth:
        connect_alice(paths)
    answers: dict[str, Answer] = {
        "auth.test": AUTH_TEST,
        "conversations.info": {"ok": True, "channel": {"id": CHANNEL, "name": CHANNEL_NAME, "is_member": True}},
        "apps.event.authorizations.list": {
            "ok": True,
            "authorizations": [{"user_id": SLACK_IDS["alice"], "team_id": TEAM, "is_bot": False}],
        },
        **(slack_answers or {}),
    }
    slack = FakeSlack(
        answers=answers,
        users={
            **{SLACK_IDS[name]: slack_user(name) for name in ("ada", "carol", "dana", "alice")},
            SLACK_IDS["erin"]: slack_user("erin", is_restricted=True),
        },
    )
    matrix = FakeMatrix()
    monkeypatch.setattr(turns, "send_matrix_message", matrix.send)
    monkeypatch.setattr(reactive_dispatch, "send_matrix_message", matrix.send)
    assistant_id = entity_identity_registry(bound_config, paths).current_id("assistant").full_id
    bots: dict[str, FakeBot] = {
        "assistant": FakeBot(
            room_state_client([ALICE, assistant_id], room_id=WATCH_ROOM),
            AsyncMock(return_value="$latest"),
        ),
    }
    if "launch" in bound_config.agents:
        bots["launch"] = FakeBot(AsyncMock(), AsyncMock(return_value="$latest"))
    return Harness(
        config=bound_config,
        paths=paths,
        slack=slack,
        matrix=matrix,
        bots=bots,
        memberships=AgentReplyMembershipIndex(),
        register_source=register_source,
    )


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
            "assistant": {
                "display_name": "Assistant",
                "access": {"users": [ALICE], "members_of_rooms": []},
                "tools": list(assistant_tools),
            },
            "other_agent": {"display_name": "Other"},
        },
        "rooms": {"launch": {"display_name": "Launch"}},
        "external_trigger_policy": {"enabled": True, "reactive_enabled": True},
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
    save_matrix_state(state, runtime_paths)


def entity_id(config: Config, runtime_paths: RuntimePaths, name: str) -> str:
    """Return an agent's Matrix user ID."""
    return entity_identity_registry(config, runtime_paths).current_id(name).full_id


def alice_slack_record(
    *,
    trigger_id: str = "slack_watch_alice",
    kind: str = "message",
    match: dict[str, str | bool | int | list[str]] | None = None,
    binding: dict[str, str | bool | int] | None = None,
    room_id: str = WATCH_ROOM,
    agent_name: str = "assistant",
    owner_user_id: str = ALICE,
    slack_user_id: str = SLACK_IDS["alice"],
    team_id: str = TEAM,
) -> ReactiveTriggerRecord:
    """Build (without storing) Alice's reactive Slack trigger record."""
    now = time.time()
    return ReactiveTriggerRecord(
        trigger_id=trigger_id,
        owner_user_id=owner_user_id,
        description="Alice Slack watch",
        source=ExternalEventSourceSpec(
            source_id=SLACK_EVENT_SOURCE_ID,
            kind=kind,
            match=match or {},
            binding={"team_id": team_id, "slack_user_id": slack_user_id} if binding is None else binding,
            source_origin="registered",
        ),
        target=TriggerTargetSpec(
            agent=agent_name,
            room_id=room_id,
            new_thread=True,
            thread_key_from_event=True,
            reopen_on_fire=True,
            prompt="Summarize this Slack message and ask if I want to reply.",
        ),
        once=False,
        max_fires_per_hour=60,
        created_at=now,
        updated_at=now,
    )


def alice_slack_trigger(runtime_paths: RuntimePaths, **kwargs: Any) -> ReactiveTriggerRecord:  # noqa: ANN401
    """Store and return Alice's reactive Slack trigger built via `alice_slack_record(**kwargs)`."""
    record = alice_slack_record(**kwargs)
    ReactiveTriggerStore(runtime_paths).create_trigger(record)
    return record


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
