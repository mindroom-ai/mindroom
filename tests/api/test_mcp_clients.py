"""Signed personal client management and provisioned-account integration."""

# Fixtures imported from the shared real-gateway harness are intentionally shadowed by pytest injection.
# ruff: noqa: F811

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from mindroom.api import config_lifecycle
from tests.api.test_mcp_gateway_api import (
    MCP_HEADERS,
    ORIGIN,
    _authorize,
    _code,
    _consent,
    _exchange,
    _list,
    _set_onboarding_limits,
    gateway_app,  # noqa: F401
    gateway_client,  # noqa: F401
    signed_headers,  # noqa: F401
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from fastapi import FastAPI

CLIENTS = "/api/connections/mcp/clients"
SCIM = "/mcp/scim/v2/Users"
SCIM_TOKEN = "synthetic-provisioning-key-for-local-tests-only"  # noqa: S105


def _connect(client: TestClient, headers: dict[str, str]) -> dict[str, str]:
    client_id, code = _code(client, headers)
    response = _exchange(client, client_id, code)
    assert response.status_code == 200, response.text
    return {**response.json(), "client_id": client_id}


@pytest.fixture
def managed_client(gateway_app: FastAPI) -> Iterator[TestClient]:
    """Enable account provisioning before the real runtime starts."""
    snapshot = config_lifecycle.require_api_state(gateway_app).snapshot
    snapshot.runtime_paths = replace(
        snapshot.runtime_paths,
        process_env={
            **snapshot.runtime_paths.process_env,
            "MINDROOM_MCP_SCIM_TOKEN": SCIM_TOKEN,
            # This lifecycle scenario exercises six consent attempts in one minute.
            "MINDROOM_MCP_GATEWAY_ONBOARDING_SOURCE_RATE_LIMIT": "20",
        },
    )
    with TestClient(gateway_app, base_url=ORIGIN, follow_redirects=False) as client:
        yield client


@pytest.fixture
def paginated_client(gateway_app: FastAPI) -> Iterator[TestClient]:
    """Raise onboarding allowances before runtime construction for bulk OAuth setup."""
    _set_onboarding_limits(gateway_app, aggregate=500, source=500)
    with TestClient(gateway_app, base_url=ORIGIN, follow_redirects=False) as client:
        yield client


def _provision(client: TestClient, user: str) -> str:
    response = client.post(
        SCIM,
        headers={"Authorization": f"Bearer {SCIM_TOKEN}"},
        json={
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": f"{user}@example.org",
            "active": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_client_management_is_owner_scoped_and_preserves_other_client_access(
    gateway_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
) -> None:
    """Selecting somebody else's grant never exposes or revokes it."""
    alice = signed_headers("alice")
    bob = signed_headers("bob")
    alice_tokens = _connect(gateway_client, alice)
    bob_tokens = _connect(gateway_client, bob)
    response = gateway_client.get(CLIENTS, headers=alice)
    assert response.status_code == 200, response.text
    assert "no-store" in response.headers["cache-control"]
    listing = response.json()
    assert listing["enabled"] is True
    assert len(listing["clients"]) == 1
    grant = listing["clients"][0]
    assert grant["last_used_at"] is None
    assert alice_tokens["access_token"] not in response.text
    assert alice_tokens["refresh_token"] not in response.text
    assert _list(gateway_client, alice_tokens["access_token"]).status_code == 200
    rejected = gateway_client.post(
        f"{CLIENTS}/{grant['id']}/revoke",
        headers={**bob, "Origin": ORIGIN},
        json={},
    )
    assert rejected.status_code == 404
    removed = gateway_client.post(
        f"{CLIENTS}/{grant['id']}/revoke",
        headers={**alice, "Origin": ORIGIN},
        json={},
    )
    assert removed.status_code == 200, removed.text
    assert gateway_client.get(CLIENTS, headers=alice).json()["clients"] == []
    assert _list(gateway_client, alice_tokens["access_token"]).status_code == 401
    assert _list(gateway_client, bob_tokens["access_token"]).status_code == 200


def test_client_head_request_never_revokes_connections(
    gateway_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
) -> None:
    """Starlette includes HEAD for GET routes; it must remain a read operation."""
    alice = signed_headers("alice")
    token = _connect(gateway_client, alice)["access_token"]
    response = gateway_client.request("HEAD", CLIENTS, headers={**alice, "Origin": ORIGIN}, json={})
    assert response.status_code == 200
    assert _list(gateway_client, token).status_code == 200


@pytest.mark.parametrize(
    ("extra_headers", "body", "query", "expected"),
    [
        ({}, {}, "", 403),
        ({"Origin": "https://attacker.example.org"}, {}, "", 403),
        ({"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"}, {}, "", 403),
        ({"Origin": ORIGIN}, {"requester_id": "@bob:example.org"}, "", 400),
        ({"Origin": ORIGIN}, {}, "?requester_id=bob", 400),
    ],
)
def test_client_mutations_reject_csrf_and_owner_overrides(
    gateway_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
    extra_headers: dict[str, str],
    body: dict[str, str],
    query: str,
    expected: int,
) -> None:
    """Browser mutation must have same-origin intent and no target selectors."""
    headers = signed_headers("alice")
    token = _connect(gateway_client, headers)["access_token"]
    response = gateway_client.post(
        CLIENTS + "/revoke-all" + query,
        headers={**headers, **extra_headers},
        json=body,
    )
    assert response.status_code == expected, response.text
    assert "no-store" in response.headers["cache-control"]
    assert _list(gateway_client, token).status_code == 200


@pytest.mark.parametrize("content_type", [None, "text/plain"])
def test_client_mutations_reject_missing_or_wrong_content_type_without_revoking(
    gateway_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
    content_type: str | None,
) -> None:
    """A same-origin raw JSON body still requires an explicit JSON media type."""
    auth_headers = signed_headers("alice")
    token = _connect(gateway_client, auth_headers)["access_token"]
    headers = {**auth_headers, "Origin": ORIGIN}
    if content_type is not None:
        headers["Content-Type"] = content_type
    response = gateway_client.post(CLIENTS + "/revoke-all", headers=headers, content=b"{}")
    assert response.status_code == 415, response.text
    assert "no-store" in response.headers["cache-control"]
    assert _list(gateway_client, token).status_code == 200


@pytest.mark.parametrize(
    "query",
    [
        "?cursor=",
        "?cursor=first&cursor=second",
        "?cursor=" + "x" * 257,
    ],
)
def test_client_listing_rejects_invalid_cursors(
    gateway_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
    query: str,
) -> None:
    """Empty, repeated, and oversized cursors never select a client page."""
    response = gateway_client.get(CLIENTS + query, headers=signed_headers("alice"))
    assert response.status_code == 400, response.text
    assert "no-store" in response.headers["cache-control"]


def test_client_listing_paginates_owned_grants_without_gaps_or_leaks(
    paginated_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
) -> None:
    """Keyset pages cross the public page bound without losing owner isolation."""
    alice = signed_headers("alice")
    bob = signed_headers("bob")
    alice_count = 103
    bob_count = 0
    for index in range(alice_count):
        _code(paginated_client, alice)
        if index % 25 == 0:
            _code(paginated_client, bob)
            bob_count += 1

    first_response = paginated_client.get(CLIENTS, headers=alice)
    assert first_response.status_code == 200, first_response.text
    first = first_response.json()
    first_ids = [client["id"] for client in first["clients"]]
    assert len(first_ids) == 100
    assert first_ids == sorted(first_ids)
    assert first["next_cursor"] == first_ids[-1]

    second_response = paginated_client.get(CLIENTS, headers=alice, params={"cursor": first["next_cursor"]})
    assert second_response.status_code == 200, second_response.text
    second = second_response.json()
    second_ids = [client["id"] for client in second["clients"]]
    assert len(second_ids) == alice_count - 100
    assert second_ids == sorted(second_ids)
    assert second["next_cursor"] is None

    alice_ids = first_ids + second_ids
    assert len(alice_ids) == alice_count
    assert len(set(alice_ids)) == alice_count
    assert first_ids[-1] < second_ids[0]
    bob_listing = paginated_client.get(CLIENTS, headers=bob)
    assert bob_listing.status_code == 200, bob_listing.text
    bob_ids = {client["id"] for client in bob_listing.json()["clients"]}
    assert len(bob_ids) == bob_count
    assert bob_ids.isdisjoint(alice_ids)


def test_disconnect_all_invalidates_issued_code_and_open_consent(
    gateway_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
) -> None:
    """An old open consent tab cannot restore a disconnected client."""
    alice = signed_headers("alice")
    connected = _connect(gateway_client, alice)
    client_id, code = _code(gateway_client, alice)
    _, url = _authorize(gateway_client)
    fields = _consent(gateway_client, url, alice)
    result = gateway_client.post(CLIENTS + "/revoke-all", headers={**alice, "Origin": ORIGIN}, json={})
    assert result.status_code == 200, result.text
    assert _list(gateway_client, connected["access_token"]).status_code == 401
    assert _exchange(gateway_client, client_id, code).status_code == 400
    assert (
        gateway_client.post(
            "/connections/mcp/authorize",
            headers={**alice, "Origin": ORIGIN},
            data={**fields, "decision": "allow"},
        ).status_code
        == 400
    )


def test_client_revocation_remains_available_after_agent_permission_removed(
    gateway_app: FastAPI,
    gateway_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
) -> None:
    """Losing tool permission must not strand the user's client controls."""
    alice = signed_headers("alice")
    _connect(gateway_client, alice)
    snapshot = config_lifecycle.require_api_state(gateway_app).snapshot
    assert snapshot.runtime_config is not None
    snapshot.runtime_config.agents["personal"].access.users = ["@bob:example.org"]
    listed = gateway_client.get(CLIENTS, headers=alice)
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["clients"]) == 1
    assert (
        gateway_client.post(
            CLIENTS + "/revoke-all",
            headers={**alice, "Origin": ORIGIN},
            json={},
        ).status_code
        == 200
    )


def test_last_used_tracks_successful_mcp_requests_without_counting_invalid_calls(
    gateway_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
) -> None:
    """Rejected tool arguments and portal polling cannot keep an idle client alive."""
    alice = signed_headers("alice")
    token = _connect(gateway_client, alice)["access_token"]
    assert gateway_client.get(CLIENTS, headers=alice).json()["clients"][0]["last_used_at"] is None
    rejected = gateway_client.post(
        "/mcp",
        headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "invented_tool"}},
    )
    assert rejected.json()["result"]["isError"] is True
    assert gateway_client.get(CLIENTS, headers=alice).json()["clients"][0]["last_used_at"] is None
    assert _list(gateway_client, token).status_code == 200
    used = gateway_client.get(CLIENTS, headers=alice).json()["clients"][0]
    assert used["last_used_at"] >= used["created_at"]


def test_managed_account_disable_invalidates_all_capabilities_and_reenable_requires_consent(
    managed_client: TestClient,
    signed_headers: Callable[[str], dict[str, str]],
) -> None:
    """Provisioning deactivation shuts off clients even with a still-valid SSO assertion."""
    alice = signed_headers("alice")
    _, url = _authorize(managed_client)
    assert managed_client.get(url, headers=alice).status_code == 403
    account_id = _provision(managed_client, "alice")
    _provision(managed_client, "bob")
    tokens = _connect(managed_client, alice)
    bob_tokens = _connect(managed_client, signed_headers("bob"))
    client_id, code = _code(managed_client, alice)
    _, pending_url = _authorize(managed_client)
    fields = _consent(managed_client, pending_url, alice)
    listing = managed_client.get(CLIENTS, headers=alice).json()["clients"]
    grant = next(item for item in listing if item["last_used_at"] is None)
    assert grant["expires_at"] - grant["created_at"] == 180 * 86400
    assert _list(managed_client, tokens["access_token"]).status_code == 200
    provisioning_headers = {"Authorization": f"Bearer {SCIM_TOKEN}"}
    response = managed_client.patch(
        f"{SCIM}/{account_id}",
        headers=provisioning_headers,
        json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "path": "active", "value": False}],
        },
    )
    assert response.status_code == 200, response.text
    assert _list(managed_client, tokens["access_token"]).status_code == 401
    assert _list(managed_client, bob_tokens["access_token"]).status_code == 200
    assert _exchange(managed_client, client_id, code).status_code == 400
    assert (
        managed_client.post(
            "/mcp/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": tokens["client_id"],
                "refresh_token": tokens["refresh_token"],
            },
        ).status_code
        == 400
    )
    assert (
        managed_client.post(
            "/connections/mcp/authorize",
            headers={**alice, "Origin": ORIGIN},
            data={**fields, "decision": "allow"},
        ).status_code
        == 403
    )
    assert (
        managed_client.patch(
            f"{SCIM}/{account_id}",
            headers=provisioning_headers,
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "replace", "path": "active", "value": True}],
            },
        ).status_code
        == 200
    )
    assert _list(managed_client, tokens["access_token"]).status_code == 401
    assert _list(managed_client, _connect(managed_client, alice)["access_token"]).status_code == 200


def test_gateway_disabled_does_not_break_client_section(
    gateway_app: FastAPI,
    signed_headers: Callable[[str], dict[str, str]],
) -> None:
    """The normal connections portal remains usable without an enabled MCP gateway."""
    snapshot = config_lifecycle.require_api_state(gateway_app).snapshot
    snapshot.runtime_paths = replace(
        snapshot.runtime_paths,
        process_env={**snapshot.runtime_paths.process_env, "MINDROOM_MCP_GATEWAY_ENABLED": "false"},
    )
    with TestClient(gateway_app, base_url=ORIGIN) as client:
        response = client.get(CLIENTS, headers=signed_headers("alice"))
        assert response.status_code == 200, response.text
        assert response.json() == {"enabled": False, "clients": []}
