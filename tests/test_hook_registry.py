"""Tests for hook registry compilation and lookup."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.plugin import HookOverrideConfig, PluginEntryConfig
from mindroom.hooks import (
    EVENT_MESSAGE_ENRICH,
    EVENT_MESSAGE_RECEIVED,
    HookRegistry,
    MessageEnvelope,
    MessageReceivedContext,
    hook,
)
from mindroom.hooks.execution import _eligible_hooks
from mindroom.logging_config import get_logger
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


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
            room_id=room_id,
            thread_id=None,
            resolved_thread_id="$event",
            requester_id="@user:localhost",
            sender_id="@user:localhost",
            body="hello",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name=agent_name,
            source_kind="message",
        ),
    )


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

    eligible = _eligible_hooks(registry, EVENT_MESSAGE_RECEIVED, _message_received_context(tmp_path))

    assert [hook.hook_name for hook in eligible] == ["matching"]
