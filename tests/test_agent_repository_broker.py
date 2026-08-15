"""Agent Vault adapter tests for constrained agent repositories."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest

from mindroom.agent_repositories import (
    AgentVaultRepositoryBroker,
    RepositoryBindingError,
    RepositoryBrokerError,
    RepositoryEnsureRequest,
)
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system.worker_routing import descriptive_worker_id_for_key

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _runtime_paths(tmp_path: Path, token_file: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={
            "MINDROOM_AGENT_REPOSITORY_BROKER_URL": "http://agent-vault:14321",
            "MINDROOM_AGENT_REPOSITORY_BROKER_TOKEN_FILE": str(token_file),
        },
    )


def _request() -> RepositoryEnsureRequest:
    return RepositoryEnsureRequest(
        worker_key="v1:default:shared:redwood",
        organization="example-org",
        repository_name="MindRoom-redwood",
    )


def _response() -> dict[str, object]:
    return {
        "repository_id": "42",
        "organization": "example-org",
        "repository_name": "MindRoom-redwood",
        "clone_url": "https://github.com/example-org/MindRoom-redwood.git",
    }


@pytest.mark.asyncio
async def test_broker_posts_exact_trusted_request_without_repository_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MindRoom should send identity and derived name, never arbitrary management fields."""
    token_file = tmp_path / "broker-token"
    token_file.write_text("control-plane-secret\n", encoding="utf-8")
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=_response())

    broker = AgentVaultRepositoryBroker.from_runtime(_runtime_paths(tmp_path, token_file))
    monkeypatch.setattr(
        broker,
        "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    lease = await broker.ensure_repository(_request())

    assert seen["url"] == "http://agent-vault:14321/v1/internal/mindroom/repositories/ensure"
    assert seen["body"] == {
        "worker_key": "v1:default:shared:redwood",
        "organization": "example-org",
        "repository_name": "MindRoom-redwood",
    }
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["authorization"] == "Bearer control-plane-secret"
    assert headers["x-vault"] == descriptive_worker_id_for_key(
        _request().worker_key,
        prefix="agent-vault",
    )
    assert lease.repository_id == "42"
    assert "secret" not in repr(lease)
    assert "token" not in repr(lease).casefold()


@pytest.mark.asyncio
async def test_broker_rereads_rotated_token_file_per_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mounted Secret rotation should take effect without rebuilding the toolkit."""
    token_file = tmp_path / "broker-token"
    token_file.write_text("first", encoding="utf-8")
    authorization: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorization.append(request.headers["authorization"])
        return httpx.Response(200, json=_response())

    broker = AgentVaultRepositoryBroker.from_runtime(_runtime_paths(tmp_path, token_file))
    monkeypatch.setattr(
        broker,
        "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    await broker.ensure_repository(_request())
    token_file.write_text("second", encoding="utf-8")
    await broker.ensure_repository(_request())

    assert authorization == ["Bearer first", "Bearer second"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 401, 404, 409, 502])
async def test_broker_maps_agent_vault_errors_without_response_body_or_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    """Broker errors should fail closed without reflecting secret-bearing bodies."""
    token_file = tmp_path / "broker-token"
    token_file.write_text("control-plane-secret", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status_code, text="control-plane-secret reflected")

    broker = AgentVaultRepositoryBroker.from_runtime(_runtime_paths(tmp_path, token_file))
    monkeypatch.setattr(
        broker,
        "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RepositoryBrokerError) as error:
        await broker.ensure_repository(_request())

    assert str(status_code) in str(error.value)
    assert "control-plane-secret" not in str(error.value)


@pytest.mark.asyncio
async def test_broker_rejects_credential_or_capability_fields_in_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any unexpected token/capability field should invalidate the full response."""
    token_file = tmp_path / "broker-token"
    token_file.write_text("control-plane-secret", encoding="utf-8")
    payload = {**_response(), "installation_token": "ghs_secret"}
    broker = AgentVaultRepositoryBroker.from_runtime(_runtime_paths(tmp_path, token_file))
    monkeypatch.setattr(
        broker,
        "_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)),
        ),
    )

    with pytest.raises(RepositoryBrokerError, match="schema"):
        await broker.ensure_repository(_request())


@pytest.mark.asyncio
@pytest.mark.parametrize("repository_id", [42, "", "0", "01", "-1", "1.0", "\uff11\uff12"])
async def test_broker_rejects_noncanonical_repository_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repository_id: object,
) -> None:
    """Repository IDs must match Agent Vault's canonical positive decimal-string contract."""
    token_file = tmp_path / "broker-token"
    token_file.write_text("control-plane-secret", encoding="utf-8")
    payload = {**_response(), "repository_id": repository_id}
    broker = AgentVaultRepositoryBroker.from_runtime(_runtime_paths(tmp_path, token_file))
    monkeypatch.setattr(
        broker,
        "_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)),
        ),
    )

    with pytest.raises((RepositoryBrokerError, RepositoryBindingError), match=r"repository ID|field types"):
        await broker.ensure_repository(_request())


@pytest.mark.asyncio
async def test_broker_rejects_credentialed_clone_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credential material cannot cross the broker response into local Git config."""
    token_file = tmp_path / "broker-token"
    token_file.write_text("control-plane-secret", encoding="utf-8")
    payload = {**_response(), "clone_url": "https://token@github.com/example-org/MindRoom-redwood.git"}
    broker = AgentVaultRepositoryBroker.from_runtime(_runtime_paths(tmp_path, token_file))
    monkeypatch.setattr(
        broker,
        "_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)),
        ),
    )

    with pytest.raises(RepositoryBindingError, match="credentialed clone URL"):
        await broker.ensure_repository(_request())


@pytest.mark.asyncio
async def test_broker_rejects_malformed_clone_url_without_parser_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed broker URLs must remain a fail-closed repository boundary error."""
    token_file = tmp_path / "broker-token"
    token_file.write_text("control-plane-secret", encoding="utf-8")
    payload = {**_response(), "clone_url": "https://github.com:not-a-port/example-org/MindRoom-redwood.git"}
    broker = AgentVaultRepositoryBroker.from_runtime(_runtime_paths(tmp_path, token_file))
    monkeypatch.setattr(
        broker,
        "_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)),
        ),
    )

    with pytest.raises(RepositoryBindingError, match="clone URL"):
        await broker.ensure_repository(_request())


def test_broker_requires_only_control_plane_url_and_token_file(tmp_path: Path) -> None:
    """No raw-token fallback or worker-facing broker configuration should exist."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={"MINDROOM_AGENT_REPOSITORY_BROKER_URL": "http://agent-vault:14321"},
    )

    with pytest.raises(RepositoryBrokerError, match="TOKEN_FILE"):
        AgentVaultRepositoryBroker.from_runtime(runtime_paths)


@pytest.mark.parametrize(
    "broker_url",
    [
        "http://agent-vault:14321/base",
        "http://agent-vault:99999",
        "http://agent-vault:0",
        "http://agent-vault:abc",
        "http://agent-vault:14321?",
        "http://agent-vault:14321#",
    ],
)
def test_broker_rejects_non_origin_or_invalid_port_base_url(tmp_path: Path, broker_url: str) -> None:
    """Malformed bases must fail closed before HTTP client construction."""
    with pytest.raises(RepositoryBrokerError, match=r"HTTP\(S\) URL"):
        AgentVaultRepositoryBroker(
            broker_url=broker_url,
            broker_token_file=(tmp_path / "token").resolve(),
            vault_name_prefix="agent-vault",
        )


@pytest.mark.asyncio
async def test_broker_waits_for_bounded_agent_vault_ensure_lifecycle(tmp_path: Path) -> None:
    """MindRoom must not disconnect before Agent Vault's bounded ensure operation finishes."""
    broker = AgentVaultRepositoryBroker(
        broker_url="http://agent-vault:14321",
        broker_token_file=(tmp_path / "token").resolve(),
        vault_name_prefix="agent-vault",
    )

    async with broker._client() as client:
        assert client.timeout.read > 30 + 5 * 60
