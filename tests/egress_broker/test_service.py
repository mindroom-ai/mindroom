"""Tests for the egress broker lifecycle in the primary and the env it gives worker calls."""

from __future__ import annotations

import asyncio
import functools
import socket
import ssl
import stat
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from structlog.testing import capture_logs

from mindroom.config.egress_broker import EgressService
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials.manager import CredentialsManager
from mindroom.egress_broker import oauth_source, service, user_services
from mindroom.egress_broker.audit import AuditLog
from mindroom.egress_broker.dial import DialPolicy
from mindroom.egress_broker.env import apply_runner_ca_bundle
from mindroom.egress_broker.secrets import delete_secret, save_egress_document, save_secret
from mindroom.egress_broker.service import (
    active_audit_log,
    active_ca_pem,
    execution_env_for_worker,
    manage_url,
    serve_egress_broker,
)
from mindroom.egress_broker.tokens import TokenSigner
from mindroom.egress_broker.user_services import (
    USER_SERVICES_CREDENTIAL_SERVICE,
    delete_user_service,
    save_user_service,
)
from mindroom.oauth.google_drive import google_drive_oauth_provider
from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, resolve_worker_target
from tests.oauth_test_utils import (
    DelayedTokenEndpointOutcome,
    rotated_token_response,
    serve_token_endpoint,
)

from .conftest import (
    OAUTH_REFRESH_TOKEN,
    account_store,
    allow_oauth_reuse,
    audit_records,
    connect_account,
    connect_request,
    free_port,
    host_not_allowed,
    proxy_authorization,
    worker_target,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.egress_broker.secrets import SecretResult
    from mindroom.oauth.credential_lifecycle import OAuthCredentialContext, OAuthCredentialsSnapshot
    from mindroom.tool_system.worker_routing import WorkerScope
    from mindroom.worker_computer.browser_proxy import BrowserEgress

    from .conftest import RawResponse, Upstream, UpstreamCA

type GetThrough = Callable[[dict[str, str]], Awaitable[httpx.Response]]

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
# Provider ids a user service may name, as the API reads them from the registry.
_PROVIDERS = frozenset({"github"})
_NO_GITHUB_KEY = "No github key is set for this agent. Ask the user to add one at manage_url, then retry."
_NO_SHARED_GITHUB_KEY = "No github key is set for this shared agent. Only an administrator can add one, at manage_url."


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


def _broker_env(port: int, **extra: str) -> dict[str, str]:
    return {
        "MINDROOM_EGRESS_BROKER_PORT": str(port),
        "MINDROOM_EGRESS_BROKER_HOST": "127.0.0.1",
        "MINDROOM_EGRESS_BROKER_URL": f"http://127.0.0.1:{port}",
        **extra,
    }


def _routable_target(
    requester_id: str = "@alice:example.org",
    agent_name: str = "code",
    *,
    scope: WorkerScope | None = "user_agent",
) -> ResolvedWorkerTarget:
    """Return `worker_target`, but with a worker key even for an unscoped call, so the broker can route it."""
    target = worker_target(requester_id, agent_name, scope=scope)
    # An unscoped call has no worker key; it is routed to a worker that its proxy token names.
    return replace(target, worker_key="v1:local:unscoped:code") if target.worker_key is None else target


def _config(**services: object) -> Config:
    return Config(egress_broker={"services": services})


def _token(env: dict[str, str]) -> str:
    username = urlsplit(env["HTTPS_PROXY"]).username
    assert username is not None
    return unquote(username)


def _broker_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == _THREAD_NAME]


@pytest.fixture
def get_through(
    allow_loopback: None,  # noqa: ARG001 - the broker must dial the loopback fake upstream
    trust_upstream: None,  # noqa: ARG001 - the broker must trust the fake upstream's CA
    tls_upstream: Upstream,
    tmp_path: Path,
) -> GetThrough:
    """Return ``get(env)``: ``GET /echo`` on the fake TLS upstream, sent as a worker's tool would with `env`.

    The request uses the env's proxy and the runner's trust bundle, so the service's broker has to intercept it.
    """

    async def get(env: dict[str, str]) -> httpx.Response:
        async with _worker_client(env, tmp_path / "runner") as client:
            return await client.get(tls_upstream.url("/echo"))

    return get


@asynccontextmanager
async def _worker_client(env: dict[str, str], runner_dir: Path) -> AsyncIterator[httpx.AsyncClient]:
    """Yield an HTTP client configured as a worker's tool is: the env's proxy and the runner's trust bundle."""
    worker_env = dict(env)
    assert apply_runner_ca_bundle(worker_env, runner_dir)
    async with httpx.AsyncClient(
        proxy=worker_env["HTTPS_PROXY"],
        verify=ssl.create_default_context(cafile=worker_env["SSL_CERT_FILE"]),
        trust_env=False,
    ) as client:
        yield client


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
            worker_target=_routable_target(),
        )
        assert env == {}
    assert not (tmp_path / "mindroom_data" / "egress_broker").exists()


@pytest.mark.asyncio
async def test_runtime_credentials_manager_is_built_only_when_the_broker_starts(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Without a port no credentials manager is built for the broker; with one it uses the runtime's own."""
    built: list[RuntimePaths] = []

    def runtime_manager(runtime_paths: RuntimePaths) -> CredentialsManager:
        built.append(runtime_paths)
        return CredentialsManager(tmp_path / "mindroom_data" / "credentials")

    monkeypatch.setattr(service, "get_runtime_credentials_manager", runtime_manager)
    async with serve_egress_broker(tmp_runtime_paths(), config_provider=lambda: None):
        assert built == []

    runtime_paths = tmp_runtime_paths(**_broker_env(free_port()))
    async with serve_egress_broker(runtime_paths, config_provider=lambda: None):
        assert built == [runtime_paths]


@pytest.mark.asyncio
async def test_requires_url_when_port_set(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
) -> None:
    """A port without the worker-facing URL refuses to start."""
    runtime_paths = tmp_runtime_paths(MINDROOM_EGRESS_BROKER_PORT=str(free_port()))
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
    runtime_paths = tmp_runtime_paths(**{**_broker_env(free_port()), name: value})
    with pytest.raises(ValueError, match=name):
        async with serve_egress_broker(runtime_paths, config_provider=lambda: None, credentials_manager=manager):
            pytest.fail("the broker must not start")
    assert _broker_threads() == []
    assert not (tmp_path / "mindroom_data" / "egress_broker").exists()


@pytest.mark.asyncio
async def test_end_to_end_inject_through_running_service(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    get_through: GetThrough,
) -> None:
    """A worker using only the env from execution_env_for_worker reaches the upstream with the scope's secret."""
    runtime_paths = tmp_runtime_paths(**_broker_env(free_port()))
    config = _config(github=_GITHUB)
    target = _routable_target()
    save_secret(manager, target, "github", "s3cret")
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=target)
        assert env["GH_TOKEN"] == _PLACEHOLDER
        assert "s3cret" not in str(env)
        response = await get_through(env)
        assert response.status_code == 200
        assert response.json()["headers"]["authorization"] == ["Bearer s3cret"]
        audit = active_audit_log()
        assert audit is not None
        [record] = await audit_records(audit, 1)
        assert (record.host, record.service, record.agent_name) == ("localhost", "github", "code")


@pytest.mark.asyncio
async def test_missing_secret_reports_manage_url(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    get_through: GetThrough,
) -> None:
    """A brokered host without a secret in scope answers 403 with the Connections link, without reaching upstream."""
    runtime_paths = tmp_runtime_paths(
        **_broker_env(free_port()),
        MINDROOM_PUBLIC_URL="https://chat.example.org/",
        MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED="true",
    )
    config = _config(github=_GITHUB)
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(
            runtime_paths,
            config=config,
            worker_target=_routable_target(),
        )
        response = await get_through(env)
    assert response.status_code == 403
    assert response.json() == {
        "error": "credential_not_configured",
        "service": "github",
        "manage_url": "https://chat.example.org/connections/egress",
        "message": _NO_GITHUB_KEY,
    }
    assert tls_upstream.hits == []


@pytest.mark.asyncio
async def test_unreadable_config_tunnels_without_injection(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    get_through: GetThrough,
) -> None:
    """When the config provider raises, the broker has no rules: traffic is tunnelled and nothing is injected."""
    runtime_paths = tmp_runtime_paths(**_broker_env(free_port()))
    config = _config(github=_GITHUB)
    target = _routable_target()
    save_secret(manager, target, "github", "s3cret")

    def broken() -> Config | None:
        msg = "config is mid-reload"
        raise RuntimeError(msg)

    async with serve_egress_broker(runtime_paths, config_provider=broken, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=target)
        response = await get_through(env)
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
    port = free_port()
    runtime_paths = tmp_runtime_paths(**_broker_env(port))
    config = Config(egress_broker={"unmatched_hosts": "deny", "services": {"openai": _OPENAI}})
    state = {"mode": "good"}

    def provider() -> Config | None:
        if state["mode"] == "raise":
            msg = "the saved config is invalid"
            raise RuntimeError(msg)
        return config if state["mode"] == "good" else None

    async with serve_egress_broker(runtime_paths, config_provider=provider, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=_routable_target())
        connect = connect_request(f"localhost:{tls_upstream.port}", authorization=proxy_authorization(_token(env)))
        responses = []
        for mode in ("good", "raise", "none"):
            state["mode"] = mode
            responses.append(await raw_proxy(port, connect))
    assert [(response.status, response.json()) for response in responses] == [
        (403, host_not_allowed("openai")),
    ] * 3
    assert tls_upstream.hits == []


@pytest.mark.asyncio
async def test_placeholder_env_only_when_secret_configured(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
) -> None:
    """Placeholders come only from services with a secret in this worker's scope; the proxy env is always set."""
    runtime_paths = tmp_runtime_paths(**_broker_env(free_port()))
    config = _config(github=_GITHUB, openai=_OPENAI)
    alice = _routable_target("@alice:example.org")
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
            worker_target=_routable_target("@bob:example.org"),
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
    runtime_paths = tmp_runtime_paths(**_broker_env(free_port()))
    config = _config(github=_GITHUB, openai=_OPENAI)
    target = _routable_target()
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
    runtime_paths = tmp_runtime_paths(**_broker_env(free_port()))
    config = _config(github=_GITHUB)
    no_identity = resolve_worker_target("user_agent", "code", None)
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        assert execution_env_for_worker(runtime_paths, config=None, worker_target=_routable_target()) == {}
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
    runtime_paths = tmp_runtime_paths(**_broker_env(free_port()))
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
        **_broker_env(free_port()),
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
        **_broker_env(free_port()),
        MINDROOM_EGRESS_BROKER_TOKEN_TTL_SECONDS="600",  # noqa: S106 - a lifetime, not a secret
    )
    config = _config(github=_GITHUB)
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(
            runtime_paths,
            config=config,
            worker_target=_routable_target(),
        )
    signer = TokenSigner.load_or_create(tmp_path / "mindroom_data" / "egress_broker" / "token.key")
    now = time.time()
    claims = signer.verify(_token(env), now=now + 590)
    assert claims is not None
    assert claims.worker_key == _routable_target().worker_key
    assert signer.verify(_token(env), now=now + 610) is None


@pytest.mark.asyncio
async def test_thread_stops_on_exit(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
) -> None:
    """The broker runs on its own named thread, which is gone and has released the port after exit."""
    port = free_port()
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
    runtime_paths = tmp_runtime_paths(**_broker_env(free_port()))

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
    runtime_paths = tmp_runtime_paths(**_broker_env(free_port()), **env)
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
    runtime_paths = tmp_runtime_paths(**_broker_env(free_port()), **env)
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
    runtime_paths = tmp_runtime_paths(**_broker_env(free_port()), **env)
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
            {
                **_broker_env(8768),
                "MINDROOM_PUBLIC_URL": "https://chat.example.org/",
                "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
                "MINDROOM_CONNECTIONS_ALLOWED_ORIGINS": "[]",
            },
            "https://chat.example.org/connections/egress",
        ),
        (
            {**_broker_env(8768), "MINDROOM_PUBLIC_URL": "https://chat.example.org/"},
            "https://chat.example.org/connections/egress",
        ),
        (
            {
                **_broker_env(8768),
                "MINDROOM_PUBLIC_URL": "https://chat.example.org/",
                "MINDROOM_CONNECTIONS_ALLOWED_ORIGINS": "[]",
            },
            "https://chat.example.org/",
        ),
        ({"MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true"}, None),
    ],
)
def test_manage_url(tmp_runtime_paths: Callable[..., RuntimePaths], env: dict[str, str], expected: str | None) -> None:
    """The manage link is the personal egress page where it is served (Matrix sign-in or signed upstream), else the dashboard."""
    assert manage_url(tmp_runtime_paths(**env)) == expected


@pytest.fixture
def github_oauth_client(manager: CredentialsManager) -> None:
    """Configure the GitHub OAuth app, so GitHub accounts can be connected and refreshed."""
    manager.save_credentials("github_oauth_client", {"client_id": "github-client-id", "client_secret": "gh-secret"})


def _no_key_refusal(message: str, *, service: str = "github") -> dict[str, str]:
    """Return the 403 body for a service without a key whose refusal names only the Connections page."""
    return {
        "error": "credential_not_configured",
        "service": service,
        "manage_url": f"{_PUBLIC_URL}/connections/egress",
        "message": message,
    }


def _save_mine(
    manager: CredentialsManager,
    target: ResolvedWorkerTarget,
    config: Config,
    service: dict[str, object],
) -> None:
    """Save `service` as the scope's own user service ``mine``, checked against `config`'s services as the API does."""
    save_user_service(
        manager,
        target,
        "mine",
        EgressService.model_validate(service),
        config_services=config.egress_broker.services,
        oauth_providers=_PROVIDERS,
    )


def _oauth_runtime(tmp_runtime_paths: Callable[..., RuntimePaths], backend: str = "docker") -> RuntimePaths:
    """Return a runtime on `backend`: personal accounts need a dedicated one (the default) to count as private."""
    return tmp_runtime_paths(
        **_broker_env(free_port()),
        MINDROOM_PUBLIC_URL=_PUBLIC_URL,
        MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED="true",
        MINDROOM_WORKER_BACKEND=backend,
    )


@pytest.mark.usefixtures("github_oauth_client")
@pytest.mark.asyncio
async def test_stored_key_wins_over_oauth_and_its_removal_falls_back_without_restart(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    get_through: GetThrough,
) -> None:
    """With both sources the stored key is injected; once it is removed the same token gets the OAuth token."""
    runtime_paths = _oauth_runtime(tmp_runtime_paths)
    config = _config(github=_GITHUB_OAUTH)
    target = _routable_target()
    save_secret(manager, target, "github", "s3cret")
    connect_account(manager, account_store("@alice:example.org"), "alice-oauth")
    allow_oauth_reuse(manager, runtime_paths, account_store("@alice:example.org"))
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=target)
        with_key = await get_through(env)
        delete_secret(manager, target, "github")
        with_oauth = await get_through(env)
    assert with_key.json()["headers"]["authorization"] == ["Bearer s3cret"]
    assert with_oauth.json()["headers"]["authorization"] == ["Bearer alice-oauth"]


@pytest.mark.usefixtures("github_oauth_client")
@pytest.mark.asyncio
async def test_placeholder_env_when_only_oauth_is_connected(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connected OAuth account Agent Keys may use gives the scope its placeholders; the token never enters the env.

    Until the account is allowed for the service it serves only its tools: there is no placeholder, and working that
    out never reads the connection.
    """
    runtime_paths = _oauth_runtime(tmp_runtime_paths)
    config = _config(github=_GITHUB_OAUTH)
    connect_account(manager, account_store("@alice:example.org"), "alice-oauth")
    connect_account(manager, account_store("@bob:example.org"), "bob-oauth")
    reads: list[str | None] = []
    real_load = oauth_source.load_oauth_credentials_snapshot_sync

    def recording_load(context: OAuthCredentialContext) -> OAuthCredentialsSnapshot:
        identity = None if context.worker_target is None else context.worker_target.execution_identity
        reads.append(None if identity is None else identity.requester_id)
        return real_load(context)

    monkeypatch.setattr(oauth_source, "load_oauth_credentials_snapshot_sync", recording_load)
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        before_env = execution_env_for_worker(runtime_paths, config=config, worker_target=_routable_target())
        reads_before = list(reads)
        allow_oauth_reuse(manager, runtime_paths, account_store("@alice:example.org"))
        alice_env = execution_env_for_worker(runtime_paths, config=config, worker_target=_routable_target())
        bob_env = execution_env_for_worker(
            runtime_paths,
            config=config,
            worker_target=_routable_target("@bob:example.org"),
        )
    assert "GH_TOKEN" not in before_env
    assert reads_before == []
    assert alice_env["GH_TOKEN"] == _PLACEHOLDER
    assert "alice-oauth" not in str(alice_env)
    assert "GH_TOKEN" not in bob_env
    assert reads == ["@alice:example.org"]


@pytest.mark.usefixtures("github_oauth_client")
@pytest.mark.asyncio
async def test_a_connected_account_is_refused_until_agent_keys_may_use_it(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    get_through: GetThrough,
) -> None:
    """A connected account nobody allowed for the service gets a 403 naming it, with no token and no connect link.

    Once the account is allowed, the same proxy token gets it injected without a restart.
    """
    runtime_paths = _oauth_runtime(tmp_runtime_paths)
    config = _config(github=_GITHUB_OAUTH)
    connect_account(manager, account_store("@alice:example.org"), "alice-oauth")
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=_routable_target())
        refused = await get_through(env)
        allow_oauth_reuse(manager, runtime_paths, account_store("@alice:example.org"))
        allowed = await get_through(env)
    assert refused.status_code == 403
    assert refused.json() == {
        "error": "credential_not_configured",
        "service": "github",
        "provider": "github",
        "oauth_reuse": "off",
        "manage_url": f"{_PUBLIC_URL}/connections/egress",
        "message": "No github key is set for this agent, and Agent Keys may not use a connected github account. "
        "Ask the user to add a key or allow that account at manage_url, then retry.",
    }
    assert "alice-oauth" not in refused.text
    assert allowed.json()["headers"]["authorization"] == ["Bearer alice-oauth"]
    assert tls_upstream.hits == ["/echo"]


@pytest.mark.usefixtures("github_oauth_client")
@pytest.mark.parametrize("scope", ["shared", None], ids=["shared", "unscoped"])
@pytest.mark.asyncio
async def test_requester_scoped_oauth_is_not_used_where_requesters_share_a_worker(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    scope: WorkerScope | None,
    get_through: GetThrough,
) -> None:
    """On a shared or unscoped worker a connected GitHub account is never injected, placeheld, or offered.

    Another requester's later command could read the proxy token from the shared sandbox, so the 403 names only the
    key page: no provider and no connect link.
    """
    runtime_paths = _oauth_runtime(tmp_runtime_paths)
    config = _config(github=_GITHUB_OAUTH)
    connect_account(manager, account_store("@alice:example.org"), "alice-oauth")
    target = _routable_target(scope=scope)
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=target)
        response = await get_through(env)
    assert "GH_TOKEN" not in env
    assert response.status_code == 403
    assert response.json() == _no_key_refusal(_NO_SHARED_GITHUB_KEY)
    assert tls_upstream.hits == []


@pytest.mark.parametrize(
    ("scope", "managers", "message"),
    [
        (
            "shared",
            ["@alice:example.org", "@bob:example.org"],
            "No github key is set for this shared agent. "
            "Only its credential managers (@alice:example.org, @bob:example.org) can add one, at manage_url.",
        ),
        (
            None,
            [],
            "No github key is set for this shared agent. Only an administrator can add one, at manage_url.",
        ),
        (
            "user_agent",
            ["@bob:example.org"],
            "No github key is set for this agent. Ask the user to add one at manage_url, then retry.",
        ),
    ],
    ids=["shared", "unscoped", "personal"],
)
@pytest.mark.asyncio
async def test_missing_key_message_names_the_calling_agents_credential_managers(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    scope: WorkerScope | None,
    managers: list[str],
    message: str,
    get_through: GetThrough,
) -> None:
    """The refusal reads the calling agent's credential managers from the config, and names them only when shared."""
    runtime_paths = _oauth_runtime(tmp_runtime_paths)
    config = Config(
        agents={"code": {"display_name": "Code", "credential_managers": managers}},
        egress_broker={"services": {"github": _GITHUB}},
    )
    target = _routable_target(scope=scope)
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=target)
        response = await get_through(env)
    assert response.status_code == 403
    assert response.json() == _no_key_refusal(message)


_NO_PERSONAL_PAGE_KEY = (
    "No github key: each user sets this agent's keys on the Connections page, "
    "which nobody can sign in to here. "
    "An administrator can enable Connections sign-in or make the agent shared."
)
_NO_CHAT_SIGN_IN = {"MINDROOM_CONNECTIONS_ALLOWED_ORIGINS": "[]"}
_SIGNED_UPSTREAM = {"MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true"}


@pytest.mark.parametrize(
    ("scope", "upstream", "link", "message"),
    [
        ("user_agent", _SIGNED_UPSTREAM, f"{_PUBLIC_URL}/connections/egress", _NO_GITHUB_KEY),
        ("user", {}, f"{_PUBLIC_URL}/connections/egress", _NO_GITHUB_KEY),
        ("user", _NO_CHAT_SIGN_IN, None, _NO_PERSONAL_PAGE_KEY),
        ("shared", {}, f"{_PUBLIC_URL}/", _NO_SHARED_GITHUB_KEY),
        (None, {}, f"{_PUBLIC_URL}/", _NO_SHARED_GITHUB_KEY),
    ],
    ids=[
        "personal-page",
        "personal-page-from-chat",
        "personal-without-page",
        "shared-dashboard",
        "unscoped-dashboard",
    ],
)
@pytest.mark.asyncio
async def test_missing_key_refusal_links_only_a_page_that_can_set_the_key(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    scope: WorkerScope | None,
    upstream: dict[str, str],
    link: str | None,
    message: str,
    get_through: GetThrough,
) -> None:
    """A personal scope's key is set only on the personal page, served behind signed trusted upstream auth.

    Without it the refusal links no page and says what an administrator can do; shared and unscoped keys are set on
    the dashboard either way. The reason phrase, cut at 200 characters, carries each message whole.
    """
    runtime_paths = tmp_runtime_paths(**_broker_env(free_port()), MINDROOM_PUBLIC_URL=_PUBLIC_URL, **upstream)
    config = _config(github=_GITHUB)
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=_routable_target(scope=scope))
        response = await get_through(env)
    expected = {"error": "credential_not_configured", "service": "github", "message": message}
    assert response.status_code == 403
    assert response.json() == expected if link is None else {**expected, "manage_url": link}
    assert response.reason_phrase == message


@pytest.mark.usefixtures("github_oauth_client")
@pytest.mark.asyncio
async def test_requester_scoped_oauth_on_a_shared_agent_injects_the_callers_token_with_the_opt_in(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    get_through: GetThrough,
) -> None:
    """With `oauth_on_shared_workers`, two requesters on one shared agent each get their own GitHub token."""
    runtime_paths = _oauth_runtime(tmp_runtime_paths)
    config = _config(github={**_GITHUB_OAUTH, "oauth_on_shared_workers": True})
    connect_account(manager, account_store("@alice:example.org"), "alice-oauth")
    connect_account(manager, account_store("@bob:example.org"), "bob-oauth")
    for requester in ("@alice:example.org", "@bob:example.org"):
        allow_oauth_reuse(manager, runtime_paths, account_store(requester))
    alice = _routable_target(scope="shared")
    bob = _routable_target("@bob:example.org", scope="shared")
    assert alice.worker_key == bob.worker_key
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        alice_env = execution_env_for_worker(runtime_paths, config=config, worker_target=alice)
        bob_env = execution_env_for_worker(runtime_paths, config=config, worker_target=bob)
        alice_response = await get_through(alice_env)
        bob_response = await get_through(bob_env)
    assert alice_env["GH_TOKEN"] == _PLACEHOLDER
    assert alice_response.json()["headers"]["authorization"] == ["Bearer alice-oauth"]
    assert bob_response.json()["headers"]["authorization"] == ["Bearer bob-oauth"]


@pytest.mark.usefixtures("github_oauth_client")
@pytest.mark.asyncio
async def test_requester_scoped_oauth_is_not_used_on_the_static_runner_without_the_opt_in(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    get_through: GetThrough,
) -> None:
    """Every call shares the static runner's process, so even a `user_agent` call gets no GitHub account there."""
    runtime_paths = _oauth_runtime(tmp_runtime_paths, backend="static_runner")
    config = _config(github=_GITHUB_OAUTH)
    connect_account(manager, account_store("@alice:example.org"), "alice-oauth")
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=_routable_target())
        response = await get_through(env)
    assert "GH_TOKEN" not in env
    assert response.status_code == 403
    assert response.json() == _no_key_refusal(_NO_GITHUB_KEY)
    assert tls_upstream.hits == []


@pytest.mark.usefixtures("github_oauth_client")
@pytest.mark.asyncio
async def test_requester_scoped_oauth_is_used_on_the_static_runner_with_the_opt_in(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    get_through: GetThrough,
) -> None:
    """With `oauth_on_shared_workers`, a `user_agent` call on the static runner gets the requester's own token."""
    runtime_paths = _oauth_runtime(tmp_runtime_paths, backend="static_runner")
    config = _config(github={**_GITHUB_OAUTH, "oauth_on_shared_workers": True})
    connect_account(manager, account_store("@alice:example.org"), "alice-oauth")
    allow_oauth_reuse(manager, runtime_paths, account_store("@alice:example.org"))
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=_routable_target())
        response = await get_through(env)
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
    connect_account(manager, account_store("@alice:example.org"), "stale-oauth", expires_at=1.0)
    allow_oauth_reuse(manager, runtime_paths, account_store("@alice:example.org"))
    presented = serve_token_endpoint(monkeypatch, [DelayedTokenEndpointOutcome(0.3, rotated_token_response())])
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=_routable_target())
        async with _worker_client(env, tmp_path / "runner") as client:
            responses = await asyncio.gather(*(client.get(tls_upstream.url("/echo")) for _ in range(6)))
    assert [response.json()["headers"]["authorization"] for response in responses] == [
        ["Bearer rotated-access-token"],
    ] * 6
    assert presented == [OAUTH_REFRESH_TOKEN]


@pytest.mark.usefixtures("github_oauth_client")
@pytest.mark.asyncio
async def test_revoked_grant_returns_oauth_connection_required(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    monkeypatch: pytest.MonkeyPatch,
    get_through: GetThrough,
) -> None:
    """A refresh the provider rejects answers 403 with a reconnect link; no token reaches the body or the logs."""
    runtime_paths = _oauth_runtime(tmp_runtime_paths)
    config = _config(github=_GITHUB_OAUTH)
    connect_account(manager, account_store("@alice:example.org"), "stale-oauth", expires_at=1.0)
    allow_oauth_reuse(manager, runtime_paths, account_store("@alice:example.org"))
    serve_token_endpoint(monkeypatch, [httpx.Response(400, json={"error": "invalid_grant"})])
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=_routable_target())
        with capture_logs() as logs:
            response = await get_through(env)
    body = response.json()
    assert response.status_code == 403
    assert body.keys() == {"error", "service", "provider", "connect_url", "manage_url", "message"}
    assert (body["error"], body["service"], body["provider"]) == ("oauth_connection_required", "github", "github")
    assert body["manage_url"] == f"{_PUBLIC_URL}/connections/egress"
    assert body["message"] == (
        "The github account for github must be connected again. Ask the user to reconnect at connect_url or manage_url."
    )
    assert body["connect_url"].startswith(f"{_PUBLIC_URL}/api/oauth/github/authorize?connect_token=")
    assert tls_upstream.hits == []
    for leaked in ("stale-oauth", OAUTH_REFRESH_TOKEN):
        assert leaked not in response.text
    for leaked in ("stale-oauth", OAUTH_REFRESH_TOKEN, urlsplit(body["connect_url"]).query):
        assert leaked not in repr(logs)
    assert any(entry["event"] == "egress_broker_oauth_token_unavailable" for entry in logs)


@pytest.mark.usefixtures("github_oauth_client")
@pytest.mark.asyncio
async def test_missing_connection_returns_connect_url(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    get_through: GetThrough,
) -> None:
    """With neither a key nor a connection, the 403 offers both the key page and a link to connect the account."""
    runtime_paths = _oauth_runtime(tmp_runtime_paths)
    config = _config(github=_GITHUB_OAUTH)
    allow_oauth_reuse(manager, runtime_paths, account_store("@alice:example.org"))
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=_routable_target())
        response = await get_through(env)
    body = response.json()
    assert response.status_code == 403
    assert body.keys() == {"error", "service", "manage_url", "provider", "connect_url", "message"}
    assert (body["error"], body["service"], body["provider"]) == ("credential_not_configured", "github", "github")
    assert body["message"] == (
        "No github credential is set for this agent. "
        "Ask the user to connect it at connect_url or add a key at manage_url, then retry."
    )
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
    target = _routable_target()
    save_secret(manager, target, "keyed", "s3cret")
    connect_account(manager, account_store("@alice:example.org"), "stale-oauth", expires_at=1.0)
    allow_oauth_reuse(manager, runtime_paths, account_store("@alice:example.org"))
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
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=target)
        async with _worker_client(env, tmp_path / "runner") as client:
            stalled = [asyncio.create_task(client.get(tls_upstream.url("/ok"))) for _ in range(2)]
            assert await asyncio.to_thread(grant_started.wait, 5)
            keyed_response = await client.get(tls_upstream.url("/echo"))
            assert not any(task.done() for task in stalled)
            outage_responses = await asyncio.gather(*stalled)
            within_backoff = await client.get(tls_upstream.url("/ok"))
    assert keyed_response.json()["headers"]["authorization"] == ["Bearer s3cret"]
    for response in (*outage_responses, within_backoff):
        assert response.status_code == 503
        assert response.json() == {
            "error": "oauth_refresh_failed",
            "service": "github",
            "provider": "github",
            "manage_url": f"{_PUBLIC_URL}/connections/egress",
            "message": "github did not refresh the account token. "
            "Retry in a minute; if it keeps failing, ask the user to reconnect at manage_url.",
        }
    assert presented == [OAUTH_REFRESH_TOKEN]
    assert "/ok" not in tls_upstream.hits
    assert len(lookup_threads) == 3
    assert all(name.startswith("mindroom-egress-oauth") for name in lookup_threads)


@pytest.mark.asyncio
async def test_unconnectable_provider_gives_the_plain_missing_credential_body(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    get_through: GetThrough,
) -> None:
    """Without an OAuth client to connect, the 403 names neither a provider nor a connect link, only the key page."""
    runtime_paths = _oauth_runtime(tmp_runtime_paths)
    config = _config(github=_GITHUB_OAUTH)
    allow_oauth_reuse(manager, runtime_paths, account_store("@alice:example.org"))
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=_routable_target())
        response = await get_through(env)
    assert response.status_code == 403
    assert response.json() == _no_key_refusal(_NO_GITHUB_KEY)


@pytest.mark.asyncio
async def test_shared_agent_scoped_provider_refusal_has_no_connect_link(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    monkeypatch: pytest.MonkeyPatch,
    get_through: GetThrough,
) -> None:
    """A shared-scope link skips sign-in and any process in the worker can read it, so a shared 403 only has the page."""
    drive = google_drive_oauth_provider()
    monkeypatch.setattr(oauth_source, "load_oauth_providers", lambda _config, _paths: {drive.id: drive})
    manager.save_credentials("google_drive_oauth_client", {"client_id": "drive-client", "client_secret": "secret"})
    runtime_paths = _oauth_runtime(tmp_runtime_paths)
    config = _config(drive={**_GITHUB, "oauth_provider": drive.id})
    for scope in ("shared", "user_agent"):
        allow_oauth_reuse(manager, runtime_paths, _routable_target(scope=scope), "drive", provider=drive)
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        shared_env = execution_env_for_worker(
            runtime_paths,
            config=config,
            worker_target=_routable_target(scope="shared"),
        )
        private_env = execution_env_for_worker(runtime_paths, config=config, worker_target=_routable_target())
        shared = await get_through(shared_env)
        private = await get_through(private_env)
    assert shared.status_code == 403
    assert shared.json() == _no_key_refusal(
        "No drive key is set for this shared agent. Only an administrator can add one, at manage_url.",
        service="drive",
    )
    assert private.status_code == 403
    assert private.json()["provider"] == drive.id
    assert "connect_token=" in private.json()["connect_url"]


@pytest.mark.asyncio
async def test_user_service_injects_its_owners_key_and_leaves_other_requesters_alone(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    get_through: GetThrough,
) -> None:
    """Alice's own service intercepts its host for her workers only, with her key and her placeholders.

    Bob holds a key under the same name but has no such service, so his CONNECT to the host stays a blind tunnel
    and his worker gets no placeholder.
    """
    runtime_paths = tmp_runtime_paths(**_broker_env(free_port()))
    config = _config()
    alice, bob = _routable_target(), _routable_target("@bob:example.org")
    _save_mine(manager, alice, config, _GITHUB)
    save_secret(manager, alice, "mine", "alice-key")
    save_secret(manager, bob, "mine", "bob-key")
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        alice_env = execution_env_for_worker(runtime_paths, config=config, worker_target=alice)
        bob_env = execution_env_for_worker(runtime_paths, config=config, worker_target=bob)
        alice_response = await get_through(alice_env)
        bob_response = await get_through(bob_env)
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


@pytest.mark.asyncio
async def test_user_service_edits_apply_to_the_running_broker(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    get_through: GetThrough,
) -> None:
    """Saving a service starts injection and deleting it stops it, from the next request on and without a restart."""
    runtime_paths = tmp_runtime_paths(**_broker_env(free_port()))
    config = _config()
    target = _routable_target()
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=target)
        before = await get_through(env)
        _save_mine(manager, target, config, _GITHUB)
        # A new service starts without a key, so the key is set after it.
        save_secret(manager, target, "mine", "alice-key")
        saved = await get_through(env)
        assert delete_user_service(manager, target, "mine", config_services=config.egress_broker.services)
        deleted = await get_through(env)
    assert "authorization" not in before.json()["headers"]
    assert saved.json()["headers"]["authorization"] == ["Bearer alice-key"]
    assert "authorization" not in deleted.json()["headers"]


@dataclass
class _PausedLookup:
    """Holds a broker's secret lookups, after routing and before resolution, until `resume` is set."""

    reached: threading.Event = field(default_factory=threading.Event)
    resume: threading.Event = field(default_factory=threading.Event)

    async def get_after(
        self,
        get_through: GetThrough,
        env: dict[str, str],
        write: Callable[[], None],
    ) -> httpx.Response:
        """Send ``GET /echo`` with `env`, and run `write` after the broker routed it but before it resolves the secret."""
        request = asyncio.create_task(get_through(env))
        assert await asyncio.to_thread(self.reached.wait, 10)
        write()
        self.resume.set()
        return await request


@pytest.fixture
def paused_lookup(monkeypatch: pytest.MonkeyPatch) -> _PausedLookup:
    """Pause the secret lookups of brokers started after this; the broker loop waits in a thread, so it stays live."""
    pause = _PausedLookup()
    resolve = service._resolve_secret

    async def paused(*args: Any, **kwargs: Any) -> SecretResult:  # noqa: ANN401 - forwards the resolver's arguments
        pause.reached.set()
        await asyncio.to_thread(pause.resume.wait, 10)
        return await resolve(*args, **kwargs)

    monkeypatch.setattr(service, "_resolve_secret", paused)
    return pause


@pytest.fixture
def paused_key_read(monkeypatch: pytest.MonkeyPatch) -> _PausedLookup:
    """Pause the key reads of brokers started after this, inside the routed-definition check, until `resume` is set."""
    pause = _PausedLookup()
    load = service.load_secret

    def paused(*args: Any, **kwargs: Any) -> str | None:  # noqa: ANN401 - forwards the reader's arguments
        pause.reached.set()
        pause.resume.wait(10)
        return load(*args, **kwargs)

    monkeypatch.setattr(service, "load_secret", paused)
    return pause


def _save_team(manager: CredentialsManager, target: ResolvedWorkerTarget, service: dict[str, object]) -> None:
    """Save `service` as the shared scope's own user service ``team``, as a credential manager does through the API."""
    save_user_service(
        manager,
        target,
        "team",
        EgressService.model_validate(service),
        config_services={},
        oauth_providers=_PROVIDERS,
    )


@pytest.mark.asyncio
async def test_a_key_saved_for_new_rules_never_reaches_the_host_a_pending_request_was_routed_to(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    get_through: GetThrough,
    paused_lookup: _PausedLookup,
) -> None:
    """Worker code calling a shared service's host while a manager moves it to OpenAI and sets its key gets a 502.

    The key belongs to the new rules, so the request routed by the old ones is refused without reaching its host, and
    worker code cannot catch the key in the moment the service changes.
    """
    runtime_paths = tmp_runtime_paths(**_broker_env(free_port()))
    config = _config()
    target = _routable_target(scope="shared")
    _save_team(manager, target, _GITHUB)

    def move_to_openai() -> None:
        _save_team(manager, target, _OPENAI)
        save_secret(manager, target, "team", "openai-key")

    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=target)
        with capture_logs() as logs:
            response = await paused_lookup.get_after(get_through, env, move_to_openai)
        audit = active_audit_log()
        assert audit is not None
        [record] = await audit_records(audit, 1)
    # The fake upstream echoes the request headers, so a sent key would show here.
    assert "openai-key" not in response.text
    assert tls_upstream.hits == []
    assert response.status_code == 502
    assert response.json()["error"] == "broker_error"
    # Operators can tell the change from a store failure, and the event names only the service.
    assert {"event": "egress_broker_service_changed", "log_level": "info", "service": "team"} in logs
    assert not [log for log in logs if log["event"] == "egress_broker_secret_lookup_failed"]
    assert "openai-key" not in repr(logs)
    assert (record.kind, record.service, record.status, record.code) == ("request", "team", 502, "broker_error")


@pytest.mark.asyncio
async def test_a_key_saved_after_a_config_change_never_reaches_the_host_a_pending_request_was_routed_to(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    get_through: GetThrough,
    paused_key_read: _PausedLookup,
) -> None:
    """Worker code calling a config service's host while an administrator moves it to OpenAI and sets its key gets a 502.

    The lookup is already reading the store when the config changes, so only the check after the read can tell.
    """
    runtime_paths = tmp_runtime_paths(**_broker_env(free_port()))
    configs = [_config(vendor=_GITHUB)]
    target = _routable_target(scope="shared")

    def move_to_openai() -> None:
        configs.append(_config(vendor=_OPENAI))
        save_secret(manager, target, "vendor", "openai-key")

    async with serve_egress_broker(runtime_paths, config_provider=lambda: configs[-1], credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=configs[0], worker_target=target)
        response = await paused_key_read.get_after(get_through, env, move_to_openai)
    # The fake upstream echoes the request headers, so a sent key would show here.
    assert "openai-key" not in response.text
    assert tls_upstream.hits == []
    assert response.status_code == 502
    assert response.json()["error"] == "broker_error"


@pytest.mark.usefixtures("github_oauth_client")
@pytest.mark.asyncio
async def test_an_account_a_service_gains_never_reaches_the_host_a_pending_request_was_routed_to(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    get_through: GetThrough,
    paused_lookup: _PausedLookup,
) -> None:
    """Worker code calling a personal service's host while its user replaces it with the GitHub preset gets a 502.

    The connected GitHub account may only go to GitHub's hosts, so the request routed by the old rule gets no token,
    and worker code cannot catch the account in the moment the service changes.
    """
    runtime_paths = _oauth_runtime(tmp_runtime_paths)
    config = _config()
    target = _routable_target()
    _save_mine(manager, target, config, {"rules": [{"host": "localhost", "auth": {"type": "bearer"}}]})
    connect_account(manager, account_store("@alice:example.org"), "alice-oauth")
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=target)
        response = await paused_lookup.get_after(
            get_through,
            env,
            lambda: _save_mine(manager, target, config, {"preset": "github"}),
        )
    assert "alice-oauth" not in response.text
    assert tls_upstream.hits == []
    assert response.status_code == 502
    assert response.json()["error"] == "broker_error"


@pytest.mark.asyncio
async def test_a_pending_request_keeps_the_key_of_a_service_saved_again_unchanged(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    get_through: GetThrough,
    paused_lookup: _PausedLookup,
) -> None:
    """Saving the routed service again with the same definition, which rereads it from the store, keeps its key."""
    runtime_paths = tmp_runtime_paths(**_broker_env(free_port()))
    config = _config()
    target = _routable_target(scope="shared")
    _save_team(manager, target, _GITHUB)
    save_secret(manager, target, "team", "team-key")
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=target)
        response = await paused_lookup.get_after(get_through, env, lambda: _save_team(manager, target, _GITHUB))
    assert response.status_code == 200
    assert response.json()["headers"]["authorization"] == ["Bearer team-key"]


@pytest.mark.usefixtures("github_oauth_client")
@pytest.mark.asyncio
async def test_user_service_uses_only_its_scopes_oauth_connection(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    get_through: GetThrough,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user service naming an OAuth provider injects its owner's connected account and nobody else's."""
    # The fake upstream is localhost, so GitHub's account is bound to it instead of to the GitHub preset's hosts.
    monkeypatch.setattr(user_services, "_OAUTH_PROVIDER_HOSTS", {"github": ("localhost",)})
    runtime_paths = _oauth_runtime(tmp_runtime_paths)
    config = _config()
    alice, bob = _routable_target(), _routable_target("@bob:example.org")
    for target in (alice, bob):
        _save_mine(manager, target, config, _GITHUB_OAUTH)
    connect_account(manager, account_store("@alice:example.org"), "alice-oauth")
    allow_oauth_reuse(manager, runtime_paths, alice, "mine")
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        alice_env = execution_env_for_worker(runtime_paths, config=config, worker_target=alice)
        bob_env = execution_env_for_worker(runtime_paths, config=config, worker_target=bob)
        alice_response = await get_through(alice_env)
        bob_response = await get_through(bob_env)
    assert alice_env["GH_TOKEN"] == _PLACEHOLDER
    assert alice_response.json()["headers"]["authorization"] == ["Bearer alice-oauth"]
    assert "GH_TOKEN" not in bob_env
    assert bob_response.status_code == 403
    assert (bob_response.json()["error"], bob_response.json()["provider"]) == ("credential_not_configured", "github")
    assert tls_upstream.hits == ["/echo"]


@pytest.mark.usefixtures("github_oauth_client")
@pytest.mark.asyncio
async def test_a_stored_user_service_off_its_providers_hosts_never_gets_the_account(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    get_through: GetThrough,
) -> None:
    """A stored GitHub-login service on a host the GitHub preset does not name never injects the connected account."""
    runtime_paths = _oauth_runtime(tmp_runtime_paths)
    config = _config()
    target = _routable_target()
    save_egress_document(manager, target, USER_SERVICES_CREDENTIAL_SERVICE, {"services": {"mine": _GITHUB_OAUTH}})
    connect_account(manager, account_store("@alice:example.org"), "alice-oauth")
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=target)
        response = await get_through(env)
    assert "GH_TOKEN" not in env
    assert response.status_code == 403
    assert response.json()["error"] == "credential_not_configured"
    assert "provider" not in response.json()
    assert "alice-oauth" not in response.text
    assert tls_upstream.hits == []


@pytest.mark.usefixtures("allow_loopback")
@pytest.mark.asyncio
async def test_user_service_cannot_open_a_host_under_deny(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
    tls_upstream: Upstream,
    raw_proxy: Callable[[int, bytes], Awaitable[RawResponse]],
) -> None:
    """Under deny the running broker refuses a host only Alice's service names, as if her service did not exist."""
    port = free_port()
    runtime_paths = tmp_runtime_paths(**_broker_env(port))
    config = Config(egress_broker={"unmatched_hosts": "deny", "services": {"openai": _OPENAI}})
    target = _routable_target()
    _save_mine(manager, target, config, _GITHUB)
    save_secret(manager, target, "mine", "alice-key")
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=target)
        connect = connect_request(f"localhost:{tls_upstream.port}", authorization=proxy_authorization(_token(env)))
        response = await raw_proxy(port, connect)
    assert (response.status, response.json()) == (403, host_not_allowed("openai"))
    assert tls_upstream.hits == []


@pytest.mark.asyncio
async def test_config_placeholders_win_over_user_placeholders(
    tmp_runtime_paths: Callable[..., RuntimePaths],
    manager: CredentialsManager,
) -> None:
    """A user service cannot replace a config service's placeholder; its own variables still reach the worker."""
    runtime_paths = tmp_runtime_paths(**_broker_env(free_port()))
    config = _config(github=_GITHUB)
    target = _routable_target()
    mine = {
        "rules": [{"host": "api.example.com", "auth": {"type": "bearer"}}],
        "placeholder_env": {"GH_TOKEN": "user-value", "MINE_TOKEN": "mine-placeholder"},
    }
    _save_mine(manager, target, config, mine)
    save_secret(manager, target, "github", "s3cret")
    save_secret(manager, target, "mine", "mine-key")
    async with serve_egress_broker(runtime_paths, config_provider=lambda: config, credentials_manager=manager):
        env = execution_env_for_worker(runtime_paths, config=config, worker_target=target)
    assert env["GH_TOKEN"] == _PLACEHOLDER
    assert env["MINE_TOKEN"] == "mine-placeholder"  # noqa: S105 - a placeholder value, not a secret
