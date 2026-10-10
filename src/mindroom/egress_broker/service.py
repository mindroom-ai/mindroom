"""Run the egress broker inside the primary and give each worker-routed call its broker env."""

from __future__ import annotations

import asyncio
import os
import threading
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.config.egress_broker import EgressBrokerConfig
from mindroom.egress_broker.audit import AuditLog
from mindroom.egress_broker.ca import BrokerCA
from mindroom.egress_broker.dial import DialPolicy
from mindroom.egress_broker.env import broker_execution_env, primary_callback_hosts
from mindroom.egress_broker.proxy import EgressBroker
from mindroom.egress_broker.secrets import load_secret, secret_status
from mindroom.egress_broker.tokens import TokenSigner, WorkerClaims
from mindroom.logging_config import get_logger
from mindroom.runtime_env_policy import (
    CREDENTIALS_ENCRYPTION_KEY_ENV,
    EGRESS_BROKER_ENV_BY_KEY,
    credentials_encryption_key_value,
)
from mindroom.worker_computer.browser_proxy import browser_egress

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

__all__ = [
    "active_audit_log",
    "active_ca_pem",
    "execution_env_for_worker",
    "manage_url",
    "serve_egress_broker",
]

logger = get_logger(__name__)

_THREAD_NAME = "mindroom-egress-broker"
_DEFAULT_HOST = "0.0.0.0"  # noqa: S104 - workers reach the broker over the container or pod network
_DEFAULT_TOKEN_TTL_SECONDS = 604800
_STOP_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class _BrokerSettings:
    host: str
    port: int
    url: str
    token_ttl_seconds: int


@dataclass(frozen=True)
class _BrokerRuntime:
    """What worker calls and the admin API need from the running broker."""

    url: str
    signer: TokenSigner
    ca_pem: str
    audit: AuditLog
    credentials_manager: CredentialsManager


@dataclass
class _ActiveBroker:
    runtime: _BrokerRuntime | None = None


_active = _ActiveBroker()


def _positive_int(name: str, raw: str, *, maximum: int | None = None) -> int:
    value = int(raw) if raw.isascii() and raw.isdigit() else 0
    if value < 1 or (maximum is not None and value > maximum):
        bound = f"from 1 to {maximum}" if maximum is not None else "of at least 1"
        msg = f"{name} must be an integer {bound}."
        raise ValueError(msg)
    return value


def _require_proxy_url(name: str, url: str) -> None:
    """Reject a worker-facing URL that proxy clients cannot use: it needs an http(s) scheme and a host."""
    try:
        parts = urlsplit(url)
        valid = parts.scheme in {"http", "https"} and bool(parts.hostname)
    except ValueError:
        valid = False
    if not valid:
        msg = f"{name} must be an http:// or https:// URL with a host, such as http://host.docker.internal:8768."
        raise ValueError(msg)


def _broker_settings(runtime_paths: RuntimePaths) -> _BrokerSettings | None:
    """Read and validate the broker env; None when no port is set, so the broker stays off."""

    def env(key: str) -> str:
        return (runtime_paths.env_value(EGRESS_BROKER_ENV_BY_KEY[key]) or "").strip()

    if not (raw_port := env("port")):
        return None
    port = _positive_int(EGRESS_BROKER_ENV_BY_KEY["port"], raw_port, maximum=65535)
    if not (url := env("url")):
        msg = f"{EGRESS_BROKER_ENV_BY_KEY['url']} is required when {EGRESS_BROKER_ENV_BY_KEY['port']} is set"
        raise ValueError(msg)
    _require_proxy_url(EGRESS_BROKER_ENV_BY_KEY["url"], url)
    raw_ttl = env("token_ttl_seconds")
    ttl = _positive_int(EGRESS_BROKER_ENV_BY_KEY["token_ttl_seconds"], raw_ttl) if raw_ttl else None
    return _BrokerSettings(
        host=env("host") or _DEFAULT_HOST,
        port=port,
        url=url,
        token_ttl_seconds=ttl or _DEFAULT_TOKEN_TTL_SECONDS,
    )


def manage_url(runtime_paths: RuntimePaths) -> str | None:
    """Return where a user sets a missing egress secret: the personal page behind trusted upstream auth, else the dashboard."""
    public_url = (runtime_paths.env_value("MINDROOM_PUBLIC_URL") or "").strip().rstrip("/")
    if not public_url:
        return None
    if runtime_paths.env_flag("MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED"):
        return f"{public_url}/connections/egress"
    return f"{public_url}/"


def _egress_config_reader(config_provider: Callable[[], Config | None]) -> Callable[[], EgressBrokerConfig]:
    """Adapt the primary's config provider to the broker, which reads it once per CONNECT or request.

    While the provider has no valid config (it returns None or raises, for example after an invalid edit),
    the broker keeps the last config it read, so a broken save cannot turn ``deny`` into ``passthrough``.
    Before any good read the broker has no rules: nothing is injected.
    """
    last_good = EgressBrokerConfig()

    def read() -> EgressBrokerConfig:
        nonlocal last_good
        try:
            config = config_provider()
        except Exception:
            return last_good
        if config is not None:
            last_good = config.egress_broker
        return last_good

    return read


class _BrokerService:
    """One broker: its state under ``<storage>/egress_broker/`` and the daemon thread whose own loop serves it.

    The bot's event loop never carries broker traffic. ``start`` and ``stop`` block, so callers on an event
    loop run them in a thread.
    """

    def __init__(
        self,
        runtime_paths: RuntimePaths,
        settings: _BrokerSettings,
        *,
        config_provider: Callable[[], Config | None],
        credentials_manager: CredentialsManager,
    ) -> None:
        self._runtime_paths = runtime_paths
        self._settings = settings
        self._config_provider = config_provider
        self._credentials_manager = credentials_manager
        self.runtime: _BrokerRuntime | None = None
        self._audit: AuditLog | None = None
        self._thread: threading.Thread | None = None
        self._listening = threading.Event()
        self._start_error: Exception | None = None
        self._request_stop: Callable[[], object] | None = None

    def start(self) -> None:
        """Load or create the broker state, then block until the listener is bound; re-raise the bind error."""
        # Upstream dials follow the primary's own HTTPS_PROXY and NO_PROXY, like its other outbound traffic.
        dial_policy = DialPolicy(
            egress=browser_egress(self._runtime_paths.process_env, os.environ, egress_control=False),
        )
        state_dir = self._runtime_paths.storage_root / "egress_broker"
        key = credentials_encryption_key_value(self._runtime_paths.env_value(CREDENTIALS_ENCRYPTION_KEY_ENV))
        ca = BrokerCA.load_or_create(state_dir, key_password=None if key is None else key.encode())
        signer = TokenSigner.load_or_create(state_dir / "token.key", ttl_seconds=self._settings.token_ttl_seconds)
        self._audit = audit = AuditLog(state_dir / "requests.sqlite3")
        credentials_manager = self._credentials_manager
        link = manage_url(self._runtime_paths)
        broker = EgressBroker(
            ca=ca,
            signer=signer,
            config_provider=_egress_config_reader(self._config_provider),
            resolve_secret=lambda claims, name: load_secret(credentials_manager, claims.to_worker_target(), name),
            audit=audit,
            dial_policy=dial_policy,
            # None selects the broker's verifying context: system roots plus certifi.
            upstream_ssl_context=None,
            manage_url=lambda _claims: link,
        )
        self._thread = threading.Thread(target=self._run, args=(broker,), name=_THREAD_NAME, daemon=True)
        self._thread.start()
        self._listening.wait()
        if self._start_error is not None:
            raise self._start_error
        self.runtime = _BrokerRuntime(
            url=self._settings.url,
            signer=signer,
            ca_pem=ca.cert_pem,
            audit=audit,
            credentials_manager=credentials_manager,
        )

    def stop(self) -> None:
        """Stop the broker's loop, which closes the listener and every connection, and join the thread."""
        if self._request_stop is not None:
            with suppress(RuntimeError):  # The loop already ended on its own.
                self._request_stop()
        if self._thread is not None:
            self._thread.join(_STOP_TIMEOUT_SECONDS)
            if self._thread.is_alive():
                # Leave the audit log open for the thread that may still write to it.
                logger.warning("egress_broker_stop_timed_out", timeout_seconds=_STOP_TIMEOUT_SECONDS)
                return
        if self._audit is not None:
            self._audit.close()

    def _run(self, broker: EgressBroker) -> None:
        try:
            asyncio.run(self._serve(broker))
        except Exception as exc:
            if self._listening.is_set():
                logger.exception("egress_broker_thread_failed")
            else:
                self._start_error = exc
        finally:
            self._listening.set()

    async def _serve(self, broker: EgressBroker) -> None:
        await broker.start(self._settings.host, self._settings.port)
        stopping = asyncio.Event()
        loop = asyncio.get_running_loop()
        self._request_stop = lambda: loop.call_soon_threadsafe(stopping.set)
        self._listening.set()
        try:
            await stopping.wait()
        finally:
            await broker.close()


@asynccontextmanager
async def serve_egress_broker(
    runtime_paths: RuntimePaths,
    *,
    config_provider: Callable[[], Config | None],
    credentials_manager: CredentialsManager,
) -> AsyncIterator[None]:
    """Run the egress broker on its own thread while the context is open; a no-op without a port.

    `config_provider` returns the primary's current config, read on the broker thread for every CONNECT, so
    service edits apply without a restart. Raises ValueError for invalid settings and the bind error when
    the port is unavailable.
    """
    settings = _broker_settings(runtime_paths)
    if settings is None:
        yield
        return
    service = _BrokerService(
        runtime_paths,
        settings,
        config_provider=config_provider,
        credentials_manager=credentials_manager,
    )
    try:
        # Startup finishes even if the owner is cancelled meanwhile, so the finally below can stop it.
        await run_blocking_until_complete(service.start)
        _active.runtime = service.runtime
        logger.info("egress_broker_started", host=settings.host, port=settings.port)
        yield
    finally:
        _active.runtime = None
        await run_blocking_until_complete(service.stop)


def execution_env_for_worker(
    runtime_paths: RuntimePaths,
    *,
    config: Config | None,
    worker_target: ResolvedWorkerTarget | None,
    call_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the broker env for one worker-routed call: a fresh scope-bound token, the CA, and placeholders.

    Empty when the broker is not running, there is no config, or the target has no worker identity to sign.
    Placeholders come only from services with a secret in the target's scope, checked in the store the broker
    injects from. `call_env` is the env the call already carries; overlaying the result on it keeps its Git
    config entries.
    """
    runtime = _active.runtime
    if runtime is None or config is None or worker_target is None:
        return {}
    claims = WorkerClaims.from_worker_target(worker_target)
    if claims is None:
        return {}
    # Check status for the target the broker will rebuild from the token, so both agree on the scope.
    scope_target = claims.to_worker_target()
    placeholder_env: dict[str, str] = {}
    for name, egress_service in config.egress_broker.services.items():
        if not egress_service.placeholder_env:
            continue
        try:
            configured = secret_status(runtime.credentials_manager, scope_target, name).configured
        except Exception as exc:
            # One unreadable secret must not fail the call; the broker reports it if the service is used.
            logger.warning("egress_broker_secret_status_failed", service=name, error_type=type(exc).__name__)
            continue
        if configured:
            placeholder_env.update(egress_service.placeholder_env)
    return broker_execution_env(
        broker_url=runtime.url,
        token=runtime.signer.mint(claims),
        ca_pem=runtime.ca_pem,
        placeholder_env=placeholder_env,
        extra_no_proxy_hosts=primary_callback_hosts(runtime_paths),
        call_env=call_env,
    )


def active_audit_log() -> AuditLog | None:
    """Return the running broker's request log, or None when the broker is not running."""
    runtime = _active.runtime
    return runtime.audit if runtime is not None else None


def active_ca_pem() -> str | None:
    """Return the running broker's CA certificate in PEM form, or None when the broker is not running."""
    runtime = _active.runtime
    return runtime.ca_pem if runtime is not None else None
