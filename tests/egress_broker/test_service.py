"""Tests for the egress broker lifecycle in the primary and the env it gives worker calls."""

from __future__ import annotations

import functools
import socket
import ssl
import stat
import threading
import time
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from structlog.testing import capture_logs

from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import CredentialsManager
from mindroom.egress_broker import service
from mindroom.egress_broker.audit import AuditLog
from mindroom.egress_broker.dial import DialPolicy
from mindroom.egress_broker.env import apply_runner_ca_bundle
from mindroom.egress_broker.secrets import save_secret
from mindroom.egress_broker.service import (
    active_audit_log,
    active_ca_pem,
    execution_env_for_worker,
    manage_url,
    serve_egress_broker,
)
from mindroom.egress_broker.tokens import TokenSigner
from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity, resolve_worker_target

from .conftest import audit_records, connect_request, proxy_authorization

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths
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


def _target(requester_id: str = "@alice:example.org", agent_name: str = "code") -> ResolvedWorkerTarget:
    identity = ToolExecutionIdentity(
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
    return resolve_worker_target("user_agent", agent_name, identity, private_agent_names=frozenset())


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
    real_secret_status = service.secret_status

    def flaky_secret_status(store: CredentialsManager, scope: ResolvedWorkerTarget, name: str) -> object:
        if name == "openai":
            msg = "sk-s3cret could not be decrypted"
            raise OSError(msg)
        return real_secret_status(store, scope, name)

    monkeypatch.setattr(service, "secret_status", flaky_secret_status)
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
