"""Tests for SlackEventSource, watch migration, watch-turn guards, and retired slack_watch config validation."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.api.app_state import ensure_app_state
from mindroom.config.main import Config
from mindroom.constants import HOOK_SOURCE_KEY, SOURCE_KIND_KEY
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.custom_tools.slack import SlackTools
from mindroom.custom_tools.triggers import TriggersTools
from mindroom.delivery_replay import DeliveryReplayStore, DeliveryReplayStoreError
from mindroom.dispatch_source import REACTIVE_TRIGGER_SOURCE_KIND, hook_source_from_content
from mindroom.entity_resolution import entity_identity_registry
from mindroom.event_sources import EventSource, EventSourceContext
from mindroom.matrix.room_reconciliation import RoomStateSnapshot
from mindroom.message_target import MessageTarget
from mindroom.oauth.providers import OAuthConnectionRequired
from mindroom.reactive_triggers import dispatch as reactive_dispatch
from mindroom.reactive_triggers.models import (
    ExternalEventSourceSpec,
    ReactiveTriggerRecord,
    TriggerEvent,
    TriggerTargetSpec,
)
from mindroom.reactive_triggers.service import ReactiveTriggerService
from mindroom.reactive_triggers.store import ReactiveTriggerStore, ReactiveTriggerStoreError
from mindroom.slack import app as slack_app
from mindroom.slack import identity as slack_identity
from mindroom.slack.app import load_slack_app
from mindroom.slack.delivery import deliver_slack_event
from mindroom.slack.event_source import (
    SLACK_EVENT_SOURCE_ID,
    SlackEventSource,
    _watch_room_problem_from_state,
    started_by_slack_trigger,
)
from mindroom.slack.integration import SLACK_INTEGRATION, bound_slack_runtime
from mindroom.slack.legacy_watch_migration import migrate_legacy_slack_watches
from mindroom.slack.oauth import slack_oauth_provider
from mindroom.tool_system.catalog import ToolConfigOverrideError
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.oauth_test_utils import publish_oauth_credentials
from tests.slack.delivery_fakes import (
    ALICE,
    APP_TOKEN,
    AUTH_TEST,
    BOB,
    CHANNEL,
    DM,
    NOW,
    REPLY_TS,
    ROOT_TS,
    SLACK_IDS,
    TEAM,
    WATCH_ROOM,
    FakeBot,
    FakeMatrix,
    FakeSlack,
    alice_slack_trigger,
    connect_alice,
    event_payload,
    install_slack_state,
    runtime_for,
)
from tests.slack.room_state import room_state_client

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.constants import RuntimePaths

ALICE_BRIDGE = "@telegram_alice:example.org"
ADMIN = "@admin:example.org"
SHARED_ROOM = "!shared:example.org"
ADMIN_ROOM = "!admin:example.org"
OTHER_CHANNEL = "C0456EFGH"
LATER_JOINERS = "people who join this room later could read earlier messages"


@pytest.fixture(autouse=True)
def _clear_slack_caches() -> Iterator[None]:
    slack_app._IDENTITIES.clear()
    slack_identity._PROFILES.clear()
    slack_identity._CHANNELS.clear()
    yield
    slack_app._IDENTITIES.clear()
    slack_identity._PROFILES.clear()
    slack_identity._CHANNELS.clear()


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
            "project_only": {
                "display_name": "Project",
                "tools": [{"slack": {"channels": [CHANNEL]}}],
            },
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


def _snapshot(
    joined: tuple[str, ...] = (),
    *,
    invited: tuple[str, ...] = (),
    departed: tuple[str, ...] = (),
    history_visibility: str | None = "invited",
    room_id: str = WATCH_ROOM,
) -> RoomStateSnapshot:
    events: dict[tuple[str, str], dict[str, Any]] = {}
    for uid in joined:
        events[("m.room.member", uid)] = {"membership": "join"}
    for uid in invited:
        events[("m.room.member", uid)] = {"membership": "invite"}
    for uid in departed:
        events[("m.room.member", uid)] = {"membership": "leave"}
    if history_visibility is not None:
        events[("m.room.history_visibility", "")] = {"history_visibility": history_visibility}
    return RoomStateSnapshot(room_id=room_id, events=events, creators=frozenset())


def _rule(
    *,
    kind: str = "message",
    match: dict[str, Any] | None = None,
    binding: dict[str, Any] | None = None,
    room_id: str = WATCH_ROOM,
    agent: str = "assistant",
    owner: str = ALICE,
) -> ReactiveTriggerRecord:
    now = time.time()
    return ReactiveTriggerRecord(
        trigger_id="slack_rule_1",
        owner_user_id=owner,
        source=ExternalEventSourceSpec(
            source_id=SLACK_EVENT_SOURCE_ID,
            kind=kind,
            match=match or {},
            binding={"team_id": TEAM, "slack_user_id": SLACK_IDS["alice"]} if binding is None else binding,
            source_origin="registered",
        ),
        target=TriggerTargetSpec(
            agent=agent,
            room_id=room_id,
            new_thread=True,
            thread_key_from_event=True,
            reopen_on_fire=True,
            prompt="Summarize Slack message.",
        ),
        once=False,
        max_fires_per_hour=60,
        created_at=now,
        updated_at=now,
    )


def _slack_event(**data: object) -> TriggerEvent:
    base = {
        "team_id": TEAM,
        "channel_id": CHANNEL,
        "channel_type": "channel",
        "user_id": SLACK_IDS["dana"],
        "is_dm": False,
        "mentioned_user_ids": [],
    }
    base.update(data)
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


def _tool_context(
    *,
    config: Config,
    paths: RuntimePaths,
    client: AsyncMock,
    service: ReactiveTriggerService,
    room_id: str = WATCH_ROOM,
    requester_id: str = ALICE,
    hook_source: str | None = None,
    thread_id: str | None = None,
) -> ToolRuntimeContext:
    bot = SimpleNamespace(
        client=client,
        running=True,
        agent_name="assistant",
        conversation_reader=SimpleNamespace(latest_thread_event_id=AsyncMock(return_value="$latest")),
    )
    orchestrator = SimpleNamespace(
        reactive_triggers=service,
        agent_bots={"assistant": cast("AgentBot", bot)},
        agent_reply_memberships=AgentReplyMembershipIndex(),
    )
    return ToolRuntimeContext(
        agent_name="assistant",
        target=MessageTarget.resolve(room_id, thread_id, "$event", room_mode=thread_id is None),
        requester_id=requester_id,
        client=client,
        config=config,
        runtime_paths=paths,
        plugins=config_plugins(config, paths),
        conversation_reader=MagicMock(),
        relations=MagicMock(),
        hook_source=hook_source,
        orchestrator=orchestrator,  # type: ignore[arg-type]
    )


def _multi_room_client(room_clients: dict[str, AsyncMock]) -> AsyncMock:
    client = AsyncMock()
    client.rooms = {rid: next(iter(rc.rooms.values())) for rid, rc in room_clients.items()}

    async def _get_state(room_id: str) -> object:
        if room_id in room_clients:
            return await room_clients[room_id].room_get_state(room_id)
        return None

    client.room_get_state.side_effect = _get_state
    return client


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
    assert desc.source_id == "slack"
    assert desc.allowed_kinds == ("message", "mention", "direct_message")
    assert desc.match_fields == ("channel_id",)
    assert (
        desc.default_once,
        desc.default_new_thread,
        desc.default_thread_key_from_event,
        desc.default_reopen_on_fire,
        desc.private_room_only,
        desc.default_max_fires_per_hour,
    ) == (False, True, True, True, True, 60)

    project_ctx = replace(ctx, target_agent="project_only")
    assert await source.describe_for_user(project_ctx) is None

    no_personal_cfg, no_personal_paths = _make_config(tmp_path / "np", assistant_tools=["triggers"])
    install_slack_state(no_personal_paths, app_token=APP_TOKEN)
    assert (
        await source.describe_for_user(
            EventSourceContext(owner_user_id=ALICE, config=no_personal_cfg, runtime_paths=no_personal_paths),
        )
        is None
    )


def test_watch_room_problem_from_state_enforces_privacy_invariants(tmp_path: Path) -> None:
    """Private rooms allow only the owner (or alias) and MindRoom entities with invited/joined history visibility."""
    config, paths = _make_config(tmp_path)
    agent_id = entity_identity_registry(config, paths).current_id("assistant").full_id
    router_id = entity_identity_registry(config, paths).current_id("router").full_id

    assert _watch_room_problem_from_state(None, ALICE, config, paths) == "room members could not be checked"
    assert (
        _watch_room_problem_from_state(_snapshot((ALICE, agent_id, router_id), departed=(BOB,)), ALICE, config, paths)
        is None
    )
    assert (
        _watch_room_problem_from_state(
            _snapshot((ALICE_BRIDGE, agent_id), history_visibility="joined"),
            ALICE,
            config,
            paths,
        )
        is None
    )
    assert _watch_room_problem_from_state(_snapshot((agent_id,)), ALICE, config, paths) == "you are not in this room"
    assert (
        _watch_room_problem_from_state(_snapshot((ALICE, agent_id, BOB)), ALICE, config, paths)
        == "another person is in this room"
    )
    assert (
        _watch_room_problem_from_state(_snapshot((ALICE, agent_id), invited=(BOB,)), ALICE, config, paths)
        == "another person is in this room"
    )
    assert (
        _watch_room_problem_from_state(_snapshot((ALICE, agent_id, "@slackbot:example.org")), ALICE, config, paths)
        == "another person is in this room"
    )
    for vis in ("shared", "world_readable", None):
        assert (
            _watch_room_problem_from_state(_snapshot((ALICE, agent_id), history_visibility=vis), ALICE, config, paths)
            == LATER_JOINERS
        )


@pytest.mark.asyncio
async def test_validate_subscription_normalization_oauth_and_immutable_binding(tmp_path: Path) -> None:
    """Validation normalizes channel_id, enforces OAuth/privacy, preserves binding on update, and rejects account swaps."""
    config, paths = _make_config(tmp_path)
    source = SlackEventSource()
    agent_id = entity_identity_registry(config, paths).current_id("assistant").full_id
    private_state = _snapshot((ALICE, agent_id))
    ctx = EventSourceContext(
        owner_user_id=ALICE,
        config=config,
        runtime_paths=paths,
        target_room_id=WATCH_ROOM,
        target_agent="assistant",
        target_room_state=private_state,
    )

    with pytest.raises(ValueError, match="not configured"):
        await source.validate_subscription(ctx, kind="message", match={})

    install_slack_state(paths, app_token=None)
    with pytest.raises(ValueError, match="app-level token"):
        await source.validate_subscription(ctx, kind="message", match={})

    install_slack_state(paths, app_token=APP_TOKEN)
    with pytest.raises(ValueError, match="personal Slack"):
        await source.validate_subscription(replace(ctx, target_agent="project_only"), kind="message", match={})
    with pytest.raises(ValueError, match="Unsupported Slack event kind"):
        await source.validate_subscription(ctx, kind="reaction", match={})
    with pytest.raises(ValueError, match="Unsupported Slack match field"):
        await source.validate_subscription(ctx, kind="message", match={"repo": "x"})
    with pytest.raises(ValueError, match="Cannot filter direct_message"):
        await source.validate_subscription(ctx, kind="direct_message", match={"channel_id": CHANNEL})
    with pytest.raises(ValueError, match="non-empty"):
        await source.validate_subscription(ctx, kind="message", match={"channel_id": []})
    with pytest.raises(ValueError, match="conversation"):
        await source.validate_subscription(ctx, kind="message", match={"channel_id": SLACK_IDS["alice"]})

    # Missing OAuth raises OAuthConnectionRequired
    with pytest.raises(OAuthConnectionRequired):
        await source.validate_subscription(ctx, kind="message", match={})

    # Unverified OAuth claims also raise OAuthConnectionRequired
    publish_oauth_credentials(
        slack_oauth_provider(),
        {"token": "xoxp-unverified", "_source": "oauth", "_oauth_claims": {"sub": SLACK_IDS["alice"], "team_id": TEAM}},
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
    # Shared target room rejected
    with pytest.raises(ValueError, match="Cannot watch Slack here: another person is in this room"):
        await source.validate_subscription(
            replace(ctx, target_room_state=_snapshot((ALICE, agent_id, BOB))),
            kind="message",
            match={},
        )

    # Valid subscription normalizes single & deduplicated multi-channel targets and produces verified binding
    single = await source.validate_subscription(ctx, kind="message", match={"channel_id": f"{CHANNEL}:{ROOT_TS}"})
    assert single.match == {"channel_id": CHANNEL}
    assert single.binding == {"team_id": TEAM, "slack_user_id": SLACK_IDS["alice"]}

    multi = await source.validate_subscription(
        ctx,
        kind="mention",
        match={"channel_id": [CHANNEL, OTHER_CHANNEL, CHANNEL]},
        existing_binding={"team_id": TEAM, "slack_user_id": SLACK_IDS["alice"]},
    )
    assert multi.match == {"channel_id": [CHANNEL, OTHER_CHANNEL]}
    assert multi.binding == {"team_id": TEAM, "slack_user_id": SLACK_IDS["alice"]}

    # Account change on update rejected
    connect_alice(paths, slack_user_id="U0OTHER99")
    with pytest.raises(ValueError, match="Slack account changed; recreate this subscription"):
        await source.validate_subscription(
            ctx,
            kind="mention",
            match={"channel_id": CHANNEL},
            existing_binding={"team_id": TEAM, "slack_user_id": SLACK_IDS["alice"]},
        )


@pytest.mark.parametrize(
    ("kind", "match", "event_data", "audience", "has_thread_binding", "expected"),
    [
        # Empty / missing audience NEVER matches
        ("message", {}, {"is_dm": True}, None, False, False),
        ("message", {}, {"is_dm": True}, frozenset(), False, False),
        # Wrong team or self-authored ALWAYS rejected
        ("message", {}, {"team_id": "T0OTHER", "is_dm": True}, frozenset({SLACK_IDS["alice"]}), False, False),
        ("message", {}, {"user_id": SLACK_IDS["alice"], "is_dm": True}, frozenset({SLACK_IDS["alice"]}), True, False),
        (
            "message",
            {},
            {"user_id": SLACK_IDS["alice"], "mentioned_user_ids": [SLACK_IDS["alice"]]},
            frozenset({SLACK_IDS["alice"]}),
            True,
            False,
        ),
        # DMs and MPIMs
        (
            "message",
            {},
            {"is_dm": True, "channel_id": DM, "channel_type": "im"},
            frozenset({SLACK_IDS["alice"]}),
            False,
            True,
        ),
        (
            "direct_message",
            {},
            {"is_dm": True, "channel_id": DM, "channel_type": "mpim"},
            frozenset({SLACK_IDS["alice"]}),
            False,
            True,
        ),
        ("mention", {}, {"is_dm": True, "channel_id": DM}, frozenset({SLACK_IDS["alice"]}), True, False),
        (
            "mention",
            {},
            {"is_dm": True, "channel_id": DM, "mentioned_user_ids": [SLACK_IDS["alice"]]},
            frozenset({SLACK_IDS["alice"]}),
            False,
            True,
        ),
        # Channel filter applies ONLY to channel messages, never DMs
        (
            "message",
            {"channel_id": [CHANNEL, OTHER_CHANNEL]},
            {"is_dm": True, "channel_id": DM},
            frozenset({SLACK_IDS["alice"]}),
            False,
            True,
        ),
        (
            "message",
            {"channel_id": [CHANNEL, OTHER_CHANNEL]},
            {"channel_id": OTHER_CHANNEL, "mentioned_user_ids": [SLACK_IDS["alice"]]},
            frozenset({SLACK_IDS["alice"]}),
            False,
            True,
        ),
        (
            "message",
            {"channel_id": [CHANNEL, OTHER_CHANNEL]},
            {"channel_id": "C0UNLISTED", "mentioned_user_ids": [SLACK_IDS["alice"]]},
            frozenset({SLACK_IDS["alice"]}),
            True,
            False,
        ),
        # Mentions & bound thread replies vs unbound channel firehose
        ("message", {}, {"mentioned_user_ids": []}, frozenset({SLACK_IDS["alice"]}), False, False),
        ("message", {}, {"mentioned_user_ids": []}, frozenset({SLACK_IDS["alice"]}), True, True),
        ("mention", {}, {"mentioned_user_ids": [SLACK_IDS["alice"]]}, frozenset({SLACK_IDS["alice"]}), False, True),
        (
            "direct_message",
            {},
            {"mentioned_user_ids": [SLACK_IDS["alice"]]},
            frozenset({SLACK_IDS["alice"]}),
            True,
            False,
        ),
        # Visibility audience isolation
        ("message", {}, {"mentioned_user_ids": [SLACK_IDS["alice"]]}, frozenset({"U0SOMEONE"}), False, False),
    ],
)
def test_matches_rule_and_dry_run_audience(
    kind: str,
    match: dict[str, Any],
    event_data: dict[str, Any],
    audience: object,
    has_thread_binding: bool,
    expected: bool,
) -> None:
    """matches_rule enforces audience isolation, kinds, channel filters, bound threads, and no self-triggering."""
    source = SlackEventSource()
    rule = _rule(kind=kind, match=match)
    assert (
        source.matches_rule(rule, _slack_event(**event_data), audience=audience, has_thread_binding=has_thread_binding)
        is expected
    )
    assert source.dry_run_audience(rule) == frozenset({SLACK_IDS["alice"]})


@pytest.mark.asyncio
async def test_triggers_tool_subscribe_update_and_immutable_binding_review_focus_2(tmp_path: Path) -> None:
    """Review Focus #2: subscribe binds verified OAuth identity, update preserves it, and binding never leaks."""
    config, paths = _make_config(tmp_path)
    install_slack_state(paths, app_token=APP_TOKEN)
    agent_id = entity_identity_registry(config, paths).current_id("assistant").full_id
    store = ReactiveTriggerStore(paths)
    service = ReactiveTriggerService(
        config_provider=lambda: config,
        runtime_paths=paths,
        bots=dict,
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )
    service.event_sources.register(SlackEventSource())
    client = _multi_room_client({WATCH_ROOM: room_state_client((ALICE, agent_id), room_id=WATCH_ROOM)})
    tool = TriggersTools()

    with tool_runtime_context(_tool_context(config=config, paths=paths, client=client, service=service)):
        unconnected = json.loads(
            await tool.triggers(action="subscribe_event", source_id="slack", kind="message", prompt="Summarize."),
        )
    assert unconnected["status"] == "error"
    assert unconnected["oauth_connection_required"] is True

    connect_alice(paths)
    with tool_runtime_context(_tool_context(config=config, paths=paths, client=client, service=service)):
        created = json.loads(
            await tool.triggers(
                action="subscribe_event",
                source_id="slack",
                kind="message",
                match={"channel_id": [CHANNEL, OTHER_CHANNEL, CHANNEL]},
                prompt="Summarize Slack message.",
            ),
        )
    assert created["status"] == "ok"
    tid = created["trigger"]["trigger_id"]
    assert "binding" not in json.dumps(created)
    assert created["trigger"]["source"]["match"] == {"channel_id": [CHANNEL, OTHER_CHANNEL]}
    assert created["trigger"]["once"] is False
    assert created["trigger"]["target"]["new_thread"] is True
    assert created["trigger"]["target"]["thread_key_from_event"] is True
    assert created["trigger"]["target"]["reopen_on_fire"] is True

    stored = store.get_trigger(tid)
    assert stored is not None
    assert isinstance(stored.source, ExternalEventSourceSpec)
    assert stored.source.binding == {"team_id": TEAM, "slack_user_id": SLACK_IDS["alice"]}

    with tool_runtime_context(_tool_context(config=config, paths=paths, client=client, service=service)):
        updated = json.loads(await tool.triggers(action="update", trigger_id=tid, match={"channel_id": CHANNEL}))
    assert updated["status"] == "ok"
    assert "binding" not in json.dumps(updated)
    reloaded = store.get_trigger(tid)
    assert reloaded is not None
    assert isinstance(reloaded.source, ExternalEventSourceSpec)
    assert reloaded.source.match == {"channel_id": CHANNEL}
    assert reloaded.source.binding == {"team_id": TEAM, "slack_user_id": SLACK_IDS["alice"]}

    # Account change on update is refused and leaves the stored binding intact.
    connect_alice(paths, slack_user_id="U0OTHER99")
    with tool_runtime_context(_tool_context(config=config, paths=paths, client=client, service=service)):
        rejected = json.loads(await tool.triggers(action="update", trigger_id=tid, match={"channel_id": OTHER_CHANNEL}))
    assert rejected["status"] == "error"
    assert "Slack account changed" in rejected["message"]


@pytest.mark.asyncio
async def test_triggers_tool_history_paused_reason_and_watch_turn_guards_review_focus_3_4(tmp_path: Path) -> None:
    """Review Focus #3/#4: private history gating, paused_reason reporting, and event_source:slack mutation/write refusal."""
    config, paths = _make_config(tmp_path)
    install_slack_state(paths, app_token=APP_TOKEN)
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
    service.event_sources.register(SlackEventSource())
    client = _multi_room_client(
        {
            WATCH_ROOM: room_state_client((ALICE, agent_id), room_id=WATCH_ROOM),
            SHARED_ROOM: room_state_client((ALICE, BOB, agent_id), room_id=SHARED_ROOM),
            ADMIN_ROOM: room_state_client((ADMIN, agent_id), room_id=ADMIN_ROOM),
        },
    )
    tool = TriggersTools()
    rule = alice_slack_trigger(paths)
    assert store.enqueue_firing(
        rule.trigger_id,
        "ev-priv-1",
        _slack_event(is_dm=True, channel_id=DM, channel_type="im"),
        expected_rule_uid=rule.rule_uid,
    )
    fid = store.list_firings(trigger_id=rule.trigger_id)[0].id
    assert store.claim_firing(fid, now=time.time(), triage_reason="Secret deal discussion") == "claimed"

    # Owner in private room sees event + triage_reason; shared room is refused; non-owner admin sees neither rule nor history.
    with tool_runtime_context(
        _tool_context(config=config, paths=paths, client=client, service=service, room_id=WATCH_ROOM),
    ):
        priv_hist = json.loads(await tool.triggers(action="history", trigger_id=rule.trigger_id))
    assert priv_hist["status"] == "ok"
    assert priv_hist["firings"][0]["triage_reason"] == "Secret deal discussion"
    assert priv_hist["firings"][0]["event"]["text"] == "hello"

    with tool_runtime_context(
        _tool_context(config=config, paths=paths, client=client, service=service, room_id=SHARED_ROOM),
    ):
        shared_hist = json.loads(await tool.triggers(action="history", trigger_id=rule.trigger_id))
    assert shared_hist["status"] == "error"

    with tool_runtime_context(
        _tool_context(
            config=config,
            paths=paths,
            client=client,
            service=service,
            room_id=ADMIN_ROOM,
            requester_id=ADMIN,
        ),
    ):
        admin_list = json.loads(await tool.triggers(action="list"))
        admin_hist_scoped = json.loads(await tool.triggers(action="history", trigger_id=rule.trigger_id))
        admin_hist_unscoped = json.loads(await tool.triggers(action="history"))
    assert admin_list["reactive_triggers"] == []
    assert rule.trigger_id not in json.dumps(admin_list)
    assert "paused_reason" not in json.dumps(admin_list)
    assert admin_hist_scoped["status"] == "error"
    assert admin_hist_unscoped["count"] == 0
    assert "Secret deal" not in json.dumps(admin_hist_unscoped)

    # Inviting another human surfaces paused_reason in triggers(action="list").
    invited_client = _multi_room_client(
        {WATCH_ROOM: room_state_client((ALICE, agent_id), invited=(BOB,), room_id=WATCH_ROOM)},
    )
    with tool_runtime_context(_tool_context(config=config, paths=paths, client=invited_client, service=service)):
        listed = json.loads(await tool.triggers(action="list"))
    row = next(r for r in listed["triggers"] if r["trigger_id"] == rule.trigger_id)
    assert row["paused_reason"] == "another person is in this room"

    # Review Focus #4: event_source:slack on REACTIVE_TRIGGER_SOURCE_KIND blocks writes & trigger mutations,
    # does not trigger hook_source_from_content labels, and allows fresh human turns.
    assert (
        hook_source_from_content({SOURCE_KIND_KEY: REACTIVE_TRIGGER_SOURCE_KIND, HOOK_SOURCE_KEY: "event_source:slack"})
        is None
    )
    assert started_by_slack_trigger(
        _tool_context(config=config, paths=paths, client=client, service=service, hook_source="event_source:slack"),
    )
    assert started_by_slack_trigger(
        _tool_context(config=config, paths=paths, client=client, service=service, hook_source="slack-watch/legacy"),
    )
    assert not started_by_slack_trigger(
        _tool_context(config=config, paths=paths, client=client, service=service, hook_source=None),
    )

    slack_tools = SlackTools(
        runtime_paths=paths,
        credentials_manager=get_runtime_credentials_manager(paths),
        runtime_config=config,
    )
    with tool_runtime_context(
        _tool_context(config=config, paths=paths, client=client, service=service, hook_source="event_source:slack"),
    ):
        assert (
            json.loads(await slack_tools.slack_post(CHANNEL, "hi"))["message"]
            == "Ask me in a new message to post to Slack"
        )
        assert json.loads(await tool.triggers(action="delete", trigger_id=rule.trigger_id))["message"] == (
            "Ask me in a new message to change your triggers."
        )
        assert json.loads(await tool.triggers(action="list"))["status"] == "ok"

    with tool_runtime_context(
        _tool_context(config=config, paths=paths, client=client, service=service, hook_source=None),
    ):
        assert json.loads(await tool.triggers(action="delete", trigger_id=rule.trigger_id))["status"] == "ok"


@pytest.mark.asyncio
async def test_emit_prefilter_pagination_reopen_and_fail_soft_lookups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """emit_slack_watch_event prefilters before Slack API calls, paginates authorizations, and reopens resolved threads."""
    config, paths = _make_config(tmp_path)
    install_slack_state(paths, app_token=APP_TOKEN)
    connect_alice(paths)
    agent_id = entity_identity_registry(config, paths).current_id("assistant").full_id
    matrix = FakeMatrix()
    reopened: list[tuple[str, str]] = []

    async def _fake_reopen(_client: object, room_id: str, thread_id: str, **_kwargs: object) -> bool:
        reopened.append((room_id, thread_id))
        return True

    monkeypatch.setattr(reactive_dispatch, "send_matrix_message", matrix.send)
    monkeypatch.setattr(reactive_dispatch, "reopen_thread_if_resolved", _fake_reopen)

    pages = [
        {
            "ok": True,
            "authorizations": [
                {"user_id": SLACK_IDS["alice"], "is_bot": True},
                {"user_id": "U0OTHER", "is_bot": False},
            ],
            "response_metadata": {"next_cursor": "page2"},
        },
        {
            "ok": True,
            "authorizations": [{"user_id": SLACK_IDS["alice"], "is_bot": False}],
            "response_metadata": {"next_cursor": ""},
        },
    ]

    async def _auth_list(request: httpx.Request) -> httpx.Response:
        idx = 1 if b"cursor=page2" in request.content else 0
        return httpx.Response(200, json=pages[idx], request=request)

    slack = FakeSlack(
        answers={
            "auth.test": AUTH_TEST,
            "conversations.info": {"ok": False, "error": "channel_not_found"},
            "apps.event.authorizations.list": _auth_list,
            "users.info": {"ok": False, "error": "user_not_found"},
        },
    )
    bots = {"assistant": FakeBot(room_state_client((ALICE, agent_id), room_id=WATCH_ROOM))}
    memberships = AgentReplyMembershipIndex()
    store = ReactiveTriggerStore(paths)
    service = ReactiveTriggerService(
        config_provider=lambda: config,
        runtime_paths=paths,
        bots=lambda: cast("dict[str, AgentBot]", bots),
        memberships=memberships,
        store=store,
    )
    service.event_sources.register(SlackEventSource())
    runtime = runtime_for(bots, memberships, slack, reactive_triggers=service)
    manager = get_runtime_credentials_manager(paths)
    app = load_slack_app(manager)
    assert app is not None

    async def _send(payload: dict[str, Any]) -> str:
        async with slack.client() as http_client:
            return await deliver_slack_event(
                payload,
                runtime=runtime,
                config=config,
                runtime_paths=paths,
                credentials_manager=manager,
                app=app,
                client=http_client,
                now=NOW,
            )

    # Zero rules -> zero apps.event.authorizations.list calls
    assert await _send(event_payload("message", author="dana", text="hi", channel=DM, channel_type="im")) == "ignored"
    assert slack.calls("apps.event.authorizations.list") == []

    # Rule with channel filter -> unrelated channel & unbound plain message make ZERO authorizations calls
    alice_slack_trigger(paths, kind="mention", match={"channel_id": [CHANNEL, OTHER_CHANNEL]})
    assert (
        await _send(event_payload("message", author="dana", text=f"<@{SLACK_IDS['alice']}>", channel="C0UNLISTED"))
        == "ignored"
    )
    assert await _send(event_payload("message", author="dana", text="plain", channel=OTHER_CHANNEL)) == "ignored"
    assert (
        await _send(event_payload("message", author="alice", text=f"<@{SLACK_IDS['alice']}>", channel=OTHER_CHANNEL))
        == "ignored"
    )
    assert slack.calls("apps.event.authorizations.list") == []

    # Matching channel mention paginates authorizations, fails soft on author & channel name, and binds thread root
    mention = event_payload("message", author="dana", text=f"<@{SLACK_IDS['alice']}> check", channel=OTHER_CHANNEL)
    assert await _send(mention) == "delivered"
    # Duplicate delivery of same event is deduped
    assert await _send(mention) == "ignored"
    await service.drain_due_firings_once()
    assert len(matrix.sent) == 1
    assert SLACK_IDS["dana"] in matrix.sent[0][1]["body"]
    assert f"#{OTHER_CHANNEL}" in matrix.sent[0][1]["body"]

    # Follow-up reply in the bound thread groups into the same Matrix thread and triggers reopen_on_fire
    reply = event_payload(
        "message",
        author="dana",
        text="any update?",
        ts=REPLY_TS,
        thread_ts=ROOT_TS,
        channel=OTHER_CHANNEL,
    )
    assert await _send(reply) == "delivered"
    await service.drain_due_firings_once()
    assert len(matrix.sent) == 2
    assert matrix.sent[1][1]["m.relates_to"]["event_id"] == "$sent1"
    assert reopened == [(WATCH_ROOM, "$sent1")]


def _legacy_watches_payload(now: int) -> dict[str, Any]:
    return {
        "watches": {
            "w_both": {
                "watch_id": "w_both",
                "owner_id": ALICE,
                "agent_name": "assistant",
                "room_id": WATCH_ROOM,
                "team_id": TEAM,
                "slack_user_id": SLACK_IDS["alice"],
                "mentions": True,
                "direct_messages": True,
                "channels": [CHANNEL, OTHER_CHANNEL],
                "created_at": now - 500,
            },
            "w_mention": {
                "watch_id": "w_mention",
                "owner_id": ALICE,
                "agent_name": "assistant",
                "room_id": "!room2:example.org",
                "team_id": TEAM,
                "slack_user_id": SLACK_IDS["alice"],
                "mentions": True,
                "direct_messages": False,
                "channels": [CHANNEL],
                "created_at": now - 400,
            },
            "w_dm": {
                "watch_id": "w_dm",
                "owner_id": BOB,
                "agent_name": "assistant",
                "room_id": "!room3:example.org",
                "team_id": TEAM,
                "slack_user_id": "U0BOB0000",
                "mentions": False,
                "direct_messages": True,
                "channels": [CHANNEL],
                "created_at": now - 300,
            },
            "w_invalid": {
                "watch_id": "w_invalid",
                "owner_id": BOB,
                "agent_name": "assistant",
                "room_id": "!room4:example.org",
                "team_id": TEAM,
                "slack_user_id": "U0BOB0000",
                "mentions": False,
                "direct_messages": False,
                "channels": [],
                "created_at": now - 200,
            },
        },
    }


def test_migrate_legacy_slack_watches_and_replay_roots_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review Focus #5: atomic one-time migration imports watches + live replay roots, archives post-commit, never resurrects."""
    config, paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(paths)
    assert paths.control_state_root is not None
    slack_dir = paths.control_state_root / "slack"
    watches_path = slack_dir / "watches.json"

    # Absent watches.json returns 0 and creates no lock file
    assert migrate_legacy_slack_watches(paths, store, config) == 0
    assert not slack_dir.exists()

    now = int(time.time())
    watches_payload = _legacy_watches_payload(now)
    slack_dir.mkdir(parents=True, exist_ok=True)
    watches_path.write_text(json.dumps(watches_payload), encoding="utf-8")

    # Seed legacy replay store with unexpired channel + DM roots, an expired root, and a wrong-room root
    replay = DeliveryReplayStore(slack_dir / "replay")
    replay.bind_thread_root(
        "slack:watch:w_both",
        f"w_both:{CHANNEL}:{ROOT_TS}",
        "$root_chan",
        room_id=WATCH_ROOM,
        reservation=None,
        now=now,
        ttl_seconds=3600,
    )
    replay.bind_thread_root(
        "slack:watch:w_both",
        f"w_both:{DM}",
        "$root_dm",
        room_id=WATCH_ROOM,
        reservation=None,
        now=now,
        ttl_seconds=3600,
    )
    replay.bind_thread_root(
        "slack:watch:w_both",
        f"w_both:{OTHER_CHANNEL}:1.0",
        "$root_wrong_room",
        room_id="!other:example.org",
        reservation=None,
        now=now,
        ttl_seconds=3600,
    )
    replay.bind_thread_root(
        "slack:watch:w_both",
        f"w_both:{OTHER_CHANNEL}:2.0",
        "$root_expired",
        room_id=WATCH_ROOM,
        reservation=None,
        now=now - 10_000,
        ttl_seconds=100,
    )

    # Failure during import_triggers_once must leave watches.json un-archived
    original_import = store.import_triggers_once

    def _boom(*_args: object, **_kwargs: object) -> int:
        msg = "sqlite boom"
        raise ReactiveTriggerStoreError(msg)

    monkeypatch.setattr(store, "import_triggers_once", _boom)
    with pytest.raises(ReactiveTriggerStoreError, match="sqlite boom"):
        migrate_legacy_slack_watches(paths, store, config)
    assert watches_path.exists()
    assert not watches_path.with_name("watches.json.migrated").exists()
    monkeypatch.setattr(store, "import_triggers_once", original_import)

    # Successful migration imports 3 valid watches + 2 valid thread roots and archives watches.json
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
    assert (
        both_src.kind,
        both_src.match,
        both_src.binding,
        both_src.source_origin,
        both_src.private_room_only,
    ) == (
        "message",
        {"channel_id": [CHANNEL, OTHER_CHANNEL]},
        {"team_id": TEAM, "slack_user_id": SLACK_IDS["alice"]},
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

    # Deleting a migrated trigger and restoring watches.json NEVER resurrects it
    assert store.delete_trigger("slack_watch_w_dm") is True
    watches_path.write_text(json.dumps(watches_payload), encoding="utf-8")
    assert migrate_legacy_slack_watches(paths, store, config) == 0
    assert store.get_trigger("slack_watch_w_dm") is None


def test_slack_integration_bind_migrates_and_registers_and_unbind_cleans_up(tmp_path: Path) -> None:
    """SLACK_INTEGRATION.bind runs legacy migration and registers SlackEventSource; unbind unregisters it."""
    config, paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(paths)
    service = ReactiveTriggerService(
        config_provider=lambda: config,
        runtime_paths=paths,
        bots=dict,
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )
    assert paths.control_state_root is not None
    watches_path = paths.control_state_root / "slack" / "watches.json"
    watches_path.parent.mkdir(parents=True, exist_ok=True)
    watches_path.write_text(
        json.dumps(
            {
                "watches": {
                    "w1": {
                        "watch_id": "w1",
                        "owner_id": ALICE,
                        "agent_name": "assistant",
                        "room_id": WATCH_ROOM,
                        "team_id": TEAM,
                        "slack_user_id": SLACK_IDS["alice"],
                        "mentions": True,
                        "direct_messages": True,
                        "channels": [],
                        "created_at": NOW,
                    },
                },
            },
        ),
        encoding="utf-8",
    )

    app = FastAPI()
    ensure_app_state(app)
    bots: dict[str, AgentBot] = {}
    SLACK_INTEGRATION.bind(app, bots, AgentReplyMembershipIndex(), service)
    runtime = bound_slack_runtime(app)
    assert runtime is not None
    assert runtime.reactive_triggers is service
    assert isinstance(service.event_sources.get(SLACK_EVENT_SOURCE_ID), SlackEventSource)
    assert store.get_trigger("slack_watch_w1") is not None

    # Re-binding with same bots & service is a clean no-op
    SLACK_INTEGRATION.bind(app, bots, AgentReplyMembershipIndex(), service)
    assert bound_slack_runtime(app) is runtime

    # Unbind unregisters SlackEventSource
    assert SLACK_INTEGRATION.unbind(app) is runtime
    assert bound_slack_runtime(app) is None
    assert service.event_sources.get(SLACK_EVENT_SOURCE_ID) is None


def test_retired_slack_watch_config_validation_fails_closed(tmp_path: Path) -> None:
    """Legacy slack_watch approval rules only gate when both slack and triggers are enabled."""
    slack_only = {
        "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
        "router": {"model": "default"},
        "agents": {"assistant": {"display_name": "Assistant", "tools": ["slack"]}},
    }
    triggers_only = {
        **slack_only,
        "agents": {"assistant": {"display_name": "Assistant", "tools": ["triggers"]}},
    }
    slack_and_triggers = {
        **slack_only,
        "agents": {"assistant": {"display_name": "Assistant", "tools": ["slack", "triggers"]}},
    }

    # 1. tools: [slack] (no triggers) + slack_* require_approval + default: auto_approve -> loads!
    Config.validate_with_runtime(
        {
            **slack_only,
            "tool_approval": {
                "default": "auto_approve",
                "rules": [{"match": "slack_*", "action": "require_approval"}],
            },
        },
        test_runtime_paths(tmp_path / "s1"),
    )

    # 1b. tools: [triggers] (no slack) + leftover slack_* require_approval + default: auto_approve -> loads!
    Config.validate_with_runtime(
        {
            **triggers_only,
            "tool_approval": {
                "default": "auto_approve",
                "rules": [{"match": "slack_*", "action": "require_approval"}],
            },
        },
        test_runtime_paths(tmp_path / "s1b"),
    )

    # 2. tools: [slack, triggers] + slack_* require_approval + default: auto_approve -> rejected!
    with pytest.raises((ToolConfigOverrideError, ValueError), match=r"slack_watch.*triggers"):
        Config.validate_with_runtime(
            {
                **slack_and_triggers,
                "tool_approval": {
                    "default": "auto_approve",
                    "rules": [{"match": "slack_*", "action": "require_approval"}],
                },
            },
            test_runtime_paths(tmp_path / "s2"),
        )

    # 3. tools: [slack, triggers] + slack_* require_approval + exact triggers auto_approve -> loads!
    Config.validate_with_runtime(
        {
            **slack_and_triggers,
            "tool_approval": {
                "default": "auto_approve",
                "rules": [
                    {"match": "slack_*", "action": "require_approval"},
                    {"match": "triggers", "action": "auto_approve"},
                ],
            },
        },
        test_runtime_paths(tmp_path / "s3"),
    )

    # 4. tools: [slack, triggers] + exact slack_watch require_approval + exact triggers auto_approve -> rejected!
    with pytest.raises((ToolConfigOverrideError, ValueError), match=r"slack_watch.*triggers"):
        Config.validate_with_runtime(
            {
                **slack_and_triggers,
                "tool_approval": {
                    "default": "auto_approve",
                    "rules": [
                        {"match": "slack_watch", "action": "require_approval"},
                        {"match": "triggers", "action": "auto_approve"},
                    ],
                },
            },
            test_runtime_paths(tmp_path / "s4"),
        )

    # 5. tools: [slack, triggers] + exact slack_watch require_approval + exact triggers require_approval -> loads!
    Config.validate_with_runtime(
        {
            **slack_and_triggers,
            "tool_approval": {
                "default": "auto_approve",
                "rules": [
                    {"match": "slack_watch", "action": "require_approval"},
                    {"match": "triggers", "action": "require_approval"},
                ],
            },
        },
        test_runtime_paths(tmp_path / "s5"),
    )

    # Exact slack_watch without triggers rule -> rejected when triggers is enabled
    with pytest.raises((ToolConfigOverrideError, ValueError), match=r"slack_watch.*triggers"):
        Config.validate_with_runtime(
            {
                **slack_and_triggers,
                "tool_approval": {
                    "default": "auto_approve",
                    "rules": [{"match": "slack_watch", "action": "require_approval"}],
                },
            },
            test_runtime_paths(tmp_path / "s6"),
        )

    # include_tools / exclude_tools referencing slack_watch on slack fail closed with subscribe_event guidance
    for key in ("include_tools", "exclude_tools"):
        override_cfg = {
            **slack_only,
            "agents": {
                "assistant": {
                    "display_name": "Assistant",
                    "tools": [{"slack": {key: ["slack_watch"]}}],
                },
            },
        }
        with pytest.raises((ToolConfigOverrideError, ValueError), match="subscribe_event"):
            Config.validate_with_runtime(override_cfg, test_runtime_paths(tmp_path / f"f_{key}"))


def test_room_triggers_api_hides_slack_trigger_from_non_owners_and_shows_paused_reason(tmp_path: Path) -> None:
    """Review Focus #3: GET /api/rooms/{room_id}/triggers hides Slack triggers from non-owners and reports paused_reason."""
    from tests.api.test_room_triggers import ROOM_A  # noqa: PLC0415
    from tests.api.test_room_triggers import _Harness as RoomTriggersHarness  # noqa: PLC0415

    room_id = ROOM_A
    room_events: dict[str, list[dict[str, object]]] = {
        room_id: [
            {
                "type": "m.room.member",
                "state_key": "@alice:localhost",
                "sender": "@alice:localhost",
                "event_id": "$m1",
                "origin_server_ts": 1_000,
                "content": {"membership": "join"},
            },
            {
                "type": "m.room.history_visibility",
                "state_key": "",
                "sender": "@alice:localhost",
                "event_id": "$hv1",
                "origin_server_ts": 1_001,
                "content": {"history_visibility": "invited"},
            },
        ],
    }
    h = RoomTriggersHarness(tmp_path, room_state_events=room_events)
    install_slack_state(h.paths, app_token=APP_TOKEN)
    service = ReactiveTriggerService(
        config_provider=lambda: h.config,
        runtime_paths=h.paths,
        bots=dict,
        memberships=AgentReplyMembershipIndex(),
        store=h.reactive_store,
    )
    service.event_sources.register(SlackEventSource())
    ensure_app_state(h.app).embedded = SimpleNamespace(reactive_triggers=service)  # type: ignore[assignment]

    h.add_reactive(
        "tr_slack_real",
        ExternalEventSourceSpec(
            source_id=SLACK_EVENT_SOURCE_ID,
            kind="message",
            match={"channel_id": CHANNEL},
            binding={"team_id": TEAM, "slack_user_id": SLACK_IDS["alice"]},
            source_origin="registered",
        ),
        target_room=room_id,
        description="Alice Slack watch",
        prompt="Summarize Slack message.",
        created_at=1.0,
    )

    # Owner sees source_id="slack" and live paused_reason without binding leakage; non-owner sees nothing.
    alice_resp = h.req("GET", room_id, "@alice:localhost")
    assert alice_resp.status_code == 200
    rows = {row["trigger_id"]: row for row in alice_resp.json()["triggers"]}
    assert rows["tr_slack_real"]["source_id"] == "slack"
    assert rows["tr_slack_real"]["paused_reason"] == "the watch's agent no longer has personal Slack"
    assert TEAM not in alice_resp.text
    assert SLACK_IDS["alice"] not in alice_resp.text

    bob_resp = h.req("GET", room_id, "@bob:localhost")
    assert bob_resp.status_code == 200
    assert "tr_slack_real" not in {row["trigger_id"] for row in bob_resp.json()["triggers"]}


@pytest.mark.asyncio
async def test_slack_subscribe_default_max_fires_per_hour_and_explicit_new_thread_false(tmp_path: Path) -> None:
    """Slack subscribe_event defaults max_fires_per_hour to min(60, policy) and honors explicit new_thread=False."""
    tool = TriggersTools()

    # High policy limit (120): default gets 60; explicit max_fires_per_hour=12 is respected; explicit new_thread=False works in a thread.
    cfg_high, paths_high = _make_config(tmp_path / "high", max_fires_per_hour=120)
    install_slack_state(paths_high, app_token=APP_TOKEN)
    connect_alice(paths_high)
    agent_id = entity_identity_registry(cfg_high, paths_high).current_id("assistant").full_id
    store_high = ReactiveTriggerStore(paths_high)
    service_high = ReactiveTriggerService(
        config_provider=lambda: cfg_high,
        runtime_paths=paths_high,
        bots=dict,
        memberships=AgentReplyMembershipIndex(),
        store=store_high,
    )
    service_high.event_sources.register(SlackEventSource())
    client_high = _multi_room_client({WATCH_ROOM: room_state_client((ALICE, agent_id), room_id=WATCH_ROOM)})

    with tool_runtime_context(
        _tool_context(
            config=cfg_high,
            paths=paths_high,
            client=client_high,
            service=service_high,
            thread_id="$existing_thread",
        ),
    ):
        default_sub = json.loads(
            await tool.triggers(
                action="subscribe_event",
                trigger_id="sl_default",
                source_id="slack",
                kind="message",
                prompt="Watch Slack.",
            ),
        )
        explicit_cap_and_thread = json.loads(
            await tool.triggers(
                action="subscribe_event",
                trigger_id="sl_in_thread",
                source_id="slack",
                kind="message",
                prompt="Deliver into this thread.",
                max_fires_per_hour=12,
                new_thread=False,
            ),
        )
        static_thread_key = json.loads(
            await tool.triggers(
                action="subscribe_event",
                trigger_id="sl_static_key",
                source_id="slack",
                kind="message",
                prompt="Group alerts.",
                thread_key="alert_feed",
            ),
        )
    assert default_sub["status"] == "ok"
    assert default_sub["trigger"]["max_fires_per_hour"] == 60
    assert default_sub["trigger"]["target"]["new_thread"] is True
    assert default_sub["trigger"]["target"]["thread_key_from_event"] is True
    assert default_sub["trigger"]["target"]["thread_id"] is None

    assert explicit_cap_and_thread["status"] == "ok"
    assert explicit_cap_and_thread["trigger"]["max_fires_per_hour"] == 12
    assert explicit_cap_and_thread["trigger"]["target"]["new_thread"] is False
    assert explicit_cap_and_thread["trigger"]["target"]["thread_key_from_event"] is False
    assert explicit_cap_and_thread["trigger"]["target"]["thread_id"] == "$existing_thread"

    assert static_thread_key["status"] == "ok"
    assert static_thread_key["trigger"]["target"]["new_thread"] is True
    assert static_thread_key["trigger"]["target"]["thread_key"] == "alert_feed"
    assert static_thread_key["trigger"]["target"]["thread_key_from_event"] is False
    assert static_thread_key["trigger"]["target"]["thread_id"] is None

    # Lower policy cap (25): default is capped to min(60, 25) == 25.
    cfg_low, paths_low = _make_config(tmp_path / "low", max_fires_per_hour=25)
    install_slack_state(paths_low, app_token=APP_TOKEN)
    connect_alice(paths_low)
    agent_id_low = entity_identity_registry(cfg_low, paths_low).current_id("assistant").full_id
    store_low = ReactiveTriggerStore(paths_low)
    service_low = ReactiveTriggerService(
        config_provider=lambda: cfg_low,
        runtime_paths=paths_low,
        bots=dict,
        memberships=AgentReplyMembershipIndex(),
        store=store_low,
    )
    service_low.event_sources.register(SlackEventSource())
    client_low = _multi_room_client({WATCH_ROOM: room_state_client((ALICE, agent_id_low), room_id=WATCH_ROOM)})
    with tool_runtime_context(_tool_context(config=cfg_low, paths=paths_low, client=client_low, service=service_low)):
        capped_sub = json.loads(
            await tool.triggers(
                action="subscribe_event",
                trigger_id="sl_capped",
                source_id="slack",
                kind="message",
                prompt="Watch Slack.",
            ),
        )
    assert capped_sub["status"] == "ok"
    assert capped_sub["trigger"]["max_fires_per_hour"] == 25


def test_slack_integration_bind_skips_migration_when_yaml_source_collides(tmp_path: Path) -> None:
    """Pre-existing YAML event_sources.slack blocks Slack registration and leaves watches.json unmigrated/unarchived."""
    config, paths = _make_config(
        tmp_path,
        event_sources={
            "slack": {
                "kind": "hmac",
                "secret_env": "SLACK_WEBHOOK_SECRET",
                "subscribers": [ALICE],
            },
        },
    )
    store = ReactiveTriggerStore(paths)
    service = ReactiveTriggerService(
        config_provider=lambda: config,
        runtime_paths=paths,
        bots=dict,
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )
    assert paths.control_state_root is not None
    watches_path = paths.control_state_root / "slack" / "watches.json"
    watches_path.parent.mkdir(parents=True, exist_ok=True)
    watches_path.write_text(
        json.dumps(
            {
                "watches": {
                    "w1": {
                        "watch_id": "w1",
                        "owner_id": ALICE,
                        "agent_name": "assistant",
                        "room_id": WATCH_ROOM,
                        "team_id": TEAM,
                        "slack_user_id": SLACK_IDS["alice"],
                        "mentions": True,
                        "direct_messages": True,
                        "channels": [],
                        "created_at": NOW,
                    },
                },
            },
        ),
        encoding="utf-8",
    )

    app = FastAPI()
    ensure_app_state(app)
    SLACK_INTEGRATION.bind(app, {}, AgentReplyMembershipIndex(), service)
    assert service.event_sources.get(SLACK_EVENT_SOURCE_ID) is None
    assert watches_path.exists()
    assert not watches_path.with_name("watches.json.migrated").exists()
    assert store.has_migration("slack_watches_v1") is False
    assert store.get_trigger("slack_watch_w1") is None


def test_migrate_legacy_slack_watches_aborts_on_replay_store_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DeliveryReplayStoreError aborts legacy watch migration before committing SQLite or archiving watches.json."""
    config, paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(paths)
    assert paths.control_state_root is not None
    watches_path = paths.control_state_root / "slack" / "watches.json"
    watches_path.parent.mkdir(parents=True, exist_ok=True)
    watches_path.write_text(
        json.dumps(
            {
                "watches": {
                    "w1": {
                        "watch_id": "w1",
                        "owner_id": ALICE,
                        "agent_name": "assistant",
                        "room_id": WATCH_ROOM,
                        "team_id": TEAM,
                        "slack_user_id": SLACK_IDS["alice"],
                        "mentions": True,
                        "direct_messages": True,
                        "channels": [],
                        "created_at": NOW,
                    },
                },
            },
        ),
        encoding="utf-8",
    )

    def _boom(self: DeliveryReplayStore, scope: str, *, now: int | None = None) -> dict[str, tuple[str, str, int]]:
        del self, scope, now
        msg = "corrupt replay store"
        raise DeliveryReplayStoreError(msg)

    monkeypatch.setattr(DeliveryReplayStore, "list_bound_thread_roots", _boom)
    with pytest.raises(DeliveryReplayStoreError, match="corrupt replay store"):
        migrate_legacy_slack_watches(paths, store, config)

    assert watches_path.exists()
    assert not watches_path.with_name("watches.json.migrated").exists()
    assert store.has_migration("slack_watches_v1") is False
    assert store.get_trigger("slack_watch_w1") is None


@pytest.mark.asyncio
async def test_admin_cannot_toggle_or_delete_private_slack_trigger_but_can_manage_visible_trigger(
    tmp_path: Path,
) -> None:
    """Non-owner trigger admins cannot read/toggle/delete private Slack triggers (registered or unregistered), while managing visible triggers."""

    class _VisibleSource(EventSource):
        source_id = "public_ops"
        description = "Public ops events"
        allowed_kinds = ("alert",)
        private_room_only = False

    config, paths = _make_config(tmp_path)
    install_slack_state(paths, app_token=APP_TOKEN)
    connect_alice(paths)
    agent_id = entity_identity_registry(config, paths).current_id("assistant").full_id
    client = _multi_room_client(
        {
            WATCH_ROOM: room_state_client((ALICE, agent_id), room_id=WATCH_ROOM),
            ADMIN_ROOM: room_state_client((ADMIN, agent_id), room_id=ADMIN_ROOM),
        },
    )
    store = ReactiveTriggerStore(paths)
    service = ReactiveTriggerService(
        config_provider=lambda: config,
        runtime_paths=paths,
        bots=dict,
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )
    slack_emitter = service.event_sources.register(SlackEventSource())
    service.event_sources.register(_VisibleSource())
    tool = TriggersTools()

    with tool_runtime_context(_tool_context(config=config, paths=paths, client=client, service=service)):
        sub_slack = json.loads(
            await tool.triggers(
                action="subscribe_event",
                trigger_id="alice_slack",
                source_id="slack",
                kind="message",
                prompt="Watch my Slack",
            ),
        )
        sub_public = json.loads(
            await tool.triggers(
                action="subscribe_event",
                trigger_id="alice_public",
                source_id="public_ops",
                kind="alert",
                prompt="Watch ops alerts",
            ),
        )
    assert (sub_slack["status"], sub_public["status"]) == ("ok", "ok")

    admin_ctx = _tool_context(
        config=config,
        paths=paths,
        client=client,
        service=service,
        room_id=ADMIN_ROOM,
        requester_id=ADMIN,
    )
    with tool_runtime_context(admin_ctx):
        listed = json.loads(await tool.triggers(action="list"))
        hist = json.loads(await tool.triggers(action="history", trigger_id="alice_slack"))
        tested = json.loads(await tool.triggers(action="test", trigger_id="alice_slack"))
        disabled = json.loads(await tool.triggers(action="set_enabled", trigger_id="alice_slack", enabled=False))
        deleted = json.loads(await tool.triggers(action="delete", trigger_id="alice_slack"))
        pub_disabled = json.loads(await tool.triggers(action="set_enabled", trigger_id="alice_public", enabled=False))
        pub_enabled = json.loads(await tool.triggers(action="set_enabled", trigger_id="alice_public", enabled=True))
        pub_deleted = json.loads(await tool.triggers(action="delete", trigger_id="alice_public"))

    assert "alice_slack" not in {t["trigger_id"] for t in listed["reactive_triggers"]}
    assert "alice_public" in {t["trigger_id"] for t in listed["reactive_triggers"]}
    assert (hist["status"], tested["status"], disabled["status"], deleted["status"]) == (
        "error",
        "error",
        "error",
        "error",
    )
    assert "Watch my Slack" not in json.dumps(disabled)
    reloaded = store.get_trigger("alice_slack")
    assert reloaded is not None
    assert reloaded.enabled is True
    assert (pub_disabled["status"], pub_enabled["status"], pub_deleted["status"]) == ("ok", "ok", "ok")
    assert store.get_trigger("alice_public") is None

    # After unregistering SlackEventSource, admin is STILL blocked from reading/toggling/deleting alice_slack.
    assert service.event_sources.unregister(SLACK_EVENT_SOURCE_ID, slack_emitter.token) is True
    with tool_runtime_context(admin_ctx):
        listed_after = json.loads(await tool.triggers(action="list"))
        hist_after = json.loads(await tool.triggers(action="history", trigger_id="alice_slack"))
        disabled_after = json.loads(await tool.triggers(action="set_enabled", trigger_id="alice_slack", enabled=False))
        deleted_after = json.loads(await tool.triggers(action="delete", trigger_id="alice_slack"))
    assert "alice_slack" not in {t["trigger_id"] for t in listed_after["reactive_triggers"]}
    assert (hist_after["status"], disabled_after["status"], deleted_after["status"]) == ("error", "error", "error")
    reloaded_after = store.get_trigger("alice_slack")
    assert reloaded_after is not None
    assert reloaded_after.enabled is True


@pytest.mark.asyncio
async def test_admin_rotate_key_on_hidden_private_slack_trigger_returns_not_found(tmp_path: Path) -> None:
    """Non-owner trigger admins get the absent-trigger not-found error on rotate_key for hidden private Slack triggers."""
    config, paths = _make_config(tmp_path)
    install_slack_state(paths, app_token=APP_TOKEN)
    connect_alice(paths)
    agent_id = entity_identity_registry(config, paths).current_id("assistant").full_id
    client = _multi_room_client(
        {
            WATCH_ROOM: room_state_client((ALICE, agent_id), room_id=WATCH_ROOM),
            ADMIN_ROOM: room_state_client((ADMIN, agent_id), room_id=ADMIN_ROOM),
        },
    )
    service = ReactiveTriggerService(
        config_provider=lambda: config,
        runtime_paths=paths,
        bots=dict,
        memberships=AgentReplyMembershipIndex(),
        store=ReactiveTriggerStore(paths),
    )
    slack_emitter = service.event_sources.register(SlackEventSource())
    tool = TriggersTools()
    sample_public_key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

    owner_ctx = _tool_context(config=config, paths=paths, client=client, service=service)
    with tool_runtime_context(owner_ctx):
        sub = json.loads(
            await tool.triggers(
                action="subscribe_event",
                trigger_id="alice_slack",
                source_id="slack",
                kind="message",
                prompt="Watch my Slack",
            ),
        )
        owner_rotate = json.loads(
            await tool.triggers(action="rotate_key", trigger_id="alice_slack", public_key=sample_public_key),
        )
    assert sub["status"] == "ok"
    assert owner_rotate["status"] == "error"
    assert owner_rotate["message"] == "Key rotation applies only to direct signed external triggers."

    admin_ctx = _tool_context(
        config=config,
        paths=paths,
        client=client,
        service=service,
        room_id=ADMIN_ROOM,
        requester_id=ADMIN,
    )
    with tool_runtime_context(admin_ctx):
        hidden_rotate = json.loads(
            await tool.triggers(action="rotate_key", trigger_id="alice_slack", public_key=sample_public_key),
        )
        absent_rotate = json.loads(
            await tool.triggers(action="rotate_key", trigger_id="absent_trigger", public_key=sample_public_key),
        )
        hidden_history = json.loads(await tool.triggers(action="history", trigger_id="alice_slack"))
        absent_history = json.loads(await tool.triggers(action="history", trigger_id="absent_trigger"))
        hidden_test = json.loads(await tool.triggers(action="test", trigger_id="alice_slack"))
        absent_test = json.loads(await tool.triggers(action="test", trigger_id="absent_trigger"))
        hidden_update = json.loads(await tool.triggers(action="update", trigger_id="alice_slack", prompt="Hijack"))
        absent_update = json.loads(await tool.triggers(action="update", trigger_id="absent_trigger", prompt="Hijack"))
    assert hidden_rotate["status"] == "error"
    assert hidden_rotate["message"] == "external trigger not found: alice_slack"
    assert absent_rotate["message"] == "external trigger not found: absent_trigger"
    for hidden_resp, absent_resp in (
        (hidden_history, absent_history),
        (hidden_test, absent_test),
        (hidden_update, absent_update),
    ):
        assert hidden_resp["status"] == "error"
        assert hidden_resp["message"] == "Reactive trigger not found: alice_slack"
        assert absent_resp["message"] == "Reactive trigger not found: absent_trigger"

    assert service.event_sources.unregister(SLACK_EVENT_SOURCE_ID, slack_emitter.token) is True
    with tool_runtime_context(admin_ctx):
        unregistered_rotate = json.loads(
            await tool.triggers(action="rotate_key", trigger_id="alice_slack", public_key=sample_public_key),
        )
    assert unregistered_rotate["status"] == "error"
    assert unregistered_rotate["message"] == "external trigger not found: alice_slack"


@pytest.mark.asyncio
async def test_pre_commit_migration_failure_ignores_until_lazy_retry_succeeds(tmp_path: Path) -> None:
    """Pre-commit migration failure returns 'ignored' (never 'retry'/503) and lazily migrates & delivers once fixed."""
    config, paths = _make_config(tmp_path)
    install_slack_state(paths, app_token=APP_TOKEN)
    connect_alice(paths)
    agent_id = entity_identity_registry(config, paths).current_id("assistant").full_id
    assert paths.control_state_root is not None
    watches_path = paths.control_state_root / "slack" / "watches.json"
    watches_path.parent.mkdir(parents=True, exist_ok=True)
    watches_path.write_text("{not valid json", encoding="utf-8")

    store = ReactiveTriggerStore(paths)
    memberships = AgentReplyMembershipIndex()
    bots = {"assistant": FakeBot(room_state_client((ALICE, agent_id), room_id=WATCH_ROOM))}
    service = ReactiveTriggerService(
        config_provider=lambda: config,
        runtime_paths=paths,
        bots=lambda: cast("dict[str, AgentBot]", bots),
        memberships=memberships,
        store=store,
    )
    app_api = FastAPI()
    ensure_app_state(app_api)
    SLACK_INTEGRATION.bind(app_api, cast("dict[str, AgentBot]", bots), memberships, service)

    registered = service.event_sources.get(SLACK_EVENT_SOURCE_ID)
    assert isinstance(registered, SlackEventSource)
    assert registered.migration_pending is True
    assert watches_path.exists()
    assert not store.has_migration("slack_watches_v1")

    slack = FakeSlack(
        answers={
            "auth.test": AUTH_TEST,
            "apps.event.authorizations.list": {
                "ok": True,
                "authorizations": [{"user_id": SLACK_IDS["alice"], "is_bot": False}],
            },
            "users.info": {"ok": False, "error": "user_not_found"},
        },
    )
    runtime = runtime_for(bots, memberships, slack, reactive_triggers=service)
    manager = get_runtime_credentials_manager(paths)
    slack_creds = load_slack_app(manager)
    assert slack_creds is not None
    dm = event_payload("message", author="dana", text="urgent dm", channel=DM, channel_type="im")
    async with slack.client() as http_client:
        result = await deliver_slack_event(
            dm,
            runtime=runtime,
            config=config,
            runtime_paths=paths,
            credentials_manager=manager,
            app=slack_creds,
            client=http_client,
            now=NOW,
        )
    assert result == "ignored"
    assert registered.migration_pending is True
    assert slack.calls("apps.event.authorizations.list") == []

    # Once the underlying watches.json error is fixed, the next event lazily completes migration and delivers.
    watches_path.write_text(
        json.dumps(
            {
                "watches": {
                    "w1": {
                        "watch_id": "w1",
                        "owner_id": ALICE,
                        "agent_name": "assistant",
                        "room_id": WATCH_ROOM,
                        "team_id": TEAM,
                        "slack_user_id": SLACK_IDS["alice"],
                        "mentions": True,
                        "direct_messages": True,
                        "channels": [],
                        "created_at": NOW,
                    },
                },
            },
        ),
        encoding="utf-8",
    )
    recovered_dm = event_payload(
        "message",
        author="dana",
        text="after fix",
        ts="1700000002.000100",
        channel=DM,
        channel_type="im",
    )
    async with slack.client() as http_client:
        recovered_result = await deliver_slack_event(
            recovered_dm,
            runtime=runtime,
            config=config,
            runtime_paths=paths,
            credentials_manager=manager,
            app=slack_creds,
            client=http_client,
            now=NOW + 1,
        )
    assert recovered_result == "delivered"
    assert registered.migration_pending is False
    assert store.has_migration("slack_watches_v1") is True
    assert not watches_path.exists()
    assert watches_path.with_name("watches.json.migrated").exists()
    assert len(store.list_firings(trigger_id="slack_watch_w1")) == 1


@pytest.mark.asyncio
async def test_post_commit_archive_rename_failure_delivers_and_self_heals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-commit watches.json rename OSError still delivers migrated watches and self-heals on next migration call."""
    from pathlib import Path as _Path  # noqa: PLC0415

    config, paths = _make_config(tmp_path)
    install_slack_state(paths, app_token=APP_TOKEN)
    connect_alice(paths)
    agent_id = entity_identity_registry(config, paths).current_id("assistant").full_id
    assert paths.control_state_root is not None
    watches_path = paths.control_state_root / "slack" / "watches.json"
    watches_path.parent.mkdir(parents=True, exist_ok=True)
    watches_path.write_text(
        json.dumps(
            {
                "watches": {
                    "w1": {
                        "watch_id": "w1",
                        "owner_id": ALICE,
                        "agent_name": "assistant",
                        "room_id": WATCH_ROOM,
                        "team_id": TEAM,
                        "slack_user_id": SLACK_IDS["alice"],
                        "mentions": True,
                        "direct_messages": True,
                        "channels": [],
                        "created_at": NOW,
                    },
                },
            },
        ),
        encoding="utf-8",
    )

    real_replace = _Path.replace

    def _failing_replace(self: _Path, target: _Path) -> _Path:
        if self == watches_path:
            msg = "archive rename failed"
            raise OSError(msg)
        return real_replace(self, target)

    monkeypatch.setattr(_Path, "replace", _failing_replace)

    store = ReactiveTriggerStore(paths)
    memberships = AgentReplyMembershipIndex()
    bots = {"assistant": FakeBot(room_state_client((ALICE, agent_id), room_id=WATCH_ROOM))}
    service = ReactiveTriggerService(
        config_provider=lambda: config,
        runtime_paths=paths,
        bots=lambda: cast("dict[str, AgentBot]", bots),
        memberships=memberships,
        store=store,
    )
    app_api = FastAPI()
    ensure_app_state(app_api)
    SLACK_INTEGRATION.bind(app_api, cast("dict[str, AgentBot]", bots), memberships, service)

    registered = service.event_sources.get(SLACK_EVENT_SOURCE_ID)
    assert isinstance(registered, SlackEventSource)
    assert registered.migration_pending is False
    assert store.has_migration("slack_watches_v1") is True
    assert store.get_trigger("slack_watch_w1") is not None
    assert watches_path.exists()
    assert not watches_path.with_name("watches.json.migrated").exists()

    # Migrated watch delivers normally despite the post-commit rename error.
    slack = FakeSlack(
        answers={
            "auth.test": AUTH_TEST,
            "apps.event.authorizations.list": {
                "ok": True,
                "authorizations": [{"user_id": SLACK_IDS["alice"], "is_bot": False}],
            },
            "users.info": {"ok": False, "error": "user_not_found"},
        },
    )
    runtime = runtime_for(bots, memberships, slack, reactive_triggers=service)
    manager = get_runtime_credentials_manager(paths)
    slack_creds = load_slack_app(manager)
    assert slack_creds is not None
    dm = event_payload("message", author="dana", text="hello after commit", channel=DM, channel_type="im")
    async with slack.client() as http_client:
        result = await deliver_slack_event(
            dm,
            runtime=runtime,
            config=config,
            runtime_paths=paths,
            credentials_manager=manager,
            app=slack_creds,
            client=http_client,
            now=NOW,
        )
    assert result == "delivered"
    assert len(store.list_firings(trigger_id="slack_watch_w1")) == 1

    # Deleting the trigger and running migrate_legacy_slack_watches again self-heals archival WITHOUT re-importing.
    assert store.delete_trigger("slack_watch_w1") is True
    monkeypatch.setattr(_Path, "replace", real_replace)
    assert migrate_legacy_slack_watches(paths, store, config) == 0
    assert not watches_path.exists()
    assert watches_path.with_name("watches.json.migrated").exists()
    assert store.get_trigger("slack_watch_w1") is None
