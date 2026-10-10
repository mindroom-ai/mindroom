"""Tests that worker-routed shell and python calls carry the running egress broker's env."""

from __future__ import annotations

import socket
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import unquote, urlsplit

import pytest

import mindroom.tool_system.sandbox_proxy as sandbox_proxy_module
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import CredentialsManager
from mindroom.egress_broker.secrets import save_secret
from mindroom.egress_broker.service import serve_egress_broker
from mindroom.egress_broker.tokens import TokenSigner
from mindroom.tool_system.runtime_context import WorkerRuntimeContext, worker_runtime_context
from mindroom.tool_system.worker_routing import (
    ResolvedWorkerTarget,
    ToolExecutionIdentity,
    resolve_unscoped_worker_key,
    resolve_worker_target,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_PLACEHOLDER = "mindroom-brokered"
_GITHUB = {
    "rules": [{"host": "api.github.com", "auth": {"type": "bearer"}}],
    "placeholder_env": {"GH_TOKEN": _PLACEHOLDER},
}


def _identity() -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-1",
        tenant_id=None,
        account_id=None,
    )


class _RecordingClient:
    """Stand-in for the sandbox proxy's httpx.Client that records each execute payload."""

    payloads: list[dict[str, Any]]

    def __init__(self, *, timeout: float) -> None:
        _ = timeout

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return

    def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _Response:
        _ = headers
        if url.endswith("/leases"):
            return _Response({"lease_id": "lease-1", "expires_at": 123.0, "max_uses": 1})
        self.payloads.append(json)
        return _Response({"ok": True, "result": "done"})


class _Response:
    def __init__(self, data: dict[str, object]) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        return

    def json(self) -> dict[str, object]:
        return self._data


@pytest.fixture
def runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Return a primary runtime with a sandbox proxy and a broker on a free loopback port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config_path = tmp_path / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MINDROOM_NAMESPACE": "",
            "MINDROOM_SANDBOX_PROXY_URL": "http://sandbox-runner:8765",
            "MINDROOM_SANDBOX_PROXY_TOKEN": "proxy-token",
            "MINDROOM_SANDBOX_EXECUTION_MODE": "all",
            "MINDROOM_EGRESS_BROKER_PORT": str(port),
            "MINDROOM_EGRESS_BROKER_HOST": "127.0.0.1",
            "MINDROOM_EGRESS_BROKER_URL": f"http://127.0.0.1:{port}",
        },
    )


_ROUTED_UNSCOPED_KEY = "v1:routed:unscoped:code"


@pytest.fixture
def payloads(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record every execute payload the sandbox proxy sends; the default backend is the shared static runner."""
    recorded: list[dict[str, Any]] = []

    @contextmanager
    def lease(*_args: object, **_kwargs: object) -> Iterator[object]:
        yield object()

    monkeypatch.setattr(sandbox_proxy_module, "lease_primary_worker_manager", lease)
    monkeypatch.setattr(_RecordingClient, "payloads", recorded, raising=False)
    monkeypatch.setattr(sandbox_proxy_module.httpx, "Client", _RecordingClient)
    return recorded


@pytest.fixture
def dedicated_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route calls to a dedicated worker whose key is the target's own or, for unscoped targets, a routed one."""

    def routing_payload(*, worker_target: ResolvedWorkerTarget | None, **_kwargs: object) -> tuple[dict, None]:
        assert worker_target is not None
        return {"worker_key": worker_target.worker_key or _ROUTED_UNSCOPED_KEY}, None

    monkeypatch.setattr(sandbox_proxy_module, "_build_worker_routing_payload", routing_payload)


def _call(
    runtime_paths: RuntimePaths,
    tool_name: str,
    worker_target: ResolvedWorkerTarget,
    credentials_manager: CredentialsManager,
    execution_env: dict[str, str] | None = None,
) -> None:
    sandbox_proxy_module._call_proxy_sync(
        runtime_paths=runtime_paths,
        tool_name=tool_name,
        function_name="run",
        args=(),
        kwargs={},
        credentials_manager=credentials_manager,
        execution_env=execution_env,
        worker_target=worker_target,
    )


def _token_worker_key(runtime_paths: RuntimePaths, execution_env: dict[str, str]) -> str:
    username = urlsplit(execution_env["HTTPS_PROXY"]).username
    assert username is not None
    signer = TokenSigner.load_or_create(runtime_paths.storage_root / "egress_broker" / "token.key")
    claims = signer.verify(unquote(username))
    assert claims is not None
    return claims.worker_key


@pytest.mark.usefixtures("dedicated_routing")
@pytest.mark.asyncio
async def test_call_proxy_sync_merges_broker_env_for_shell_only(
    runtime_paths: RuntimePaths,
    payloads: list[dict[str, Any]],
) -> None:
    """Shell calls get the broker env on top of their own env, with placeholders from the live config; file calls do not."""
    config = Config(egress_broker={"services": {"github": _GITHUB}})
    manager = CredentialsManager(runtime_paths.storage_root / "credentials")
    target = resolve_worker_target("user_agent", "code", _identity(), private_agent_names=frozenset())
    save_secret(manager, target, "github", "s3cret")
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        with worker_runtime_context(WorkerRuntimeContext(runtime_paths=runtime_paths, config=config)):
            _call(runtime_paths, "shell", target, manager, {"KEEP": "me"})
            _call(runtime_paths, "file", target, manager)
    shell_payload, file_payload = payloads
    shell_env = shell_payload["execution_env"]
    assert shell_env["KEEP"] == "me"
    assert shell_env["GH_TOKEN"] == _PLACEHOLDER
    assert shell_env["HTTPS_PROXY"].endswith("@" + runtime_paths.process_env["MINDROOM_EGRESS_BROKER_URL"][7:])
    assert _token_worker_key(runtime_paths, shell_env) == target.worker_key
    assert "HTTPS_PROXY" not in file_payload.get("execution_env", {})


@pytest.mark.usefixtures("dedicated_routing")
@pytest.mark.asyncio
async def test_unscoped_shell_call_signs_routed_worker_key(
    runtime_paths: RuntimePaths,
    payloads: list[dict[str, Any]],
) -> None:
    """An unscoped target has no key of its own, so the token carries the key the call was routed to."""
    config = Config(egress_broker={"services": {"github": _GITHUB}})
    manager = CredentialsManager(runtime_paths.storage_root / "credentials")
    target = resolve_worker_target(None, "code", _identity())
    assert target.worker_key is None
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        with worker_runtime_context(WorkerRuntimeContext(runtime_paths=runtime_paths, config=config)):
            _call(runtime_paths, "shell", target, manager)
    [payload] = payloads
    assert _token_worker_key(runtime_paths, payload["execution_env"]) == _ROUTED_UNSCOPED_KEY


@pytest.mark.asyncio
async def test_static_runner_shell_call_signs_unscoped_worker_key(
    runtime_paths: RuntimePaths,
    payloads: list[dict[str, Any]],
) -> None:
    """On the shared static runner no worker key is routed, so the token carries the agent's unscoped key."""
    config = Config(egress_broker={"services": {"github": _GITHUB}})
    manager = CredentialsManager(runtime_paths.storage_root / "credentials")
    target = resolve_worker_target(None, "code", _identity())
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        with worker_runtime_context(WorkerRuntimeContext(runtime_paths=runtime_paths, config=config)):
            _call(runtime_paths, "python", target, manager)
    [payload] = payloads
    assert "worker_key" not in payload
    assert "HTTPS_PROXY" in payload["execution_env"]
    assert _token_worker_key(runtime_paths, payload["execution_env"]) == resolve_unscoped_worker_key(
        "code",
        _identity(),
    )


@pytest.mark.usefixtures("dedicated_routing")
@pytest.mark.asyncio
async def test_broker_git_config_appends_to_call_git_config(
    runtime_paths: RuntimePaths,
    payloads: list[dict[str, Any]],
) -> None:
    """Git config entries the call already carries survive; the broker's proxy entries are appended after them."""
    config = Config(egress_broker={"services": {"github": _GITHUB}})
    manager = CredentialsManager(runtime_paths.storage_root / "credentials")
    target = resolve_worker_target("user_agent", "code", _identity(), private_agent_names=frozenset())
    call_env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.askPass", "GIT_CONFIG_VALUE_0": ""}
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        with worker_runtime_context(WorkerRuntimeContext(runtime_paths=runtime_paths, config=config)):
            _call(runtime_paths, "shell", target, manager, call_env)
    [payload] = payloads
    env = payload["execution_env"]
    assert env["GIT_CONFIG_COUNT"] == "3"
    assert [env[f"GIT_CONFIG_KEY_{index}"] for index in range(3)] == [
        "core.askPass",
        "http.proxy",
        "http.proxyAuthMethod",
    ]
    assert env["GIT_CONFIG_VALUE_0"] == ""
    assert env["GIT_CONFIG_VALUE_1"] == env["HTTPS_PROXY"]
