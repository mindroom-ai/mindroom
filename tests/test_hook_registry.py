"""Tests for hook registry compilation and lookup."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.automations import automation
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.plugin import HookOverrideConfig, PluginEntryConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.hooks import (
    EVENT_BOT_READY,
    EVENT_CONFIG_RELOADED,
    EVENT_MESSAGE_ENRICH,
    EVENT_MESSAGE_RECEIVED,
    HookRegistry,
    MessageEnvelope,
    MessageReceivedContext,
    hook,
)
from mindroom.hooks.execution import _eligible_hooks
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget
from tests.conftest import bind_runtime_paths, message_origin, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.hooks.types import RegisteredHook


def _config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(display_name="Code", rooms=["!room:localhost"]),
            },
        ),
        runtime_paths,
    )


def _plugin(
    name: str,
    callbacks: list[Any],
    *,
    plugin_order: int = 0,
    settings: dict[str, Any] | None = None,
    hooks: dict[str, HookOverrideConfig] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        discovered_hooks=tuple(callbacks),
        discovered_automations=(),
        entry_config=PluginEntryConfig(
            path=f"./plugins/{name}",
            settings=settings or {},
            hooks=hooks or {},
        ),
        plugin_order=plugin_order,
    )


def _message_received_context(
    tmp_path: Path,
    *,
    agent_name: str = "code",
    room_id: str = "!room:localhost",
) -> MessageReceivedContext:
    config = _config(tmp_path)
    return MessageReceivedContext(
        event_name=EVENT_MESSAGE_RECEIVED,
        plugin_name="",
        settings={},
        config=config,
        runtime_paths=runtime_paths_for(config),
        logger=get_logger("tests.hooks").bind(event_name=EVENT_MESSAGE_RECEIVED),
        correlation_id="corr-1",
        envelope=MessageEnvelope(
            source_event_id="$event",
            target=MessageTarget.resolve(room_id, None, "$event"),
            body="hello",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name=agent_name,
            origin=message_origin(sender_id="@user:localhost", requester_id="@user:localhost", source_kind="message"),
        ),
    )


@pytest.mark.parametrize("event_name", [EVENT_MESSAGE_RECEIVED, "agent:stopped", "custom:test"])
def test_required_hooks_are_limited_to_startup(event_name: str) -> None:
    """Required initialization must not change ordinary observer fault isolation."""
    with pytest.raises(ValueError, match="required hooks are only supported for agent:started"):
        hook(event_name, required=True)


def test_hook_registry_orders_by_priority_plugin_order_and_lineno() -> None:
    """The registry should sort hooks deterministically across plugins and files."""

    @hook(EVENT_MESSAGE_RECEIVED, priority=10)
    async def alpha(ctx: object) -> None:
        del ctx

    @hook(EVENT_MESSAGE_RECEIVED, priority=10)
    async def beta(ctx: object) -> None:
        del ctx

    @hook(EVENT_MESSAGE_RECEIVED, priority=50)
    async def gamma(ctx: object) -> None:
        del ctx

    @hook(EVENT_MESSAGE_RECEIVED, priority=10)
    async def delta(ctx: object) -> None:
        del ctx

    registry = HookRegistry.from_plugins(
        [
            _plugin("second-plugin", [delta], plugin_order=1),
            _plugin("first-plugin", [alpha, beta, gamma], plugin_order=0),
        ],
    )

    hooks = registry.hooks_for(EVENT_MESSAGE_RECEIVED)

    assert [(hook.plugin_name, hook.hook_name) for hook in hooks] == [
        ("first-plugin", "alpha"),
        ("first-plugin", "beta"),
        ("second-plugin", "delta"),
        ("first-plugin", "gamma"),
    ]


def test_hook_registry_applies_overrides_and_deduplicates_within_plugin() -> None:
    """Per-hook overrides should apply to the first unique hook name only."""

    @hook(EVENT_MESSAGE_ENRICH, name="duplicate", priority=100, timeout_ms=50)
    async def first_duplicate(ctx: object) -> None:
        del ctx

    @hook(EVENT_MESSAGE_ENRICH, name="duplicate", priority=1, timeout_ms=5)
    async def second_duplicate(ctx: object) -> None:
        del ctx

    @hook(EVENT_MESSAGE_ENRICH, name="disabled")
    async def disabled_hook(ctx: object) -> None:
        del ctx

    registry = HookRegistry.from_plugins(
        [
            _plugin(
                "demo-plugin",
                [first_duplicate, second_duplicate, disabled_hook],
                settings={"api_url": "http://example.test"},
                hooks={
                    "duplicate": HookOverrideConfig(priority=7, timeout_ms=900),
                    "disabled": HookOverrideConfig(enabled=False),
                },
            ),
        ],
    )

    hooks = registry.hooks_for(EVENT_MESSAGE_ENRICH)

    assert len(hooks) == 1
    assert hooks[0].hook_name == "duplicate"
    assert hooks[0].callback is first_duplicate
    assert hooks[0].priority == 7
    assert hooks[0].timeout_ms == 900
    assert hooks[0].settings == {"api_url": "http://example.test"}


def test_hook_registry_scope_filtering_uses_decorator_agents_and_rooms(tmp_path: Path) -> None:
    """Hook eligibility should respect decorator-level agent and room filters."""

    @hook(EVENT_MESSAGE_RECEIVED, name="matching", agents=["code"], rooms=["!room:localhost"])
    async def matching_hook(ctx: object) -> None:
        del ctx

    @hook(EVENT_MESSAGE_RECEIVED, name="wrong-agent", agents=["research"])
    async def wrong_agent_hook(ctx: object) -> None:
        del ctx

    @hook(EVENT_MESSAGE_RECEIVED, name="wrong-room", rooms=["!elsewhere:localhost"])
    async def wrong_room_hook(ctx: object) -> None:
        del ctx

    registry = HookRegistry.from_plugins(
        [_plugin("scoped-plugin", [matching_hook, wrong_agent_hook, wrong_room_hook])],
    )

    eligible = asyncio.run(_eligible_hooks(registry, EVENT_MESSAGE_RECEIVED, _message_received_context(tmp_path)))

    assert [hook.hook_name for hook in eligible] == ["matching"]


def test_the_plugin_snapshot_carries_the_automations_plugins_register() -> None:
    """A plugin's decorated check reaches the runner through the compiled snapshot, beside the built-ins."""

    def check(ctx: object) -> None:  # noqa: ARG001
        return None

    digest = automation("weekly_digest")(check)
    plugin = _plugin("digest", [])
    plugin.discovered_automations = (digest,)

    registry = HookRegistry.from_plugins([plugin])

    definition = registry.automations.get("weekly_digest")
    assert definition is not None
    assert definition.check is digest
    assert registry.automations.get("dreaming") is not None
    assert HookRegistry.empty().automations.get("weekly_digest") is None


def _registered(callback: object, event_name: str) -> RegisteredHook:
    (registered,) = HookRegistry.from_plugins([_plugin("demo", [callback])]).hooks_for(event_name)
    return registered


def test_scope_defaults_to_agent() -> None:
    """A hook that names no scope runs per bot, as every hook did."""

    @hook(EVENT_MESSAGE_RECEIVED, agents=["code"])
    async def per_bot(ctx: object) -> None:
        del ctx

    registered = _registered(per_bot, EVENT_MESSAGE_RECEIVED)

    assert registered.scope == "agent"
    assert registered.agents == ("code",)


def test_room_scope_rejects_agents_filter() -> None:
    """A room-scoped hook runs for the room, so it cannot also name agents."""
    with pytest.raises(ValueError, match="agents"):
        hook(EVENT_MESSAGE_RECEIVED, scope="room", agents=["code"])

    @hook(EVENT_MESSAGE_RECEIVED, scope="room")
    async def per_room(ctx: object) -> None:
        del ctx

    assert _registered(per_room, EVENT_MESSAGE_RECEIVED).scope == "room"


def test_service_scope_rejected_on_message_received() -> None:
    """Only the two once-per-process events take the service scope."""
    with pytest.raises(ValueError, match="service"):
        hook(EVENT_MESSAGE_RECEIVED, scope="service")

    for event_name in (EVENT_BOT_READY, EVENT_CONFIG_RELOADED):

        @hook(event_name, scope="service", name=f"service-{event_name}")
        async def per_process(ctx: object) -> None:
            del ctx

        assert _registered(per_process, event_name).scope == "service"


@pytest.mark.parametrize(
    ("event_name", "agents", "rooms", "expected"),
    [
        # The deprecated router filter on bot:ready is the service scope.
        (EVENT_BOT_READY, (ROUTER_AGENT_NAME,), None, ("service", None, None)),
        # On config:reloaded it stays a filter, which never matched that event, so the hook still never runs.
        (EVENT_CONFIG_RELOADED, (ROUTER_AGENT_NAME,), None, ("agent", (ROUTER_AGENT_NAME,), None)),
        # On any other event it is the room scope.
        (EVENT_MESSAGE_RECEIVED, (ROUTER_AGENT_NAME,), ["!room:localhost"], ("room", None, ("!room:localhost",))),
        # Only the exact router filter is the alias; a longer filter stays an agent filter.
        (EVENT_MESSAGE_RECEIVED, (ROUTER_AGENT_NAME, "code"), None, ("agent", (ROUTER_AGENT_NAME, "code"), None)),
    ],
    ids=["bot-ready-service", "config-reloaded-not-aliased", "message-received-room", "longer-filter-not-aliased"],
)
def test_router_alias_registration(
    event_name: str,
    agents: tuple[str, ...],
    rooms: list[str] | None,
    expected: tuple[object, ...],
) -> None:
    """The deprecated router filter is the service scope on bot:ready, the room scope elsewhere, a filter otherwise."""

    @hook(event_name, agents=agents, rooms=rooms)
    async def aliased(ctx: object) -> None:
        del ctx

    registered = _registered(aliased, event_name)

    assert (registered.scope, registered.agents, registered.rooms) == expected


def test_router_alias_warns_once_with_plugin_and_hook_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each aliased hook gets one deprecation warning that says which plugin and hook to change."""
    warnings: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "mindroom.hooks.registry.logger",
        SimpleNamespace(warning=lambda message, **fields: warnings.append((message, fields))),
    )

    @hook(EVENT_MESSAGE_RECEIVED, agents=(ROUTER_AGENT_NAME,), name="once-per-message")
    async def aliased(ctx: object) -> None:
        del ctx

    @hook(EVENT_MESSAGE_RECEIVED, scope="room")
    async def scoped(ctx: object) -> None:
        del ctx

    HookRegistry.from_plugins([_plugin("demo", [aliased, scoped])])

    ((message, fields),) = warnings
    assert "deprecated" in message
    assert fields == {"plugin_name": "demo", "hook_name": "once-per-message", "scope": "room"}
