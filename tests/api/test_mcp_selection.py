"""Signed dashboard ownership and validation for the shared MCP client selection."""

# Imported harness fixtures are intentionally shadowed by pytest injection.
# ruff: noqa: F811

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import nio
import pytest

from mindroom import agents
from mindroom.api import config_lifecycle, mcp_selection
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig
from tests.access_schema_support import membership_index
from tests.api.test_mcp_clients import _connect
from tests.api.test_mcp_gateway_api import (
    MCP_HEADERS,
    ORIGIN,
    _native_dispatch_builder,
    gateway_app,  # noqa: F401
    gateway_client,  # noqa: F401
    signed_headers,  # noqa: F401
)
from tests.conftest import bind_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient

SELECTION = "/api/connections/mcp/selection"


@pytest.mark.parametrize("current_room_only", [False, True])
def test_room_membership_grants_require_room_independent_access(
    gateway_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
    current_room_only: bool,
) -> None:
    """Named room grants allow MCP tools; conversation-only grants do not escape the room."""
    snapshot = config_lifecycle.require_api_state(gateway_client.app).snapshot
    config = bind_runtime_paths(snapshot.runtime_config, snapshot.runtime_paths)
    snapshot.runtime_config = config
    config.agents["shared"] = AgentConfig(
        display_name="Shared",
        role="Shared tools",
        tools=["calculator"],
        rooms=["project"],
        access=ResponderAccessConfig(
            current_room_members=current_room_only,
            members_of_rooms=[] if current_room_only else ["project"],
        ),
    )
    memberships = asyncio.run(membership_index(config, {"project": {"@alice:example.org"}}))
    config_lifecycle.app_state(gateway_client.app).agent_reply_memberships = memberships
    alice = signed_headers("alice")
    token = _connect(gateway_client, alice)["access_token"]
    response = gateway_client.post(SELECTION, headers={**alice, "Origin": ORIGIN}, json={"agents": {"shared": None}})
    if current_room_only:
        assert response.status_code == 404
        return
    assert response.status_code == 200, response.text
    result = _call(
        gateway_client,
        token,
        "invoke_tool",
        {
            "agent": "shared",
            "toolkit": "calculator",
            "function": "add",
            "arguments": {"a": 1, "b": 2},
        },
    )
    assert not result["isError"]
    assert json.loads(result["structuredContent"]["result"])["result"] == 3
    memberships.mark_room_unready(config, snapshot.runtime_paths, "!project:example.com", reason="membership_unknown")
    assert gateway_client.get(SELECTION, headers=alice).json()["agents"] == {}
    assert _call(gateway_client, token, "search_tools", {})["structuredContent"] == {"results": []}
    assert _call(
        gateway_client,
        token,
        "get_tool",
        {
            "agent": "shared",
            "toolkit": "calculator",
            "function": "add",
        },
    )["isError"]


@pytest.mark.parametrize("async_body", [False, True], ids=["sync", "async"])
def test_membership_revocation_during_preparation_stops_tool_body(
    gateway_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    async_body: bool,
) -> None:
    """A live room departure revokes a prepared call without a config or selection change."""
    snapshot = config_lifecycle.require_api_state(gateway_client.app).snapshot
    config = bind_runtime_paths(snapshot.runtime_config, snapshot.runtime_paths)
    snapshot.runtime_config = config
    config.agents["personal"].access = ResponderAccessConfig(members_of_rooms=["project"])
    memberships = asyncio.run(membership_index(config, {"project": {"@alice:example.org"}}))
    config_lifecycle.app_state(gateway_client.app).agent_reply_memberships = memberships
    token = _connect(gateway_client, signed_headers("alice"))["access_token"]
    paused, release = threading.Event(), threading.Event()
    events: list[str] = []
    monkeypatch.setattr(
        agents,
        "build_agent_toolkit",
        _native_dispatch_builder("hook", async_body, paused, release, events),
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            _call,
            gateway_client,
            token,
            "invoke_tool",
            {
                "agent": "personal",
                "toolkit": "calculator",
                "function": "async_account" if async_body else "account",
                "arguments": {},
            },
        )
        try:
            assert paused.wait(10), "Tool hook never paused"
            event = nio.RoomMemberEvent.from_dict(
                {
                    "type": "m.room.member",
                    "state_key": "@alice:example.org",
                    "sender": "@alice:example.org",
                    "event_id": "$departure",
                    "origin_server_ts": 1,
                    "content": {"membership": "leave"},
                },
            )
            assert isinstance(event, nio.RoomMemberEvent)
            memberships.apply_member_event(
                config,
                snapshot.runtime_paths,
                "!project:example.com",
                event,
                control_user_id="@router:example.org",
            )
        finally:
            release.set()
        assert pending.result(timeout=10)["isError"]
    assert "body" not in events
    assert "close" in events


@pytest.mark.parametrize("all_tools", [False, True], ids=["custom", "all-tools"])
def test_removed_tools_cannot_block_access_withdrawal(
    gateway_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
    all_tools: bool,
) -> None:
    """Old browser state and fresh reads can both withdraw access after a tool is removed."""
    config = config_lifecycle.require_api_state(gateway_client.app).snapshot.runtime_config
    config.agents["personal"].tools = ["calculator", "duckduckgo", "shell"]
    config.agents["shared"] = AgentConfig(
        display_name="Shared",
        role="Shared tools",
        tools=["calculator"],
        access=ResponderAccessConfig(users=["@alice:example.org"]),
    )
    headers = {**signed_headers("alice"), "Origin": ORIGIN}
    browser_tools = ["calculator", "duckduckgo"]
    original = {"personal": None if all_tools else browser_tools, "shared": None}
    assert gateway_client.post(SELECTION, headers=headers, json={"agents": original}).status_code == 200
    config.agents["personal"].tools = ["calculator", "shell"]
    assert gateway_client.get(SELECTION, headers=headers).json()["agents"] == {
        "personal": None if all_tools else ["calculator"],
        "shared": None,
    }
    response = gateway_client.post(
        SELECTION,
        headers=headers,
        json={"agents": {"personal": browser_tools}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["agents"] == {"personal": ["calculator"]}
    config.agents["personal"].tools = []
    assert gateway_client.get(SELECTION, headers=headers).json()["agents"] == {}
    response = gateway_client.post(SELECTION, headers=headers, json={"agents": {"personal": ["calculator"]}})
    assert response.status_code == 200, response.text
    assert response.json()["agents"] == {}


def test_withdrawal_does_not_require_plugin_metadata(
    gateway_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken plugin cannot prevent withdrawing all tool access."""
    alice = signed_headers("alice")
    assert gateway_client.get(SELECTION, headers=alice).status_code == 200

    def unavailable(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Withdrawing access must not load plugins")

    monkeypatch.setattr(mcp_selection, "resolved_tool_metadata_for_runtime", unavailable)
    response = gateway_client.post(SELECTION, headers={**alice, "Origin": ORIGIN}, json={"agents": {}})
    assert response.status_code == 200, response.text
    assert response.json()["agents"] == {}


def test_tool_selection_filters_discovery_and_blocks_unselected_dispatch(
    gateway_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
) -> None:
    """Hidden tools cannot be discovered, inspected, or invoked by any connected client."""
    config = config_lifecycle.require_api_state(gateway_client.app).snapshot.runtime_config
    config.agents["personal"].tools = ["calculator", "duckduckgo"]
    alice = signed_headers("alice")
    token = _connect(gateway_client, alice)["access_token"]
    response = gateway_client.post(
        SELECTION,
        headers={**alice, "Origin": ORIGIN},
        json={"agents": {"personal": ["duckduckgo"]}},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"enabled": True, "agents": {"personal": ["duckduckgo"]}}
    for arguments in ({"limit": 1}, {"agent": "personal", "limit": 1}):
        result = _call(gateway_client, token, "search_tools", arguments)["structuredContent"]
        assert [(item["agent"], item["toolkit"]) for item in result["results"]] == [("personal", "duckduckgo")]
    for operation, extra in (
        ("search_tools", {}),
        ("get_tool", {"function": "add"}),
        ("invoke_tool", {"function": "add", "arguments": {"a": 1, "b": 2}}),
    ):
        result = _call(gateway_client, token, operation, {"agent": "personal", "toolkit": "calculator", **extra})
        assert result["isError"]


def test_selection_defaults_and_empty_are_user_scoped(
    gateway_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
) -> None:
    """The first personal default persists, while other verified users retain their own choice."""
    alice = signed_headers("alice")
    response = gateway_client.get(SELECTION, headers=alice)
    assert response.status_code == 200, response.text
    assert response.json() == {"enabled": True, "agents": {"personal": None}}
    assert "no-store" in response.headers["cache-control"]
    response = gateway_client.post(SELECTION, headers={**alice, "Origin": ORIGIN}, json={"agents": {}})
    assert response.status_code == 200, response.text
    assert response.json()["agents"] == {}
    assert gateway_client.get(SELECTION, headers=alice).json()["agents"] == {}
    assert gateway_client.get(SELECTION, headers=signed_headers("bob")).json()["agents"] == {"personal": None}


def test_shared_only_user_can_select_assigned_shared_agent(
    gateway_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
) -> None:
    """Shared selection follows agent access independently of personal-agent access."""
    config = config_lifecycle.require_api_state(gateway_client.app).snapshot.runtime_config
    config.agents["personal"].access.users = ["@bob:example.org"]
    config.agents["shared"] = AgentConfig(
        display_name="Shared",
        role="Shared tools",
        tools=["calculator"],
        access=ResponderAccessConfig(users=["@alice:example.org"]),
    )
    alice = signed_headers("alice")
    assert gateway_client.get(SELECTION, headers=alice).json()["agents"] == {}
    response = gateway_client.post(SELECTION, headers={**alice, "Origin": ORIGIN}, json={"agents": {"shared": None}})
    assert response.status_code == 200, response.text
    assert response.json()["agents"] == {"shared": None}
    config.agents["shared"].access.users = []
    assert gateway_client.get(SELECTION, headers=alice).json()["agents"] == {}


def test_credential_manager_without_agent_access_cannot_select_tools(
    gateway_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
) -> None:
    """Account management alone cannot authorize a direct MCP call."""
    config = config_lifecycle.require_api_state(gateway_client.app).snapshot.runtime_config
    config.agents["shared"] = AgentConfig(
        display_name="Shared",
        role="Shared tools",
        tools=["calculator"],
        credential_managers=["@alice:example.org"],
    )
    alice = signed_headers("alice")
    token = _connect(gateway_client, alice)["access_token"]
    response = gateway_client.post(SELECTION, headers={**alice, "Origin": ORIGIN}, json={"agents": {"shared": None}})
    assert response.status_code == 404
    assert _call(
        gateway_client,
        token,
        "invoke_tool",
        {
            "agent": "shared",
            "toolkit": "calculator",
            "function": "add",
            "arguments": {"a": 1, "b": 2},
        },
    )["isError"]


@pytest.mark.parametrize(
    "body",
    [
        {"agents": {"missing": None}},
        {"agents": {"personal": ["calculator", "calculator"]}},
        {"agents": "personal"},
        {"agents": {"personal": ["unknown"]}},
        {"agents": {"personal": ["matrix_message"]}},
        {"agents": {"personal": "calculator"}},
        {"agents": {"personal": [False]}},
        {"agents": {1: None}},
        {"agents": {}, "user": "bob"},
        [],
    ],
)
def test_selection_rejects_invalid_or_unauthorized_names(
    gateway_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
    body: object,
) -> None:
    """Browser input can narrow current authority but cannot invent agent access or owner IDs."""
    assert (
        gateway_client.post(
            SELECTION,
            headers={**signed_headers("alice"), "Origin": ORIGIN},
            json={"agents": {}},
        ).status_code
        == 200
    )
    response = gateway_client.post(SELECTION, headers={**signed_headers("alice"), "Origin": ORIGIN}, json=body)
    assert response.status_code in {400, 404}


def test_selection_requires_signed_identity_and_same_origin(
    gateway_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
) -> None:
    """Gateway settings retain the existing signed dashboard and browser CSRF boundaries."""
    assert gateway_client.get(SELECTION).status_code == 401
    response = gateway_client.post(SELECTION, headers=signed_headers("alice"), json={"agents": {}})
    assert response.status_code == 403
    response = gateway_client.get(SELECTION + "?agent=personal", headers=signed_headers("alice"))
    assert response.status_code == 400


def _call(client: TestClient, token: str, name: str, arguments: dict[str, object]) -> dict:
    response = client.post(
        "/mcp",
        headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


@pytest.mark.parametrize("shared_agent", ["shared", "s" * 129])
def test_all_clients_follow_selection_and_agent_qualified_tools(
    gateway_client: TestClient,
    shared_agent: str,
    signed_headers: Callable[[str], dict[str, str]],
) -> None:
    """Identical toolkit names on two agents remain distinct; both clients follow one saved choice."""
    config = config_lifecycle.require_api_state(gateway_client.app).snapshot.runtime_config
    config.agents[shared_agent] = AgentConfig(
        display_name="Shared",
        role="Shared tools",
        tools=["calculator"],
        access=ResponderAccessConfig(users=["@alice:example.org"]),
    )
    alice = signed_headers("alice")
    tokens = [_connect(gateway_client, alice)["access_token"] for _ in range(2)]
    result = _call(gateway_client, tokens[0], "search_tools", {})["structuredContent"]
    assert {item["agent"] for item in result["results"]} == {"personal"}
    assert (
        gateway_client.post(
            SELECTION,
            headers={**alice, "Origin": ORIGIN},
            json={"agents": {"personal": None, shared_agent: None}},
        ).status_code
        == 200
    )
    for token in tokens:
        result = _call(gateway_client, token, "search_tools", {})["structuredContent"]
        assert {(item["agent"], item["toolkit"]) for item in result["results"]} == {
            ("personal", "calculator"),
            (shared_agent, "calculator"),
        }
        limited = _call(gateway_client, token, "search_tools", {"limit": 1})["structuredContent"]
        assert len(limited["results"]) == 1
        functions = _call(gateway_client, token, "search_tools", {"agent": shared_agent, "toolkit": "calculator"})[
            "structuredContent"
        ]
        assert functions["results"]
        assert all(item["agent"] == shared_agent for item in functions["results"])
        function = functions["results"][0]["function"]
        schema = _call(
            gateway_client,
            token,
            "get_tool",
            {"agent": shared_agent, "toolkit": "calculator", "function": function},
        )["structuredContent"]
        assert schema["agent"] == shared_agent
    assert gateway_client.post(SELECTION, headers={**alice, "Origin": ORIGIN}, json={"agents": {}}).status_code == 200
    for token in tokens:
        assert _call(gateway_client, token, "search_tools", {})["structuredContent"] == {"results": []}
        rejected = _call(
            gateway_client,
            token,
            "get_tool",
            {"agent": "personal", "toolkit": "calculator", "function": function},
        )
        assert rejected["isError"]


@pytest.mark.parametrize("keep_agent", [False, True], ids=["agent", "toolkit"])
@pytest.mark.parametrize("phase", ["build", "connect", "hook"])
@pytest.mark.parametrize("async_body", [False, True], ids=["sync", "async"])
def test_deselection_during_preparation_stops_provider_body(
    gateway_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    async_body: bool,
    keep_agent: bool,
) -> None:
    """A dashboard change wins over a call waiting in preparation, for synchronous and async tools."""
    config = config_lifecycle.require_api_state(gateway_client.app).snapshot.runtime_config
    config.agents["personal"].tools = ["calculator", "duckduckgo"]
    alice = signed_headers("alice")
    token = _connect(gateway_client, alice)["access_token"]
    paused, release = threading.Event(), threading.Event()
    events: list[str] = []
    monkeypatch.setattr(
        agents,
        "build_agent_toolkit",
        _native_dispatch_builder(phase, async_body, paused, release, events),
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            _call,
            gateway_client,
            token,
            "invoke_tool",
            {
                "agent": "personal",
                "toolkit": "calculator",
                "function": "async_account" if async_body else "account",
                "arguments": {},
            },
        )
        try:
            assert paused.wait(10), "Provider preparation never paused"
            response = gateway_client.post(
                SELECTION,
                headers={**alice, "Origin": ORIGIN},
                json={"agents": {"personal": ["duckduckgo"]} if keep_agent else {}},
            )
            assert response.status_code == 200, response.text
        finally:
            release.set()
        assert pending.result(timeout=10)["isError"]
    assert "body" not in events
    assert "close" in events
