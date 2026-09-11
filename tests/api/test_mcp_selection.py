"""Signed dashboard ownership and validation for the shared MCP client selection."""

# Imported harness fixtures are intentionally shadowed by pytest injection.
# ruff: noqa: F811

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest

from mindroom import agents
from mindroom.api import config_lifecycle
from mindroom.config.agent import AgentConfig
from tests.api.test_mcp_clients import _connect
from tests.api.test_mcp_gateway_api import (
    MCP_HEADERS,
    ORIGIN,
    _native_dispatch_builder,
    gateway_app,  # noqa: F401
    gateway_client,  # noqa: F401
    signed_headers,  # noqa: F401
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient

SELECTION = "/api/connections/mcp/selection"


def test_selection_defaults_and_empty_are_user_scoped(
    gateway_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
) -> None:
    """The first personal default persists, while other verified users retain their own choice."""
    alice = signed_headers("alice")
    response = gateway_client.get(SELECTION, headers=alice)
    assert response.status_code == 200, response.text
    assert response.json() == {"enabled": True, "selected_agents": ["personal"]}
    assert "no-store" in response.headers["cache-control"]
    response = gateway_client.post(SELECTION, headers={**alice, "Origin": ORIGIN}, json={"agents": []})
    assert response.status_code == 200, response.text
    assert response.json()["selected_agents"] == []
    assert gateway_client.get(SELECTION, headers=alice).json()["selected_agents"] == []
    assert gateway_client.get(SELECTION, headers=signed_headers("bob")).json()["selected_agents"] == ["personal"]


def test_shared_only_manager_can_select_assigned_shared_agent(
    gateway_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
) -> None:
    """Shared selection needs credential management, independently of personal-agent access."""
    config = config_lifecycle.require_api_state(gateway_client.app).snapshot.runtime_config
    config.agents["personal"].access.users = ["@bob:example.org"]
    config.agents["shared"] = AgentConfig(
        display_name="Shared",
        role="Shared tools",
        tools=["calculator"],
        credential_managers=["@alice:example.org"],
    )
    alice = signed_headers("alice")
    assert gateway_client.get(SELECTION, headers=alice).json()["selected_agents"] == []
    response = gateway_client.post(SELECTION, headers={**alice, "Origin": ORIGIN}, json={"agents": ["shared"]})
    assert response.status_code == 200, response.text
    assert response.json()["selected_agents"] == ["shared"]
    config.agents["shared"].credential_managers = []
    assert gateway_client.get(SELECTION, headers=alice).json()["selected_agents"] == []


@pytest.mark.parametrize(
    "body",
    [
        {"agents": ["missing"]},
        {"agents": ["personal", "personal"]},
        {"agents": "personal"},
        {"agents": [1]},
        {"agents": [], "user": "bob"},
        [],
    ],
)
def test_selection_rejects_invalid_or_unauthorized_names(
    gateway_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
    body: object,
) -> None:
    """Browser input can narrow current authority but cannot invent agent access or owner IDs."""
    response = gateway_client.post(SELECTION, headers={**signed_headers("alice"), "Origin": ORIGIN}, json=body)
    assert response.status_code in {400, 404}


def test_selection_requires_signed_identity_and_same_origin(
    gateway_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
) -> None:
    """Gateway settings retain the existing signed dashboard and browser CSRF boundaries."""
    assert gateway_client.get(SELECTION).status_code == 401
    response = gateway_client.post(SELECTION, headers=signed_headers("alice"), json={"agents": []})
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
        credential_managers=["@alice:example.org"],
    )
    alice = signed_headers("alice")
    tokens = [_connect(gateway_client, alice)["access_token"] for _ in range(2)]
    result = _call(gateway_client, tokens[0], "search_tools", {})["structuredContent"]
    assert {item["agent"] for item in result["results"]} == {"personal"}
    assert (
        gateway_client.post(
            SELECTION,
            headers={**alice, "Origin": ORIGIN},
            json={"agents": ["personal", shared_agent]},
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
    assert gateway_client.post(SELECTION, headers={**alice, "Origin": ORIGIN}, json={"agents": []}).status_code == 200
    for token in tokens:
        assert _call(gateway_client, token, "search_tools", {})["structuredContent"] == {"results": []}
        rejected = _call(
            gateway_client,
            token,
            "get_tool",
            {"agent": "personal", "toolkit": "calculator", "function": function},
        )
        assert rejected["isError"]


@pytest.mark.parametrize("phase", ["build", "connect", "hook"])
@pytest.mark.parametrize("async_body", [False, True], ids=["sync", "async"])
def test_deselection_during_preparation_stops_provider_body(
    gateway_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    async_body: bool,
) -> None:
    """A dashboard change wins over a call waiting in preparation, for synchronous and async tools."""
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
            response = gateway_client.post(SELECTION, headers={**alice, "Origin": ORIGIN}, json={"agents": []})
            assert response.status_code == 200, response.text
        finally:
            release.set()
        assert pending.result(timeout=10)["isError"]
    assert "body" not in events
    assert "close" in events
