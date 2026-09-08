"""SCIM uses dedicated authentication and bounded, atomic User operations."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mindroom.api.mcp_scim import scim_routes
from mindroom.mcp_gateway.accounts import GatewayAccounts
from mindroom.mcp_gateway.store import GatewayOAuthStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from httpx import Response

BASE = "/mcp/scim/v2"
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
TOKEN = "provisioning-test-credential-" + "x" * 32


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """Serve real SCIM handlers against a durable SQLite account store."""
    store = GatewayOAuthStore(
        tmp_path,
        onboarding_max_bytes=1000000,
        max_bytes=1000000,
        user_max_bytes=1000000,
        clock=lambda: 2_000_000_000.0,
    )
    runtime = SimpleNamespace(
        provider=SimpleNamespace(accounts_required=True),
        accounts=GatewayAccounts(store),
        origin="https://example.org",
        scim_token=TOKEN,
    )
    with TestClient(Starlette(routes=scim_routes(lambda _: runtime))) as http:
        http.app.state.store = store
        http.headers["Authorization"] = "Bearer " + TOKEN
        yield http


def _create(client: TestClient, **fields: object) -> Response:
    return client.post(
        BASE + "/Users",
        json={"schemas": [USER_SCHEMA], "userName": "alice@example.org", "active": True, **fields},
    )


def test_user_roundtrip_filter_pagination_replace_delete(client: TestClient) -> None:
    """A connector can create, find, replace and delete its bounded User profile."""
    response = _create(
        client,
        password="never-reflect",  # noqa: S106
        displayName="Alice",
        emails=[{"value": "alice@example.org", "primary": True}],
    )
    assert response.status_code == 201
    user = response.json()
    assert user["schemas"] == [USER_SCHEMA]
    assert "password" not in user
    assert "never-reflect" not in response.text
    assert response.headers["location"].endswith("/Users/" + user["id"])
    path = BASE + "/Users/" + user["id"]
    assert client.get(path).json()["displayName"] == "Alice"
    for attribute in ("userName", "USERNAME", "emails.value", "id"):
        value = user["id"] if attribute == "id" else "alice@example.org"
        listing = client.get(BASE + "/Users", params={"filter": f'{attribute} eq "{value}"'}).json()
        assert listing["totalResults"] == 1
        assert listing["Resources"][0]["id"] == user["id"]
    assert client.get(BASE + "/Users", params={"filter": 'userName eq "Alice@example.org"'}).json()["totalResults"] == 0
    assert client.get(BASE + "/Users", params={"count": 0}).json()["Resources"] == []
    assert client.get(BASE + "/Users", params={"startIndex": 2}).json()["Resources"] == []
    assert (
        client.put(path, json={"schemas": [USER_SCHEMA], "userName": "new@example.org", "active": False}).status_code
        == 200
    )
    assert "displayName" not in client.get(path).json()
    assert client.delete(path).status_code == 204
    assert client.get(path).status_code == 404


def test_patch_case_insensitive_attributes_atomic_failure(client: TestClient) -> None:
    """SCIM attribute casing works and unsupported operations roll back the whole PATCH."""
    path = BASE + "/Users/" + _create(client).json()["id"]
    patch = {"schemas": [PATCH_SCHEMA], "Operations": [{"op": "Replace", "path": "Active", "value": False}]}
    assert client.patch(path, json=patch).json()["active"] is False
    patch["Operations"] = [{"op": "replace", "value": {"ACTIVE": True, "displayName": "Updated"}}]
    assert client.patch(path, json=patch).json()["displayName"] == "Updated"
    patch["Operations"] = [
        {"op": "replace", "path": "active", "value": False},
        {"op": "replace", "path": "groups", "value": []},
    ]
    assert client.patch(path, json=patch).status_code == 400
    assert client.get(path).json()["active"] is True
    patch["Operations"] = [{"op": "remove", "path": "displayName"}]
    assert "displayName" not in client.patch(path, json=patch).json()


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer oauth-access-token"},
        {"Cookie": "session=browser"},
        {"X-Auth-Request-Email": "alice@example.org"},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Bearer \u00e9"},
    ],
)
def test_dedicated_bearer_only(client: TestClient, headers: dict[str, str]) -> None:
    """Browser identity and unrelated tokens cannot enumerate provisioned users."""
    client.headers.pop("Authorization")
    # Send non-ASCII header bytes explicitly, as valid HTTP opaque bytes.
    raw_headers = [(key.encode(), value.encode("utf-8")) for key, value in headers.items()]
    response = client.get(BASE + "/Users", headers=raw_headers)
    assert response.status_code == 401
    assert response.json()["schemas"] == [ERROR_SCHEMA]
    assert response.headers["content-type"].startswith("application/scim+json")
    assert "access-control-allow-origin" not in response.headers
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"userName": "alice@example.org", "active": "false"},
        {"userName": "alice@example.org", "active": 0},
        {"userName": "x" * 321, "active": True},
        {"userName": "alice@example.org", "USERNAME": "other@example.org", "active": True},
    ],
)
def test_invalid_user_fields_are_safe_errors(client: TestClient, payload: object) -> None:
    """Invalid profiles return bounded SCIM errors without reflecting input."""
    response = client.post(BASE + "/Users", json=payload)
    assert response.status_code == 400
    assert response.json()["schemas"] == [ERROR_SCHEMA]


@pytest.mark.parametrize(
    "query",
    [
        {"filter": "active eq true"},
        {"filter": 'userName co "alice"'},
        {"filter": 'userName eq "alice" or active eq true'},
        {"count": "bad"},
        {"count": "-1"},
        {"sortBy": "userName"},
        {"filter": "x" * 3000},
    ],
)
def test_unsupported_queries_rejected(client: TestClient, query: dict[str, str]) -> None:
    """Unsupported filters and sorting never silently broaden directory results."""
    assert client.get(BASE + "/Users", params=query).status_code == 400


def test_limits_errors_discovery_and_groups(client: TestClient) -> None:
    """Discovery reports actual features and rejects unsupported provisioning surfaces."""
    assert (
        client.post(BASE + "/Users", content="{" * 70000, headers={"Content-Type": "application/scim+json"}).status_code
        == 413
    )
    assert client.post(BASE + "/Users", content="{", headers={"Content-Type": "application/json"}).status_code == 400
    assert client.post(BASE + "/Users", content="{}", headers={"Content-Type": "text/plain"}).status_code == 415
    assert _create(client).status_code == 201
    assert _create(client).status_code == 409
    discovery = client.get(BASE + "/ServiceProviderConfig").json()
    assert discovery["patch"]["supported"] is True
    assert discovery["bulk"]["supported"] is False
    assert discovery["changePassword"]["supported"] is False
    assert client.get(BASE + "/ResourceTypes").json()["Resources"][0]["name"] == "User"
    assert client.get(BASE + "/Schemas").json()["Resources"][0]["id"] == USER_SCHEMA
    assert client.get(BASE + "/Groups").status_code == 501
    assert client.post(BASE + "/Groups", json={}).status_code == 501
    assert client.options(BASE + "/Users", headers={"Origin": "https://example.org"}).status_code == 405


@pytest.mark.parametrize("query", [{"startIndex": str(2**100)}, {"filter": 'userName eq "\\ud800"'}])
def test_extreme_query_inputs_are_rejected(client: TestClient, query: dict[str, str]) -> None:
    """Malformed scalar values never reach SQLite bindings."""
    assert client.get(BASE + "/Users", params=query).status_code == 400


def test_unpaired_surrogate_profile_is_rejected(client: TestClient) -> None:
    """JSON escapes cannot inject invalid Unicode into stored identities."""
    payload = {"schemas": [USER_SCHEMA], "userName": "\ud800@example.org", "active": True}
    assert (
        client.post(
            BASE + "/Users",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        ).status_code
        == 400
    )


def test_name_subattribute_patch_preserves_other_name_fields(client: TestClient) -> None:
    """Connector subattribute updates preserve sibling profile values."""
    path = BASE + "/Users/" + _create(client, name={"givenName": "Alice", "familyName": "Example"}).json()["id"]
    response = client.patch(
        path,
        json={
            "schemas": [PATCH_SCHEMA],
            "Operations": [
                {"op": "replace", "path": "name.givenName", "value": "Alicia"},
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["name"] == {"givenName": "Alicia", "familyName": "Example"}


def test_transient_deactivation_in_atomic_patch_still_revokes(client: TestClient) -> None:
    """A later reactivation in one PATCH cannot preserve pre-disable grants."""
    account_id = _create(client).json()["id"]
    with sqlite3.connect(client.app.state.store.path) as connection:
        connection.execute(
            "INSERT INTO pending (state_hash, payload, expires_at, account_id) VALUES (?, '{}', ?, ?)",
            ("bound-consent", 2_100_000_000, account_id),
        )
    response = client.patch(
        BASE + "/Users/" + account_id,
        json={
            "schemas": [PATCH_SCHEMA],
            "Operations": [
                {"op": "replace", "path": "active", "value": False},
                {"op": "replace", "path": "active", "value": True},
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["active"] is True
    with sqlite3.connect(client.app.state.store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0


@pytest.mark.parametrize(
    "body",
    [
        '{"schemas":["' + USER_SCHEMA + '"],"userName":"alice@example.org","active":true,"unknown":NaN}',
        '{"schemas":["' + USER_SCHEMA + '"],"userName":"alice@example.org","active":true,"active":false}',
    ],
)
def test_nonstandard_json_and_duplicate_keys_rejected(client: TestClient, body: str) -> None:
    """Parsing does not accept ambiguous keys or non-JSON numeric values."""
    response = client.post(BASE + "/Users", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 400


def test_duplicate_queries_and_unsupported_resource_queries(client: TestClient) -> None:
    """Query parameters never silently broaden a requested result."""
    assert client.get(BASE + "/Users?count=1&count=2").status_code == 400
    account_id = _create(client).json()["id"]
    assert client.get(BASE + "/Users/" + account_id + "?attributes=password").status_code == 400


def test_multiple_primary_emails_rejected(client: TestClient) -> None:
    """SCIM email lists cannot claim two primary values."""
    response = _create(
        client,
        emails=[{"value": "alice@example.org", "primary": True}, {"value": "other@example.org", "primary": True}],
    )
    assert response.status_code == 400


def test_failed_patch_restores_revoked_pending_consent(client: TestClient) -> None:
    """An invalid later PATCH step rolls back earlier revocation and profile writes."""
    account_id = _create(client).json()["id"]
    with sqlite3.connect(client.app.state.store.path) as connection:
        connection.execute(
            "INSERT INTO pending (state_hash, payload, expires_at, account_id) VALUES (?, '{}', ?, ?)",
            ("bound-consent", 2_100_000_000, account_id),
        )
    response = client.patch(
        BASE + "/Users/" + account_id,
        json={
            "schemas": [PATCH_SCHEMA],
            "Operations": [
                {"op": "replace", "path": "active", "value": False},
                {"op": "replace", "path": "password", "value": "never-store"},
            ],
        },
    )
    assert response.status_code == 400
    with sqlite3.connect(client.app.state.store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 1
    assert client.get(BASE + "/Users/" + account_id).json()["active"] is True


def test_query_is_bounded_before_parameter_parsing(client: TestClient) -> None:
    """Empty separators still count toward the raw request query limit."""
    assert client.get(BASE + "/Users?" + "&" * 5000).status_code == 400


@pytest.mark.parametrize("existing", [False, True])
def test_email_add_switches_primary_and_retry_is_noop(client: TestClient, existing: bool) -> None:
    """Adding a primary value demotes the old primary, and retries retain one copy."""
    emails = [{"value": "first@example.org", "type": "work", "primary": True}]
    if existing:
        emails.append({"value": "second@example.org", "type": "home", "primary": False})
    path = BASE + "/Users/" + _create(client, emails=emails).json()["id"]
    patch = {
        "schemas": [PATCH_SCHEMA],
        "Operations": [
            {
                "op": "add",
                "path": "emails",
                "value": [
                    {"VALUE": "second@example.org", "TYPE": "home", "PRIMARY": True},
                ],
            },
        ],
    }
    response = client.patch(path, json=patch)
    assert response.status_code == 200
    assert response.json()["emails"] == [
        {"value": "first@example.org", "type": "work", "primary": False},
        {"value": "second@example.org", "type": "home", "primary": True},
    ]
    retried = client.patch(path, json=patch)
    assert retried.status_code == 200
    assert retried.json() == response.json()


def test_repeated_nonprimary_email_add_preserves_capacity_and_timestamp(client: TestClient) -> None:
    """A repeated existing value is a no-op even when the stored list is full."""
    emails = [{"value": f"address{index}@example.org"} for index in range(10)]
    created = _create(client, emails=emails).json()
    path = BASE + "/Users/" + created["id"]
    patch = {
        "schemas": [PATCH_SCHEMA],
        "Operations": [
            {
                "op": "add",
                "value": {
                    "emails": [
                        {"value": "address0@example.org"},
                        {"value": "address0@example.org"},
                    ],
                },
            },
        ],
    }
    response = client.patch(path, json=patch)
    assert response.status_code == 200
    assert response.json() == created


def test_email_primary_switch_rolls_back_with_later_invalid_operation(client: TestClient) -> None:
    """Primary demotion and other writes remain atomic with later PATCH failure."""
    created = _create(client, emails=[{"value": "first@example.org", "primary": True}]).json()
    path = BASE + "/Users/" + created["id"]
    patch = {
        "schemas": [PATCH_SCHEMA],
        "Operations": [
            {"op": "add", "path": "emails", "value": [{"value": "second@example.org", "primary": True}]},
            {"op": "replace", "path": "groups", "value": []},
        ],
    }
    response = client.patch(path, json=patch)
    assert response.status_code == 400
    assert response.json()["scimType"] == "invalidPath"
    assert client.get(path).json() == created


def test_email_schema_matches_exact_filter_comparison(client: TestClient) -> None:
    """Discovery accurately tells connectors how mixed-case email values match."""
    _create(client, emails=[{"value": "Mixed@example.org", "type": "work"}])
    schema = client.get(BASE + "/Schemas/" + USER_SCHEMA).json()
    emails = next(attribute for attribute in schema["attributes"] if attribute["name"] == "emails")
    string_attributes = [attribute for attribute in emails["subAttributes"] if attribute["type"] == "string"]
    assert all(attribute.get("caseExact") is True for attribute in string_attributes)
    for value, count in [("Mixed@example.org", 1), ("mixed@example.org", 0)]:
        response = client.get(BASE + "/Users", params={"filter": f'emails.value eq "{value}"'})
        assert response.json()["totalResults"] == count
