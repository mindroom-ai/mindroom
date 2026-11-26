"""Tests for the EventSource emitter seam and Task 1 reactive trigger extensions."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.external_trigger_policy import ExternalTriggerPolicyConfig, GitHubEventSourceConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import HOOK_SOURCE_KEY, PER_FIRE_THREAD_ROOT_KEY, SOURCE_KIND_KEY
from mindroom.dispatch_source import REACTIVE_TRIGGER_SOURCE_KIND, hook_source_from_content
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.matrix.room_reconciliation import RoomStateSnapshot
from mindroom.reactive_triggers import (
    DispatchResult,
    EventSource,
    EventSourceContext,
    EventSourceDescriptor,
    EventSourceEmitter,
    EventSourceInactiveError,
    EventSourceRegistry,
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
    ValidatedSubscription,
    check_reactive_source_access,
    dispatch_reactive_trigger,
    matches_external_event,
)
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Collection
    from pathlib import Path

    from mindroom.constants import RuntimePaths

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
    target: TriggerTargetSpec | None = None,
    triage: TriageSpec | None = None,
    enabled: bool = True,
    expires_at: float | None = None,
) -> ReactiveTriggerRecord:
    return ReactiveTriggerRecord(
        trigger_id=trigger_id,
        owner_user_id=owner_user_id,
        source=ExternalEventSourceSpec(source_id=source_id, kind=kind, match=match or {}, binding=binding or {}),
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


def _bot(*, latest_thread_event_id: str | None = "$latest", joined_rooms: Collection[str] = (_ROOM_B,)) -> SimpleNamespace:
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
        delivery_decision: DispatchResult | None = None,
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
    ) -> DispatchResult | None:
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
        agent="research", room_id=_ROOM_B, new_thread=True, thread_key_from_event=True, prompt="go",
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
            agent="research", room_id=_ROOM_B, thread_id="$t1", new_thread=True, thread_key_from_event=True, prompt="go",
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
            ExternalEventSourceSpec(source_id="slack", kind="message", match={"urgent": ["True"]}), ev,
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
        ctx, kind=" build.failed ", match={"repo": ["a/b"]}, existing_binding={"w": "1"},
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
                kind="github", secret_env="GH_SECRET", allowed_repos=["org/repo"], subscribers=[_OWNER],  # noqa: S106
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
    assert registry.is_active_token("slack", builtin_slack.token) is True


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
    assert registry.is_active_token("acme_builds", old_emitter.token) is False
    assert registry.is_active_token("acme_builds", new_emitter.token) is True

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
        ["collide_rule", "slack_watch_a"], "C1:1", ttl_seconds=600.0, now=1599.0,
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
            "mindroom.reactive_triggers.dispatch.read_room_state", new=AsyncMock(return_value=state),
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

    # Registered check_delivery_access skip vs fallback to YAML check (source_removed).
    source.delivery_decision = DispatchResult(status="skipped", reason="room_not_private")
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
    source.delivery_decision = DispatchResult(status="skipped", reason="account_changed")
    with (
        patch("mindroom.reactive_triggers.dispatch.read_room_state", new=AsyncMock(return_value=state)),
        patch(
            "mindroom.reactive_triggers.service.evaluate_triage", new=AsyncMock(return_value=TriageOutcome(act=True)),
        ) as triage_mock,
    ):
        await service.drain_due_firings_once()
    triage_mock.assert_not_awaited()
    assert store.list_firings(trigger_id="alias_rule")[0].error_message == "account_changed"

    source.delivery_decision = None
    assert (
        await emitter.emit(
            kind="message", event_id="ev-ok", thread_key="C1:thread1", owner_user_ids=[_OWNER], audience={"U_ALICE"},
        )
        == 2
    )
    with (
        patch("mindroom.reactive_triggers.dispatch.read_room_state", new=AsyncMock(return_value=state)),
        patch(
            "mindroom.reactive_triggers.service.evaluate_triage", new=AsyncMock(return_value=TriageOutcome(act=True)),
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
