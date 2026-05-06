"""Tests for ToolExecutionIdentity JSON payload helpers."""

from __future__ import annotations

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


def test_serialize_tool_execution_identity_preserves_payload_fields_by_caller() -> None:
    """Callers should choose whether their existing payload includes transport_agent_name."""
    identity = _identity()

    full_payload = serialize_tool_execution_identity(identity)
    compact_payload = serialize_tool_execution_identity(identity, include_transport_agent_name=False)

    assert full_payload == {
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
    assert compact_payload == {
        "channel": "matrix",
        "agent_name": "general",
        "requester_id": "@alice:example.org",
        "room_id": "!room:example.org",
        "thread_id": "$thread",
        "resolved_thread_id": "$thread",
        "session_id": "session-1",
        "tenant_id": "tenant-1",
        "account_id": "account-1",
    }


def test_parse_tool_execution_identity_payload_strict_uses_caller_error_prefix() -> None:
    """Strict subprocess parsing should keep caller-owned error messages."""
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


def test_parse_tool_execution_identity_payload_lenient_returns_none_for_invalid_payload() -> None:
    """Persisted state parsing should drop invalid identities without raising."""
    payload = serialize_tool_execution_identity(_identity(), include_transport_agent_name=False)
    payload["tenant_id"] = {"not": "a string"}

    assert parse_tool_execution_identity_payload(payload, strict=False) is None
    assert parse_tool_execution_identity_payload("not an object", strict=False) is None


def test_parse_tool_execution_identity_payload_round_trips_transport_agent_name() -> None:
    """Full payload parsing should preserve transport_agent_name when present."""
    parsed = parse_tool_execution_identity_payload(serialize_tool_execution_identity(_identity()))

    assert parsed == _identity()
