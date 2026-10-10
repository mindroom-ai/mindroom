"""Run the egress broker inside the primary and give each worker-routed call its broker env."""

from __future__ import annotations

import asyncio
import functools
import os
import threading
from concurrent.futures import ThreadPoolExecutor
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
from mindroom.egress_broker.oauth_source import (
    Missing,
    NeedsReconnect,
    OAuthTokenResult,
    Token,
    oauth_status,
    resolve_oauth_token,
)
from mindroom.egress_broker.proxy import EgressBroker
from mindroom.egress_broker.secrets import (
    Secret,
    SecretMissing,
    SecretNeedsReconnect,
    SecretResult,
    SecretUnavailable,
    load_secret,
    service_status,
)
from mindroom.egress_broker.tokens import TokenSigner, WorkerClaims
from mindroom.logging_config import get_logger
from mindroom.runtime_env_policy import (
    CREDENTIALS_ENCRYPTION_KEY_ENV,
    EGRESS_BROKER_ENV_BY_KEY,
    credentials_encryption_key_value,
)
from mindroom.worker_computer.browser_proxy import browser_egress, proxy_env_setting

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget
    from mindroom.worker_computer.browser_proxy import BrowserEgress

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
# OAuth lookups can wait up to a token endpoint's deadline, so they get a small pool of their own: a stalled
# provider holds at most these threads, never the loop's default executor that key lookups, audit writes, and
# leaf certificates share.
_OAUTH_LOOKUP_WORKERS = 4


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


def _operator_egress(runtime_paths: RuntimePaths) -> BrowserEgress:
    """Return the operator proxies the primary's own env names, refusing any it names but cannot use.

    ``browser_egress`` ignores an unsupported proxy URL and dials directly. The broker must not: where the
    operator proxy is the only route out, a silent direct dial would just fail, and where it is a policy,
    it would be bypassed. So a proxy variable that is set but leaves its scheme without an upstream stops startup.
    The error names the variable and never its value, which may hold credentials.
    """
    envs = (runtime_paths.process_env, os.environ)
    egress = browser_egress(*envs, egress_control=False)
    upstreams = {"http": egress.upstream_for(80), "https": egress.upstream_for(443)}
    for scheme, variable in (("http", "HTTP_PROXY"), ("https", "HTTPS_PROXY")):
        if proxy_env_setting(envs, variable.lower()) and upstreams[scheme] is None:
            raise ValueError(_unusable_proxy_message(variable))
    # Past the checks above, a scheme without an upstream has no variable of its own, so ALL_PROXY was its fallback.
    if proxy_env_setting(envs, "all_proxy") and None in upstreams.values():
        raise ValueError(_unusable_proxy_message("ALL_PROXY"))
    return egress


def _unusable_proxy_message(variable: str) -> str:
    return (
        f"{variable} cannot be used by the egress broker: "
        "it supports only http:// or https:// proxies without credentials or paths."
    )


def manage_url(runtime_paths: RuntimePaths) -> str | None:
    """Return where a user sets a missing egress secret: the personal page behind trusted upstream auth, else the dashboard."""
    public_url = (runtime_paths.env_value("MINDROOM_PUBLIC_URL") or "").strip().rstrip("/")
    if not public_url:
        return None
    if runtime_paths.env_flag("MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED"):
        return f"{public_url}/connections/egress"
    return f"{public_url}/"


class _LastGoodConfig:
    """The primary's config as the broker reads it: once per CONNECT, request, and secret lookup.

    While the provider has no valid config (it returns None or raises, for example after an invalid edit),
    the broker keeps the last config it read, so a broken save cannot turn ``deny`` into ``passthrough``.
    Before any good read the broker has no rules: nothing is injected.
    """

    def __init__(self, config_provider: Callable[[], Config | None]) -> None:
        self._config_provider = config_provider
        self._config: Config | None = None

    def current(self) -> Config | None:
        try:
            config = self._config_provider()
        except Exception:
            return self._config
        if config is not None:
            self._config = config
        return self._config

    def egress(self) -> EgressBrokerConfig:
        config = self.current()
        return config.egress_broker if config is not None else EgressBrokerConfig()


async def _resolve_secret(
    claims: WorkerClaims,
    name: str,
    *,
    config: _LastGoodConfig,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager,
    oauth_executor: ThreadPoolExecutor,
) -> SecretResult:
    """Return the secret for service `name` in the claims' scope: the stored key, else the OAuth connection's token.

    The key is read on the loop's default executor; the OAuth lookup, which may refresh, runs on `oauth_executor`.
    """
    target = claims.to_worker_target()
    if key := await asyncio.to_thread(load_secret, credentials_manager, target, name):
        return Secret(key)
    current = config.current()
    egress_service = current.egress_broker.services.get(name) if current is not None else None
    if current is None or egress_service is None or egress_service.oauth_provider is None:
        return SecretMissing()
    provider_id = egress_service.oauth_provider
    lookup = functools.partial(
        resolve_oauth_token,
        service=name,
        provider_id=provider_id,
        config=current,
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=target,
    )
    result = await asyncio.get_running_loop().run_in_executor(oauth_executor, lookup)
    return _oauth_secret_result(provider_id, result)


def _oauth_secret_result(provider_id: str, result: OAuthTokenResult) -> SecretResult:
    if isinstance(result, Token):
        return Secret(result.value)
    if isinstance(result, Missing):
        # Only a connectable provider has a connect link, and only then is it worth naming.
        if result.connect_url is None:
            return SecretMissing()
        return SecretMissing(provider=provider_id, connect_url=result.connect_url)
    if isinstance(result, NeedsReconnect):
        return SecretNeedsReconnect(
            provider=provider_id,
            connect_url=result.connect_url,
            reset_required=result.reset_required,
        )
    return SecretUnavailable(provider=provider_id)


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
        self._oauth_executor: ThreadPoolExecutor | None = None
        self._thread: threading.Thread | None = None
        self._listening = threading.Event()
        self._start_error: Exception | None = None
        self._request_stop: Callable[[], object] | None = None

    def start(self) -> None:
        """Load or create the broker state, then block until the listener is bound; re-raise the bind error."""
        # Upstream dials follow the primary's own HTTPS_PROXY and NO_PROXY, like its other outbound traffic.
        dial_policy = DialPolicy(egress=_operator_egress(self._runtime_paths))
        state_dir = self._runtime_paths.storage_root / "egress_broker"
        key = credentials_encryption_key_value(self._runtime_paths.env_value(CREDENTIALS_ENCRYPTION_KEY_ENV))
        ca = BrokerCA.load_or_create(state_dir, key_password=None if key is None else key.encode())
        signer = TokenSigner.load_or_create(state_dir / "token.key", ttl_seconds=self._settings.token_ttl_seconds)
        self._audit = audit = AuditLog(state_dir / "requests.sqlite3")
        credentials_manager = self._credentials_manager
        link = manage_url(self._runtime_paths)
        config = _LastGoodConfig(self._config_provider)
        self._oauth_executor = oauth_executor = ThreadPoolExecutor(
            max_workers=_OAUTH_LOOKUP_WORKERS,
            thread_name_prefix="mindroom-egress-oauth",
        )
        broker = EgressBroker(
            ca=ca,
            signer=signer,
            config_provider=config.egress,
            resolve_secret=functools.partial(
                _resolve_secret,
                config=config,
                runtime_paths=self._runtime_paths,
                credentials_manager=credentials_manager,
                oauth_executor=oauth_executor,
            ),
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
        if self._oauth_executor is not None:
            # A lookup stalled on a token endpoint ends within that endpoint's deadline; stopping never waits for it.
            self._oauth_executor.shutdown(wait=False, cancel_futures=True)
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
    Placeholders come only from services with a secret in the target's scope, a stored key or a usable OAuth
    connection, checked in the stores the broker injects from. `call_env` is the env the call already carries;
    overlaying the result on it keeps its Git config entries.
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
        read_oauth_status = functools.partial(
            oauth_status,
            service=name,
            config=config,
            runtime_paths=runtime_paths,
            credentials_manager=runtime.credentials_manager,
        )
        try:
            configured = service_status(
                runtime.credentials_manager,
                scope_target,
                egress_service,
                name,
                oauth_status=read_oauth_status,
            ).configured
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
