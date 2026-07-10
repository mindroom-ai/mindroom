"""Tests for the public tool-runtime worker-target resolution API."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from mindroom.config.main import Config, RuntimeConfig
from mindroom.constants import ROUTER_AGENT_NAME, resolve_primary_runtime_paths
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import ToolRuntimeContext
from mindroom.tool_system.worker_routing import descriptive_worker_id_for_key

if TYPE_CHECKING:
    from pathlib import Path


def _context(config: Config, agent_name: str, tmp_path: Path) -> ToolRuntimeContext:
    runtime_paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml")
    return ToolRuntimeContext(
        agent_name=agent_name,
        target=MessageTarget.resolve(
            room_id="!room:localhost",
            thread_id="$thread",
            reply_to_event_id=None,
        ),
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=RuntimeConfig.from_authored(config, runtime_paths),
        runtime_paths=runtime_paths,
        event_cache=AsyncMock(),
        conversation_cache=AsyncMock(),
    )


def test_private_user_agent_scope_resolves_requester_scoped_target(tmp_path: Path) -> None:
    """A private user_agent-scoped agent resolves a requester-scoped target isolating itself."""
    config = Config(
        agents={
            "mind": {
                "display_name": "Mind",
                "private": {"per": "user_agent", "root": "workspace/mind_data"},
            },
        },
    )
    context = _context(config, "mind", tmp_path)

    target = context.resolve_worker_target()

    assert target.worker_scope == "user_agent"
    assert target.routing_agent_name == "mind"
    assert target.worker_key
    assert target.private_agent_names == frozenset({"mind"})


def test_authored_user_agent_scope_resolves_without_private_isolation(tmp_path: Path) -> None:
    """A shared agent with authored worker_scope=user_agent gets a requester-scoped key and no private set."""
    config = Config(agents={"scoped": {"display_name": "Scoped", "worker_scope": "user_agent"}})
    context = _context(config, "scoped", tmp_path)

    target = context.resolve_worker_target()

    assert target.worker_scope == "user_agent"
    assert target.routing_agent_name == "scoped"
    assert target.worker_key
    assert target.private_agent_names == frozenset()


def test_unscoped_agent_resolves_shared_target(tmp_path: Path) -> None:
    """An agent without any worker scope resolves an unscoped target."""
    config = Config(agents={"helper": {"display_name": "Helper"}})
    context = _context(config, "helper", tmp_path)

    target = context.resolve_worker_target()

    assert target.worker_scope is None
    assert target.routing_agent_name == "helper"
    assert target.worker_key is None
    assert target.private_agent_names is None


def test_team_dispatch_raises_a_purposeful_error(tmp_path: Path) -> None:
    """A team-named dispatch context fails loudly instead of resolving a wrong scope."""
    config = Config(
        agents={"code": {"display_name": "Code"}},
        teams={
            "super_team": {
                "display_name": "Super Team",
                "role": "Collaborative engineering assistant",
                "agents": ["code"],
                "mode": "collaborate",
            },
        },
    )
    context = _context(config, "super_team", tmp_path)

    with pytest.raises(ValueError, match="requires an agent dispatch"):
        context.resolve_worker_target()


def test_router_dispatch_raises_a_purposeful_error(tmp_path: Path) -> None:
    """A router-named dispatch context fails loudly like any non-agent dispatch."""
    config = Config(agents={"code": {"display_name": "Code"}})
    context = _context(config, ROUTER_AGENT_NAME, tmp_path)

    with pytest.raises(ValueError, match="requires an agent dispatch"):
        context.resolve_worker_target()


def test_descriptive_worker_id_keeps_scope_readable() -> None:
    """Descriptive worker ids embed scope, requester localpart, and agent, dropping v1 and the default tenant."""
    name = descriptive_worker_id_for_key("v1:default:user_agent:@alice.doe:example.test:code", prefix="agent-vault")
    assert re.fullmatch(r"agent-vault-user-agent-alice-doe-code-[0-9a-f]{10}", name)
    assert len(name) <= 63


def test_descriptive_worker_id_shared_scope_with_default_tenant() -> None:
    """The most common shape, a default-tenant shared agent, reads as shared-<agent>."""
    name = descriptive_worker_id_for_key("v1:default:shared:research", prefix="agent-vault")
    assert name.startswith("agent-vault-shared-research-")


def test_descriptive_worker_id_slugifies_non_dns_prefix_characters() -> None:
    """Underscores or dots in a configured prefix are normalized to a DNS-safe slug."""
    name = descriptive_worker_id_for_key("v1:default:shared:research", prefix="agent_vault.v2")
    assert name.startswith("agent-vault-v2-shared-research-")
    assert re.fullmatch(r"[a-z0-9-]+", name)


def test_descriptive_worker_id_user_scope_uses_requester_localpart() -> None:
    """A user-scoped key keeps only the requester's Matrix localpart, not the homeserver."""
    name = descriptive_worker_id_for_key("v1:default:user:@alice.doe:example.test", prefix="agent-vault")
    assert name.startswith("agent-vault-user-alice-doe-")


def test_descriptive_worker_id_keeps_non_default_tenant() -> None:
    """A non-default tenant stays visible in the descriptive name."""
    name = descriptive_worker_id_for_key("v1:acme:shared:code", prefix="agent-vault")
    assert name.startswith("agent-vault-acme-shared-code-")


def test_descriptive_worker_id_is_stable_and_unique_after_truncation() -> None:
    """Two keys sharing a long slug prefix still get distinct, stable, DNS-safe names."""
    long_requester = "@" + "a" * 80 + ":example.org"
    first = descriptive_worker_id_for_key(f"v1:default:user_agent:{long_requester}:alpha", prefix="agent-vault")
    second = descriptive_worker_id_for_key(f"v1:default:user_agent:{long_requester}:beta", prefix="agent-vault")
    assert first != second
    assert len(first) <= 63
    assert len(second) <= 63
    assert first == descriptive_worker_id_for_key(f"v1:default:user_agent:{long_requester}:alpha", prefix="agent-vault")


def test_descriptive_worker_id_long_prefix_leaves_no_slug_room() -> None:
    """A prefix consuming the whole label budget falls back to prefix-digest within 63 chars."""
    long_prefix = "a" * 80
    name = descriptive_worker_id_for_key("v1:default:user_agent:@alice.doe:example.test:code", prefix=long_prefix)
    assert len(name) <= 63
    assert re.fullmatch(r"[a-z0-9-]+", name)
    assert name.startswith("a" * 52)
