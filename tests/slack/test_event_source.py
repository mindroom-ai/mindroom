"""Tests for SlackEventSource, watch migration, privacy, and retired slack_watch config validation."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.api.app_state import ensure_app_state
from mindroom.config.main import Config
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.custom_tools.triggers import TriggersTools
from mindroom.delivery_replay import DeliveryReplayStore, DeliveryReplayStoreError
from mindroom.entity_resolution import entity_identity_registry
from mindroom.event_sources import EventSourceContext, EventSourceEmitter
from mindroom.message_target import MessageTarget
from mindroom.oauth.providers import OAuthConnectionRequired
from mindroom.reactive_triggers import dispatch as reactive_dispatch
from mindroom.reactive_triggers.models import ExternalEventSourceSpec, TriggerEvent
from mindroom.reactive_triggers.service import ReactiveTriggerService
from mindroom.reactive_triggers.store import ReactiveTriggerStore, ReactiveTriggerStoreError
from mindroom.slack.event_source import SLACK_EVENT_SOURCE_ID, SlackEventSource, _watch_room_problem_from_state
from mindroom.slack.integration import SLACK_INTEGRATION, bound_slack_runtime
from mindroom.slack.legacy_watch_migration import migrate_legacy_slack_watches
from mindroom.slack.oauth import slack_oauth_provider
from mindroom.tool_system.catalog import ToolConfigOverrideError
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
from tests.api.test_room_triggers import ROOM_A
from tests.api.test_room_triggers import _Harness as RoomTriggersHarness
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.oauth_test_utils import publish_oauth_credentials
from tests.slack.delivery_fakes import (
    ALICE,
    APP_TOKEN,
    BOB,
    CHANNEL,
    DM,
    NOW,
    REPLY_TS,
    ROOT_TS,
    SLACK_IDS,
    TEAM,
    WATCH_ROOM,
    alice_slack_record,
    alice_slack_trigger,
    connect_alice,
    event_payload,
    install_slack_state,
    make_delivery_harness,
)
from tests.slack.room_state import room_state_client, room_state_events, room_state_snapshot

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

ALICE_BRIDGE = "@telegram_alice:example.org"
ADMIN = "@admin:example.org"
SHARED_ROOM = "!shared:example.org"
ADMIN_ROOM = "!admin:example.org"
OTHER_CHANNEL = "C0456EFGH"
LATER_JOINERS = "people who join this room later could read earlier messages"
_ALICE_SLACK = SLACK_IDS["alice"]
_AUD = frozenset({_ALICE_SLACK})
_BOTH_CH = [CHANNEL, OTHER_CHANNEL]


def _make_config(
    tmp_path: Path,
    *,
    assistant_tools: list[Any] | None = None,
    reactive_enabled: bool = True,
    max_fires_per_hour: int = 60,
    event_sources: dict[str, Any] | None = None,
) -> tuple[Config, RuntimePaths]:
    data: dict[str, Any] = {
        "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
        "router": {"model": "default"},
        "authorization": {"aliases": {ALICE: [ALICE_BRIDGE]}},
        "administrators": [ADMIN],
        "agents": {
            "assistant": {
                "display_name": "Assistant",
                "rooms": [WATCH_ROOM, SHARED_ROOM, ADMIN_ROOM],
                "access": {"users": [ALICE, ADMIN], "members_of_rooms": []},
                "tools": ["slack", "triggers"] if assistant_tools is None else assistant_tools,
            },
            "project_only": {"display_name": "Project", "tools": [{"slack": {"channels": [CHANNEL]}}]},
        },
        "external_trigger_policy": {
            "enabled": True,
            "reactive_enabled": reactive_enabled,
            "max_fires_per_hour": max_fires_per_hour,
            **({"event_sources": event_sources} if event_sources else {}),
        },
        "slack": {"email_domain": "example.com", "matrix_user_id_template": "@{localpart}:example.org"},
    }
    config = bind_runtime_paths(Config.model_validate(data), test_runtime_paths(tmp_path))
    return config, runtime_paths_for(config)


def _multi_room_client(room_clients: dict[str, AsyncMock]) -> AsyncMock:
    client = AsyncMock()
    client.rooms = {rid: next(iter(rc.rooms.values())) for rid, rc in room_clients.items()}

    async def _get_state(room_id: str) -> object:
        return await room_clients[room_id].room_get_state(room_id) if room_id in room_clients else None

    client.room_get_state.side_effect = _get_state
    return client


@dataclass
class _SlackEnv:
    config: Config
    paths: RuntimePaths
    store: ReactiveTriggerStore
    service: ReactiveTriggerService
    emitter: EventSourceEmitter | None
    client: AsyncMock
    tool: TriggersTools
    agent_id: str

    def ctx(
        self,
        *,
        room_id: str = WATCH_ROOM,
        requester_id: str = ALICE,
        thread_id: str | None = None,
        client: AsyncMock | None = None,
    ) -> ToolRuntimeContext:
        active_client = client or self.client
        bot = SimpleNamespace(
            client=active_client,
            running=True,
            agent_name="assistant",
            conversation_reader=SimpleNamespace(latest_thread_event_id=AsyncMock(return_value="$latest")),
        )
        orchestrator = SimpleNamespace(
            reactive_triggers=self.service,
            agent_bots={"assistant": bot},
            agent_reply_memberships=AgentReplyMembershipIndex(),
        )
        return ToolRuntimeContext(
            agent_name="assistant",
            target=MessageTarget.resolve(room_id, thread_id, "$event", room_mode=thread_id is None),
            requester_id=requester_id,
            client=active_client,
            config=self.config,
            runtime_paths=self.paths,
            plugins=config_plugins(self.config, self.paths),
            conversation_reader=MagicMock(),
            relations=MagicMock(),
            orchestrator=orchestrator,  # type: ignore[arg-type]
        )

    async def call(
        self,
        action: str,
        *,
        room_id: str = WATCH_ROOM,
        requester_id: str = ALICE,
        thread_id: str | None = None,
        client: AsyncMock | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> dict[str, Any]:
        with tool_runtime_context(
            self.ctx(room_id=room_id, requester_id=requester_id, thread_id=thread_id, client=client),
        ):
            return json.loads(await self.tool.triggers(action=action, **kwargs))

    async def admin_call(self, action: str, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        return await self.call(action, room_id=ADMIN_ROOM, requester_id=ADMIN, **kwargs)

    async def subscribe(
        self,
        *,
        thread_id: str | None = None,
        kind: str = "message",
        prompt: str = "Summarize Slack message.",
        **kwargs: Any,  # noqa: ANN401
    ) -> dict[str, Any]:
        return await self.call(
            "subscribe_event",
            thread_id=thread_id,
            source_id="slack",
            kind=kind,
            prompt=prompt,
            **kwargs,
        )


def _slack_env(
    tmp_path: Path,
    *,
    install_app: bool = True,
    connect: bool = True,
    register: bool = True,
    max_fires_per_hour: int = 60,
    event_sources: dict[str, Any] | None = None,
) -> _SlackEnv:
    config, paths = _make_config(tmp_path, max_fires_per_hour=max_fires_per_hour, event_sources=event_sources)
    if install_app:
        install_slack_state(paths, app_token=APP_TOKEN)
    if connect:
        connect_alice(paths)
    agent_id = entity_identity_registry(config, paths).current_id("assistant").full_id
    store = ReactiveTriggerStore(paths)
    service = ReactiveTriggerService(
        config_provider=lambda: config,
        runtime_paths=paths,
        bots=dict,
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )
    emitter = service.event_sources.register(SlackEventSource()) if register else None
    client = _multi_room_client(
        {
            WATCH_ROOM: room_state_client((ALICE, agent_id), room_id=WATCH_ROOM),
            SHARED_ROOM: room_state_client((ALICE, BOB, agent_id), room_id=SHARED_ROOM),
            ADMIN_ROOM: room_state_client((ADMIN, agent_id), room_id=ADMIN_ROOM),
        },
    )
    return _SlackEnv(
        config=config,
        paths=paths,
        store=store,
        service=service,
        emitter=emitter,
        client=client,
        tool=TriggersTools(),
        agent_id=agent_id,
    )


def _slack_event(**data: object) -> TriggerEvent:
    base = {
        "team_id": TEAM,
        "channel_id": CHANNEL,
        "channel_type": "channel",
        "user_id": SLACK_IDS["dana"],
        "is_dm": False,
        "mentioned_user_ids": [],
        **data,
    }
    return TriggerEvent(
        source_type="external_event",
        source_id=SLACK_EVENT_SOURCE_ID,
        kind="direct_message" if base["is_dm"] else "message",
        event_id=f"{base['team_id']}:{base['channel_id']}:1.0",
        timestamp=float(NOW),
        actor=str(base["user_id"]),
        text="hello",
        data=base,
    )


def _legacy_watch(watch_id: str = "w1", **overrides: object) -> dict[str, Any]:
    return {
        "watch_id": watch_id,
        "owner_id": ALICE,
        "agent_name": "assistant",
        "room_id": WATCH_ROOM,
        "team_id": TEAM,
        "slack_user_id": _ALICE_SLACK,
        "mentions": True,
        "direct_messages": True,
        "channels": [],
        "created_at": NOW,
        **overrides,
    }


def _write_watches(paths: RuntimePaths, *watches: dict[str, Any]) -> Path:
    assert paths.control_state_root is not None
    watches_path = paths.control_state_root / "slack" / "watches.json"
    watches_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"watches": {watch["watch_id"]: watch for watch in watches}}
    watches_path.write_text(json.dumps(payload), encoding="utf-8")
    return watches_path


@pytest.mark.asyncio
async def test_describe_for_user_requires_app_token_and_personal_slack(tmp_path: Path) -> None:
    """SlackEventSource is advertised only when app_token and a personal Slack agent exist."""
    config, paths = _make_config(tmp_path)
    source = SlackEventSource()
    ctx = EventSourceContext(owner_user_id=ALICE, config=config, runtime_paths=paths, target_agent="assistant")

    assert await source.describe_for_user(ctx) is None
    install_slack_state(paths, app_token=None)
    assert await source.describe_for_user(ctx) is None

    install_slack_state(paths, app_token=APP_TOKEN)
    desc = await source.describe_for_user(ctx)
    assert desc is not None
    assert (desc.source_id, desc.allowed_kinds, desc.match_fields) == (
        "slack",
        ("message", "mention", "direct_message"),
        ("channel_id",),
    )
    assert (
        desc.default_once,
        desc.default_new_thread,
        desc.default_thread_key_from_event,
        desc.default_reopen_on_fire,
        desc.private_room_only,
        desc.default_max_fires_per_hour,
    ) == (False, True, True, True, True, 60)

    assert await source.describe_for_user(replace(ctx, target_agent="project_only")) is None

    no_personal_cfg, no_personal_paths = _make_config(tmp_path / "np", assistant_tools=["triggers"])
    install_slack_state(no_personal_paths, app_token=APP_TOKEN)
    no_personal_ctx = EventSourceContext(
        owner_user_id=ALICE,
        config=no_personal_cfg,
        runtime_paths=no_personal_paths,
    )
    assert await source.describe_for_user(no_personal_ctx) is None


def test_watch_room_problem_from_state_enforces_privacy_invariants(tmp_path: Path) -> None:
    """Private rooms allow only the owner (or alias) and MindRoom entities with invited/joined history visibility."""
    config, paths = _make_config(tmp_path)
    agent_id = entity_identity_registry(config, paths).current_id("assistant").full_id
    router_id = entity_identity_registry(config, paths).current_id("router").full_id
    snap = room_state_snapshot
    cases = [
        (None, "room members could not be checked"),
        (snap((ALICE, agent_id, router_id), departed=(BOB,)), None),
        (snap((ALICE_BRIDGE, agent_id), history_visibility="joined"), None),
        (snap((agent_id,)), "you are not in this room"),
        (snap((ALICE, agent_id, BOB)), "another person is in this room"),
        (snap((ALICE, agent_id), invited=(BOB,)), "another person is in this room"),
        (snap((ALICE, agent_id, "@slackbot:example.org")), "another person is in this room"),
        *(
            (snap((ALICE, agent_id), history_visibility=vis), LATER_JOINERS)
            for vis in ("shared", "world_readable", None)
        ),
    ]
    for state, expected in cases:
        assert _watch_room_problem_from_state(state, ALICE, config, paths) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("install_app", "app_token", "target_agent", "kind", "match", "shared_room", "error_match"),
    [
        (False, None, "assistant", "message", {}, False, "not configured"),
        (True, None, "assistant", "message", {}, False, "app-level token"),
        (True, APP_TOKEN, "project_only", "message", {}, False, "personal Slack"),
        (True, APP_TOKEN, "assistant", "reaction", {}, False, "Unsupported Slack event kind"),
        (True, APP_TOKEN, "assistant", "message", {"repo": "x"}, False, "Unsupported Slack match field"),
        (
            True,
            APP_TOKEN,
            "assistant",
            "direct_message",
            {"channel_id": CHANNEL},
            False,
            "Cannot filter direct_message",
        ),
        (True, APP_TOKEN, "assistant", "message", {"channel_id": []}, False, "non-empty"),
        (True, APP_TOKEN, "assistant", "message", {"channel_id": _ALICE_SLACK}, False, "conversation"),
        (True, APP_TOKEN, "assistant", "message", {}, True, "Cannot watch Slack here: another person is in this room"),
    ],
)
async def test_validate_subscription_rejects_invalid_inputs(
    tmp_path: Path,
    install_app: bool,
    app_token: str | None,
    target_agent: str,
    kind: str,
    match: dict[str, Any],
    shared_room: bool,
    error_match: str,
) -> None:
    """Validation rejects unconfigured Slack, non-personal agents, invalid kinds/matches, and shared rooms."""
    config, paths = _make_config(tmp_path)
    if install_app:
        install_slack_state(paths, app_token=app_token)
    if shared_room:
        connect_alice(paths)
    agent_id = entity_identity_registry(config, paths).current_id("assistant").full_id
    members = (ALICE, agent_id, BOB) if shared_room else (ALICE, agent_id)
    ctx = EventSourceContext(
        owner_user_id=ALICE,
        config=config,
        runtime_paths=paths,
        target_room_id=WATCH_ROOM,
        target_agent=target_agent,
        target_room_state=room_state_snapshot(members),
    )
    with pytest.raises(ValueError, match=error_match):
        await SlackEventSource().validate_subscription(ctx, kind=kind, match=match)


@pytest.mark.asyncio
async def test_validate_subscription_oauth_normalization_and_immutable_binding(tmp_path: Path) -> None:
    """Validation enforces verified OAuth, normalizes channel_id targets, preserves binding, and rejects account swaps."""
    config, paths = _make_config(tmp_path)
    install_slack_state(paths, app_token=APP_TOKEN)
    source = SlackEventSource()
    agent_id = entity_identity_registry(config, paths).current_id("assistant").full_id
    ctx = EventSourceContext(
        owner_user_id=ALICE,
        config=config,
        runtime_paths=paths,
        target_room_id=WATCH_ROOM,
        target_agent="assistant",
        target_room_state=room_state_snapshot((ALICE, agent_id)),
    )

    with pytest.raises(OAuthConnectionRequired):
        await source.validate_subscription(ctx, kind="message", match={})

    publish_oauth_credentials(
        slack_oauth_provider(),
        {"token": "xoxp-unverified", "_source": "oauth", "_oauth_claims": {"sub": _ALICE_SLACK, "team_id": TEAM}},
        credentials_manager=get_runtime_credentials_manager(paths),
        worker_target=resolve_worker_target(
            "user",
            "assistant",
            execution_identity=ToolExecutionIdentity("matrix", "assistant", ALICE, None, None, None, None),
        ),
    )
    with pytest.raises(OAuthConnectionRequired):
        await source.validate_subscription(ctx, kind="message", match={})

    connect_alice(paths)
    expected_binding = {"team_id": TEAM, "slack_user_id": _ALICE_SLACK}
    single = await source.validate_subscription(ctx, kind="message", match={"channel_id": f"{CHANNEL}:{ROOT_TS}"})
    assert (single.match, single.binding) == ({"channel_id": CHANNEL}, expected_binding)

    multi = await source.validate_subscription(
        ctx,
        kind="mention",
        match={"channel_id": [CHANNEL, OTHER_CHANNEL, CHANNEL]},
        existing_binding=expected_binding,
    )
    assert (multi.match, multi.binding) == ({"channel_id": _BOTH_CH}, expected_binding)

    connect_alice(paths, slack_user_id="U0OTHER99")
    with pytest.raises(ValueError, match="Slack account changed; recreate this subscription"):
        await source.validate_subscription(
            ctx,
            kind="mention",
            match={"channel_id": CHANNEL},
            existing_binding=expected_binding,
        )


def test_dry_run_audience_returns_bound_slack_user() -> None:
    """dry_run_audience extracts the rule's bound Slack user ID."""
    assert SlackEventSource().dry_run_audience(alice_slack_record()) == _AUD


@pytest.mark.parametrize(
    ("kind", "match", "event_data", "audience", "has_thread_binding", "expected"),
    [
        # Empty / missing audience NEVER matches
        ("message", {}, {"is_dm": True}, None, False, False),
        ("message", {}, {"is_dm": True}, frozenset(), False, False),
        # Wrong team or self-authored ALWAYS rejected
        ("message", {}, {"team_id": "T0OTHER", "is_dm": True}, _AUD, False, False),
        ("message", {}, {"user_id": _ALICE_SLACK, "is_dm": True}, _AUD, True, False),
        ("message", {}, {"user_id": _ALICE_SLACK, "mentioned_user_ids": [_ALICE_SLACK]}, _AUD, True, False),
        # DMs and MPIMs
        ("message", {}, {"is_dm": True, "channel_id": DM, "channel_type": "im"}, _AUD, False, True),
        ("direct_message", {}, {"is_dm": True, "channel_id": DM, "channel_type": "mpim"}, _AUD, False, True),
        ("mention", {}, {"is_dm": True, "channel_id": DM}, _AUD, True, False),
        ("mention", {}, {"is_dm": True, "channel_id": DM, "mentioned_user_ids": [_ALICE_SLACK]}, _AUD, False, True),
        # Channel filter applies ONLY to channel messages, never DMs
        ("message", {"channel_id": _BOTH_CH}, {"is_dm": True, "channel_id": DM}, _AUD, False, True),
        (
            "message",
            {"channel_id": _BOTH_CH},
            {"channel_id": OTHER_CHANNEL, "mentioned_user_ids": [_ALICE_SLACK]},
            _AUD,
            False,
            True,
        ),
        (
            "message",
            {"channel_id": _BOTH_CH},
            {"channel_id": "C0UNLISTED", "mentioned_user_ids": [_ALICE_SLACK]},
            _AUD,
            True,
            False,
        ),
        # Mentions & bound thread replies vs unbound channel firehose
        ("message", {}, {"mentioned_user_ids": []}, _AUD, False, False),
        ("message", {}, {"mentioned_user_ids": []}, _AUD, True, False),
        ("message", {}, {"mentioned_user_ids": [], "thread_ts": ROOT_TS}, _AUD, True, True),
        ("mention", {}, {"mentioned_user_ids": [_ALICE_SLACK]}, _AUD, False, True),
        ("direct_message", {}, {"mentioned_user_ids": [_ALICE_SLACK]}, _AUD, True, False),
        # Visibility audience isolation
        ("message", {}, {"mentioned_user_ids": [_ALICE_SLACK]}, frozenset({"U0SOMEONE"}), False, False),
    ],
)
def test_matches_rule_enforces_audience_kinds_channels_and_self_exclusion(
    kind: str,
    match: dict[str, Any],
    event_data: dict[str, Any],
    audience: object,
    has_thread_binding: bool,
    expected: bool,
) -> None:
    """matches_rule enforces audience isolation, kinds, channel filters, bound threads, and no self-triggering."""
    rule = alice_slack_record(kind=kind, match=match)
    assert (
        SlackEventSource().matches_rule(
            rule,
            _slack_event(**event_data),
            audience=audience,
            has_thread_binding=has_thread_binding,
        )
        is expected
    )


@pytest.mark.asyncio
async def test_triggers_tool_subscribe_update_and_immutable_binding_review_focus_2(tmp_path: Path) -> None:
    """Review Focus #2: subscribe binds verified OAuth identity, update preserves it, and binding never leaks."""
    env = _slack_env(tmp_path, connect=False)

    unconnected = await env.subscribe(prompt="Summarize.")
    assert (unconnected["status"], unconnected["oauth_connection_required"]) == ("error", True)

    connect_alice(env.paths)
    created = await env.subscribe(match={"channel_id": [CHANNEL, OTHER_CHANNEL, CHANNEL]})
    assert created["status"] == "ok"
    tid = created["trigger"]["trigger_id"]
    assert "binding" not in json.dumps(created)
    assert created["trigger"]["source"]["match"] == {"channel_id": _BOTH_CH}
    target = created["trigger"]["target"]
    assert (
        created["trigger"]["once"],
        target["new_thread"],
        target["thread_key_from_event"],
        target["reopen_on_fire"],
    ) == (False, True, True, True)

    expected_binding = {"team_id": TEAM, "slack_user_id": _ALICE_SLACK}
    stored = env.store.get_trigger(tid)
    assert stored is not None
    assert isinstance(stored.source, ExternalEventSourceSpec)
    assert stored.source.binding == expected_binding

    updated = await env.call("update", trigger_id=tid, match={"channel_id": CHANNEL})
    assert updated["status"] == "ok"
    assert "binding" not in json.dumps(updated)
    reloaded = env.store.get_trigger(tid)
    assert reloaded is not None
    assert isinstance(reloaded.source, ExternalEventSourceSpec)
    assert (reloaded.source.match, reloaded.source.binding) == ({"channel_id": CHANNEL}, expected_binding)

    # Account change on update is refused and leaves both match and binding intact in SQLite.
    connect_alice(env.paths, slack_user_id="U0OTHER99")
    rejected = await env.call("update", trigger_id=tid, match={"channel_id": OTHER_CHANNEL})
    assert rejected["status"] == "error"
    assert "Slack account changed" in rejected["message"]
    after_reject = env.store.get_trigger(tid)
    assert after_reject is not None
    assert isinstance(after_reject.source, ExternalEventSourceSpec)
    assert (after_reject.source.match, after_reject.source.binding) == ({"channel_id": CHANNEL}, expected_binding)


@pytest.mark.asyncio
async def test_triggers_tool_history_and_paused_reason_review_focus_3(tmp_path: Path) -> None:
    """Review Focus #3: private history gating, non-owner hiding, and live paused_reason reporting."""
    env = _slack_env(tmp_path)
    rule = alice_slack_trigger(env.paths)
    assert env.store.enqueue_firing(
        rule.trigger_id,
        "ev-priv-1",
        _slack_event(is_dm=True, channel_id=DM, channel_type="im"),
        expected_rule_uid=rule.rule_uid,
    )
    fid = env.store.list_firings(trigger_id=rule.trigger_id)[0].id
    assert env.store.claim_firing(fid, now=time.time(), triage_reason="Secret deal discussion") == "claimed"

    # Owner in private room sees event + triage_reason; shared room is refused; non-owner admin sees neither.
    priv_hist = await env.call("history", trigger_id=rule.trigger_id)
    assert priv_hist["status"] == "ok"
    assert priv_hist["firings"][0]["triage_reason"] == "Secret deal discussion"
    assert priv_hist["firings"][0]["event"]["text"] == "hello"

    shared_hist = await env.call("history", room_id=SHARED_ROOM, trigger_id=rule.trigger_id)
    assert shared_hist["status"] == "error"

    admin_list = await env.admin_call("list")
    admin_hist_scoped = await env.admin_call("history", trigger_id=rule.trigger_id)
    admin_hist_unscoped = await env.admin_call("history")
    assert admin_list["reactive_triggers"] == []
    assert rule.trigger_id not in json.dumps(admin_list)
    assert "paused_reason" not in json.dumps(admin_list)
    assert (admin_hist_scoped["status"], admin_hist_unscoped["count"]) == ("error", 0)
    assert "Secret deal" not in json.dumps(admin_hist_unscoped)

    # Inviting another human surfaces paused_reason in triggers(action="list").
    invited_client = _multi_room_client(
        {WATCH_ROOM: room_state_client((ALICE, env.agent_id), invited=(BOB,), room_id=WATCH_ROOM)},
    )
    listed = await env.call("list", client=invited_client)
    row = next(r for r in listed["triggers"] if r["trigger_id"] == rule.trigger_id)
    assert row["paused_reason"] == "another person is in this room"


@pytest.mark.asyncio
async def test_emit_prefilter_pagination_reopen_and_fail_soft_lookups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """emit_slack_watch_event prefilters before Slack API calls, paginates & filters bots, and reopens resolved threads."""
    config, _ = _make_config(tmp_path)
    reopened: list[tuple[str, str]] = []

    async def _fake_reopen(_client: object, room_id: str, thread_id: str, **_kwargs: object) -> bool:
        reopened.append((room_id, thread_id))
        return True

    monkeypatch.setattr(reactive_dispatch, "reopen_thread_if_resolved", _fake_reopen)

    pages = [
        {
            "ok": True,
            "authorizations": [
                {"user_id": "U0BOTONLY", "is_bot": True},
                {"user_id": _ALICE_SLACK, "is_bot": True},
                {"user_id": "U0OTHER", "is_bot": False},
            ],
            "response_metadata": {"next_cursor": "page2"},
        },
        {
            "ok": True,
            "authorizations": [{"user_id": _ALICE_SLACK, "is_bot": False}],
            "response_metadata": {"next_cursor": ""},
        },
    ]

    async def _auth_list(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[1 if b"cursor=page2" in request.content else 0], request=request)

    h = make_delivery_harness(
        tmp_path,
        monkeypatch,
        config=config,
        connect_alice_oauth=True,
        slack_answers={
            "conversations.info": {"ok": False, "error": "channel_not_found"},
            "apps.event.authorizations.list": _auth_list,
            "users.info": {"ok": False, "error": "user_not_found"},
        },
    )

    # Zero rules -> zero apps.event.authorizations.list calls.
    assert (
        await h.deliver(event_payload("message", author="dana", text="hi", channel=DM, channel_type="im")) == "ignored"
    )
    assert h.slack.calls("apps.event.authorizations.list") == []

    # Rules with channel filter -> unrelated channel, plain message, and self-mention make ZERO authorizations calls.
    alice_slack_trigger(h.paths, kind="mention", match={"channel_id": _BOTH_CH})
    alice_slack_trigger(
        h.paths,
        trigger_id="slack_watch_bot_only",
        owner_user_id=BOB,
        slack_user_id="U0BOTONLY",
        kind="mention",
        match={"channel_id": _BOTH_CH},
    )
    for ignored_event in (
        event_payload("message", author="dana", text=f"<@{_ALICE_SLACK}>", channel="C0UNLISTED"),
        event_payload("message", author="dana", text="plain", channel=OTHER_CHANNEL),
        event_payload("message", author="alice", text=f"<@{_ALICE_SLACK}>", channel=OTHER_CHANNEL),
    ):
        assert await h.deliver(ignored_event) == "ignored"
    assert h.slack.calls("apps.event.authorizations.list") == []

    # Matching mention paginates authorizations, excludes the distinct bot-only candidate, fails soft on lookups, and binds thread root.
    mention = event_payload(
        "message",
        author="dana",
        text=f"<@{_ALICE_SLACK}> <@U0BOTONLY> check",
        channel=OTHER_CHANNEL,
    )
    assert await h.deliver(mention) == "delivered"
    assert await h.deliver(mention) == "ignored"
    await h.drain_triggers()
    assert h.trigger_store.list_firings(trigger_id="slack_watch_bot_only") == []
    assert len(h.matrix.sent) == 1
    assert SLACK_IDS["dana"] in h.matrix.sent[0][1]["body"]
    assert f"#{OTHER_CHANNEL}" in h.matrix.sent[0][1]["body"]

    # Follow-up reply in the bound thread groups into the same Matrix thread and triggers reopen_on_fire.
    reply = event_payload(
        "message",
        author="dana",
        text="any update?",
        ts=REPLY_TS,
        thread_ts=ROOT_TS,
        channel=OTHER_CHANNEL,
    )
    assert await h.deliver(reply) == "delivered"
    await h.drain_triggers()
    assert len(h.matrix.sent) == 2
    assert h.matrix.sent[1][1]["m.relates_to"]["event_id"] == "$sent1"
    assert reopened == [(WATCH_ROOM, "$sent1")]


def test_migrate_legacy_slack_watches_and_replay_roots_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review Focus #5: atomic one-time migration imports watches + live replay roots, archives post-commit, never resurrects."""
    config, paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(paths)
    assert paths.control_state_root is not None
    slack_dir = paths.control_state_root / "slack"

    # Absent watches.json returns 0 and creates no lock file.
    assert migrate_legacy_slack_watches(paths, store, config) == 0
    assert not slack_dir.exists()

    now = int(time.time())
    watches = (
        _legacy_watch("w_both", channels=_BOTH_CH, created_at=now - 500),
        _legacy_watch(
            "w_mention",
            room_id="!room2:example.org",
            direct_messages=False,
            channels=[CHANNEL],
            created_at=now - 400,
        ),
        _legacy_watch(
            "w_dm",
            owner_id=BOB,
            room_id="!room3:example.org",
            slack_user_id="U0BOB0000",
            mentions=False,
            channels=[CHANNEL],
            created_at=now - 300,
        ),
        _legacy_watch(
            "w_invalid",
            owner_id=BOB,
            room_id="!room4:example.org",
            slack_user_id="U0BOB0000",
            mentions=False,
            direct_messages=False,
            created_at=now - 200,
        ),
    )
    watches_path = _write_watches(paths, *watches)

    # Seed legacy replay store with unexpired channel + DM roots, a wrong-room root, and an expired root.
    replay = DeliveryReplayStore(slack_dir / "replay")
    for key, root_id, room_id, ts, ttl in (
        (f"w_both:{CHANNEL}:{ROOT_TS}", "$root_chan", WATCH_ROOM, now, 3600),
        (f"w_both:{DM}", "$root_dm", WATCH_ROOM, now, 3600),
        (f"w_both:{OTHER_CHANNEL}:1.0", "$root_wrong_room", "!other:example.org", now, 3600),
        (f"w_both:{OTHER_CHANNEL}:2.0", "$root_expired", WATCH_ROOM, now - 10_000, 100),
    ):
        replay.bind_thread_root(
            "slack:watch:w_both",
            key,
            root_id,
            room_id=room_id,
            reservation=None,
            now=ts,
            ttl_seconds=ttl,
        )

    # Failure during import_triggers_once must leave watches.json un-archived.
    with monkeypatch.context() as mp:
        mp.setattr(store, "import_triggers_once", MagicMock(side_effect=ReactiveTriggerStoreError("sqlite boom")))
        with pytest.raises(ReactiveTriggerStoreError, match="sqlite boom"):
            migrate_legacy_slack_watches(paths, store, config)
    assert watches_path.exists()
    assert not watches_path.with_name("watches.json.migrated").exists()

    # Successful migration imports 3 valid watches + 2 valid thread roots and archives watches.json.
    assert migrate_legacy_slack_watches(paths, store, config) == 3
    assert not watches_path.exists()
    assert watches_path.with_name("watches.json.migrated").exists()
    assert store.has_migration("slack_watches_v1")

    migrated = {
        tid: store.get_trigger(tid) for tid in ("slack_watch_w_both", "slack_watch_w_mention", "slack_watch_w_dm")
    }
    assert all(rec is not None and isinstance(rec.source, ExternalEventSourceSpec) for rec in migrated.values())
    both_src = cast("ExternalEventSourceSpec", migrated["slack_watch_w_both"].source)
    mention_src = cast("ExternalEventSourceSpec", migrated["slack_watch_w_mention"].source)
    dm_src = cast("ExternalEventSourceSpec", migrated["slack_watch_w_dm"].source)
    assert (both_src.kind, both_src.match, both_src.binding, both_src.source_origin, both_src.private_room_only) == (
        "message",
        {"channel_id": _BOTH_CH},
        {"team_id": TEAM, "slack_user_id": _ALICE_SLACK},
        "registered",
        True,
    )
    assert (mention_src.private_room_only, dm_src.private_room_only) == (True, True)
    assert (mention_src.kind, mention_src.match, dm_src.kind, dm_src.match) == (
        "mention",
        {"channel_id": CHANNEL},
        "direct_message",
        {},
    )
    assert store.get_trigger("slack_watch_w_invalid") is None
    assert store.get_thread_key_binding("slack_watch_w_both", f"{CHANNEL}:{ROOT_TS}") == "$root_chan"
    assert store.get_thread_key_binding("slack_watch_w_both", DM) == "$root_dm"
    assert store.get_thread_key_binding("slack_watch_w_both", f"{OTHER_CHANNEL}:1.0") is None
    assert store.get_thread_key_binding("slack_watch_w_both", f"{OTHER_CHANNEL}:2.0") is None

    # Deleting a migrated trigger and restoring watches.json NEVER resurrects it.
    assert store.delete_trigger("slack_watch_w_dm") is True
    _write_watches(paths, *watches)
    assert migrate_legacy_slack_watches(paths, store, config) == 0
    assert store.get_trigger("slack_watch_w_dm") is None


def test_slack_integration_bind_migrates_and_registers_and_unbind_cleans_up(tmp_path: Path) -> None:
    """SLACK_INTEGRATION.bind runs legacy migration and registers SlackEventSource; unbind unregisters it."""
    env = _slack_env(tmp_path, install_app=False, connect=False, register=False)
    _write_watches(env.paths, _legacy_watch())

    app = FastAPI()
    ensure_app_state(app)
    bots: dict[str, Any] = {}
    SLACK_INTEGRATION.bind(app, bots, AgentReplyMembershipIndex(), env.service)
    runtime = bound_slack_runtime(app)
    assert runtime is not None
    assert runtime.reactive_triggers is env.service
    assert isinstance(env.service.event_sources.get(SLACK_EVENT_SOURCE_ID), SlackEventSource)
    assert env.store.get_trigger("slack_watch_w1") is not None

    # Re-binding with same bots & service is a clean no-op; unbind unregisters SlackEventSource.
    SLACK_INTEGRATION.bind(app, bots, AgentReplyMembershipIndex(), env.service)
    assert bound_slack_runtime(app) is runtime
    assert SLACK_INTEGRATION.unbind(app) is runtime
    assert bound_slack_runtime(app) is None
    assert env.service.event_sources.get(SLACK_EVENT_SOURCE_ID) is None


@pytest.mark.parametrize(
    ("tools", "rules", "should_reject"),
    [
        (["slack"], [{"match": "slack_*", "action": "require_approval"}], False),
        (["triggers"], [{"match": "slack_*", "action": "require_approval"}], False),
        (["slack", "triggers"], [{"match": "slack_*", "action": "require_approval"}], True),
        (
            ["slack", "triggers"],
            [{"match": "slack_*", "action": "require_approval"}, {"match": "triggers", "action": "auto_approve"}],
            False,
        ),
        (
            ["slack", "triggers"],
            [{"match": "slack_watch", "action": "require_approval"}, {"match": "triggers", "action": "auto_approve"}],
            True,
        ),
        (
            ["slack", "triggers"],
            [
                {"match": "slack_watch", "action": "require_approval"},
                {"match": "triggers", "action": "require_approval"},
            ],
            False,
        ),
        (["slack", "triggers"], [{"match": "slack_watch", "action": "require_approval"}], True),
        (["slack", "triggers"], [{"match": ["slack_watch", "slack_post"], "action": "require_approval"}], True),
        (
            ["slack", "triggers"],
            [
                {"match": ["slack_watch", "slack_post"], "action": "require_approval"},
                {"match": "triggers", "action": "auto_approve"},
            ],
            True,
        ),
        (
            ["slack", "triggers"],
            [{"match": "triggers", "arguments": {"action": "subscribe_event"}, "action": "require_approval"}],
            False,
        ),
        (
            ["slack", "triggers"],
            [
                {"match": "slack_watch", "action": "require_approval"},
                {"match": "triggers", "arguments": {"action": "list"}, "action": "auto_approve"},
            ],
            True,
        ),
    ],
)
def test_retired_slack_watch_approval_rules_fail_closed(
    tmp_path: Path,
    tools: list[str],
    rules: list[dict[str, Any]],
    should_reject: bool,
) -> None:
    """Legacy slack_watch approval rules fail closed only when both slack and triggers are enabled."""
    cfg = {
        "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
        "router": {"model": "default"},
        "agents": {"assistant": {"display_name": "Assistant", "tools": tools}},
        "tool_approval": {"default": "auto_approve", "rules": rules},
    }
    if should_reject:
        with pytest.raises((ToolConfigOverrideError, ValueError), match=r"slack_watch.*triggers"):
            Config.validate_with_runtime(cfg, test_runtime_paths(tmp_path))
    else:
        Config.validate_with_runtime(cfg, test_runtime_paths(tmp_path))


@pytest.mark.parametrize("key", ["include_tools", "exclude_tools"])
def test_retired_slack_watch_tool_override_fails_closed(tmp_path: Path, key: str) -> None:
    """include_tools/exclude_tools referencing slack_watch on slack fail closed with subscribe_event guidance."""
    cfg = {
        "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
        "router": {"model": "default"},
        "agents": {"assistant": {"display_name": "Assistant", "tools": [{"slack": {key: ["slack_watch"]}}]}},
    }
    with pytest.raises((ToolConfigOverrideError, ValueError), match="subscribe_event"):
        Config.validate_with_runtime(cfg, test_runtime_paths(tmp_path))


def test_room_triggers_api_hides_slack_trigger_from_non_owners_and_shows_paused_reason(tmp_path: Path) -> None:
    """Review Focus #3: GET /api/rooms/{room_id}/triggers hides Slack triggers from non-owners and reports paused_reason."""
    h = RoomTriggersHarness(
        tmp_path,
        room_state_events={ROOM_A: room_state_events(room_state_snapshot(("@alice:localhost",), room_id=ROOM_A))},
    )
    install_slack_state(h.paths, app_token=APP_TOKEN)
    h.use_event_sources(SlackEventSource())
    h.add_reactive(
        "tr_slack_real",
        ExternalEventSourceSpec(
            source_id=SLACK_EVENT_SOURCE_ID,
            kind="message",
            match={"channel_id": CHANNEL},
            binding={"team_id": TEAM, "slack_user_id": _ALICE_SLACK},
            source_origin="registered",
        ),
        target_room=ROOM_A,
        description="Alice Slack watch",
        prompt="Summarize Slack message.",
        created_at=1.0,
    )

    alice_resp = h.req("GET", ROOM_A, "@alice:localhost")
    assert alice_resp.status_code == 200
    rows = {row["trigger_id"]: row for row in alice_resp.json()["triggers"]}
    assert rows["tr_slack_real"]["source_id"] == "slack"
    assert rows["tr_slack_real"]["paused_reason"] == "the watch's agent no longer has personal Slack"
    assert TEAM not in alice_resp.text
    assert _ALICE_SLACK not in alice_resp.text

    bob_resp = h.req("GET", ROOM_A, "@bob:localhost")
    assert bob_resp.status_code == 200
    assert "tr_slack_real" not in {row["trigger_id"] for row in bob_resp.json()["triggers"]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy_cap", "thread_id", "kwargs", "expected_cap", "expected_target"),
    [
        (120, "$existing_thread", {}, 60, (True, True, None, None)),
        (
            120,
            "$existing_thread",
            {"max_fires_per_hour": 12, "new_thread": False},
            12,
            (False, False, "$existing_thread", None),
        ),
        (120, "$existing_thread", {"thread_key": "alert_feed"}, 60, (True, False, None, "alert_feed")),
        (25, None, {}, 25, (True, True, None, None)),
    ],
)
async def test_slack_subscribe_defaults_and_overrides(
    tmp_path: Path,
    policy_cap: int,
    thread_id: str | None,
    kwargs: dict[str, Any],
    expected_cap: int,
    expected_target: tuple[bool, bool, str | None, str | None],
) -> None:
    """Slack subscribe_event defaults max_fires_per_hour to min(60, policy) and honors explicit thread overrides."""
    env = _slack_env(tmp_path, max_fires_per_hour=policy_cap)
    sub = await env.subscribe(thread_id=thread_id, **kwargs)
    assert sub["status"] == "ok"
    assert sub["trigger"]["max_fires_per_hour"] == expected_cap
    target = sub["trigger"]["target"]
    assert (
        target["new_thread"],
        target["thread_key_from_event"],
        target["thread_id"],
        target["thread_key"],
    ) == expected_target


def test_slack_integration_bind_skips_migration_when_yaml_source_collides(tmp_path: Path) -> None:
    """Pre-existing YAML event_sources.slack blocks Slack registration and leaves watches.json unmigrated/unarchived."""
    env = _slack_env(
        tmp_path,
        install_app=False,
        connect=False,
        register=False,
        event_sources={"slack": {"kind": "hmac", "secret_env": "SLACK_WEBHOOK_SECRET", "subscribers": [ALICE]}},
    )
    watches_path = _write_watches(env.paths, _legacy_watch())

    app = FastAPI()
    ensure_app_state(app)
    SLACK_INTEGRATION.bind(app, {}, AgentReplyMembershipIndex(), env.service)
    assert env.service.event_sources.get(SLACK_EVENT_SOURCE_ID) is None
    assert watches_path.exists()
    assert not watches_path.with_name("watches.json.migrated").exists()
    assert env.store.has_migration("slack_watches_v1") is False
    assert env.store.get_trigger("slack_watch_w1") is None


def test_migrate_legacy_slack_watches_aborts_on_replay_store_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DeliveryReplayStoreError aborts legacy watch migration before committing SQLite or archiving watches.json."""
    config, paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(paths)
    watches_path = _write_watches(paths, _legacy_watch())

    monkeypatch.setattr(
        DeliveryReplayStore,
        "list_bound_thread_roots",
        MagicMock(side_effect=DeliveryReplayStoreError("corrupt replay store")),
    )
    with pytest.raises(DeliveryReplayStoreError, match="corrupt replay store"):
        migrate_legacy_slack_watches(paths, store, config)

    assert watches_path.exists()
    assert not watches_path.with_name("watches.json.migrated").exists()
    assert store.has_migration("slack_watches_v1") is False
    assert store.get_trigger("slack_watch_w1") is None


@pytest.mark.asyncio
async def test_real_slack_trigger_hidden_from_non_owner_admin_smoke(tmp_path: Path) -> None:
    """Real SlackEventSource triggers stay hidden from non-owner admins before and after source unregistration."""
    env = _slack_env(tmp_path)
    sample_key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    assert (await env.subscribe(trigger_id="alice_slack"))["status"] == "ok"

    for unregistered in (False, True):
        if unregistered:
            assert env.emitter is not None
            assert env.service.event_sources.unregister(SLACK_EVENT_SOURCE_ID, env.emitter.token) is True
        listed = await env.admin_call("list")
        hist = await env.admin_call("history", trigger_id="alice_slack")
        disabled = await env.admin_call("set_enabled", trigger_id="alice_slack", enabled=False)
        deleted = await env.admin_call("delete", trigger_id="alice_slack")
        rotated = await env.admin_call("rotate_key", trigger_id="alice_slack", public_key=sample_key)
        assert "alice_slack" not in {t["trigger_id"] for t in listed["reactive_triggers"]}
        assert (hist["status"], disabled["status"], deleted["status"], rotated["status"]) == ("error",) * 4
        assert rotated["message"] == "external trigger not found: alice_slack"
        reloaded = env.store.get_trigger("alice_slack")
        assert reloaded is not None
        assert reloaded.enabled is True


@pytest.mark.asyncio
async def test_pre_commit_migration_failure_ignores_until_lazy_retry_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-commit migration failure returns 'ignored' (never 'retry'/503) and lazily migrates & delivers once fixed."""
    config, _ = _make_config(tmp_path)
    h = make_delivery_harness(
        tmp_path,
        monkeypatch,
        config=config,
        register_source=False,
        connect_alice_oauth=True,
        slack_answers={"users.info": {"ok": False, "error": "user_not_found"}},
    )
    assert h.paths.control_state_root is not None
    watches_path = h.paths.control_state_root / "slack" / "watches.json"
    watches_path.parent.mkdir(parents=True, exist_ok=True)
    watches_path.write_text("{not valid json", encoding="utf-8")

    _, registered = h.bind_integration()
    assert registered.retry_migration is not None
    assert watches_path.exists()
    assert not h.trigger_store.has_migration("slack_watches_v1")

    dm = event_payload("message", author="dana", text="urgent dm", channel=DM, channel_type="im")
    assert await h.deliver(dm) == "ignored"
    assert registered.retry_migration is not None
    assert h.slack.calls("apps.event.authorizations.list") == []

    # A valid SQLite-backed Slack subscription still fires even while legacy watches.json is broken.
    alice_slack_trigger(h.paths, trigger_id="sqlite_slack_watch", kind="direct_message")
    dm_for_sqlite = event_payload(
        "message",
        author="dana",
        text="sqlite watch still works",
        ts="1700000001.500000",
        channel=DM,
        channel_type="im",
    )
    assert await h.deliver(dm_for_sqlite, now=NOW + 0.5) == "delivered"
    assert registered.retry_migration is not None
    assert len(h.trigger_store.list_firings(trigger_id="sqlite_slack_watch")) == 1

    # Once watches.json is fixed, the next event lazily completes migration and delivers.
    _write_watches(h.paths, _legacy_watch())
    recovered_dm = event_payload(
        "message",
        author="dana",
        text="after fix",
        ts="1700000002.000100",
        channel=DM,
        channel_type="im",
    )
    assert await h.deliver(recovered_dm, now=NOW + 1) == "delivered"
    assert registered.retry_migration is None
    assert h.trigger_store.has_migration("slack_watches_v1") is True
    assert not watches_path.exists()
    assert watches_path.with_name("watches.json.migrated").exists()
    assert len(h.trigger_store.list_firings(trigger_id="slack_watch_w1")) == 1


@pytest.mark.asyncio
async def test_post_commit_archive_rename_failure_delivers_and_self_heals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-commit watches.json rename OSError still delivers migrated watches and self-heals on next migration call."""
    config, _ = _make_config(tmp_path)
    h = make_delivery_harness(
        tmp_path,
        monkeypatch,
        config=config,
        register_source=False,
        connect_alice_oauth=True,
        slack_answers={"users.info": {"ok": False, "error": "user_not_found"}},
    )
    watches_path = _write_watches(h.paths, _legacy_watch())

    real_replace = Path.replace

    def _failing_replace(self: Path, target: Path) -> Path:
        if self == watches_path:
            msg = "archive rename failed"
            raise OSError(msg)
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _failing_replace)

    _, registered = h.bind_integration()
    assert registered.retry_migration is None
    assert h.trigger_store.has_migration("slack_watches_v1") is True
    assert h.trigger_store.get_trigger("slack_watch_w1") is not None
    assert watches_path.exists()
    assert not watches_path.with_name("watches.json.migrated").exists()

    # Migrated watch delivers normally despite the post-commit rename error.
    dm = event_payload("message", author="dana", text="hello after commit", channel=DM, channel_type="im")
    assert await h.deliver(dm) == "delivered"
    assert len(h.trigger_store.list_firings(trigger_id="slack_watch_w1")) == 1

    # Deleting the trigger and running migrate_legacy_slack_watches again self-heals archival WITHOUT re-importing.
    assert h.trigger_store.delete_trigger("slack_watch_w1") is True
    monkeypatch.setattr(Path, "replace", real_replace)
    assert migrate_legacy_slack_watches(h.paths, h.trigger_store, h.config) == 0
    assert not watches_path.exists()
    assert watches_path.with_name("watches.json.migrated").exists()
    assert h.trigger_store.get_trigger("slack_watch_w1") is None
