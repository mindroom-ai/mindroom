"""Tests for pure hook-ingress policy helpers."""

from __future__ import annotations

from typing import Any, cast

import pytest

from mindroom.dispatch_source import is_automation_source_kind
from mindroom.hooks.context import MessageEnvelope
from mindroom.hooks.ingress import hook_ingress_policy
from mindroom.hooks.types import format_hook_source, split_hook_source
from mindroom.message_target import MessageTarget
from tests.conftest import message_origin


def _envelope(
    *,
    source_kind: str = "message",
    hook_source: str | None = None,
    message_received_depth: int = 0,
) -> MessageEnvelope:
    return MessageEnvelope(
        source_event_id="$event",
        room_id="!room:localhost",
        target=MessageTarget.resolve("!room:localhost", None, "$event"),
        requester_id="@user:localhost",
        sender_id="@user:localhost",
        body="hello",
        attachment_ids=(),
        mentioned_agents=(),
        agent_name="code",
        source_kind=source_kind,
        hook_source=hook_source,
        message_received_depth=message_received_depth,
        origin=message_origin(sender_id="@user:localhost", requester_id="@user:localhost", source_kind=source_kind),
    )


def test_split_hook_source_parses_serialized_tag() -> None:
    """Hook provenance tags should split into plugin and event name once."""
    assert split_hook_source("origin-plugin:message:received") == (
        "origin-plugin",
        "message:received",
    )
    assert split_hook_source("bad") == (None, None)
    assert split_hook_source(None) == (None, None)


def test_hook_source_formatter_matches_ingress_parser() -> None:
    """Serialized hook provenance should round-trip through the ingress parser."""
    source = format_hook_source("origin-plugin", "message:received")

    assert source == "origin-plugin:message:received"
    assert split_hook_source(source) == ("origin-plugin", "message:received")


def test_message_envelope_requires_origin() -> None:
    """Every envelope should carry canonical turn-origin policy."""
    missing_origin_kwargs: dict[str, Any] = {
        "source_event_id": "$event",
        "room_id": "!room:localhost",
        "target": MessageTarget.resolve("!room:localhost", None, "$event"),
        "requester_id": "@user:localhost",
        "sender_id": "@user:localhost",
        "body": "hello",
        "attachment_ids": (),
        "mentioned_agents": (),
        "agent_name": "code",
        "source_kind": "message",
    }
    with pytest.raises(TypeError, match="origin"):
        MessageEnvelope(**missing_origin_kwargs)
    with pytest.raises(TypeError, match="origin"):
        MessageEnvelope(
            source_event_id="$event",
            room_id="!room:localhost",
            target=MessageTarget.resolve("!room:localhost", None, "$event"),
            requester_id="@user:localhost",
            sender_id="@user:localhost",
            body="hello",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name="code",
            source_kind="message",
            origin=cast("Any", None),
        )


def test_message_envelope_source_kind_must_match_origin() -> None:
    """Envelope source kind and origin policy should stay one value."""
    with pytest.raises(ValueError, match="source_kind"):
        MessageEnvelope(
            source_event_id="$event",
            room_id="!room:localhost",
            target=MessageTarget.resolve("!room:localhost", None, "$event"),
            requester_id="@user:localhost",
            sender_id="@user:localhost",
            body="hello",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name="code",
            source_kind="message",
            origin=message_origin(
                sender_id="@user:localhost",
                requester_id="@user:localhost",
                source_kind="hook",
            ),
        )


def test_hook_ingress_policy_skips_origin_plugin_on_first_message_received_hop() -> None:
    """First-hop message:received relays should rerun ingress once and skip the origin plugin."""
    policy = hook_ingress_policy(
        _envelope(
            source_kind="hook_dispatch",
            hook_source="origin-plugin:message:received",
            message_received_depth=1,
        ),
    )

    assert policy.rerun_message_received is True
    assert policy.allow_full_dispatch is True
    assert policy.skip_message_received_plugin_names == frozenset({"origin-plugin"})


def test_hook_ingress_policy_blocks_deeper_synthetic_hops() -> None:
    """Deep synthetic relays should stop before further dispatch."""
    policy = hook_ingress_policy(
        _envelope(
            source_kind="hook_dispatch",
            hook_source="origin-plugin:message:before_response",
            message_received_depth=2,
        ),
    )

    assert policy.rerun_message_received is False
    assert policy.allow_full_dispatch is False
    assert policy.skip_message_received_plugin_names == frozenset()


def test_automation_source_kinds_do_not_answer_interactive_prompts() -> None:
    """Synthetic automation should never be treated as a human interactive response."""
    assert is_automation_source_kind("hook")
    assert is_automation_source_kind("hook_dispatch")
    assert is_automation_source_kind("scheduled")
    assert not is_automation_source_kind("message")

    assert not _envelope(source_kind="hook").origin.may_answer_interactive_prompt
    assert not _envelope(source_kind="hook_dispatch").origin.may_answer_interactive_prompt
    assert not _envelope(source_kind="scheduled").origin.may_answer_interactive_prompt
    assert _envelope(source_kind="message").origin.may_answer_interactive_prompt
