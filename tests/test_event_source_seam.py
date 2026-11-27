"""Tests for the EventSource emitter seam and Task 1 reactive trigger extensions."""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import uuid
from contextlib import closing
from dataclasses import replace
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request

from mindroom import integrations, plugin_http
from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.api.app_state import ensure_app_state
from mindroom.api.config_lifecycle import ApiSnapshot, ApiState
from mindroom.api.plugin_routes import mount_plugin_routes
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.external_trigger_policy import ExternalTriggerPolicyConfig, GitHubEventSourceConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import HOOK_SOURCE_KEY, PER_FIRE_THREAD_ROOT_KEY, SOURCE_KIND_KEY
from mindroom.custom_tools.triggers import TriggersTools
from mindroom.dispatch_source import REACTIVE_TRIGGER_SOURCE_KIND, hook_source_from_content
from mindroom.event_sources import (
    EventSource,
    EventSourceAccessDenial,
    EventSourceContext,
    EventSourceDescriptor,
    EventSourceEmitter,
    EventSourceInactiveError,
    EventSourceRegistry,
    ValidatedSubscription,
)
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.matrix.room_reconciliation import RoomStateSnapshot
from mindroom.matrix.state import MatrixState
from mindroom.message_target import MessageTarget
from mindroom.oauth.providers import OAuthConnectionRequired
from mindroom.plugin_lifecycle import PluginLifecycle, PluginLifespans
from mindroom.reactive_triggers import (
    DispatchResult,
    ExternalEventSourceSpec,
    FiringRecord,
    ReactiveTriggerRecord,
    ReactiveTriggerService,
    ReactiveTriggerStore,
    RoomMessageSourceSpec,
    TriageOutcome,
    TriageSpec,
    TriggerEvent,
    TriggerTargetSpec,
    check_external_event_source_access,
    check_reactive_source_access,
    dispatch_reactive_trigger,
    fan_out_external_event,
    matches_external_event,
)
from mindroom.slack.integration import SLACK_INTEGRATION
from mindroom.tool_system.plugins import PLUGIN_NAME_SCOPE_KEY, PluginInactiveError
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    _TEST_PROBE_PREFIX,
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    publish_test_plugins,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.matrix_state_helpers import save_matrix_state
from tests.runtime_fakes import ready_runtime

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_OWNER = "@alice:localhost"
_OWNER_ALIAS = "@bridge_alice:localhost"
_OTHER = "@bob:localhost"
_ROOM_B = "!roomB:localhost"


def _make_config(
    tmp_path: Path,
    *,
    reactive_enabled: bool = True,
    aliases: dict[str, list[str]] | None = None,
    event_sources: dict[str, Any] | None = None,
) -> tuple[Config, RuntimePaths]:
    config = bind_runtime_paths(
        Config(
            agents={"research": AgentConfig(display_name="Research", access={"users": [_OWNER, _OTHER]})},
            models={
                "default": ModelConfig(provider="openai", id="gpt-test"),
                "cheap": ModelConfig(provider="openai", id="gpt-mini"),
            },
            authorization=AuthorizationConfig(aliases=aliases or {}),
            external_trigger_policy=ExternalTriggerPolicyConfig(
                enabled=True,
                reactive_enabled=reactive_enabled,
                event_sources=event_sources or {},
            ),
        ),
        test_runtime_paths(tmp_path),
    )
    return config, runtime_paths_for(config)


def _make_rule(
    *,
    trigger_id: str = "ev_rule",
    owner_user_id: str = _OWNER,
    source_id: str = "acme_builds",
    kind: str = "build.failed",
    match: dict[str, str | bool | int | list[str]] | None = None,
    binding: dict[str, str | bool | int] | None = None,
    source_origin: Literal["yaml", "registered"] = "registered",
    target: TriggerTargetSpec | None = None,
    triage: TriageSpec | None = None,
    enabled: bool = True,
    expires_at: float | None = None,
) -> ReactiveTriggerRecord:
    return ReactiveTriggerRecord(
        trigger_id=trigger_id,
        owner_user_id=owner_user_id,
        source=ExternalEventSourceSpec(
            source_id=source_id,
            kind=kind,
            match=match or {},
            binding=binding or {},
            source_origin=source_origin,
        ),
        triage=triage,
        target=target
        or TriggerTargetSpec(
            agent="research",
            room_id=_ROOM_B,
            new_thread=True,
            thread_key_from_event=True,
            prompt="Handle event.",
        ),
        enabled=enabled,
        expires_at=expires_at,
        created_at=1000.0,
        updated_at=1000.0,
    )


def _bot(
    *,
    latest_thread_event_id: str | None = "$latest",
    joined_rooms: Collection[str] = (_ROOM_B,),
) -> SimpleNamespace:
    client = AsyncMock()
    client.rooms = {room_id: SimpleNamespace(room_id=room_id) for room_id in joined_rooms}
    return SimpleNamespace(
        running=True,
        client=client,
        conversation_reader=SimpleNamespace(latest_thread_event_id=AsyncMock(return_value=latest_thread_event_id)),
    )


def _room_state(room_id: str, *members: str) -> RoomStateSnapshot:
    return RoomStateSnapshot(
        room_id=room_id,
        events={("m.room.member", member): {"membership": "join"} for member in members},
        creators=frozenset(),
    )


def _make_firing(rule: ReactiveTriggerRecord, event: TriggerEvent, *, firing_id: int = 1) -> FiringRecord:
    return FiringRecord(
        id=firing_id,
        trigger_id=rule.trigger_id,
        rule_uid=rule.rule_uid,
        dedupe_key=event.event_id,
        status="claimed",
        event=event,
        triage_reason=None,
        matrix_event_id=None,
        attempt_count=0,
        next_retry_at=None,
        error_message=None,
        created_at=1000.0,
        updated_at=1000.0,
    )


class _SimpleSource(EventSource):
    source_id = "acme_builds"
    description = "Acme CI build notifications"
    allowed_kinds = ("build.failed", "build.passed")


class _CustomSource(EventSource):
    def __init__(
        self,
        source_id: str,
        *,
        delivery_decision: EventSourceAccessDenial | None = None,
        require_audience_member: bool = False,
    ) -> None:
        self.source_id = source_id
        self.description = f"Custom {source_id}"
        self.allowed_kinds = ("message", "mention")
        self.delivery_decision = delivery_decision
        self.require_audience_member = require_audience_member
        self.match_calls: list[tuple[str, object, bool]] = []
        self.delivery_contexts: list[EventSourceContext] = []

    def matches_rule(
        self,
        rule: ReactiveTriggerRecord,
        event: TriggerEvent,
        *,
        audience: object = None,
        has_thread_binding: bool = False,
    ) -> bool:
        self.match_calls.append((rule.trigger_id, audience, has_thread_binding))
        if not isinstance(rule.source, ExternalEventSourceSpec):
            return False
        if self.require_audience_member:
            expected_user = rule.source.binding.get("ext_user_id")
            if not isinstance(audience, set) or expected_user not in audience:
                return False
        if rule.source.kind == "mention" and has_thread_binding:
            return True
        return matches_external_event(rule.source, event)

    async def check_delivery_access(
        self,
        ctx: EventSourceContext,
        *,
        rule: ReactiveTriggerRecord,
        event: TriggerEvent | None = None,
    ) -> EventSourceAccessDenial | None:
        del rule, event
        self.delivery_contexts.append(ctx)
        return self.delivery_decision


def test_external_event_models_and_list_matching() -> None:
    """ExternalEventSourceSpec.match/binding, TriggerTargetSpec.thread_key_from_event, and list matching work strictly."""
    spec = ExternalEventSourceSpec(
        source_id="slack",
        kind="message",
        match={"channel_id": [" C1 ", "C2"], "nested.env": ["prod", "staging"], "retry": 1, "urgent": True},
        binding={"team_id": "T1", "slack_user_id": "U1", "verified": True, "version": 2},
    )
    assert spec.match["channel_id"] == ["C1", "C2"]
    assert ExternalEventSourceSpec.model_validate_json(spec.model_dump_json()) == spec

    bad_matches: list[dict[str, Any]] = [{"c": []}, {"c": ["  "]}, {"c": ["C1", ""]}, {"c": [1]}]
    for bad_match in bad_matches:
        with pytest.raises(ValidationError):
            ExternalEventSourceSpec(source_id="slack", kind="message", match=bad_match)

    assert TriggerTargetSpec(
        agent="research",
        room_id=_ROOM_B,
        new_thread=True,
        thread_key_from_event=True,
        prompt="go",
    ).thread_key_from_event
    with pytest.raises(ValidationError, match="thread_key_from_event requires new_thread=True"):
        TriggerTargetSpec(agent="research", room_id=_ROOM_B, new_thread=False, thread_key_from_event=True, prompt="go")
    with pytest.raises(ValidationError, match="mutually exclusive"):
        TriggerTargetSpec(
            agent="research",
            room_id=_ROOM_B,
            new_thread=True,
            thread_key="static",
            thread_key_from_event=True,
            prompt="go",
        )
    with pytest.raises(ValidationError, match="mutually exclusive"):
        TriggerTargetSpec(
            agent="research",
            room_id=_ROOM_B,
            thread_id="$t1",
            new_thread=True,
            thread_key_from_event=True,
            prompt="go",
        )

    ev = TriggerEvent(
        source_type="external_event",
        source_id="slack",
        kind="message",
        event_id="e1",
        timestamp=1000.0,
        thread_key="C1:1700.1",
        data={"channel_id": "C2", "nested": {"env": "prod"}, "retry": 1, "urgent": True},
    )
    assert ev.thread_key == "C1:1700.1"
    assert (
        TriggerEvent(
            source_type="external_event",
            source_id="slack",
            kind="message",
            event_id="e2",
            timestamp=1.0,
            thread_key="   ",
        ).thread_key
        is None
    )

    assert matches_external_event(spec, ev) is True
    assert matches_external_event(spec, ev.model_copy(update={"data": {**ev.data, "channel_id": "C3"}})) is False
    assert (
        matches_external_event(ExternalEventSourceSpec(source_id="slack", kind="message", match={"retry": ["1"]}), ev)
        is False
    )
    assert (
        matches_external_event(
            ExternalEventSourceSpec(source_id="slack", kind="message", match={"urgent": ["True"]}),
            ev,
        )
        is False
    )
    assert (
        matches_external_event(
            ExternalEventSourceSpec(source_id="slack", kind="message", match={"channel_id": ["C2"]}),
            ev.model_copy(update={"data": {"channel_id": ["C2"]}}),
        )
        is False
    )
    assert (
        matches_external_event(ExternalEventSourceSpec(source_id="slack", kind="message", match={"retry": True}), ev)
        is False
    )


@pytest.mark.asyncio
async def test_event_source_base_defaults(tmp_path: Path) -> None:
    """Default EventSource methods work cleanly for simple connectors and preserve existing_binding."""
    config, runtime_paths = _make_config(tmp_path)
    ctx = EventSourceContext(owner_user_id=_OWNER, config=config, runtime_paths=runtime_paths)
    src = _SimpleSource()

    assert await src.describe_for_user(ctx) == EventSourceDescriptor(
        source_id="acme_builds",
        description="Acme CI build notifications",
        allowed_kinds=("build.failed", "build.passed"),
    )
    assert await src.validate_subscription(
        ctx,
        kind=" build.failed ",
        match={"repo": ["a/b"]},
        existing_binding={"w": "1"},
    ) == ValidatedSubscription(
        kind="build.failed",
        match={"repo": ["a/b"]},
        binding={"w": "1"},
    )
    for bad_kind in ("unknown.kind", "   "):
        with pytest.raises(ValueError, match="Event kind"):
            await src.validate_subscription(ctx, kind=bad_kind, match={})

    rule = _make_rule(source_id="acme_builds", kind="build.failed", match={"repo": "a/b"})
    ev = TriggerEvent(
        source_type="external_event",
        source_id="acme_builds",
        kind="build.failed",
        event_id="e1",
        timestamp=1000.0,
        data={"repo": "a/b"},
    )
    assert src.matches_rule(rule, ev) is True
    assert await src.check_delivery_access(ctx, rule=rule, event=ev) is None
    assert await src.check_history_access(ctx, rule=rule, event=ev) is True


def test_event_source_registry_validation_and_collisions(tmp_path: Path) -> None:
    """Registry enforces source_id regex, plugin namespacing, YAML/built-in/cross-plugin collisions, and ownership."""
    config, _ = _make_config(
        tmp_path,
        event_sources={
            "github": GitHubEventSourceConfig(
                kind="github",
                secret_env="GH_SECRET",  # noqa: S106
                allowed_repos=["org/repo"],
                subscribers=[_OWNER],
            ),
        },
    )
    registry = EventSourceRegistry(config_provider=lambda: config, emit_handler=AsyncMock(return_value=0))

    for bad_id in ("", "bad.id", "bad id", "a" * 65, "ok\n"):
        with pytest.raises(ValueError, match="source_id"):
            registry.register(_CustomSource(bad_id))
    for bad_plugin_id in ("other", "acmefoo", "my_acme"):
        with pytest.raises(ValueError, match="Plugin 'acme'"):
            registry.register(_CustomSource(bad_plugin_id), owner_plugin="acme")

    e_exact = registry.register(_CustomSource("acme"), owner_plugin="acme")
    e_under = registry.register(_CustomSource("acme_builds"), owner_plugin="acme")
    e_dash = registry.register(_CustomSource("acme-alerts"), owner_plugin="acme")
    assert [s.source_id for s in registry.all()] == ["acme", "acme-alerts", "acme_builds"]
    assert registry.get("acme_builds") is not None

    with pytest.raises(ValueError, match="YAML"):
        registry.register(_CustomSource("github"))
    with pytest.raises(ValueError, match="YAML"):
        registry.register(_CustomSource("github"), owner_plugin="github")
    with pytest.raises(ValueError, match="built-in"):
        registry.register(_CustomSource("slack"), owner_plugin="slack")

    builtin_slack = registry.register(_CustomSource("slack"))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_CustomSource("slack"))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_CustomSource("acme_builds"), owner_plugin="acme_builds")

    assert registry.emitter_for_plugin("acme", "acme") is e_exact
    assert registry.emitter_for_plugin("acme", "acme_builds") is e_under
    assert registry.emitter_for_plugin("acme", "acme-alerts") is e_dash
    assert registry.emitter_for_plugin("other", "acme_builds") is None
    assert registry.emitter_for_plugin("slack", "slack") is None
    assert registry.unregister("slack", "wrong-token") is False
    assert registry.unregister("slack", builtin_slack.token) is True


@pytest.mark.asyncio
async def test_event_source_registry_reload_token_fencing(tmp_path: Path) -> None:
    """Review Focus #1: hot-reload replaces token atomically; old teardown is a no-op and old emitter fails closed."""
    config, _ = _make_config(tmp_path)
    emitted: list[tuple[str, TriggerEvent]] = []

    async def _handler(
        source_id: str,
        event: TriggerEvent,
        *,
        owner_user_ids: Collection[str] | None = None,
        audience: object = None,
        dedupe_key: str | None = None,
    ) -> int:
        del owner_user_ids, audience, dedupe_key
        emitted.append((source_id, event))
        return 1

    registry = EventSourceRegistry(config_provider=lambda: config, emit_handler=_handler)
    old_emitter = registry.register(_CustomSource("acme_builds"), owner_plugin="acme")
    new_source = _CustomSource("acme_builds")
    new_emitter = registry.register(new_source, owner_plugin="acme")

    assert isinstance(old_emitter, EventSourceEmitter)
    assert new_emitter.token != old_emitter.token

    # Old lifespan teardown must NOT remove replacement; old emitter fails closed.
    assert registry.unregister("acme_builds", old_emitter.token) is False
    assert registry.get("acme_builds") is new_source
    with pytest.raises(EventSourceInactiveError):
        await old_emitter.emit(kind="message", event_id="stale-1")
    assert emitted == []

    with pytest.raises(ValueError, match="non-empty"):
        await new_emitter.emit(kind=" ", event_id="e1")
    assert await new_emitter.emit(kind="message", event_id="fresh-1", text="hi", thread_key="T:1", data={"k": "v"}) == 1
    assert emitted[0][0] == "acme_builds"
    assert emitted[0][1].source_type == "external_event"
    assert emitted[0][1].source_id == "acme_builds"
    assert emitted[0][1].thread_key == "T:1"

    assert registry.unregister("acme_builds", new_emitter.token) is True
    assert registry.get("acme_builds") is None
    with pytest.raises(EventSourceInactiveError):
        await new_emitter.emit(kind="message", event_id="after-stop")


def test_store_v3_migrations_import_once_and_bound_thread_keys(tmp_path: Path) -> None:
    """Schema v2->v3 auto-migrates, import_triggers_once is idempotent, and thread keys batch-query + prune."""
    _, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    with closing(sqlite3.connect(store.db_path)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 3
        conn.execute("DROP TABLE applied_migrations")
        conn.execute("UPDATE schema_version SET version = 2")
        conn.commit()

    upgraded = ReactiveTriggerStore(runtime_paths)
    with closing(sqlite3.connect(upgraded.db_path)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 3
        assert "applied_migrations" in {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }

    preexisting = _make_rule(trigger_id="collide_rule", source_id="slack", kind="message")
    upgraded.create_trigger(preexisting)
    upgraded.save_thread_key_binding("collide_rule", "C1:1", "$original-root", now=1000.0)

    migrated_a = _make_rule(trigger_id="slack_watch_a", source_id="slack", kind="message", binding={"team_id": "T1"})
    migrated_b = _make_rule(trigger_id="slack_watch_b", source_id="slack", kind="mention")
    assert upgraded.has_migration("slack_watches_v1") is False
    with patch("mindroom.reactive_triggers.store.logger.warning") as mock_warn:
        imported = upgraded.import_triggers_once(
            "slack_watches_v1",
            [preexisting, migrated_a, migrated_b],
            thread_keys=[
                ("collide_rule", "C1:1", "$hijacked-root", 2000.0),
                ("slack_watch_a", "C1:1", "$root-a", 1000.0),
                ("slack_watch_b", "C1:1", "$root-b", 1200.0),
                ("orphan_rule", "C1:1", "$orphan-root", 1200.0),
            ],
            now=1500.0,
        )
    assert imported == 2
    mock_warn.assert_called_once()
    assert mock_warn.call_args.kwargs["skipped_trigger_ids"] == ["collide_rule"]
    assert upgraded.has_migration("slack_watches_v1") is True
    assert upgraded.get_thread_key_binding("collide_rule", "C1:1", now=1500.0) == "$original-root"
    loaded_a = upgraded.get_trigger("slack_watch_a")
    assert loaded_a is not None
    assert isinstance(loaded_a.source, ExternalEventSourceSpec)
    assert loaded_a.source.binding == {"team_id": "T1"}

    # Deleting an imported trigger and re-running import_triggers_once returns 0 and never resurrects it.
    assert upgraded.delete_trigger("slack_watch_b") is True
    assert upgraded.import_triggers_once("slack_watches_v1", [migrated_b], now=1600.0) == 0
    assert upgraded.get_trigger("slack_watch_b") is None
    assert upgraded.import_triggers_once("empty_migration", [], now=1600.0) == 0
    assert upgraded.has_migration("empty_migration") is True

    # Batch get_bound_thread_keys respects TTL boundary; prune_old_firings prunes expired thread keys.
    upgraded.save_thread_key_binding("slack_watch_a", "C1:2", "$root-a2", now=2000.0)
    assert upgraded.get_bound_thread_keys([], "C1:1", now=1500.0) == frozenset()
    assert upgraded.get_bound_thread_keys(
        ["collide_rule", "slack_watch_a"],
        "C1:1",
        ttl_seconds=600.0,
        now=1599.0,
    ) == frozenset({"collide_rule", "slack_watch_a"})
    assert (
        upgraded.get_bound_thread_keys(["collide_rule", "slack_watch_a"], "C1:1", ttl_seconds=600.0, now=1600.0)
        == frozenset()
    )

    ev = TriggerEvent(source_type="external_event", source_id="slack", kind="message", event_id="f1", timestamp=100.0)
    assert upgraded.enqueue_firing("slack_watch_a", "f1", ev) is True
    firing_id = upgraded.list_firings(trigger_id="slack_watch_a")[0].id
    assert upgraded.mark_skipped(firing_id, "old", now=100.0) is True
    assert upgraded.prune_old_firings(retention_seconds=500.0, thread_key_ttl_seconds=600.0, now=2200.0) == 1
    with closing(sqlite3.connect(upgraded.db_path)) as conn:
        keys = {
            (row[0], row[1])
            for row in conn.execute("SELECT trigger_id, thread_key FROM trigger_thread_keys").fetchall()
        }
    assert keys == {("slack_watch_a", "C1:2")}


@pytest.mark.asyncio
async def test_dispatch_event_source_thread_key_sliding_ttl_hook_source_and_access(tmp_path: Path) -> None:
    """Dispatch handles thread_key_from_event, sliding 7d TTL, HOOK_SOURCE_KEY, and registered access checks."""
    config, runtime_paths = _make_config(tmp_path)
    store = ReactiveTriggerStore(runtime_paths)
    memberships = AgentReplyMembershipIndex()
    source = _CustomSource("acme_builds")
    registry = EventSourceRegistry(config_provider=lambda: config, emit_handler=AsyncMock(return_value=0))
    registry.register(source, owner_plugin="acme")

    rule = _make_rule(trigger_id="ev_sliding", source_id="acme_builds", kind="message")
    store.create_trigger(rule)
    state = _room_state(_ROOM_B, _OWNER)

    def _ev_firing(event_id: str, thread_key: str | None) -> FiringRecord:
        return _make_firing(
            rule,
            TriggerEvent(
                source_type="external_event",
                source_id="acme_builds",
                kind="message",
                event_id=event_id,
                timestamp=1000.0,
                thread_key=thread_key,
                text="hi",
            ),
        )

    day = 86400.0
    t0, t6, t12 = 10_000.0, 10_000.0 + 6 * day, 10_000.0 + 12 * day
    with (
        patch(
            "mindroom.reactive_triggers.dispatch.read_room_state",
            new=AsyncMock(return_value=state),
        ) as read_state_mock,
        patch("mindroom.reactive_triggers.dispatch.send_matrix_message", new=AsyncMock()) as send_mock,
        patch("mindroom.reactive_triggers.store.time.time", side_effect=[t0, t0, t6, t6, t12, t12]),
    ):
        send_mock.return_value = DeliveredMatrixEvent(event_id="$root-1", content_sent={})
        res0 = await dispatch_reactive_trigger(
            rule=rule,
            firing=_ev_firing("e0", "C1:100"),
            triage_reason=None,
            store=store,
            target_bot=_bot(),
            config=config,
            runtime_paths=runtime_paths,
            memberships=memberships,
            event_sources=registry,
        )
        assert res0 == DispatchResult(status="delivered", matrix_event_id="$root-1")
        assert read_state_mock.await_count == 1
        assert source.delivery_contexts[0].target_room_state is state
        content0 = send_mock.await_args.args[2]
        assert content0[SOURCE_KIND_KEY] == REACTIVE_TRIGGER_SOURCE_KIND
        assert content0[HOOK_SOURCE_KEY] == "event_source:acme_builds"
        assert hook_source_from_content(content0) is None
        assert content0[PER_FIRE_THREAD_ROOT_KEY] is True

        # Day 6 continuation refreshes updated_at while preserving $root-1 -> Day 12 still reuses $root-1.
        send_mock.return_value = DeliveredMatrixEvent(event_id="$reply-6", content_sent={})
        assert (
            await dispatch_reactive_trigger(
                rule=rule,
                firing=_ev_firing("e6", "C1:100"),
                triage_reason=None,
                store=store,
                target_bot=_bot(latest_thread_event_id="$root-1"),
                config=config,
                runtime_paths=runtime_paths,
                memberships=memberships,
                event_sources=registry,
            )
        ).status == "delivered"
        assert send_mock.await_args.args[2]["m.relates_to"]["event_id"] == "$root-1"
        assert PER_FIRE_THREAD_ROOT_KEY not in send_mock.await_args.args[2]

        send_mock.return_value = DeliveredMatrixEvent(event_id="$reply-12", content_sent={})
        assert (
            await dispatch_reactive_trigger(
                rule=rule,
                firing=_ev_firing("e12", "C1:100"),
                triage_reason=None,
                store=store,
                target_bot=_bot(latest_thread_event_id="$reply-6"),
                config=config,
                runtime_paths=runtime_paths,
                memberships=memberships,
                event_sources=registry,
            )
        ).status == "delivered"
        assert send_mock.await_args.args[2]["m.relates_to"]["event_id"] == "$root-1"

    # None thread_key opens standalone per-fire root; non-external rule omits HOOK_SOURCE_KEY.
    room_rule = ReactiveTriggerRecord(
        trigger_id="rm_rule",
        owner_user_id=_OWNER,
        source=RoomMessageSourceSpec(room_id=_ROOM_B),
        target=TriggerTargetSpec(agent="research", room_id=_ROOM_B, new_thread=True, prompt="go"),
        created_at=1000.0,
        updated_at=1000.0,
    )
    store.create_trigger(room_rule)
    with (
        patch("mindroom.reactive_triggers.dispatch.read_room_state", new=AsyncMock(return_value=state)),
        patch(
            "mindroom.reactive_triggers.dispatch.send_matrix_message",
            new=AsyncMock(return_value=DeliveredMatrixEvent(event_id="$standalone", content_sent={})),
        ) as send_mock,
    ):
        await dispatch_reactive_trigger(
            rule=rule,
            firing=_ev_firing("e-none", None),
            triage_reason=None,
            store=store,
            target_bot=_bot(),
            config=config,
            runtime_paths=runtime_paths,
            memberships=memberships,
            event_sources=registry,
        )
        assert send_mock.await_args.args[2][PER_FIRE_THREAD_ROOT_KEY] is True
        rm_event = TriggerEvent(
            source_type="room_message",
            source_id=_ROOM_B,
            kind="m.room.message",
            event_id="$m1",
            timestamp=1000.0,
            room_id=_ROOM_B,
            actor=_OWNER,
            text="hi",
        )
        await dispatch_reactive_trigger(
            rule=room_rule,
            firing=_make_firing(room_rule, rm_event, firing_id=2),
            triage_reason=None,
            store=store,
            target_bot=_bot(),
            config=config,
            runtime_paths=runtime_paths,
            memberships=memberships,
            event_sources=registry,
        )
        assert HOOK_SOURCE_KEY not in send_mock.await_args.args[2]

    # Registered check_delivery_access skip/retry translation vs fallback to YAML check (source_removed).
    source.delivery_decision = EventSourceAccessDenial(reason="room_not_private")
    assert await check_reactive_source_access(
        rule=rule,
        event=_ev_firing("e-d", None).event,
        source_bot=_bot(),
        config=config,
        runtime_paths=runtime_paths,
        memberships=memberships,
        known_room_states={_ROOM_B: state},
        event_sources=registry,
    ) == DispatchResult(status="skipped", reason="room_not_private")
    source.delivery_decision = EventSourceAccessDenial(reason="upstream_timeout", retry=True)
    assert await check_reactive_source_access(
        rule=rule,
        event=_ev_firing("e-retry", None).event,
        source_bot=_bot(),
        config=config,
        runtime_paths=runtime_paths,
        memberships=memberships,
        known_room_states={_ROOM_B: state},
        event_sources=registry,
    ) == DispatchResult(status="retry", reason="upstream_timeout")
    assert await check_reactive_source_access(
        rule=_make_rule(trigger_id="missing", source_id="removed_src", kind="message"),
        event=_ev_firing("e-r", None).event,
        source_bot=_bot(),
        config=config,
        runtime_paths=runtime_paths,
        memberships=memberships,
        known_room_states={_ROOM_B: state},
        event_sources=registry,
    ) == DispatchResult(status="skipped", reason="source_removed")


@pytest.mark.asyncio
async def test_service_emit_event_fanout_aliases_audience_thread_binding_and_access(tmp_path: Path) -> None:
    """Service.emit_event enforces policy, alias expansion, empty owner_user_ids, thread bindings, and pre-triage access."""
    config, runtime_paths = _make_config(tmp_path, aliases={_OWNER: [_OWNER_ALIAS]})
    store = ReactiveTriggerStore(runtime_paths)
    service = ReactiveTriggerService(
        config_provider=lambda: config,
        runtime_paths=runtime_paths,
        bots=lambda: {"research": _bot()},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )
    source = _CustomSource("acme_personal", require_audience_member=True)
    emitter = service.event_sources.register(source, owner_plugin="acme")

    for rule in (
        _make_rule(
            trigger_id="alias_rule",
            owner_user_id=_OWNER_ALIAS,
            source_id="acme_personal",
            kind="message",
            binding={"ext_user_id": "U_ALICE"},
            triage=TriageSpec(instructions="check", provider="llm", model="cheap"),
        ),
        _make_rule(
            trigger_id="bound_mention",
            owner_user_id=_OWNER,
            source_id="acme_personal",
            kind="mention",
            binding={"ext_user_id": "U_ALICE"},
        ),
        _make_rule(
            trigger_id="disabled_rule",
            owner_user_id=_OWNER,
            source_id="acme_personal",
            kind="message",
            binding={"ext_user_id": "U_ALICE"},
            enabled=False,
        ),
        _make_rule(
            trigger_id="expired_rule",
            owner_user_id=_OWNER,
            source_id="acme_personal",
            kind="message",
            binding={"ext_user_id": "U_ALICE"},
            expires_at=1.0,
        ),
        _make_rule(
            trigger_id="other_rule",
            owner_user_id=_OTHER,
            source_id="acme_personal",
            kind="message",
            binding={"ext_user_id": "U_BOB"},
        ),
    ):
        store.create_trigger(rule)
    store.save_thread_key_binding("bound_mention", "C1:thread1", "$root-existing")

    # Empty owner_user_ids matches 0 rules; direct emit_event rejects forged source_id / blank dedupe_key.
    assert await emitter.emit(kind="message", event_id="ev-empty", owner_user_ids=[], audience={"U_ALICE"}) == 0
    assert source.match_calls == []
    with pytest.raises(ValueError, match="mismatch"):
        await service.emit_event(
            "acme_personal",
            TriggerEvent(source_type="external_event", source_id="other", kind="message", event_id="e", timestamp=1.0),
        )
    with pytest.raises(ValueError, match="dedupe_key"):
        await emitter.emit(kind="message", event_id="e", dedupe_key="   ")

    # Canonical owner matches alias_rule (via alias expansion) and bound_mention (via has_thread_binding=True).
    notify_spy = MagicMock(wraps=service.notify_work)
    service.notify_work = notify_spy  # type: ignore[method-assign]
    assert (
        await emitter.emit(
            kind="message",
            event_id="ev-1",
            text="hi",
            thread_key="C1:thread1",
            owner_user_ids=[_OWNER],
            audience={"U_ALICE"},
            dedupe_key="dk-1",
        )
        == 2
    )
    notify_spy.assert_called_once_with(poll_github_prs=False)
    assert ("alias_rule", {"U_ALICE"}, False) in source.match_calls
    assert ("bound_mention", {"U_ALICE"}, True) in source.match_calls

    notify_spy.reset_mock()
    assert (
        await emitter.emit(
            kind="message",
            event_id="ev-1-dup",
            thread_key="C1:thread1",
            owner_user_ids=[_OWNER],
            audience={"U_ALICE"},
            dedupe_key="dk-1",
        )
        == 0
    )
    notify_spy.assert_not_called()
    assert store.list_firings(trigger_id="alias_rule")[0].event.data == {}

    # Pre-triage access denial skips before evaluate_triage runs; allowing delivers to Matrix.
    state = _room_state(_ROOM_B, _OWNER)
    source.delivery_decision = EventSourceAccessDenial(reason="account_changed")
    with (
        patch("mindroom.reactive_triggers.dispatch.read_room_state", new=AsyncMock(return_value=state)),
        patch(
            "mindroom.reactive_triggers.service.evaluate_triage",
            new=AsyncMock(return_value=TriageOutcome(act=True)),
        ) as triage_mock,
    ):
        await service.drain_due_firings_once()
    triage_mock.assert_not_awaited()
    assert store.list_firings(trigger_id="alias_rule")[0].error_message == "account_changed"

    source.delivery_decision = None
    assert (
        await emitter.emit(
            kind="message",
            event_id="ev-ok",
            thread_key="C1:thread1",
            owner_user_ids=[_OWNER],
            audience={"U_ALICE"},
        )
        == 2
    )
    with (
        patch("mindroom.reactive_triggers.dispatch.read_room_state", new=AsyncMock(return_value=state)),
        patch(
            "mindroom.reactive_triggers.service.evaluate_triage",
            new=AsyncMock(return_value=TriageOutcome(act=True)),
        ),
        patch(
            "mindroom.reactive_triggers.dispatch.send_matrix_message",
            new=AsyncMock(return_value=DeliveredMatrixEvent(event_id="$delivered-ok", content_sent={})),
        ) as send_mock,
    ):
        await service.drain_due_firings_once()
    assert send_mock.await_count == 2
    assert store.list_firings(trigger_id="alias_rule")[0].status == "delivered"

    # Disabled reactive policy returns 0 on emit.
    config, _ = _make_config(tmp_path, reactive_enabled=False)
    assert await emitter.emit(kind="message", event_id="ev-off", owner_user_ids=[_OWNER], audience={"U_ALICE"}) == 0


def _write_event_source_plugin(
    tmp_path: Path,
    name: str,
    *,
    source_id: str | None = None,
    probe_name: str | None = None,
    version: int = 1,
    fail_before_yield: bool = False,
    with_routes: bool = False,
) -> Path:
    root = tmp_path / "plugins" / name
    root.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {"name": name}
    if source_id is not None and probe_name is not None:
        manifest["lifecycle_module"] = "lifecycle.py"
        (root / "lifecycle.py").write_text(
            f"""
import sys
from contextlib import asynccontextmanager

from mindroom.plugin_api import EventSource

_probe = sys.modules[{probe_name!r}]


class _Source(EventSource):
    source_id = {source_id!r}
    description = "Plugin source v{version}"
    allowed_kinds = ("build.failed",)
    version = {version}


@asynccontextmanager
async def lifespan(lifecycle):
    emitter = lifecycle.register_event_source(_Source())
    _probe.emitters[{version}] = emitter
    if {fail_before_yield!r}:
        raise RuntimeError("startup boom")
    _probe.events.append(("start", {version}))
    try:
        yield
    finally:
        _probe.events.append(("stop", {version}, lifecycle.stop_reason))
""",
            encoding="utf-8",
        )
    if with_routes:
        manifest["routes_module"] = "routes.py"
        (root / "routes.py").write_text(
            """
from fastapi import APIRouter, Request

from mindroom.plugin_http import event_source_emitter

router = APIRouter()


@router.post("/emit/{source_id}")
async def emit_route(request: Request, source_id: str) -> dict[str, int]:
    emitter = event_source_emitter(request, source_id)
    enqueued = await emitter.emit(kind="build.failed", event_id=f"http-{source_id}")
    return {"enqueued": enqueued}
""",
            encoding="utf-8",
        )
    (root / "mindroom.plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _plugin_probe() -> Any:  # noqa: ANN401
    probe = ModuleType(f"{_TEST_PROBE_PREFIX}{uuid.uuid4().hex}")
    probe.events = []  # type: ignore[attr-defined]
    probe.emitters = {}  # type: ignore[attr-defined]
    sys.modules[probe.__name__] = probe
    return probe


def _with_plugins(config: Config, runtime_paths: RuntimePaths, *names: str) -> Config:
    return bind_runtime_paths(
        config.model_copy(update={"plugins": [PluginEntryConfig(path=f"./plugins/{n}") for n in names]}),
        runtime_paths,
    )


def _make_service(
    runtime_paths: RuntimePaths,
    config_provider: Any,  # noqa: ANN401
    *,
    trigger_id: str,
) -> tuple[ReactiveTriggerStore, ReactiveTriggerService]:
    store = ReactiveTriggerStore(runtime_paths)
    store.create_trigger(_make_rule(trigger_id=trigger_id, source_id="acme_builds"))
    service = ReactiveTriggerService(
        runtime_paths=runtime_paths,
        config_provider=config_provider,
        bots=lambda: {"research": _bot()},
        memberships=AgentReplyMembershipIndex(),
        store=store,
    )
    return store, service


@pytest.mark.asyncio
async def test_plugin_lifespan_event_source_registration_reload_and_cleanup(tmp_path: Path) -> None:
    """PluginLifespans registers EventSources, fences reloads by token, and auto-unregisters on failure/disable/stop."""
    config, runtime_paths = _make_config(tmp_path)
    _, service = _make_service(runtime_paths, lambda: config, trigger_id="acme_rule")
    probe = _plugin_probe()
    lifespans = PluginLifespans(runtime_paths, lambda: config, event_sources=service.event_sources)

    async def publish_acme(version: int, *, fail_before_yield: bool = False) -> None:
        nonlocal config
        _write_event_source_plugin(
            tmp_path,
            "acme",
            source_id="acme_builds",
            probe_name=probe.__name__,
            version=version,
            fail_before_yield=fail_before_yield,
        )
        config = _with_plugins(config, runtime_paths, "acme")
        publish_test_plugins(config, runtime_paths)
        await lifespans.apply_published()

    # Without a primary EventSourceRegistry, register_event_source fails closed.
    detached = PluginLifecycle("acme", {}, runtime_paths, lambda: config)
    with pytest.raises(RuntimeError, match="primary runtime"):
        detached.register_event_source(_CustomSource(source_id="acme_builds"))

    # v1 registers acme_builds and emits.
    await publish_acme(1)
    assert getattr(service.event_sources.get("acme_builds"), "version", None) == 1
    assert await probe.emitters[1].emit(kind="build.failed", event_id="ev-v1") == 1

    # Hot-reload to v2: new lifespan registers before old stops; old teardown never clobbers v2, old emitter fails closed.
    await publish_acme(2)
    assert ("stop", 1, "reload") in probe.events
    assert getattr(service.event_sources.get("acme_builds"), "version", None) == 2
    with pytest.raises(EventSourceInactiveError):
        await probe.emitters[1].emit(kind="build.failed", event_id="ev-v1-stale")
    assert await probe.emitters[2].emit(kind="build.failed", event_id="ev-v2") == 1

    # Startup failure before yield auto-unregisters the registered EventSource.
    await publish_acme(3, fail_before_yield=True)
    assert service.event_sources.get("acme_builds") is None
    with pytest.raises(EventSourceInactiveError):
        await probe.emitters[3].emit(kind="build.failed", event_id="ev-v3-failed")

    # Disable auto-unregisters.
    await publish_acme(4)
    assert service.event_sources.get("acme_builds") is not None
    config = _with_plugins(config, runtime_paths)
    publish_test_plugins(config, runtime_paths)
    await lifespans.apply_published()
    assert service.event_sources.get("acme_builds") is None
    assert ("stop", 4, "disabled") in probe.events

    # Shutdown auto-unregisters and rejects post-stop registrations on the ended lifecycle.
    await publish_acme(5)
    ended_lifecycle = lifespans._running["acme"].lifecycle
    await lifespans.stop()
    assert service.event_sources.get("acme_builds") is None
    assert ("stop", 5, "shutdown") in probe.events
    with pytest.raises(RuntimeError, match="lifespan has ended"):
        ended_lifecycle.register_event_source(_CustomSource(source_id="acme_builds"))


@pytest.mark.asyncio
async def test_plugin_http_event_source_emitter_ownership_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """plugin_http.event_source_emitter enforces active plugin ownership and fails closed when unavailable."""
    config, runtime_paths = _make_config(tmp_path)
    store, service = _make_service(runtime_paths, lambda: config, trigger_id="acme_http_rule")
    service.event_sources.register(_CustomSource(source_id="slack"))
    probe = _plugin_probe()

    _write_event_source_plugin(tmp_path, "acme", source_id="acme_builds", probe_name=probe.__name__, with_routes=True)
    _write_event_source_plugin(tmp_path, "other", with_routes=True)
    config = _with_plugins(config, runtime_paths, "acme", "other")

    app = FastAPI()
    api_state = ApiState(threading.Lock(), ApiSnapshot(1, runtime_paths, runtime_config=config))
    ensure_app_state(app).api_state = api_state
    mount_plugin_routes(app)

    publish_test_plugins(config, runtime_paths)
    lifespans = PluginLifespans(runtime_paths, lambda: config, event_sources=service.event_sources)
    await lifespans.apply_published()

    try:
        with pytest.raises(RuntimeError, match="event_source_emitter"):
            plugin_http.event_source_emitter(Request({"type": "http", "app": app, "headers": []}), "acme_builds")

        client = TestClient(app)
        assert client.post("/api/plugins/acme/emit/acme_builds").status_code == 503

        ensure_app_state(app).embedded = SimpleNamespace(reactive_triggers=service)  # type: ignore[assignment]
        ok = client.post("/api/plugins/acme/emit/acme_builds")
        assert (ok.status_code, ok.json()) == (200, {"enqueued": 1})
        assert len(store.list_firings(trigger_id="acme_http_rule")) == 1

        # Unregistered source, built-in source, and another plugin's source all fail closed with 503.
        for path in (
            "/api/plugins/acme/emit/acme_missing",
            "/api/plugins/acme/emit/slack",
            "/api/plugins/other/emit/acme_builds",
        ):
            assert client.post(path).status_code == 503

        # Integration binding forwards reactive_triggers from embedded runtime when omitted.
        seen_triggers: list[object] = []
        custom_integration = replace(
            SLACK_INTEGRATION,
            name="probe_integration",
            bind=lambda _app, _bots, _memberships, rt: seen_triggers.append(rt),
            unbind=lambda _app: None,
        )
        monkeypatch.setattr(integrations, "_integrations", lambda: (custom_integration,))
        integrations.bind_integrations(app, {}, AgentReplyMembershipIndex())
        assert seen_triggers == [service]

        # Orchestrator wires reactive_triggers into both EmbeddedRuntime and PluginLifespans.
        orchestrator = ready_runtime(config, runtime_paths)
        assert orchestrator.embedded_runtime().reactive_triggers is orchestrator.reactive_triggers
        assert orchestrator._plugin_lifespans.event_sources is orchestrator.reactive_triggers.event_sources

        # When the plugin is no longer active in config, event_source_emitter raises PluginInactiveError.
        api_state.snapshot = ApiSnapshot(2, runtime_paths, runtime_config=_with_plugins(config, runtime_paths))
        stale_request = Request({"type": "http", "app": app, "headers": [], PLUGIN_NAME_SCOPE_KEY: "acme"})
        with pytest.raises(PluginInactiveError):
            plugin_http.event_source_emitter(stale_request, "acme_builds")
    finally:
        await lifespans.stop()


class _PrivateToolEventSource(EventSource):
    """Test EventSource exercising OAuth binding, private room checks, defaults, history, and dry-run audience."""

    source_id = "slack_private"
    description = "Private Slack watch"
    allowed_kinds = ("message", "mention")
    match_fields = ("channel_id",)
    example = "subscribe_event(source_id='slack_private', kind='message')"
    default_once = False
    default_new_thread = True
    default_thread_key_from_event = True
    default_reopen_on_fire = True
    private_room_only = True

    def __init__(self, *, visible_to: set[str], connected_users: set[str]) -> None:
        self.visible_to = visible_to
        self.connected_users = connected_users
        self.validate_calls: list[tuple[EventSourceContext, str, dict[str, Any], dict[str, Any] | None]] = []
        self.history_contexts: list[tuple[EventSourceContext, TriggerEvent | None]] = []
        self.delivery_contexts: list[EventSourceContext] = []
        self.denied_event_ids: set[str] = set()

    async def describe_for_user(self, ctx: EventSourceContext) -> EventSourceDescriptor | None:
        if ctx.owner_user_id not in self.visible_to:
            return None
        return await super().describe_for_user(ctx)

    async def validate_subscription(
        self,
        ctx: EventSourceContext,
        *,
        kind: str,
        match: Mapping[str, str | bool | int | list[str]],
        existing_binding: Mapping[str, str | bool | int] | None = None,
    ) -> ValidatedSubscription:
        self.validate_calls.append(
            (ctx, kind, dict(match), dict(existing_binding) if existing_binding is not None else None),
        )
        if ctx.owner_user_id not in self.connected_users:
            msg = "Connect Slack first"
            raise OAuthConnectionRequired(
                msg,
                provider_id="slack",
                connect_url="https://example.test/connect/slack",
            )
        if kind not in self.allowed_kinds:
            msg = f"Unsupported kind {kind!r}"
            raise ValueError(msg)
        if not set(match).issubset({"channel_id"}):
            msg = "Only channel_id filter is allowed"
            raise ValueError(msg)
        # Deliberately return a forged/empty binding on update to verify triggers.py ignores it and preserves existing.
        binding = (
            {"team_id": "FORGED", "slack_user_id": "FORGED"}
            if existing_binding is not None
            else {"team_id": "T1", "slack_user_id": "U_ALICE"}
        )
        return ValidatedSubscription(kind=kind, match=dict(match), binding=binding)

    def dry_run_audience(self, rule: ReactiveTriggerRecord) -> object:
        if isinstance(rule.source, ExternalEventSourceSpec) and "slack_user_id" in rule.source.binding:
            return frozenset({rule.source.binding["slack_user_id"]})
        return None

    def matches_rule(
        self,
        rule: ReactiveTriggerRecord,
        event: TriggerEvent,
        *,
        audience: object = None,
        has_thread_binding: bool = False,
    ) -> bool:
        del has_thread_binding
        if not isinstance(rule.source, ExternalEventSourceSpec):
            return False
        uid = rule.source.binding.get("slack_user_id")
        if not isinstance(audience, (set, frozenset)) or uid not in audience:
            return False
        return matches_external_event(rule.source, event)

    @staticmethod
    def _is_private_room(state: RoomStateSnapshot | None, owner_user_id: str) -> bool:
        if state is None:
            return False
        joined_or_invited = {
            state_key
            for (etype, state_key), content in state.events.items()
            if etype == "m.room.member" and content.get("membership") in {"join", "invite"}
        }
        return owner_user_id in joined_or_invited and joined_or_invited <= {
            owner_user_id,
            "@mindroom_research:localhost",
        }

    async def check_delivery_access(
        self,
        ctx: EventSourceContext,
        *,
        rule: ReactiveTriggerRecord,
        event: TriggerEvent | None = None,
    ) -> EventSourceAccessDenial | None:
        del event
        self.delivery_contexts.append(ctx)
        if not self._is_private_room(ctx.target_room_state, rule.owner_user_id):
            return EventSourceAccessDenial(reason="Room is no longer private")
        return None

    async def check_history_access(
        self,
        ctx: EventSourceContext,
        *,
        rule: ReactiveTriggerRecord,
        event: TriggerEvent | None = None,
    ) -> bool:
        self.history_contexts.append((ctx, event))
        if event is not None and event.event_id in self.denied_event_ids:
            return False
        return self._is_private_room(ctx.calling_room_state, rule.owner_user_id) and self._is_private_room(
            ctx.target_room_state,
            rule.owner_user_id,
        )


def _tool_room_client(room_members: dict[str, list[str]]) -> SimpleNamespace:
    rooms = {
        rid: SimpleNamespace(room_id=rid, users={uid: SimpleNamespace(user_id=uid) for uid in members})
        for rid, members in room_members.items()
    }

    async def room_get_state(room_id: str) -> object:
        if room_id not in room_members:
            return nio.RoomGetStateError(message="not found", status_code="M_NOT_FOUND")
        events = [
            {
                "type": "m.room.member",
                "state_key": uid,
                "sender": uid,
                "event_id": f"$m_{idx}",
                "origin_server_ts": 1_000,
                "content": {"membership": "join"},
            }
            for idx, uid in enumerate(room_members[room_id])
        ]
        return nio.RoomGetStateResponse(events=events, room_id=room_id)

    return SimpleNamespace(
        user_id="@mindroom_research:localhost",
        rooms=rooms,
        room_get_state=AsyncMock(side_effect=room_get_state),
    )


def _setup_registered_tool_env(
    tmp_path: Path,
    *,
    connected_users: set[str] | None = None,
) -> tuple[
    TriggersTools,
    ReactiveTriggerStore,
    _PrivateToolEventSource,
    dict[str, list[str]],
    Callable[..., ToolRuntimeContext],
]:
    private_room, shared_room, admin = "!priv:localhost", "!shared:localhost", "@admin:localhost"
    config = bind_runtime_paths(
        Config(
            agents={
                "research": AgentConfig(
                    display_name="Research",
                    rooms=["priv", "shared"],
                    access={"users": [_OWNER, _OTHER, admin]},
                ),
            },
            rooms={"priv": {"display_name": "Private"}, "shared": {"display_name": "Shared"}},
            models={"default": ModelConfig(provider="openai", id="gpt-test")},
            external_trigger_policy=ExternalTriggerPolicyConfig(
                enabled=True,
                reactive_enabled=True,
                admin_users=[admin],
                event_sources={
                    "github": GitHubEventSourceConfig(
                        kind="github",
                        secret_env="GH_SECRET",  # noqa: S106
                        allowed_repos=["org/app"],
                        allowed_events=["push"],
                        subscribers=[_OWNER],
                    ),
                },
            ),
        ),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    matrix_state = MatrixState.load(runtime_paths)
    matrix_state.add_room("priv", private_room, "#priv:localhost", "Private")
    matrix_state.add_room("shared", shared_room, "#shared:localhost", "Shared")
    save_matrix_state(matrix_state, runtime_paths)

    registry = EventSourceRegistry(lambda: config, AsyncMock(return_value=0))
    source = _PrivateToolEventSource(visible_to={_OWNER}, connected_users=set(connected_users or ()))
    registry.register(source)
    orchestrator = SimpleNamespace(
        agent_bots={},
        reactive_triggers=SimpleNamespace(event_sources=registry, notify_work=MagicMock()),
    )
    room_members = {
        private_room: [_OWNER, "@mindroom_research:localhost"],
        shared_room: [_OWNER, _OTHER, admin, "@mindroom_research:localhost"],
    }
    client = _tool_room_client(room_members)

    def _ctx(
        *,
        requester_id: str = _OWNER,
        room_id: str = private_room,
        hook_source: str | None = None,
    ) -> ToolRuntimeContext:
        reader = make_conversation_reader_mock()
        reader.latest_room_cursor = AsyncMock(return_value=None)
        return make_test_tool_runtime_context(
            agent_name="research",
            target=MessageTarget.resolve(room_id=room_id, thread_id="$root:localhost", reply_to_event_id=None),
            requester_id=requester_id,
            client=client,
            config=config,
            runtime_paths=runtime_paths,
            relations=make_relation_lookup(),
            conversation_reader=reader,
            orchestrator=orchestrator,
            hook_source=hook_source,
        )

    return TriggersTools(), ReactiveTriggerStore(runtime_paths), source, room_members, _ctx


@pytest.mark.asyncio
async def test_triggers_tool_registered_event_source_discovery_subscribe_and_binding_safety(
    tmp_path: Path,
) -> None:
    """TriggersTools discovers registered EventSources, handles OAuth/defaults, and protects binding on update."""
    tool, store, source, _room_members, make_ctx = _setup_registered_tool_env(tmp_path)

    with tool_runtime_context(make_ctx(requester_id=_OWNER)):
        owner_list = json.loads(await tool.triggers(action="list"))
    assert [s["source_id"] for s in owner_list["event_sources"]] == ["github", "slack_private"]
    assert owner_list["event_sources"][1] == {
        "source_id": "slack_private",
        "kind": "registered",
        "description": "Private Slack watch",
        "allowed_kinds": ["message", "mention"],
        "match_fields": ["channel_id"],
        "example": "subscribe_event(source_id='slack_private', kind='message')",
        "private_room_only": True,
    }

    with tool_runtime_context(make_ctx(requester_id=_OTHER, room_id="!shared:localhost")):
        other_list = json.loads(await tool.triggers(action="list"))
        other_sub = json.loads(
            await tool.triggers(action="subscribe_event", source_id="slack_private", kind="message", prompt="Watch"),
        )
    assert other_list["event_sources"] == []
    assert other_sub["status"] == "error"
    assert "unavailable" in other_sub["message"]

    with tool_runtime_context(make_ctx(requester_id=_OWNER)):
        need_oauth = json.loads(
            await tool.triggers(action="subscribe_event", source_id="slack_private", kind="message", prompt="Watch"),
        )
    assert (need_oauth["status"], need_oauth["oauth_connection_required"], need_oauth["connect_url"]) == (
        "error",
        True,
        "https://example.test/connect/slack",
    )

    source.connected_users.add(_OWNER)
    with tool_runtime_context(make_ctx(requester_id=_OWNER)):
        created = json.loads(
            await tool.triggers(
                action="subscribe_event",
                trigger_id="sl_1",
                source_id="slack_private",
                kind="message",
                prompt="Summarize Slack ping",
                match={"channel_id": ["C1", "C2"]},
            ),
        )
        override_created = json.loads(
            await tool.triggers(
                action="subscribe_event",
                trigger_id="sl_room",
                source_id="slack_private",
                kind="mention",
                prompt="Post in room",
                target_thread_id="room",
                once=True,
                reopen_on_fire=False,
            ),
        )
    assert created["status"] == "ok", created
    assert (
        created["trigger"]["once"],
        created["trigger"]["target"]["new_thread"],
        created["trigger"]["target"]["thread_key_from_event"],
        created["trigger"]["target"]["reopen_on_fire"],
    ) == (False, True, True, True)
    assert created["trigger"]["source"] == {
        "source_type": "external_event",
        "source_id": "slack_private",
        "kind": "message",
        "match": {"channel_id": ["C1", "C2"]},
    }
    assert "binding" not in json.dumps(created)
    assert "U_ALICE" not in json.dumps(created)
    assert (
        override_created["trigger"]["once"],
        override_created["trigger"]["target"]["new_thread"],
        override_created["trigger"]["target"]["thread_key_from_event"],
        override_created["trigger"]["target"]["reopen_on_fire"],
    ) == (True, False, False, False)

    stored = store.get_trigger("sl_1")
    assert stored is not None
    assert isinstance(stored.source, ExternalEventSourceSpec)
    assert stored.source.binding == {"team_id": "T1", "slack_user_id": "U_ALICE"}
    assert source.validate_calls[-1][0].target_room_state is not None
    assert source.validate_calls[-1][0].credentials_manager is not None

    with tool_runtime_context(make_ctx(requester_id=_OWNER)):
        updated = json.loads(await tool.triggers(action="update", trigger_id="sl_1", match={"channel_id": ["C3"]}))
        updated_prompt = json.loads(await tool.triggers(action="update", trigger_id="sl_1", prompt="Updated prompt"))
        bad_update = json.loads(await tool.triggers(action="update", trigger_id="sl_1", match={"bogus": "x"}))
    assert (updated["status"], updated_prompt["status"], bad_update["status"]) == ("ok", "ok", "error")
    assert updated["trigger"]["source"]["match"] == {"channel_id": ["C3"]}
    assert "binding" not in json.dumps(updated)
    reloaded = store.get_trigger("sl_1")
    assert reloaded is not None
    assert isinstance(reloaded.source, ExternalEventSourceSpec)
    assert reloaded.source.binding == {"team_id": "T1", "slack_user_id": "U_ALICE"}
    assert reloaded.source.match == {"channel_id": ["C3"]}


@pytest.mark.asyncio
async def test_triggers_tool_registered_event_source_paused_test_history_and_watch_guard(
    tmp_path: Path,
) -> None:
    """Registered EventSource list paused_reason, dry-run test, calling-room history gating, and watch-turn guard."""
    private_room, shared_room, admin = "!priv:localhost", "!shared:localhost", "@admin:localhost"
    tool, store, _source, room_members, make_ctx = _setup_registered_tool_env(tmp_path, connected_users={_OWNER})

    with tool_runtime_context(make_ctx(requester_id=_OWNER)):
        created = json.loads(
            await tool.triggers(
                action="subscribe_event",
                trigger_id="sl_1",
                source_id="slack_private",
                kind="message",
                prompt="Summarize Slack ping",
                match={"channel_id": ["C3"]},
            ),
        )
        active_list = json.loads(await tool.triggers(action="list"))
    assert created["status"] == "ok"
    assert active_list["reactive_triggers"][0]["paused_reason"] is None
    assert "binding" not in json.dumps(active_list)

    room_members[private_room] = [_OWNER, _OTHER, "@mindroom_research:localhost"]
    with tool_runtime_context(make_ctx(requester_id=_OWNER)):
        paused_list = json.loads(await tool.triggers(action="list"))
    assert paused_list["reactive_triggers"][0]["paused_reason"] == "Room is no longer private"
    room_members[private_room] = [_OWNER, "@mindroom_research:localhost"]

    with tool_runtime_context(make_ctx(requester_id=_OWNER)):
        dry_hit = json.loads(await tool.triggers(action="test", trigger_id="sl_1", sample_data={"channel_id": "C3"}))
        dry_miss = json.loads(await tool.triggers(action="test", trigger_id="sl_1", sample_data={"channel_id": "C9"}))
    assert (dry_hit["matched"], dry_hit["would_fire"], dry_miss["matched"], dry_miss["would_fire"]) == (
        True,
        True,
        False,
        False,
    )

    rule = store.get_trigger("sl_1")
    assert rule is not None
    assert store.enqueue_firing(
        "sl_1",
        "sl-ev-1",
        TriggerEvent(
            source_type="external_event",
            source_id="slack_private",
            kind="message",
            event_id="sl-ev-1",
            timestamp=1_000.0,
            actor="U_COWORKER",
            text="Private Slack DM about acquisition",
            data={"channel_id": "C3"},
        ),
        expected_rule_uid=rule.rule_uid,
    )
    firing_id = store.list_firings(trigger_id="sl_1")[0].id
    assert store.claim_firing(firing_id, now=1_001.0, triage_reason="Mentions secret acquisition deal") == "claimed"

    with tool_runtime_context(make_ctx(requester_id=_OWNER, room_id=private_room)):
        priv_hist = json.loads(await tool.triggers(action="history", trigger_id="sl_1"))
    assert priv_hist["firings"][0]["event"]["text"] == "Private Slack DM about acquisition"
    assert priv_hist["firings"][0]["triage_reason"] == "Mentions secret acquisition deal"

    with tool_runtime_context(make_ctx(requester_id=_OWNER, room_id=shared_room)):
        shared_scoped = json.loads(await tool.triggers(action="history", trigger_id="sl_1"))
        shared_unscoped = json.loads(await tool.triggers(action="history"))
    assert (shared_scoped["status"], shared_unscoped["count"]) == ("error", 0)

    with tool_runtime_context(make_ctx(requester_id=admin, room_id=shared_room)):
        admin_list = json.loads(await tool.triggers(action="list"))
        admin_hist_scoped = json.loads(await tool.triggers(action="history", trigger_id="sl_1"))
        admin_hist_unscoped = json.loads(await tool.triggers(action="history"))
        admin_test = json.loads(await tool.triggers(action="test", trigger_id="sl_1"))
    assert (
        admin_list["reactive_triggers"],
        admin_hist_scoped["status"],
        admin_hist_unscoped["count"],
        admin_test["status"],
    ) == ([], "error", 0, "error")
    assert "sl_1" not in json.dumps(admin_list)
    assert "paused_reason" not in json.dumps(admin_list)
    assert "acquisition" not in json.dumps(admin_hist_unscoped)

    mutating_calls: list[dict[str, Any]] = [
        {"action": "watch_room", "prompt": "x"},
        {"action": "watch_thread", "prompt": "x"},
        {"action": "watch_pr", "repo": "org/repo", "number": 1},
        {"action": "subscribe_event", "source_id": "slack_private", "kind": "message", "prompt": "x"},
        {"action": "create", "trigger_id": "d1", "public_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="},
        {"action": "update", "trigger_id": "sl_1", "prompt": "hijack"},
        {"action": "set_enabled", "trigger_id": "sl_1", "enabled": False},
        {"action": "delete", "trigger_id": "sl_1"},
        {"action": "rotate_key", "trigger_id": "d1", "public_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="},
    ]
    for watch_hook in ("event_source:slack_private", "slack-watch/w1"):
        with tool_runtime_context(make_ctx(requester_id=_OWNER, room_id=private_room, hook_source=watch_hook)):
            for call_kwargs in mutating_calls:
                res = json.loads(await tool.triggers(**call_kwargs))
                assert (res["status"], res["message"]) == (
                    "error",
                    "Ask me in a new message to change your triggers.",
                )
            assert json.loads(await tool.triggers(action="list"))["status"] == "ok"
            assert json.loads(await tool.triggers(action="history", trigger_id="sl_1"))["status"] == "ok"
            assert json.loads(await tool.triggers(action="test", trigger_id="sl_1"))["status"] == "ok"

    with tool_runtime_context(make_ctx(requester_id=_OWNER, room_id=private_room, hook_source=None)):
        assert json.loads(await tool.triggers(action="delete", trigger_id="sl_1"))["status"] == "ok"


def test_event_source_descriptor_default_max_fires_validation() -> None:
    """EventSourceDescriptor validates default_max_fires_per_hour >= 1 (and rejects booleans)."""
    assert EventSourceDescriptor(source_id="ok", description="Ok").default_max_fires_per_hour is None
    assert (
        EventSourceDescriptor(
            source_id="ok",
            description="Ok",
            default_max_fires_per_hour=60,
        ).default_max_fires_per_hour
        == 60
    )
    for bad in (0, -5, True, False):
        with pytest.raises(ValueError, match="default_max_fires_per_hour"):
            EventSourceDescriptor(source_id="ok", description="Ok", default_max_fires_per_hour=bad)


@pytest.mark.asyncio
async def test_registered_source_admin_visibility_and_unregistered_hiding(tmp_path: Path) -> None:
    """Non-owner admins cannot read/toggle/delete private or unregistered triggers, but can manage visible ones."""
    shared_room, admin = "!shared:localhost", "@admin:localhost"
    tool, store, _priv_source, _room_members, make_ctx = _setup_registered_tool_env(tmp_path, connected_users={_OWNER})
    registry: EventSourceRegistry = make_ctx(requester_id=_OWNER).orchestrator.reactive_triggers.event_sources  # type: ignore[union-attr]

    class _PublicSource(EventSource):
        source_id = "public_alerts"
        description = "Public alerts"
        allowed_kinds = ("alert",)
        private_room_only = False

        async def check_delivery_access(
            self,
            ctx: EventSourceContext,
            *,
            rule: ReactiveTriggerRecord,
            event: TriggerEvent | None = None,
        ) -> EventSourceAccessDenial | None:
            del ctx, rule, event
            return EventSourceAccessDenial(reason="owner-only pause detail")

    registry.register(_PublicSource())
    with tool_runtime_context(make_ctx(requester_id=_OWNER)):
        sub_priv = json.loads(
            await tool.triggers(
                action="subscribe_event",
                trigger_id="owner_priv",
                source_id="slack_private",
                kind="message",
                prompt="Private watch",
            ),
        )
        sub_pub = json.loads(
            await tool.triggers(
                action="subscribe_event",
                trigger_id="owner_pub",
                source_id="public_alerts",
                kind="alert",
                prompt="Public alert",
            ),
        )
    assert (sub_priv["status"], sub_pub["status"]) == ("ok", "ok")

    with tool_runtime_context(make_ctx(requester_id=admin, room_id=shared_room)):
        admin_list = json.loads(await tool.triggers(action="list"))
        admin_priv_hist = json.loads(await tool.triggers(action="history", trigger_id="owner_priv"))
        admin_priv_test = json.loads(await tool.triggers(action="test", trigger_id="owner_priv"))
        admin_priv_disable = json.loads(
            await tool.triggers(action="set_enabled", trigger_id="owner_priv", enabled=False),
        )
        admin_priv_delete = json.loads(await tool.triggers(action="delete", trigger_id="owner_priv"))
        admin_pub_disable = json.loads(await tool.triggers(action="set_enabled", trigger_id="owner_pub", enabled=False))
        admin_pub_enable = json.loads(await tool.triggers(action="set_enabled", trigger_id="owner_pub", enabled=True))
        admin_pub_delete = json.loads(await tool.triggers(action="delete", trigger_id="owner_pub"))
    listed_by_id = {t["trigger_id"]: t for t in admin_list["reactive_triggers"]}
    assert "owner_priv" not in listed_by_id
    assert "owner_pub" in listed_by_id
    assert "paused_reason" not in listed_by_id["owner_pub"]
    assert "owner-only pause detail" not in json.dumps(admin_list)
    assert (
        admin_priv_hist["status"],
        admin_priv_test["status"],
        admin_priv_disable["status"],
        admin_priv_delete["status"],
    ) == ("error", "error", "error", "error")
    assert "Private watch" not in json.dumps(admin_priv_disable)
    reloaded_priv = store.get_trigger("owner_priv")
    assert reloaded_priv is not None
    assert reloaded_priv.enabled is True
    assert (admin_pub_disable["status"], admin_pub_enable["status"], admin_pub_delete["status"]) == ("ok", "ok", "ok")
    assert store.get_trigger("owner_pub") is None

    entry_token = registry._entries["slack_private"].token
    assert registry.unregister("slack_private", entry_token) is True
    with tool_runtime_context(make_ctx(requester_id=admin, room_id=shared_room)):
        admin_list_after = json.loads(await tool.triggers(action="list"))
        admin_hist_after = json.loads(await tool.triggers(action="history", trigger_id="owner_priv"))
        admin_disable_after = json.loads(
            await tool.triggers(action="set_enabled", trigger_id="owner_priv", enabled=False),
        )
        admin_delete_after = json.loads(await tool.triggers(action="delete", trigger_id="owner_priv"))
    assert "owner_priv" not in {t["trigger_id"] for t in admin_list_after["reactive_triggers"]}
    assert (admin_hist_after["status"], admin_disable_after["status"], admin_delete_after["status"]) == (
        "error",
        "error",
        "error",
    )
    reloaded_priv_after = store.get_trigger("owner_priv")
    assert reloaded_priv_after is not None
    assert reloaded_priv_after.enabled is True


@pytest.mark.asyncio
async def test_registered_source_history_redaction_and_per_event_checks(tmp_path: Path) -> None:
    """check_history_access runs per firing event (caching only room state) and redacts denied firings."""
    private_room, shared_room, admin = "!priv:localhost", "!shared:localhost", "@admin:localhost"
    tool, store, priv_source, room_members, make_ctx = _setup_registered_tool_env(
        tmp_path,
        connected_users={_OWNER, admin},
    )
    priv_source.visible_to.add(admin)

    def _enqueue(trigger_id: str, dedupe_key: str, ev_id: str, text: str, ts: float) -> None:
        rule = store.get_trigger(trigger_id)
        assert rule is not None
        assert store.enqueue_firing(
            trigger_id,
            dedupe_key,
            TriggerEvent(
                source_type="external_event",
                source_id="slack_private",
                kind="message",
                event_id=ev_id,
                timestamp=ts,
                text=text,
            ),
            expected_rule_uid=rule.rule_uid,
        )

    # Non-admin owner: firing 1 allowed, firing 2 denied on the same rule.
    priv_source.denied_event_ids.update({"owner-deny", "admin-ev-deny"})
    with tool_runtime_context(make_ctx(requester_id=_OWNER)):
        sub = json.loads(
            await tool.triggers(
                action="subscribe_event",
                trigger_id="owner_priv",
                source_id="slack_private",
                kind="message",
                prompt="Private watch",
            ),
        )
    assert sub["status"] == "ok"
    _enqueue("owner_priv", "T1:C3:100.0", "owner-allow", "Allowed owner msg", 1_000.0)
    _enqueue("owner_priv", "T1:C3:100.1", "owner-deny", "Denied owner msg", 1_001.0)
    priv_source.history_contexts.clear()
    owner_ctx = make_ctx(requester_id=_OWNER, room_id=private_room)
    owner_ctx.client.room_get_state.reset_mock()  # type: ignore[union-attr]
    with tool_runtime_context(owner_ctx):
        owner_hist = json.loads(await tool.triggers(action="history", trigger_id="owner_priv"))
    assert (owner_hist["count"], owner_hist["firings"][0]["event"]["event_id"]) == (1, "owner-allow")
    assert [ev.event_id if ev is not None else None for _, ev in priv_source.history_contexts] == [
        None,
        "owner-deny",
        "owner-allow",
    ]
    assert owner_ctx.client.room_get_state.await_count == 1  # type: ignore[union-attr]

    # Admin owner on the SAME rule with TWO firings: firing 1 allowed (payload kept), firing 2 denied (redacted).
    room_members[private_room] = [admin, "@mindroom_research:localhost"]
    with tool_runtime_context(make_ctx(requester_id=admin, room_id=private_room)):
        admin_sub = json.loads(
            await tool.triggers(
                action="subscribe_event",
                trigger_id="admin_priv",
                source_id="slack_private",
                kind="message",
                prompt="Admin private watch",
            ),
        )
    assert admin_sub["status"] == "ok"
    _enqueue("admin_priv", "T_TEAM:C_ALLOWED:1700000.001", "admin-ev-allow", "Allowed admin DM", 2_000.0)
    _enqueue("admin_priv", "T_TEAM:C_SECRET:1700000.002", "admin-ev-deny", "Top secret denied DM", 2_001.0)
    firings_by_dedupe = {f.dedupe_key: f for f in store.list_firings(trigger_id="admin_priv")}
    assert store.mark_skipped(firings_by_dedupe["T_TEAM:C_ALLOWED:1700000.001"].id, "allowable note")
    assert store.mark_skipped(firings_by_dedupe["T_TEAM:C_SECRET:1700000.002"].id, "another person is in this room")
    priv_source.history_contexts.clear()

    admin_priv_ctx = make_ctx(requester_id=admin, room_id=private_room)
    admin_priv_ctx.client.room_get_state.reset_mock()  # type: ignore[union-attr]
    with tool_runtime_context(admin_priv_ctx):
        mixed_hist = json.loads(await tool.triggers(action="history", trigger_id="admin_priv"))
    assert mixed_hist["count"] == 2
    assert [ev.event_id if ev is not None else None for _, ev in priv_source.history_contexts] == [
        None,
        "admin-ev-deny",
        "admin-ev-allow",
    ]
    assert admin_priv_ctx.client.room_get_state.await_count == 1  # type: ignore[union-attr]
    rows_by_id = {row["id"]: row for row in mixed_hist["firings"]}
    allowed_row = rows_by_id[firings_by_dedupe["T_TEAM:C_ALLOWED:1700000.001"].id]
    denied_row = rows_by_id[firings_by_dedupe["T_TEAM:C_SECRET:1700000.002"].id]
    assert (
        allowed_row["event"]["event_id"],
        allowed_row["event"]["text"],
        allowed_row["dedupe_key"],
        allowed_row["error_message"],
    ) == ("admin-ev-allow", "Allowed admin DM", "T_TEAM:C_ALLOWED:1700000.001", "allowable note")
    assert (denied_row["event"], denied_row["triage_reason"]) == (None, None)
    assert {"error_message", "dedupe_key"}.isdisjoint(denied_row)
    assert all(
        secret not in json.dumps(denied_row)
        for secret in ("another person is in this room", "T_TEAM:C_SECRET", "Top secret")
    )

    # In a shared room, both firings are redacted.
    with tool_runtime_context(make_ctx(requester_id=admin, room_id=shared_room)):
        redacted_hist = json.loads(await tool.triggers(action="history", trigger_id="admin_priv"))
    assert redacted_hist["count"] == 2
    assert all(
        row["event"] is None and "error_message" not in row and "dedupe_key" not in row
        for row in redacted_hist["firings"]
    )


@pytest.mark.asyncio
async def test_registered_and_yaml_external_sources_are_strictly_isolated(tmp_path: Path) -> None:
    """YAML webhooks never match/authorize registered rules, emit_event never matches YAML rules, and subscribe fails closed on conflicts."""
    gh_cfg = GitHubEventSourceConfig(
        kind="github",
        secret_env="GH_SECRET",  # noqa: S106
        allowed_repos=["org/app"],
        allowed_events=["push"],
        subscribers=[_OWNER],
    )
    config, runtime_paths = _make_config(tmp_path, event_sources={"shared_id": gh_cfg})
    store = ReactiveTriggerStore(runtime_paths)

    reg_rule = _make_rule(
        trigger_id="reg_rule",
        source_id="shared_id",
        kind="push",
        match={"repo": "org/app"},
        source_origin="registered",
    )
    yaml_rule = _make_rule(
        trigger_id="yaml_rule",
        source_id="shared_id",
        kind="push",
        match={"repo": "org/app"},
        source_origin="yaml",
    )
    store.create_trigger(reg_rule)
    store.create_trigger(yaml_rule)

    event = TriggerEvent(
        source_type="external_event",
        source_id="shared_id",
        kind="push",
        event_id="ev-1",
        timestamp=1_000.0,
        data={"repo": "org/app"},
    )

    # YAML webhook fan-out must match ONLY yaml_rule, NEVER reg_rule.
    enqueued = fan_out_external_event(
        source_id="shared_id",
        source=gh_cfg,
        event=event,
        store=store,
        config=config,
        runtime_paths=runtime_paths,
        now=1_000.0,
    )
    assert enqueued == 1
    assert len(store.list_firings(trigger_id="yaml_rule")) == 1
    assert store.list_firings(trigger_id="reg_rule") == []

    # Even with an empty EventSourceRegistry (registered source absent), dispatch NEVER falls back to YAML config!
    empty_registry = EventSourceRegistry(lambda: config, AsyncMock(return_value=0))
    assert (
        check_external_event_source_access(
            owner_user_id=_OWNER,
            source_spec=reg_rule.source,  # type: ignore[arg-type]
            event=event,
            config=config,
            runtime_paths=runtime_paths,
        )
        == "source_removed"
    )
    reg_dispatch_check = await check_reactive_source_access(
        rule=reg_rule,
        event=event,
        source_bot=None,
        config=config,
        runtime_paths=runtime_paths,
        memberships=AgentReplyMembershipIndex(),
        event_sources=empty_registry,
    )
    assert reg_dispatch_check == DispatchResult(status="skipped", reason="source_removed")

    # Service emit_event matches ONLY source_origin=="registered" rules, NEVER source_origin=="yaml" rules.
    config_no_yaml, runtime_paths2 = _make_config(tmp_path / "svc")
    store2 = ReactiveTriggerStore(runtime_paths2)
    store2.create_trigger(reg_rule)
    store2.create_trigger(yaml_rule)
    service = ReactiveTriggerService(
        config_provider=lambda: config_no_yaml,
        runtime_paths=runtime_paths2,
        bots=dict,
        memberships=AgentReplyMembershipIndex(),
        store=store2,
    )
    emitter = service.event_sources.register(_CustomSource("shared_id"))
    assert await emitter.emit(kind="push", event_id="ev-2", data={"repo": "org/app"}) == 1
    assert len(store2.list_firings(trigger_id="reg_rule")) == 1
    assert store2.list_firings(trigger_id="yaml_rule") == []

    # If a source_id appears in BOTH YAML event_sources and EventSourceRegistry (e.g. after config reload),
    # triggers(action="subscribe_event") fails closed with an explicit conflict error.
    tool, _store3, _priv, _rm, make_ctx = _setup_registered_tool_env(tmp_path / "conflict", connected_users={_OWNER})
    ctx = make_ctx(requester_id=_OWNER)
    conflict_registry: EventSourceRegistry = ctx.orchestrator.reactive_triggers.event_sources  # type: ignore[union-attr]
    # Simulate a config reload introducing a YAML source colliding with an already-registered source.
    ctx.config.external_trigger_policy.event_sources["slack_private"] = gh_cfg
    assert conflict_registry.get("slack_private") is not None
    with tool_runtime_context(ctx):
        conflict_res = json.loads(
            await tool.triggers(
                action="subscribe_event",
                source_id="slack_private",
                kind="message",
                prompt="Watch",
            ),
        )
    assert conflict_res["status"] == "error"
    assert "conflict" in conflict_res["message"].lower()


@pytest.mark.asyncio
async def test_update_passes_readonly_binding_copy_and_ignores_in_place_mutation_attempts(tmp_path: Path) -> None:
    """Updating match passes a read-only MappingProxyType copy of binding and persists the original binding untouched."""
    mutation_blocked = False

    class _MutatingSource(EventSource):
        source_id = "mut_src"
        description = "Mutating test source"
        allowed_kinds = ("message",)

        async def validate_subscription(
            self,
            ctx: EventSourceContext,
            *,
            kind: str,
            match: Mapping[str, Any],
            existing_binding: Mapping[str, Any] | None = None,
        ) -> ValidatedSubscription:
            nonlocal mutation_blocked
            del ctx
            if existing_binding is not None:
                try:
                    existing_binding["team_id"] = "T_TAMPERED"  # type: ignore[index]
                except TypeError:
                    mutation_blocked = True
                return ValidatedSubscription(
                    kind=kind,
                    match=dict(match),
                    binding={"team_id": "T_RETURNED_TAMPERED", "slack_user_id": "U_EVIL"},
                )
            return ValidatedSubscription(
                kind=kind,
                match=dict(match),
                binding={"team_id": "T_ORIG", "slack_user_id": "U_ORIG"},
            )

    tool, store, _priv, _rm, make_ctx = _setup_registered_tool_env(tmp_path)
    ctx = make_ctx(requester_id=_OWNER)
    ctx.orchestrator.reactive_triggers.event_sources.register(_MutatingSource())  # type: ignore[union-attr]

    with tool_runtime_context(ctx):
        created = json.loads(
            await tool.triggers(
                action="subscribe_event",
                trigger_id="mut_1",
                source_id="mut_src",
                kind="message",
                prompt="Watch",
                match={"channel_id": "C1"},
            ),
        )
        assert created["status"] == "ok"
        updated = json.loads(
            await tool.triggers(
                action="update",
                trigger_id="mut_1",
                match={"channel_id": "C2"},
            ),
        )
    assert updated["status"] == "ok"
    assert mutation_blocked is True
    reloaded = store.get_trigger("mut_1")
    assert reloaded is not None
    assert isinstance(reloaded.source, ExternalEventSourceSpec)
    assert reloaded.source.match == {"channel_id": "C2"}
    assert reloaded.source.binding == {"team_id": "T_ORIG", "slack_user_id": "U_ORIG"}
    assert reloaded.source.source_origin == "registered"


@pytest.mark.asyncio
async def test_dynamic_descriptor_private_room_only_hides_trigger_in_list_and_room_api(tmp_path: Path) -> None:
    """Descriptor-level private_room_only=True is persisted and hides the rule in both list and GET room triggers."""
    from tests.api.test_room_triggers import ALICE, BOB, ROOM_A  # noqa: PLC0415
    from tests.api.test_room_triggers import _Harness as RoomTriggersHarness  # noqa: PLC0415

    shared_room, admin = "!shared:localhost", "@admin:localhost"
    tool, store, _priv_source, _room_members, make_ctx = _setup_registered_tool_env(tmp_path, connected_users={_OWNER})
    registry: EventSourceRegistry = make_ctx(requester_id=_OWNER).orchestrator.reactive_triggers.event_sources  # type: ignore[union-attr]

    class _DynamicPrivateSource(EventSource):
        source_id = "dyn_private"
        description = "Dynamic privacy source"
        allowed_kinds = ("alert",)
        private_room_only = False

        async def describe_for_user(self, ctx: EventSourceContext) -> EventSourceDescriptor | None:
            del ctx
            return EventSourceDescriptor(
                source_id=self.source_id,
                description=self.description,
                allowed_kinds=self.allowed_kinds,
                private_room_only=True,
            )

    dyn_source = _DynamicPrivateSource()
    registry.register(dyn_source)

    with tool_runtime_context(make_ctx(requester_id=_OWNER)):
        created = json.loads(
            await tool.triggers(
                action="subscribe_event",
                trigger_id="dyn_priv_1",
                source_id="dyn_private",
                kind="alert",
                match={"env": "prod"},
                prompt="Secret dynamic alert",
            ),
        )
        updated = json.loads(
            await tool.triggers(
                action="update",
                trigger_id="dyn_priv_1",
                match={"env": "staging"},
            ),
        )
    assert (created["status"], updated["status"]) == ("ok", "ok")
    stored = store.get_trigger("dyn_priv_1")
    assert stored is not None
    assert isinstance(stored.source, ExternalEventSourceSpec)
    assert stored.source.private_room_only is True

    # Hidden from non-owners (including admin) in triggers(action="list") and blocked for set_enabled/delete.
    with tool_runtime_context(make_ctx(requester_id=admin, room_id=shared_room)):
        admin_list = json.loads(await tool.triggers(action="list"))
        admin_disable = json.loads(await tool.triggers(action="set_enabled", trigger_id="dyn_priv_1", enabled=False))
        admin_delete = json.loads(await tool.triggers(action="delete", trigger_id="dyn_priv_1"))
    assert "dyn_priv_1" not in {t["trigger_id"] for t in admin_list["reactive_triggers"]}
    assert (admin_disable["status"], admin_delete["status"]) == ("error", "error")

    # Also hidden from non-owners in GET /api/rooms/{room_id}/triggers even though class-level private_room_only=False.
    h = RoomTriggersHarness(tmp_path / "api_room")
    api_registry = EventSourceRegistry(lambda: h.config, AsyncMock(return_value=0))
    api_registry.register(_DynamicPrivateSource())
    ensure_app_state(h.app).embedded = SimpleNamespace(  # type: ignore[assignment]
        reactive_triggers=SimpleNamespace(event_sources=api_registry),
    )
    h.add_reactive(
        "dyn_priv_api",
        stored.source,
        description="Dynamic private rule",
        prompt="Secret dynamic alert",
        created_at=1.0,
    )
    alice_rows = {row["trigger_id"]: row for row in h.req("GET", ROOM_A, ALICE).json()["triggers"]}
    bob_resp = h.req("GET", ROOM_A, BOB)
    assert bob_resp.status_code == 200
    assert "dyn_priv_api" in alice_rows
    assert "dyn_priv_api" not in bob_resp.text


@pytest.mark.asyncio
async def test_broken_plugin_hooks_do_not_crash_triggers_list(tmp_path: Path) -> None:
    """Raising describe_for_user / check_delivery_access hooks are isolated during triggers(action='list')."""
    tool, _store, _priv_source, _room_members, make_ctx = _setup_registered_tool_env(tmp_path, connected_users={_OWNER})
    registry: EventSourceRegistry = make_ctx(requester_id=_OWNER).orchestrator.reactive_triggers.event_sources  # type: ignore[union-attr]

    class _BrokenDiscoverySource(EventSource):
        source_id = "broken_describe"
        description = "Broken describe"

        async def describe_for_user(self, ctx: EventSourceContext) -> EventSourceDescriptor | None:
            del ctx
            msg = "describe crashed"
            raise RuntimeError(msg)

    class _BrokenAccessSource(EventSource):
        source_id = "broken_access"
        description = "Broken delivery access"
        allowed_kinds = ("alert",)

        async def check_delivery_access(
            self,
            ctx: EventSourceContext,
            *,
            rule: ReactiveTriggerRecord,
            event: TriggerEvent | None = None,
        ) -> EventSourceAccessDenial | None:
            del ctx, rule, event
            msg = "access crashed"
            raise RuntimeError(msg)

    registry.register(_BrokenDiscoverySource())
    registry.register(_BrokenAccessSource())

    with tool_runtime_context(make_ctx(requester_id=_OWNER)):
        sub = json.loads(
            await tool.triggers(
                action="subscribe_event",
                trigger_id="broken_access_rule",
                source_id="broken_access",
                kind="alert",
                prompt="Handle alert",
            ),
        )
        assert sub["status"] == "ok"
        listed = json.loads(await tool.triggers(action="list"))

    assert listed["status"] == "ok"
    source_ids = {item["source_id"] for item in listed["event_sources"]}
    assert "broken_describe" not in source_ids
    assert "slack_private" in source_ids
    assert "broken_access" in source_ids
    by_id = {t["trigger_id"]: t for t in listed["reactive_triggers"]}
    assert by_id["broken_access_rule"]["paused_reason"] == "source_error"
