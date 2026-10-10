"""Tests for the egress broker lifecycle in the primary and the env it gives worker calls."""

from __future__ import annotations

import asyncio
import functools
import socket
import ssl
import stat
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from structlog.testing import capture_logs

from mindroom.config.egress_broker import EgressService
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import CredentialsManager
from mindroom.egress_broker import oauth_source, service
from mindroom.egress_broker.audit import AuditLog
from mindroom.egress_broker.dial import DialPolicy
from mindroom.egress_broker.env import apply_runner_ca_bundle
from mindroom.egress_broker.secrets import delete_secret, save_secret
from mindroom.egress_broker.service import (
    active_audit_log,
    active_ca_pem,
    execution_env_for_worker,
    manage_url,
    serve_egress_broker,
)
from mindroom.egress_broker.tokens import TokenSigner
from mindroom.egress_broker.user_services import delete_user_service, save_user_service
from mindroom.oauth.github import github_oauth_provider
from mindroom.oauth.google_drive import google_drive_oauth_provider
from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity, resolve_worker_target
from tests.oauth_test_utils import (
    DelayedTokenEndpointOutcome,
    publish_oauth_credentials,
    rotated_token_response,
    serve_token_endpoint,
)

from .conftest import audit_records, connect_request, proxy_authorization

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import WorkerScope
    from mindroom.worker_computer.browser_proxy import BrowserEgress

    from .conftest import RawResponse, Upstream, UpstreamCA

_THREAD_NAME = "mindroom-egress-broker"
_PLACEHOLDER = "mindroom-brokered"
_GITHUB = {
    "rules": [{"host": "localhost", "auth": {"type": "bearer"}}],
    "placeholder_env": {"GH_TOKEN": _PLACEHOLDER},
}
_OPENAI = {
    "rules": [{"host": "api.openai.com", "auth": {"type": "bearer"}}],
    "placeholder_env": {"OPENAI_API_KEY": _PLACEHOLDER},
}
_GITHUB_OAUTH = {**_GITHUB, "oauth_provider": "github"}
_PUBLIC_URL = "https://chat.example.org"
_OAUTH_REFRESH_TOKEN = "github-refresh"  # noqa: S105 - test credential


@pytest.fixture
def tmp_runtime_paths(tmp_path: Path) -> Callable[..., RuntimePaths]:
    """Return ``make(**env)``: a runtime whose storage is under `tmp_path` and whose env is exactly `env`."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")

    def make(**env: str) -> RuntimePaths:
        return resolve_runtime_paths(
            config_path=config_path,
            storage_path=tmp_path / "mindroom_data",
            process_env={"MINDROOM_NAMESPACE": "", **env},
        )

    return make


@pytest.fixture
def manager(tmp_path: Path) -> CredentialsManager:
    """Return the primary credentials manager for the runtime `tmp_runtime_paths` builds."""
    return CredentialsManager(tmp_path / "mindroom_data" / "credentials")


@pytest.fixture
def allow_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the service's broker dial the loopback fake upstreams."""
    monkeypatch.setattr(service, "DialPolicy", functools.partial(DialPolicy, allow_loopback=True))


@pytest.fixture
def trust_upstream(upstream_ca: UpstreamCA, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the fake upstream's CA the process's trust store, as public roots are for real upstreams."""
    path = tmp_path / "upstream-ca.pem"
    path.write_text(upstream_ca.pem)
    monkeypatch.setenv("SSL_CERT_FILE", str(path))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _broker_env(port: int, **extra: str) -> dict[str, str]:
    return {
        "MINDROOM_EGRESS_BROKER_PORT": str(port),
        "MINDROOM_EGRESS_BROKER_HOST": "127.0.0.1",
        "MINDROOM_EGRESS_BROKER_URL": f"http://127.0.0.1:{port}",
        **extra,
    }


def _identity(requester_id: str, agent_name: str = "code") -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent_name,
        requester_id=requester_id,
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
        tenant_id=None,
        account_id=None,
    )


def _target(
    requester_id: str = "@alice:example.org",
    agent_name: str = "code",
    *,
    scope: WorkerScope = "user_agent",
) -> ResolvedWorkerTarget:
    return resolve_worker_target(
        scope,
        agent_name,
        _identity(requester_id, agent_name),
        private_agent_names=frozenset(),
    )


def _config(**services: object) -> Config:
    return Config(egress_broker={"services": services})


def _token(env: dict[str, str]) -> str:
    username = urlsplit(env["HTTPS_PROXY"]).username
    assert username is not None
    return unquote(username)


def _broker_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == _THREAD_NAME]


async def _get_through(env: dict[str, str], url: str, runner_dir: Path) -> httpx.Response:
    """Send one request the way a worker's tool would: the env's proxy and the runner's trust bundle."""
    worker_env = dict(env)
    assert apply_runner_ca_bundle(worker_env, runner_dir)
    async with httpx.AsyncClient(
        proxy=worker_env["HTTPS_PROXY"],
        verify=ssl.create_default_context(cafile=worker_env["SSL_CERT_FILE"]),
        trust_env=False,
    ) as client:
        return await client.get(url)


@pytest.mark.asyncio
async def test_disabled_without_port(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tmp_path: Path,
) -> None:
    """Without a port the context is a no-op: no thread, no state, and worker calls get no broker env."""
    runtime_paths = tmp_runtime_paths()
    config = _config(github=_GITHUB)
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        assert _broker_threads() == []
        assert active_audit_log() is None
        assert active_ca_pem() is None
        env = execution_env_for_worker(
            runtime_paths,
            config=config,
            worker_target=_target(),
        )
        assert env == {}
    assert not (tmp_path / "mindroom_data" / "egress_broker").exists()


@pytest.mark.asyncio
async def test_requires_url_when_port_set(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
) -> None:
    """A port without the worker-facing URL refuses to start."""
    runtime_paths = tmp_runtime_paths(MINDROOM_EGRESS_BROKER_PORT=str(_free_port()))
    with pytest.raises(
        ValueError,
        match="MINDROOM_EGRESS_BROKER_URL is required when MINDROOM_EGRESS_BROKER_PORT is set",
    ):
        async with serve_egress_broker(runtime_paths, config_provider=lambda: None, credentials_manager=manager):
            pytest.fail("the broker must not start")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MINDROOM_EGRESS_BROKER_PORT", "0"),
        ("MINDROOM_EGRESS_BROKER_PORT", "65536"),
        ("MINDROOM_EGRESS_BROKER_PORT", "http"),
        ("MINDROOM_EGRESS_BROKER_PORT", "8_768"),
        ("MINDROOM_EGRESS_BROKER_TOKEN_TTL_SECONDS", "0"),
        ("MINDROOM_EGRESS_BROKER_TOKEN_TTL_SECONDS", "-60"),
        ("MINDROOM_EGRESS_BROKER_TOKEN_TTL_SECONDS", "a week"),
        ("MINDROOM_EGRESS_BROKER_URL", "host.docker.internal:8768"),
        ("MINDROOM_EGRESS_BROKER_URL", "ftp://broker.example.org:8768"),
        ("MINDROOM_EGRESS_BROKER_URL", "http://"),
        ("MINDROOM_EGRESS_BROKER_URL", "http://:8768"),
        ("MINDROOM_EGRESS_BROKER_URL", "http://[::1"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_settings_refuse_to_start(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    """An invalid port, URL, or token TTL raises before any state is written or any thread starts."""
    runtime_paths = tmp_runtime_paths(**{**_broker_env(_free_port()), name: value})
    with pytest.raises(ValueError, match=name):
        async with serve_egress_broker(runtime_paths, config_provider=lambda: None, credentials_manager=manager):
            pytest.fail("the broker must not start")
    assert _broker_threads() == []
    assert not (tmp_path / "mindroom_data" / "egress_broker").exists()


@pytest.mark.usefixtures("allow_loopback", "trust_upstream")
@pytest.mark.asyncio
async def test_end_to_end_inject_through_running_service(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    tmp_path: Path,
) -> None:
    """A worker using only the env from execution_env_for_worker reaches the upstream with the scope's secret."""
    runtime_paths = tmp_runtime_paths(**_broker_env(_free_port()))
    config = _config(github=_GITHUB)
    target = _target()
    save_secret(manager, target, "github", "s3cret")
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=target)
        assert env["GH_TOKEN"] == _PLACEHOLDER
        assert "s3cret" not in str(env)
        response = await _get_through(env, tls_upstream.url("/echo"), tmp_path / "runner")
        assert response.status_code == 200
        assert response.json()["headers"]["authorization"] == ["Bearer s3cret"]
        audit = active_audit_log()
        assert audit is not None
        [record] = await audit_records(audit, 1)
        assert (record.host, record.service, record.agent_name) == ("localhost", "github", "code")


@pytest.mark.usefixtures("allow_loopback", "trust_upstream")
@pytest.mark.asyncio
async def test_missing_secret_reports_manage_url(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    tmp_path: Path,
) -> None:
    """A brokered host without a secret in scope answers 403 with the Connections link, without reaching upstream."""
    runtime_paths = tmp_runtime_paths(
        **_broker_env(_free_port()),
        MINDROOM_PUBLIC_URL="https://chat.example.org/",
        MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED="true",
    )
    config = _config(github=_GITHUB)
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(
            runtime_paths,
            config=config,
            worker_target=_target(),
        )
        response = await _get_through(env, tls_upstream.url("/echo"), tmp_path / "runner")
    assert response.status_code == 403
    assert response.json() == {
        "error": "credential_not_configured",
        "service": "github",
        "manage_url": "https://chat.example.org/connections/egress",
    }
    assert tls_upstream.hits == []


@pytest.mark.usefixtures("allow_loopback", "trust_upstream")
@pytest.mark.asyncio
async def test_unreadable_config_tunnels_without_injection(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    tmp_path: Path,
) -> None:
    """When the config provider raises, the broker has no rules: traffic is tunnelled and nothing is injected."""
    runtime_paths = tmp_runtime_paths(**_broker_env(_free_port()))
    config = _config(github=_GITHUB)
    target = _target()
    save_secret(manager, target, "github", "s3cret")

    def broken() -> Config | None:
        msg = "config is mid-reload"
        raise RuntimeError(msg)

    async with serve_egress_broker(runtime_paths, config_provider=broken, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=target)
        response = await _get_through(env, tls_upstream.url("/echo"), tmp_path / "runner")
    assert response.status_code == 200
    assert "authorization" not in response.json()["headers"]


@pytest.mark.usefixtures("allow_loopback", "trust_upstream")
@pytest.mark.asyncio
async def test_last_good_config_survives_unreadable_config(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    raw_proxy: Callable[[int, bytes], Awaitable[RawResponse]],
) -> None:
    """Once the broker has read a config, a provider that raises or has none keeps that config, deny included."""
    port = _free_port()
    runtime_paths = tmp_runtime_paths(**_broker_env(port))
    config = Config(egress_broker={"unmatched_hosts": "deny", "services": {"openai": _OPENAI}})
    state = {"mode": "good"}

    def provider() -> Config | None:
        if state["mode"] == "raise":
            msg = "the saved config is invalid"
            raise RuntimeError(msg)
        return config if state["mode"] == "good" else None

    async with serve_egress_broker(runtime_paths, config_provider=provider, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=_target())
        connect = connect_request(f"localhost:{tls_upstream.port}", authorization=proxy_authorization(_token(env)))
        responses = []
        for mode in ("good", "raise", "none"):
            state["mode"] = mode
            responses.append(await raw_proxy(port, connect))
    assert [(response.status, response.json()) for response in responses] == [
        (403, {"error": "host_not_allowed", "services": ["openai"]}),
    ] * 3
    assert tls_upstream.hits == []


@pytest.mark.asyncio
async def test_placeholder_env_only_when_secret_configured(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
) -> None:
    """Placeholders come only from services with a secret in this worker's scope; the proxy env is always set."""
    runtime_paths = tmp_runtime_paths(**_broker_env(_free_port()))
    config = _config(github=_GITHUB, openai=_OPENAI)
    alice = _target("@alice:example.org")
    save_secret(manager, alice, "github", "s3cret")
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        alice_env = execution_env_for_worker(
            runtime_paths,
            config=config,
            worker_target=alice,
        )
        bob_env = execution_env_for_worker(
            runtime_paths,
            config=config,
            worker_target=_target("@bob:example.org"),
        )
    assert alice_env["GH_TOKEN"] == _PLACEHOLDER
    assert "OPENAI_API_KEY" not in alice_env
    assert "GH_TOKEN" not in bob_env
    assert "OPENAI_API_KEY" not in bob_env
    assert bob_env["HTTPS_PROXY"].endswith("@127.0.0.1:" + runtime_paths.process_env["MINDROOM_EGRESS_BROKER_PORT"])


@pytest.mark.asyncio
async def test_secret_status_failure_skips_only_that_service(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secret store error for one service drops that service's placeholders, logs only names, and spares the call."""
    runtime_paths = tmp_runtime_paths(**_broker_env(_free_port()))
    config = _config(github=_GITHUB, openai=_OPENAI)
    target = _target()
    save_secret(manager, target, "github", "s3cret")
    save_secret(manager, target, "openai", "sk-s3cret")
    real_service_status = service.service_status

    def flaky_service_status(
        store: CredentialsManager,
        scope: ResolvedWorkerTarget,
        egress_service: EgressService,
        name: str,
        **kwargs: Any,  # noqa: ANN401
    ) -> object:
        if name == "openai":
            msg = "sk-s3cret could not be decrypted"
            raise OSError(msg)
        return real_service_status(store, scope, egress_service, name, **kwargs)

    monkeypatch.setattr(service, "service_status", flaky_service_status)
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        with capture_logs() as logs:
            env = execution_env_for_worker(runtime_paths, config=config, worker_target=target)
    assert env["GH_TOKEN"] == _PLACEHOLDER
    assert "OPENAI_API_KEY" not in env
    assert "HTTPS_PROXY" in env
    [warning] = [entry for entry in logs if entry["event"] == "egress_broker_secret_status_failed"]
    assert warning == {
        "event": "egress_broker_secret_status_failed",
        "log_level": "warning",
        "service": "openai",
        "error_type": "OSError",
    }


@pytest.mark.asyncio
async def test_no_env_without_config_or_claims(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
) -> None:
    """A running broker still gives nothing when the call has no config or no worker identity to sign."""
    runtime_paths = tmp_runtime_paths(**_broker_env(_free_port()))
    config = _config(github=_GITHUB)
    no_identity = resolve_worker_target("user_agent", "code", None)
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        assert execution_env_for_worker(runtime_paths, config=None, worker_target=_target()) == {}
        assert execution_env_for_worker(runtime_paths, config=config, worker_target=None) == {}
        assert (
            execution_env_for_worker(
                runtime_paths,
                config=config,
                worker_target=no_identity,
            )
            == {}
        )


@pytest.mark.asyncio
async def test_state_files_and_modes(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tmp_path: Path,
) -> None:
    """State lives under <storage>/egress_broker with private keys and log; the holder is cleared on exit."""
    runtime_paths = tmp_runtime_paths(**_broker_env(_free_port()))
    state = tmp_path / "mindroom_data" / "egress_broker"
    async with serve_egress_broker(runtime_paths, config_provider=lambda: None, credentials_manager=manager):
        for name in ("ca.key", "token.key", "requests.sqlite3"):
            assert stat.S_IMODE((state / name).stat().st_mode) == 0o600, name
        assert active_ca_pem() == (state / "ca.pem").read_text()
        assert isinstance(active_audit_log(), AuditLog)
    assert active_ca_pem() is None
    assert active_audit_log() is None


@pytest.mark.asyncio
async def test_ca_key_encrypted_with_credentials_key(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tmp_path: Path,
) -> None:
    """With a credentials encryption key set, the CA key is encrypted with it."""
    encryption_key = "dGhpcyBpcyBhIDMyIGJ5dGUgdGVzdCBrZXkgISEhISE="
    runtime_paths = tmp_runtime_paths(
        **_broker_env(_free_port()),
        MINDROOM_CREDENTIALS_ENCRYPTION_KEY=encryption_key,
    )
    async with serve_egress_broker(runtime_paths, config_provider=lambda: None, credentials_manager=manager):
        pass
    key_pem = (tmp_path / "mindroom_data" / "egress_broker" / "ca.key").read_bytes()
    with pytest.raises(TypeError):
        serialization.load_pem_private_key(key_pem, password=None)
    serialization.load_pem_private_key(key_pem, password=encryption_key.encode())


@pytest.mark.asyncio
async def test_token_ttl_from_env(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tmp_path: Path,
) -> None:
    """Minted tokens expire after MINDROOM_EGRESS_BROKER_TOKEN_TTL_SECONDS."""
    runtime_paths = tmp_runtime_paths(
        **_broker_env(_free_port()),
        MINDROOM_EGRESS_BROKER_TOKEN_TTL_SECONDS="600",  # noqa: S106 - a lifetime, not a secret
    )
    config = _config(github=_GITHUB)
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(
            runtime_paths,
            config=config,
            worker_target=_target(),
        )
    signer = TokenSigner.load_or_create(tmp_path / "mindroom_data" / "egress_broker" / "token.key")
    now = time.time()
    claims = signer.verify(_token(env), now=now + 590)
    assert claims is not None
    assert claims.worker_key == _target().worker_key
    assert signer.verify(_token(env), now=now + 610) is None


@pytest.mark.asyncio
async def test_thread_stops_on_exit(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
) -> None:
    """The broker runs on its own named thread, which is gone and has released the port after exit."""
    port = _free_port()
    runtime_paths = tmp_runtime_paths(**_broker_env(port))
    async with serve_egress_broker(runtime_paths, config_provider=lambda: None, credentials_manager=manager):
        [thread] = _broker_threads()
        assert thread.daemon
        assert thread is not threading.current_thread()
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass
    assert _broker_threads() == []
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), timeout=5).close()


@pytest.mark.asyncio
async def test_error_inside_context_still_stops_broker(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
) -> None:
    """An exception raised while the broker runs propagates after the thread stops and the holder clears."""
    runtime_paths = tmp_runtime_paths(**_broker_env(_free_port()))

    async def fail_while_running() -> None:
        async with serve_egress_broker(runtime_paths, config_provider=lambda: None, credentials_manager=manager):
            assert active_ca_pem() is not None
            msg = "the primary failed"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="the primary failed"):
        await fail_while_running()
    assert active_ca_pem() is None
    assert active_audit_log() is None
    assert _broker_threads() == []


@pytest.fixture
def no_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the test process's own proxy variables out of the broker's dial policy."""
    for name in ("all_proxy", "http_proxy", "https_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.mark.usefixtures("no_proxy_env")
@pytest.mark.parametrize(
    ("env", "https_proxy", "no_proxy"),
    [
        ({}, None, ()),
        (
            {"HTTPS_PROXY": "http://proxy.corp:3128", "NO_PROXY": ".svc, 10.0.0.0/8"},
            ("proxy.corp", 3128),
            (".svc", "10.0.0.0/8"),
        ),
    ],
)
@pytest.mark.asyncio
async def test_dial_policy_follows_the_primary_proxy_env(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    https_proxy: tuple[str, int] | None,
    no_proxy: tuple[str, ...],
) -> None:
    """The broker dials upstreams through the primary's HTTPS_PROXY, honoring its NO_PROXY, and directly without them."""
    policies: list[DialPolicy] = []

    def dial_policy(*, egress: BrowserEgress | None = None) -> DialPolicy:
        policies.append(DialPolicy(egress=egress))
        return policies[-1]

    monkeypatch.setattr(service, "DialPolicy", dial_policy)
    runtime_paths = tmp_runtime_paths(**_broker_env(_free_port()), **env)
    async with serve_egress_broker(runtime_paths, config_provider=lambda: None, credentials_manager=manager):
        pass
    [policy] = policies
    assert policy.egress is not None
    upstream = policy.egress.upstream_for(443)
    assert (None if upstream is None else (upstream.host, upstream.port)) == https_proxy
    assert policy.egress.no_proxy == no_proxy
    assert policy.egress.upstream_for(80) is None


@pytest.mark.usefixtures("no_proxy_env")
@pytest.mark.parametrize(
    ("env", "variable", "value"),
    [
        ({"HTTPS_PROXY": "socks5://x:1080"}, "HTTPS_PROXY", "socks5://x:1080"),
        ({"https_proxy": "socks5://x:1080"}, "HTTPS_PROXY", "socks5://x:1080"),
        ({"HTTPS_PROXY": "http://user:hunter2@proxy:3128"}, "HTTPS_PROXY", "hunter2"),
        ({"HTTP_PROXY": "http://proxy:3128/path"}, "HTTP_PROXY", "/path"),
        # HTTP_PROXY alone leaves https without a proxy, but its own variable is the one that is broken.
        ({"HTTP_PROXY": "ftp://proxy:3128", "HTTPS_PROXY": "http://proxy:3128"}, "HTTP_PROXY", "ftp://proxy"),
        ({"ALL_PROXY": "socks5://x:1080"}, "ALL_PROXY", "socks5://x:1080"),
        ({"ALL_PROXY": "http://user:hunter2@proxy:3128", "HTTP_PROXY": "http://proxy:3128"}, "ALL_PROXY", "hunter2"),
    ],
)
@pytest.mark.asyncio
async def test_unusable_operator_proxy_stops_startup(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tmp_path: Path,
    env: dict[str, str],
    variable: str,
    value: str,
) -> None:
    """A proxy variable the broker cannot use refuses startup, naming the variable and never its value."""
    runtime_paths = tmp_runtime_paths(**_broker_env(_free_port()), **env)
    with pytest.raises(ValueError, match=variable) as refused:
        async with serve_egress_broker(runtime_paths, config_provider=lambda: None, credentials_manager=manager):
            pytest.fail("the broker must not start")
    assert "only http:// or https:// proxies without credentials or paths" in str(refused.value)
    assert value not in str(refused.value)
    assert _broker_threads() == []
    assert active_audit_log() is None
    assert not (tmp_path / "mindroom_data" / "egress_broker").exists()


@pytest.mark.usefixtures("no_proxy_env")
@pytest.mark.asyncio
async def test_unused_all_proxy_does_not_stop_startup(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
) -> None:
    """ALL_PROXY only fills in schemes without their own variable, so a broken one beside both is never used."""
    env = {"HTTP_PROXY": "http://proxy:3128", "HTTPS_PROXY": "http://proxy:3128", "ALL_PROXY": "socks5://x:1080"}
    runtime_paths = tmp_runtime_paths(**_broker_env(_free_port()), **env)
    async with serve_egress_broker(runtime_paths, config_provider=lambda: None, credentials_manager=manager):
        assert active_ca_pem() is not None


@pytest.mark.asyncio
async def test_bind_error_propagates(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
) -> None:
    """A port already in use fails the start with the bind error and leaves nothing running."""
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        runtime_paths = tmp_runtime_paths(**_broker_env(occupied.getsockname()[1]))
        with pytest.raises(OSError, match=r"(?i)address already in use"):
            async with serve_egress_broker(runtime_paths, config_provider=lambda: None, credentials_manager=manager):
                pytest.fail("the broker must not start")
    assert _broker_threads() == []
    assert active_audit_log() is None


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, None),
        ({"MINDROOM_PUBLIC_URL": "  "}, None),
        ({"MINDROOM_PUBLIC_URL": "https://chat.example.org"}, "https://chat.example.org/"),
        ({"MINDROOM_PUBLIC_URL": "https://chat.example.org/"}, "https://chat.example.org/"),
        (
            {"MINDROOM_PUBLIC_URL": "https://chat.example.org/", "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true"},
            "https://chat.example.org/connections/egress",
        ),
        ({"MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true"}, None),
    ],
)
def test_manage_url(tmp_runtime_paths: Callable[..., RuntimePaths], env: dict[str, str], expected: str | None) -> None:
    """The manage link is the personal egress page behind trusted upstream auth, else the dashboard."""
    assert manage_url(tmp_runtime_paths(**env)) == expected


@pytest.fixture
def github_oauth_client(manager: CredentialsManager) -> None:
    """Configure the GitHub OAuth app, so GitHub accounts can be connected and refreshed."""
    manager.save_credentials("github_oauth_client", {"client_id": "github-client-id", "client_secret": "gh-secret"})


def _connect_github(
    manager: CredentialsManager,
    requester_id: str,
    token: str,
    *,
    expires_at: float = 4_102_444_800.0,
) -> None:
    """Store a GitHub connection in the requester's own store, as the connect flow does for every agent scope."""
    provider = github_oauth_provider()
    publish_oauth_credentials(
        provider,
        {
            "token": token,
            "refresh_token": _OAUTH_REFRESH_TOKEN,
            "token_uri": provider.token_url,
            "client_id": "github-client-id",
            "scopes": [],
            "expires_at": expires_at,
            "_source": "oauth",
            "_oauth_provider": "github",
        },
        credentials_manager=manager,
        worker_target=resolve_worker_target("user", "code", _identity(requester_id)),
    )


def _oauth_runtime(tmp_runtime_paths: Callable[..., RuntimePaths], backend: str = "docker") -> RuntimePaths:
    """Return a runtime on `backend`: personal accounts need a dedicated one (the default) to count as private."""
    return tmp_runtime_paths(
        **_broker_env(_free_port()),
        MINDROOM_PUBLIC_URL=_PUBLIC_URL,
        MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED="true",
        MINDROOM_WORKER_BACKEND=backend,
    )


@pytest.mark.usefixtures("allow_loopback", "trust_upstream", "github_oauth_client")
@pytest.mark.asyncio
async def test_stored_key_wins_over_oauth_and_its_removal_falls_back_without_restart(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    tmp_path: Path,
) -> None:
    """With both sources the stored key is injected; once it is removed the same token gets the OAuth token."""
    runtime_paths = _oauth_runtime(tmp_runtime_paths)
    config = _config(github=_GITHUB_OAUTH)
    target = _target()
    save_secret(manager, target, "github", "s3cret")
    _connect_github(manager, "@alice:example.org", "alice-oauth")
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=target)
        with_key = await _get_through(env, tls_upstream.url("/echo"), tmp_path / "runner")
        delete_secret(manager, target, "github")
        with_oauth = await _get_through(env, tls_upstream.url("/echo"), tmp_path / "runner")
    assert with_key.json()["headers"]["authorization"] == ["Bearer s3cret"]
    assert with_oauth.json()["headers"]["authorization"] == ["Bearer alice-oauth"]


@pytest.mark.usefixtures("github_oauth_client")
@pytest.mark.asyncio
async def test_placeholder_env_when_only_oauth_is_connected(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
) -> None:
    """A connected OAuth account alone gives the scope its placeholders; the token itself never enters the env."""
    runtime_paths = _oauth_runtime(tmp_runtime_paths)
    config = _config(github=_GITHUB_OAUTH)
    _connect_github(manager, "@alice:example.org", "alice-oauth")
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        alice_env = execution_env_for_worker(runtime_paths, config=config, worker_target=_target())
        bob_env = execution_env_for_worker(runtime_paths, config=config, worker_target=_target("@bob:example.org"))
    assert alice_env["GH_TOKEN"] == _PLACEHOLDER
    assert "alice-oauth" not in str(alice_env)
    assert "GH_TOKEN" not in bob_env


@pytest.mark.usefixtures("allow_loopback", "trust_upstream", "github_oauth_client")
@pytest.mark.parametrize("scope", ["shared", None], ids=["shared", "unscoped"])
@pytest.mark.asyncio
async def test_requester_scoped_oauth_is_not_used_where_requesters_share_a_worker(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    tmp_path: Path,
    scope: WorkerScope | None,
) -> None:
    """On a shared or unscoped worker a connected GitHub account is never injected, placeheld, or offered.

    Another requester's later command could read the proxy token from the shared sandbox, so the 403 names only the
    key page: no provider and no connect link.
    """
    runtime_paths = _oauth_runtime(tmp_runtime_paths)
    config = _config(github=_GITHUB_OAUTH)
    _connect_github(manager, "@alice:example.org", "alice-oauth")
    target = _target(scope=scope)
    if target.worker_key is None:
        # An unscoped call is routed to a worker that its proxy token names.
        target = replace(target, worker_key="v1:local:unscoped:code")
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=target)
        response = await _get_through(env, tls_upstream.url("/echo"), tmp_path / "runner")
    assert "GH_TOKEN" not in env
    assert response.status_code == 403
    assert response.json() == {
        "error": "credential_not_configured",
        "service": "github",
        "manage_url": f"{_PUBLIC_URL}/connections/egress",
    }
    assert tls_upstream.hits == []


@pytest.mark.usefixtures("allow_loopback", "trust_upstream", "github_oauth_client")
@pytest.mark.asyncio
async def test_requester_scoped_oauth_on_a_shared_agent_injects_the_callers_token_with_the_opt_in(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    tmp_path: Path,
) -> None:
    """With `oauth_on_shared_workers`, two requesters on one shared agent each get their own GitHub token."""
    runtime_paths = _oauth_runtime(tmp_runtime_paths)
    config = _config(github={**_GITHUB_OAUTH, "oauth_on_shared_workers": True})
    _connect_github(manager, "@alice:example.org", "alice-oauth")
    _connect_github(manager, "@bob:example.org", "bob-oauth")
    alice = _target(scope="shared")
    bob = _target("@bob:example.org", scope="shared")
    assert alice.worker_key == bob.worker_key
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        alice_env = execution_env_for_worker(runtime_paths, config=config, worker_target=alice)
        bob_env = execution_env_for_worker(runtime_paths, config=config, worker_target=bob)
        alice_response = await _get_through(alice_env, tls_upstream.url("/echo"), tmp_path / "runner")
        bob_response = await _get_through(bob_env, tls_upstream.url("/echo"), tmp_path / "runner")
    assert alice_env["GH_TOKEN"] == _PLACEHOLDER
    assert alice_response.json()["headers"]["authorization"] == ["Bearer alice-oauth"]
    assert bob_response.json()["headers"]["authorization"] == ["Bearer bob-oauth"]


@pytest.mark.usefixtures("allow_loopback", "trust_upstream", "github_oauth_client")
@pytest.mark.asyncio
async def test_requester_scoped_oauth_is_not_used_on_the_static_runner_without_the_opt_in(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    tmp_path: Path,
) -> None:
    """Every call shares the static runner's process, so even a `user_agent` call gets no GitHub account there."""
    runtime_paths = _oauth_runtime(tmp_runtime_paths, backend="static_runner")
    config = _config(github=_GITHUB_OAUTH)
    _connect_github(manager, "@alice:example.org", "alice-oauth")
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=_target())
        response = await _get_through(env, tls_upstream.url("/echo"), tmp_path / "runner")
    assert "GH_TOKEN" not in env
    assert response.status_code == 403
    assert response.json() == {
        "error": "credential_not_configured",
        "service": "github",
        "manage_url": f"{_PUBLIC_URL}/connections/egress",
    }
    assert tls_upstream.hits == []


@pytest.mark.usefixtures("allow_loopback", "trust_upstream", "github_oauth_client")
@pytest.mark.asyncio
async def test_requester_scoped_oauth_is_used_on_the_static_runner_with_the_opt_in(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    tmp_path: Path,
) -> None:
    """With `oauth_on_shared_workers`, a `user_agent` call on the static runner gets the requester's own token."""
    runtime_paths = _oauth_runtime(tmp_runtime_paths, backend="static_runner")
    config = _config(github={**_GITHUB_OAUTH, "oauth_on_shared_workers": True})
    _connect_github(manager, "@alice:example.org", "alice-oauth")
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=_target())
        response = await _get_through(env, tls_upstream.url("/echo"), tmp_path / "runner")
    assert env["GH_TOKEN"] == _PLACEHOLDER
    assert response.json()["headers"]["authorization"] == ["Bearer alice-oauth"]


@pytest.mark.usefixtures("allow_loopback", "trust_upstream", "github_oauth_client")
@pytest.mark.asyncio
async def test_concurrent_requests_refresh_an_expired_token_once(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Several tunnels hitting an expired token at once share a single refresh and all inject the new token."""
    runtime_paths = _oauth_runtime(tmp_runtime_paths)
    config = _config(github=_GITHUB_OAUTH)
    _connect_github(manager, "@alice:example.org", "stale-oauth", expires_at=1.0)
    presented = serve_token_endpoint(monkeypatch, [DelayedTokenEndpointOutcome(0.3, rotated_token_response())])
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        worker_env = execution_env_for_worker(runtime_paths, config=config, worker_target=_target())
        assert apply_runner_ca_bundle(worker_env, tmp_path / "runner")
        async with httpx.AsyncClient(
            proxy=worker_env["HTTPS_PROXY"],
            verify=ssl.create_default_context(cafile=worker_env["SSL_CERT_FILE"]),
            trust_env=False,
        ) as client:
            responses = await asyncio.gather(*(client.get(tls_upstream.url("/echo")) for _ in range(6)))
    assert [response.json()["headers"]["authorization"] for response in responses] == [
        ["Bearer rotated-access-token"],
    ] * 6
    assert presented == [_OAUTH_REFRESH_TOKEN]


@pytest.mark.usefixtures("allow_loopback", "trust_upstream", "github_oauth_client")
@pytest.mark.asyncio
async def test_revoked_grant_returns_oauth_connection_required(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh the provider rejects answers 403 with a reconnect link; no token reaches the body or the logs."""
    runtime_paths = _oauth_runtime(tmp_runtime_paths)
    config = _config(github=_GITHUB_OAUTH)
    _connect_github(manager, "@alice:example.org", "stale-oauth", expires_at=1.0)
    serve_token_endpoint(monkeypatch, [httpx.Response(400, json={"error": "invalid_grant"})])
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=_target())
        with capture_logs() as logs:
            response = await _get_through(env, tls_upstream.url("/echo"), tmp_path / "runner")
    body = response.json()
    assert response.status_code == 403
    assert body.keys() == {"error", "service", "provider", "connect_url"}
    assert (body["error"], body["service"], body["provider"]) == ("oauth_connection_required", "github", "github")
    assert body["connect_url"].startswith(f"{_PUBLIC_URL}/api/oauth/github/authorize?connect_token=")
    assert tls_upstream.hits == []
    for leaked in ("stale-oauth", _OAUTH_REFRESH_TOKEN):
        assert leaked not in response.text
    for leaked in ("stale-oauth", _OAUTH_REFRESH_TOKEN, urlsplit(body["connect_url"]).query):
        assert leaked not in repr(logs)
    assert any(entry["event"] == "egress_broker_oauth_token_unavailable" for entry in logs)


@pytest.mark.usefixtures("allow_loopback", "trust_upstream", "github_oauth_client")
@pytest.mark.asyncio
async def test_missing_connection_returns_connect_url(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    tmp_path: Path,
) -> None:
    """With neither a key nor a connection, the 403 offers both the key page and a link to connect the account."""
    runtime_paths = _oauth_runtime(tmp_runtime_paths)
    config = _config(github=_GITHUB_OAUTH)
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=_target())
        response = await _get_through(env, tls_upstream.url("/echo"), tmp_path / "runner")
    body = response.json()
    assert response.status_code == 403
    assert body.keys() == {"error", "service", "manage_url", "provider", "connect_url"}
    assert (body["error"], body["service"], body["provider"]) == ("credential_not_configured", "github", "github")
    assert body["manage_url"] == f"{_PUBLIC_URL}/connections/egress"
    assert body["connect_url"].startswith(f"{_PUBLIC_URL}/api/oauth/github/authorize?connect_token=")
    assert tls_upstream.hits == []


@pytest.mark.usefixtures("allow_loopback", "trust_upstream", "github_oauth_client")
@pytest.mark.asyncio
async def test_provider_outage_is_a_retryable_503_off_the_shared_executor(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled then failing token endpoint answers 503 to every waiter after one grant and never delays key lookups.

    OAuth lookups run on the broker's own OAuth pool, and requests within the backoff skip the endpoint entirely.
    """
    runtime_paths = _oauth_runtime(tmp_runtime_paths)
    keyed = {"rules": [{"host": "localhost", "path_prefix": "/echo", "auth": {"type": "bearer"}}]}
    github = {
        "rules": [{"host": "localhost", "path_prefix": "/ok", "auth": {"type": "bearer"}}],
        "oauth_provider": "github",
    }
    config = _config(keyed=keyed, github=github)
    target = _target()
    save_secret(manager, target, "keyed", "s3cret")
    _connect_github(manager, "@alice:example.org", "stale-oauth", expires_at=1.0)
    grant_started = threading.Event()
    outage = httpx.Response(503, json={"error": "temporarily_unavailable"})
    presented = serve_token_endpoint(
        monkeypatch,
        [DelayedTokenEndpointOutcome(2.0, outage)],
        request_received=grant_started,
    )
    lookup_threads: list[str] = []
    real_resolve_oauth_token = service.resolve_oauth_token

    def recording_resolve_oauth_token(**kwargs: Any) -> object:  # noqa: ANN401
        lookup_threads.append(threading.current_thread().name)
        return real_resolve_oauth_token(**kwargs)

    monkeypatch.setattr(service, "resolve_oauth_token", recording_resolve_oauth_token)
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        worker_env = execution_env_for_worker(runtime_paths, config=config, worker_target=target)
        assert apply_runner_ca_bundle(worker_env, tmp_path / "runner")
        async with httpx.AsyncClient(
            proxy=worker_env["HTTPS_PROXY"],
            verify=ssl.create_default_context(cafile=worker_env["SSL_CERT_FILE"]),
            trust_env=False,
        ) as client:
            stalled = [asyncio.create_task(client.get(tls_upstream.url("/ok"))) for _ in range(2)]
            assert await asyncio.to_thread(grant_started.wait, 5)
            keyed_response = await client.get(tls_upstream.url("/echo"))
            assert not any(task.done() for task in stalled)
            outage_responses = await asyncio.gather(*stalled)
            within_backoff = await client.get(tls_upstream.url("/ok"))
    assert keyed_response.json()["headers"]["authorization"] == ["Bearer s3cret"]
    for response in (*outage_responses, within_backoff):
        assert response.status_code == 503
        assert response.json() == {"error": "oauth_refresh_failed", "service": "github", "provider": "github"}
    assert presented == [_OAUTH_REFRESH_TOKEN]
    assert "/ok" not in tls_upstream.hits
    assert len(lookup_threads) == 3
    assert all(name.startswith("mindroom-egress-oauth") for name in lookup_threads)


@pytest.mark.usefixtures("allow_loopback", "trust_upstream")
@pytest.mark.asyncio
async def test_unconnectable_provider_gives_the_plain_missing_credential_body(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    tmp_path: Path,
) -> None:
    """Without an OAuth client to connect, the 403 names neither a provider nor a connect link, only the key page."""
    runtime_paths = _oauth_runtime(tmp_runtime_paths)
    config = _config(github=_GITHUB_OAUTH)
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=_target())
        response = await _get_through(env, tls_upstream.url("/echo"), tmp_path / "runner")
    assert response.status_code == 403
    assert response.json() == {
        "error": "credential_not_configured",
        "service": "github",
        "manage_url": f"{_PUBLIC_URL}/connections/egress",
    }


@pytest.mark.usefixtures("allow_loopback", "trust_upstream")
@pytest.mark.asyncio
async def test_shared_agent_scoped_provider_refusal_has_no_connect_link(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared-scope link skips sign-in and any process in the worker can read it, so a shared 403 only has the page."""
    drive = google_drive_oauth_provider()
    monkeypatch.setattr(oauth_source, "load_oauth_providers", lambda _config, _paths: {drive.id: drive})
    manager.save_credentials("google_drive_oauth_client", {"client_id": "drive-client", "client_secret": "secret"})
    runtime_paths = _oauth_runtime(tmp_runtime_paths)
    config = _config(drive={**_GITHUB, "oauth_provider": drive.id})
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        shared_env = execution_env_for_worker(runtime_paths, config=config, worker_target=_target(scope="shared"))
        private_env = execution_env_for_worker(runtime_paths, config=config, worker_target=_target())
        shared = await _get_through(shared_env, tls_upstream.url("/echo"), tmp_path / "runner")
        private = await _get_through(private_env, tls_upstream.url("/echo"), tmp_path / "runner")
    assert shared.status_code == 403
    assert shared.json() == {
        "error": "credential_not_configured",
        "service": "drive",
        "manage_url": f"{_PUBLIC_URL}/connections/egress",
    }
    assert private.status_code == 403
    assert private.json()["provider"] == drive.id
    assert "connect_token=" in private.json()["connect_url"]


@pytest.mark.usefixtures("allow_loopback", "trust_upstream")
@pytest.mark.asyncio
async def test_user_service_injects_its_owners_key_and_leaves_other_requesters_alone(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    tmp_path: Path,
) -> None:
    """Alice's own service intercepts its host for her workers only, with her key and her placeholders.

    Bob holds a key under the same name but has no such service, so his CONNECT to the host stays a blind tunnel
    and his worker gets no placeholder.
    """
    runtime_paths = tmp_runtime_paths(**_broker_env(_free_port()))
    config = _config()
    alice, bob = _target(), _target("@bob:example.org")
    mine = EgressService.model_validate(_GITHUB)
    save_user_service(manager, alice, "mine", mine, config_services=config.egress_broker.services)
    save_secret(manager, alice, "mine", "alice-key")
    save_secret(manager, bob, "mine", "bob-key")
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        alice_env = execution_env_for_worker(runtime_paths, config=config, worker_target=alice)
        bob_env = execution_env_for_worker(runtime_paths, config=config, worker_target=bob)
        alice_response = await _get_through(alice_env, tls_upstream.url("/echo"), tmp_path / "runner")
        bob_response = await _get_through(bob_env, tls_upstream.url("/echo"), tmp_path / "runner")
        audit = active_audit_log()
        assert audit is not None
        records = await audit_records(audit, 2)
    assert alice_env["GH_TOKEN"] == _PLACEHOLDER
    assert "GH_TOKEN" not in bob_env
    assert alice_response.json()["headers"]["authorization"] == ["Bearer alice-key"]
    assert bob_response.status_code == 200
    assert "authorization" not in bob_response.json()["headers"]
    assert sorted((record.requester_id, record.kind, record.service) for record in records) == [
        ("@alice:example.org", "request", "mine"),
        ("@bob:example.org", "tunnel", None),
    ]


@pytest.mark.usefixtures("allow_loopback", "trust_upstream")
@pytest.mark.asyncio
async def test_user_service_edits_apply_to_the_running_broker(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    tmp_path: Path,
) -> None:
    """Saving a service starts injection and deleting it stops it, from the next request on and without a restart."""
    runtime_paths = tmp_runtime_paths(**_broker_env(_free_port()))
    config = _config()
    target = _target()
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=target)
        before = await _get_through(env, tls_upstream.url("/echo"), tmp_path / "runner")
        mine = EgressService.model_validate(_GITHUB)
        save_user_service(manager, target, "mine", mine, config_services=config.egress_broker.services)
        # A new service starts without a key, so the key is set after it.
        save_secret(manager, target, "mine", "alice-key")
        saved = await _get_through(env, tls_upstream.url("/echo"), tmp_path / "runner")
        assert delete_user_service(manager, target, "mine")
        deleted = await _get_through(env, tls_upstream.url("/echo"), tmp_path / "runner")
    assert "authorization" not in before.json()["headers"]
    assert saved.json()["headers"]["authorization"] == ["Bearer alice-key"]
    assert "authorization" not in deleted.json()["headers"]


@pytest.mark.usefixtures("allow_loopback", "trust_upstream", "github_oauth_client")
@pytest.mark.asyncio
async def test_user_service_uses_only_its_scopes_oauth_connection(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    tmp_path: Path,
) -> None:
    """A user service naming an OAuth provider injects its owner's connected account and nobody else's."""
    runtime_paths = _oauth_runtime(tmp_runtime_paths)
    config = _config()
    alice, bob = _target(), _target("@bob:example.org")
    mine = EgressService.model_validate(_GITHUB_OAUTH)
    for target in (alice, bob):
        save_user_service(manager, target, "mine", mine, config_services=config.egress_broker.services)
    _connect_github(manager, "@alice:example.org", "alice-oauth")
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        alice_env = execution_env_for_worker(runtime_paths, config=config, worker_target=alice)
        bob_env = execution_env_for_worker(runtime_paths, config=config, worker_target=bob)
        alice_response = await _get_through(alice_env, tls_upstream.url("/echo"), tmp_path / "runner")
        bob_response = await _get_through(bob_env, tls_upstream.url("/echo"), tmp_path / "runner")
    assert alice_env["GH_TOKEN"] == _PLACEHOLDER
    assert alice_response.json()["headers"]["authorization"] == ["Bearer alice-oauth"]
    assert "GH_TOKEN" not in bob_env
    assert bob_response.status_code == 403
    assert (bob_response.json()["error"], bob_response.json()["provider"]) == ("credential_not_configured", "github")
    assert tls_upstream.hits == ["/echo"]


@pytest.mark.usefixtures("allow_loopback")
@pytest.mark.asyncio
async def test_user_service_cannot_open_a_host_under_deny(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    raw_proxy: Callable[[int, bytes], Awaitable[RawResponse]],
) -> None:
    """Under deny the running broker refuses a host only Alice's service names, as if her service did not exist."""
    port = _free_port()
    runtime_paths = tmp_runtime_paths(**_broker_env(port))
    config = Config(egress_broker={"unmatched_hosts": "deny", "services": {"openai": _OPENAI}})
    target = _target()
    mine = EgressService.model_validate(_GITHUB)
    save_user_service(manager, target, "mine", mine, config_services=config.egress_broker.services)
    save_secret(manager, target, "mine", "alice-key")
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=target)
        connect = connect_request(f"localhost:{tls_upstream.port}", authorization=proxy_authorization(_token(env)))
        response = await raw_proxy(port, connect)
    assert (response.status, response.json()) == (403, {"error": "host_not_allowed", "services": ["openai"]})
    assert tls_upstream.hits == []
