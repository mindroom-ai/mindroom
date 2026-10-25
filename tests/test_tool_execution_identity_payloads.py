"""Tests for ToolExecutionIdentity JSON payload helpers."""

from __future__ import annotations

from dataclasses import replace

import pytest

from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    parse_tool_execution_identity_payload,
    serialize_tool_execution_identity,
)


def _identity() -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
        tenant_id="tenant-1",
        account_id="account-1",
        transport_agent_name="mindroom_general",
    )


def test_serialize_tool_execution_identity_keeps_every_field() -> None:
    """The payload carries every identity field, transport_agent_name included."""
    assert serialize_tool_execution_identity(_identity()) == {
        "channel": "matrix",
        "agent_name": "general",
        "requester_id": "@alice:example.org",
        "room_id": "!room:example.org",
        "thread_id": "$thread",
        "resolved_thread_id": "$thread",
        "session_id": "session-1",
        "tenant_id": "tenant-1",
        "account_id": "account-1",
        "transport_agent_name": "mindroom_general",
    }


def test_parse_tool_execution_identity_payload_uses_caller_error_prefix() -> None:
    """Parsing raises under the caller's error prefix."""
    payload = serialize_tool_execution_identity(_identity())
    payload["requester_id"] = 123

    with pytest.raises(
        TypeError,
        match=r"Knowledge refresh execution_identity\.requester_id must be a string when present",
    ):
        parse_tool_execution_identity_payload(
            payload,
            error_prefix="Knowledge refresh execution_identity",
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("not an object", r"^Tool execution_identity must be an object$"),
        ({"channel": "email", "agent_name": "general"}, r"^Tool execution_identity\.channel must be matrix"),
        ({"channel": "matrix", "agent_name": " "}, r"^Tool execution_identity\.agent_name must be a non-empty string$"),
        (
            {"channel": "matrix", "agent_name": "general", "tenant_id": {"not": "a string"}},
            r"^Tool execution_identity\.tenant_id must be a string when present$",
        ),
    ],
)
def test_parse_tool_execution_identity_payload_rejects_each_unreadable_payload(payload: object, message: str) -> None:
    """Every unreadable payload raises, naming the first field it cannot read."""
    with pytest.raises(TypeError, match=message):
        parse_tool_execution_identity_payload(payload)


def test_parse_tool_execution_identity_payload_round_trips_transport_agent_name() -> None:
    """Full payload parsing should preserve transport_agent_name when present."""
    parsed = parse_tool_execution_identity_payload(serialize_tool_execution_identity(_identity()))

    assert parsed == _identity()


def test_mcp_execution_channel_survives_worker_serialization() -> None:
    """Gateway tool execution must retain its transport across worker boundaries."""
    identity = replace(_identity(), channel="mcp")
    payload = serialize_tool_execution_identity(identity)
    parsed = parse_tool_execution_identity_payload(payload)
    assert parsed == identity
    assert parsed.channel == "mcp"
    assert parsed.requester_id == "@alice:example.org"
