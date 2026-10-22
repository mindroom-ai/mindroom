"""Tests for OAuth connections as an egress broker secret source, run through the real credential lifecycle."""

from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx
import pytest
from structlog.testing import capture_logs

from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.egress_broker import oauth_source
from mindroom.egress_broker.oauth_reuse import OAUTH_REUSE_CREDENTIAL_SERVICE, set_oauth_reuse
from mindroom.egress_broker.oauth_source import broker_credential_context, oauth_status, resolve_oauth_token
from mindroom.egress_broker.secrets import (
    OAuthStatus,
    Secret,
    SecretMissing,
    SecretNeedsReconnect,
    SecretResult,
    SecretUnavailable,
    load_egress_document,
)
from mindroom.egress_broker.tokens import WorkerClaims
from mindroom.oauth.credential_lifecycle import load_oauth_credentials_snapshot_sync, resolve_oauth_credential_context
from mindroom.oauth.credential_store import _oauth_credential_database_path
from mindroom.oauth.github import github_oauth_provider
from mindroom.oauth.providers import OAuthProvider
from mindroom.tool_system.worker_routing import resolve_worker_target
from tests.egress_broker.conftest import (
    OAUTH_REFRESH_TOKEN,
    account_store,
    allow_oauth_reuse,
    connect_account,
    tool_identity,
    worker_target,
)
from tests.oauth_test_utils import (
    DelayedTokenEndpointOutcome,
    TokenEndpointOutcome,
    corrupt_oauth_credential_payload,
    rotated_token_response,
    serve_token_endpoint,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.oauth.credential_lifecycle import OAuthCredentialContext
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, WorkerScope

PUBLIC_URL = "https://chat.example.org"
GITHUB_CONNECT_PREFIX = f"{PUBLIC_URL}/api/oauth/github/authorize?"
_DEMO = OAuthProvider(
    id="demo",
    display_name="Demo",
    authorization_url="https://auth.example.test/authorize",
    token_url="https://auth.example.test/token",  # noqa: S106 - an endpoint, not a secret
    scopes=("read",),
    credential_service="demo_oauth",
    client_config_services=("demo_oauth_client",),
)


@pytest.fixture
def runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Return a runtime on the Docker worker backend, so `user` and `user_agent` workers are private sandboxes.

    Its storage is under `tmp_path` and it has a public URL for connect links.
    """
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MINDROOM_NAMESPACE": "",
            "MINDROOM_PUBLIC_URL": PUBLIC_URL,
            "MINDROOM_WORKER_BACKEND": "docker",
        },
    )


def _on_backend(runtime_paths: RuntimePaths, backend: str) -> RuntimePaths:
    """Return the same runtime and storage with `backend` as its worker backend."""
    return replace(runtime_paths, process_env={**runtime_paths.process_env, "MINDROOM_WORKER_BACKEND": backend})


@pytest.fixture
def manager(runtime_paths: RuntimePaths) -> CredentialsManager:
    """Return the primary credentials manager, with GitHub and demo OAuth clients configured."""
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials("github_oauth_client", {"client_id": "github-client-id", "client_secret": "gh-secret"})
    manager.save_credentials("demo_oauth_client", {"client_id": "demo-client-id", "client_secret": "demo-secret"})
    return manager


@pytest.fixture
def config() -> Config:
    """Return a config with no plugins: the registry holds only the built-in providers."""
    return Config()


@pytest.fixture
def opted_in_config() -> Config:
    """Return a config whose `github` service lets requester-scoped accounts work on shared workers."""
    return Config(egress_broker={"services": {"github": {"preset": "github", "oauth_on_shared_workers": True}}})


@pytest.fixture
def demo_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register the non-requester-scoped demo provider beside GitHub."""
    registry = {"demo": _DEMO, "github": github_oauth_provider()}
    monkeypatch.setattr(oauth_source, "load_oauth_providers", lambda _config, _runtime_paths: registry)


@pytest.fixture(autouse=True)
def _fresh_unknown_provider_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oauth_source, "_warned_unknown_providers", set())


@pytest.fixture
def enable_reuse(runtime_paths: RuntimePaths, manager: CredentialsManager) -> None:
    """Let Agent Keys use every connection these tests store: Alice's and Bob's GitHub, and agent `code`'s demo."""
    for requester in ("@alice:example.org", "@bob:example.org"):
        allow_oauth_reuse(manager, runtime_paths, account_store(requester))
        for scope in ("user", "user_agent", "shared", None):
            allow_oauth_reuse(manager, runtime_paths, _broker_target(requester, scope), "demo", provider=_DEMO)


def _tool_target(requester_id: str | None, scope: WorkerScope | None = "user_agent") -> ResolvedWorkerTarget:
    """Return the worker target a tool call on agent `code` carries."""
    return resolve_worker_target(scope, "code", tool_identity(requester_id))


def _broker_target(requester_id: str | None, scope: WorkerScope | None = "user_agent") -> ResolvedWorkerTarget:
    """Return the target the broker rebuilds from the proxy token minted for that call."""
    tool_target = _tool_target(requester_id, scope)
    # An unscoped call's token names its routed worker; its tool target has no worker key.
    claims = WorkerClaims.from_worker_target(replace(tool_target, worker_key=tool_target.worker_key or "unscoped"))
    assert claims is not None
    return claims.to_worker_target()


def _store_context(
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
    store: ResolvedWorkerTarget,
    provider: OAuthProvider | None = None,
) -> OAuthCredentialContext:
    return resolve_oauth_credential_context(provider or github_oauth_provider(), runtime_paths, manager, store)


def _resolve(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
    target: ResolvedWorkerTarget,
    *,
    provider_id: str = "github",
    service: str | None = None,
) -> SecretResult:
    """Resolve the token of the service named `service`, by default after its provider."""
    return resolve_oauth_token(
        service=service or provider_id,
        provider_id=provider_id,
        config=config,
        runtime_paths=runtime_paths,
        credentials_manager=manager,
        worker_target=target,
    )


def _status(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
    target: ResolvedWorkerTarget,
    *,
    provider_id: str = "github",
    service: str | None = None,
) -> OAuthStatus | None:
    return oauth_status(
        provider_id,
        target,
        service=service or provider_id,
        config=config,
        runtime_paths=runtime_paths,
        credentials_manager=manager,
    )


@pytest.mark.usefixtures("enable_reuse")
def test_connected_account_yields_its_token(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
) -> None:
    """A connected GitHub account supplies its access token, which never appears in the result's repr."""
    connect_account(manager, account_store("@alice:example.org"), "alice-access")

    result = _resolve(config, runtime_paths, manager, _broker_target("@alice:example.org"))

    assert result == Secret("alice-access")
    assert "alice-access" not in repr(result)


@pytest.mark.parametrize("scope", ["shared", None], ids=["shared", "unscoped"])
@pytest.mark.usefixtures("enable_reuse")
def test_requester_scoped_provider_is_unavailable_on_workers_requesters_share(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
    scope: WorkerScope | None,
) -> None:
    """Every requester's commands share a shared or unscoped worker, so no one's GitHub account is used there.

    The token would sit in a sandbox another user's later command can read, so there is no token, no connect link,
    and the status says why instead of offering a connection.
    """
    connect_account(
        manager,
        account_store("@alice:example.org"),
        "alice-access",
        _oauth_claims={"email": "alice@example.org"},
        _oauth_claims_verified=True,
    )
    alice = _broker_target("@alice:example.org", scope)

    result = _resolve(config, runtime_paths, manager, alice)
    status = _status(config, runtime_paths, manager, alice)

    assert result == SecretMissing()
    assert status == OAuthStatus(
        provider="github",
        display_name="GitHub",
        connected=False,
        account_label=None,
        can_connect=False,
        reset_required=False,
        unavailable_reason="shared_sandbox",
    )


@pytest.mark.parametrize("scope", ["user", "user_agent"])
@pytest.mark.usefixtures("enable_reuse")
def test_requester_scoped_provider_is_unavailable_on_the_static_runner(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
    scope: WorkerScope,
) -> None:
    """The static runner serves every call from one process, so a `user` or `user_agent` call shares its sandbox.

    The account is refused there exactly as on a shared worker: no token, no connect link, and the same reason.
    """
    connect_account(manager, account_store("@alice:example.org"), "alice-access")
    static = _on_backend(runtime_paths, "static_runner")
    alice = _broker_target("@alice:example.org", scope)

    result = _resolve(config, static, manager, alice)
    status = _status(config, static, manager, alice)

    assert result == SecretMissing()
    assert status is not None
    assert (status.connected, status.can_connect, status.unavailable_reason) == (False, False, "shared_sandbox")
    assert status.shared_worker_opt_in is False


@pytest.mark.usefixtures("enable_reuse")
def test_unset_worker_backend_is_the_static_runner(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
) -> None:
    """A runtime without `MINDROOM_WORKER_BACKEND` runs on the static runner, so personal accounts are refused."""
    connect_account(manager, account_store("@alice:example.org"), "alice-access")
    env = {key: value for key, value in runtime_paths.process_env.items() if key != "MINDROOM_WORKER_BACKEND"}

    result = _resolve(config, replace(runtime_paths, process_env=env), manager, _broker_target("@alice:example.org"))

    assert result == SecretMissing()


@pytest.mark.parametrize("backend", ["docker", "kubernetes"])
@pytest.mark.parametrize("scope", ["user", "user_agent"])
@pytest.mark.usefixtures("enable_reuse")
def test_requester_scoped_provider_is_used_on_a_dedicated_backend(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
    backend: str,
    scope: WorkerScope,
) -> None:
    """A Docker or Kubernetes `user` or `user_agent` worker belongs to one requester, so their account is used."""
    connect_account(manager, account_store("@alice:example.org"), "alice-access")
    dedicated = _on_backend(runtime_paths, backend)
    alice = _broker_target("@alice:example.org", scope)

    result = _resolve(config, dedicated, manager, alice)
    status = _status(config, dedicated, manager, alice)

    assert result == Secret("alice-access")
    assert status is not None
    assert (status.connected, status.unavailable_reason, status.shared_worker_opt_in) == (True, None, False)


@pytest.mark.parametrize("backend", ["static_runner", "docker", "kubernetes"])
@pytest.mark.parametrize("scope", ["user_agent", "shared", None])
@pytest.mark.usefixtures("enable_reuse")
def test_opt_in_uses_the_requesters_account_on_every_backend(
    opted_in_config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
    backend: str,
    scope: WorkerScope | None,
) -> None:
    """`oauth_on_shared_workers` makes the account usable wherever the sandbox is shared, and changes nothing else."""
    connect_account(manager, account_store("@alice:example.org"), "alice-access")
    target = _broker_target("@alice:example.org", scope)
    runtime = _on_backend(runtime_paths, backend)

    result = _resolve(opted_in_config, runtime, manager, target)
    status = _status(opted_in_config, runtime, manager, target)

    private = backend != "static_runner" and scope == "user_agent"
    assert result == Secret("alice-access")
    assert status is not None
    assert (status.connected, status.unavailable_reason, status.shared_worker_opt_in) == (True, None, not private)


@pytest.mark.usefixtures("enable_reuse")
def test_status_without_a_worker_target_treats_it_as_unscoped(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
) -> None:
    """The global store serves agents without a worker scope, so its status follows the shared-worker rule too."""
    connect_account(manager, account_store("@alice:example.org"), "alice-access")

    status = oauth_status(
        "github",
        None,
        service="github",
        config=config,
        runtime_paths=runtime_paths,
        credentials_manager=manager,
    )

    assert status is not None
    assert (status.connected, status.can_connect, status.unavailable_reason) == (False, False, "shared_sandbox")


@pytest.mark.usefixtures("enable_reuse")
def test_opt_in_uses_the_callers_account_on_a_shared_worker(
    opted_in_config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
) -> None:
    """With `oauth_on_shared_workers`, a shared worker gets the calling requester's own connection, never another's."""
    connect_account(manager, account_store("@alice:example.org"), "alice-access")

    alice = _resolve(opted_in_config, runtime_paths, manager, _broker_target("@alice:example.org", "shared"))
    bob = _resolve(opted_in_config, runtime_paths, manager, _broker_target("@bob:example.org", "shared"))
    connect_account(manager, account_store("@bob:example.org"), "bob-access")
    bob_connected = _resolve(opted_in_config, runtime_paths, manager, _broker_target("@bob:example.org", "shared"))
    status = _status(opted_in_config, runtime_paths, manager, _broker_target("@alice:example.org", "shared"))

    assert alice == Secret("alice-access")
    assert isinstance(bob, SecretMissing)
    assert bob.connect_url is not None
    assert bob.connect_url.startswith(GITHUB_CONNECT_PREFIX)
    assert bob_connected == Secret("bob-access")
    assert status is not None
    assert (status.connected, status.unavailable_reason, status.shared_worker_opt_in) == (True, None, True)


@pytest.mark.usefixtures("enable_reuse")
def test_requester_scoped_status_on_a_private_worker_has_no_shared_worker_flags(
    opted_in_config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
) -> None:
    """The opt-in only matters on shared workers: a private worker's status carries neither flag."""
    connect_account(manager, account_store("@alice:example.org"), "alice-access")

    status = _status(opted_in_config, runtime_paths, manager, _broker_target("@alice:example.org"))

    assert status is not None
    assert (status.connected, status.unavailable_reason, status.shared_worker_opt_in) == (True, None, False)


def test_requester_scoped_provider_without_requester_has_no_token(
    opted_in_config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
) -> None:
    """A call without a requester has no GitHub connection to use, even when another requester has one.

    Nor is there a store to allow one in, so the result offers nothing to connect or allow.
    """
    connect_account(manager, account_store("@alice:example.org"), "alice-access")
    allow_oauth_reuse(manager, runtime_paths, account_store("@alice:example.org"))

    result = _resolve(opted_in_config, runtime_paths, manager, _broker_target(None, "shared"))

    assert result == SecretMissing()


@pytest.mark.usefixtures("enable_reuse")
def test_missing_connection_offers_a_connect_link(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
) -> None:
    """Without a connection the result carries a MindRoom link that connects one for this scope."""
    result = _resolve(config, runtime_paths, manager, _broker_target("@alice:example.org"))

    assert isinstance(result, SecretMissing)
    assert result.connect_url is not None
    assert result.connect_url.startswith(GITHUB_CONNECT_PREFIX)
    assert "connect_token=" in urlsplit(result.connect_url).query
    assert "connect_token" not in repr(result)


@pytest.mark.usefixtures("enable_reuse")
def test_repeated_misses_reuse_one_connect_link(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker retrying without a connection gets one link per scope, not a new stored one-time token per request."""
    alice = _broker_target("@alice:example.org")

    links = {_resolve(config, runtime_paths, manager, alice).connect_url for _ in range(5)}
    bob = _resolve(config, runtime_paths, manager, _broker_target("@bob:example.org"))
    monkeypatch.setattr(oauth_source, "_CONNECT_URL_REUSE_SECONDS", 0.0)
    expired = _resolve(config, runtime_paths, manager, alice)

    assert len(links) == 1
    assert None not in links
    assert isinstance(bob, SecretMissing)
    assert bob.connect_url not in links
    assert isinstance(expired, SecretMissing)
    assert expired.connect_url not in links
    state = json.loads((runtime_paths.storage_root / "oauth_state" / "oauth_state.json").read_text())
    assert len(state["states"]) == 3


@pytest.mark.usefixtures("enable_reuse")
def test_provider_without_client_config_offers_no_link(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
) -> None:
    """A provider without an OAuth client cannot be connected, so there is no link and no status to connect."""
    manager.delete_credentials("github_oauth_client")

    result = _resolve(config, runtime_paths, manager, _broker_target("@alice:example.org"))
    status = _status(config, runtime_paths, manager, _broker_target("@alice:example.org"))

    assert result == SecretMissing()
    assert status is not None
    assert (status.connected, status.can_connect) == (False, False)


def test_unknown_provider_warns_once_naming_the_service(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
) -> None:
    """A provider id the registry does not know yields nothing and one warning naming the service."""
    target = _broker_target("@alice:example.org")
    with capture_logs() as logs:
        first = _resolve(config, runtime_paths, manager, target, provider_id="nope", service="custom")
        second = _resolve(config, runtime_paths, manager, target, provider_id="nope", service="custom")
        status = _status(config, runtime_paths, manager, target, provider_id="nope", service="custom")

    assert first == second == SecretMissing()
    assert status is None
    warnings = [entry for entry in logs if entry["event"] == "egress_broker_oauth_provider_unknown"]
    assert warnings == [
        {
            "event": "egress_broker_oauth_provider_unknown",
            "log_level": "warning",
            "service": "custom",
            "provider_id": "nope",
        },
    ]


@pytest.mark.usefixtures("enable_reuse")
def test_expired_token_is_refreshed_through_the_lifecycle(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired access token is refreshed once and the rotated credential is stored for the tools too."""
    store = account_store("@alice:example.org")
    connect_account(manager, store, "stale-access", expires_at=1.0)
    presented = serve_token_endpoint(monkeypatch, [rotated_token_response()])

    result = _resolve(config, runtime_paths, manager, _broker_target("@alice:example.org"))

    assert result == Secret("rotated-access-token")
    assert presented == [OAUTH_REFRESH_TOKEN]
    stored = load_oauth_credentials_snapshot_sync(_store_context(runtime_paths, manager, store)).credentials
    assert stored is not None
    assert (stored["token"], stored["refresh_token"]) == ("rotated-access-token", "rotated-refresh-token")


@pytest.mark.usefixtures("enable_reuse")
def test_concurrent_lookups_refresh_once(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lookups racing on several threads for an expired token share one refresh under the lifecycle's lock."""
    connect_account(manager, account_store("@alice:example.org"), "stale-access", expires_at=1.0)
    presented = serve_token_endpoint(monkeypatch, [DelayedTokenEndpointOutcome(0.3, rotated_token_response())])
    target = _broker_target("@alice:example.org")

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: _resolve(config, runtime_paths, manager, target), range(8)))

    assert results == [Secret("rotated-access-token")] * 8
    assert presented == [OAUTH_REFRESH_TOKEN]


@pytest.mark.usefixtures("enable_reuse")
def test_revoked_grant_needs_reconnect_without_leaking(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grant the provider rejects asks for a reconnect; logs carry only the error type."""
    connect_account(manager, account_store("@alice:example.org"), "stale-access", expires_at=1.0)
    serve_token_endpoint(monkeypatch, [httpx.Response(400, json={"error": "invalid_grant"})])

    with capture_logs() as logs:
        result = _resolve(config, runtime_paths, manager, _broker_target("@alice:example.org"))

    assert isinstance(result, SecretNeedsReconnect)
    assert not result.reset_required
    assert result.connect_url is not None
    assert result.connect_url.startswith(GITHUB_CONNECT_PREFIX)
    assert {
        "event": "egress_broker_oauth_token_unavailable",
        "log_level": "warning",
        "service": "github",
        "provider_id": "github",
        "error_type": "OAuthRefreshRejectedError",
    } in logs
    for leaked in ("stale-access", OAUTH_REFRESH_TOKEN, urlsplit(result.connect_url).query):
        assert leaked not in repr(logs)


def _connect_failure(request: httpx.Request) -> Exception:
    return httpx.ConnectError("token endpoint unreachable", request=request)


def _connect_timeout(request: httpx.Request) -> Exception:
    return httpx.ConnectTimeout("token endpoint timed out", request=request)


@pytest.mark.parametrize(
    "outcome",
    [httpx.Response(503, json={"error": "temporarily_unavailable"}), _connect_failure, _connect_timeout],
    ids=["provider-503", "network-error", "timeout"],
)
@pytest.mark.usefixtures("enable_reuse")
def test_transient_refresh_failure_is_retryable_and_backs_off(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
    monkeypatch: pytest.MonkeyPatch,
    outcome: httpx.Response | Callable[[httpx.Request], Exception],
) -> None:
    """An outage answers `SecretUnavailable`, not a reconnect, and the scope skips the grant until the backoff ends."""
    store = account_store("@alice:example.org")
    connect_account(manager, store, "stale-access", expires_at=1.0)
    outcomes: list[TokenEndpointOutcome] = [outcome]
    presented = serve_token_endpoint(monkeypatch, outcomes)
    target = _broker_target("@alice:example.org")

    with capture_logs() as logs:
        first = _resolve(config, runtime_paths, manager, target)
        backing_off = _resolve(config, runtime_paths, manager, target)
    monkeypatch.setattr(oauth_source, "_TRANSIENT_FAILURE_BACKOFF_SECONDS", 0.0)
    outcomes.append(rotated_token_response())
    recovered = _resolve(config, runtime_paths, manager, target)

    assert first == backing_off == SecretUnavailable(provider="github")
    assert recovered == Secret("rotated-access-token")
    assert presented == [OAUTH_REFRESH_TOKEN, OAUTH_REFRESH_TOKEN]
    unavailable = [entry for entry in logs if entry["event"] == "egress_broker_oauth_token_unavailable"]
    assert unavailable == [
        {
            "event": "egress_broker_oauth_token_unavailable",
            "log_level": "warning",
            "service": "github",
            "provider_id": "github",
            "error_type": "OAuthProviderError",
        },
    ]
    assert "stale-access" not in repr(logs)
    assert OAUTH_REFRESH_TOKEN not in repr(logs)
    # The stored connection survives an outage, so it is still there when the provider recovers.
    assert load_oauth_credentials_snapshot_sync(_store_context(runtime_paths, manager, store)).credentials is not None


@pytest.mark.usefixtures("enable_reuse")
def test_lookups_queued_behind_a_stalled_refresh_do_not_repeat_it(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lookups waiting while a refresh stalls and then fails answer `SecretUnavailable` without another grant."""
    connect_account(manager, account_store("@alice:example.org"), "stale-access", expires_at=1.0)
    stalled_failure = DelayedTokenEndpointOutcome(0.5, httpx.Response(503, json={"error": "temporarily_unavailable"}))
    presented = serve_token_endpoint(monkeypatch, [stalled_failure])
    target = _broker_target("@alice:example.org")

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: _resolve(config, runtime_paths, manager, target), range(4)))

    assert results == [SecretUnavailable(provider="github")] * 4
    assert presented == [OAUTH_REFRESH_TOKEN]


@pytest.mark.usefixtures("enable_reuse")
def test_unreadable_connection_requires_reset(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
) -> None:
    """An undecodable stored credential needs a reset before reconnecting, and its status says so."""
    store = account_store("@alice:example.org")
    connect_account(manager, store, "alice-access")
    corrupt_oauth_credential_payload(
        _oauth_credential_database_path(_store_context(runtime_paths, manager, store)),
        base64.b64encode(b"not a credential"),
    )
    target = _broker_target("@alice:example.org")

    result = _resolve(config, runtime_paths, manager, target)
    status = _status(config, runtime_paths, manager, target)

    assert result == SecretNeedsReconnect(provider="github", connect_url=None, reset_required=True)
    assert status is not None
    assert (status.connected, status.reset_required) == (False, True)


@pytest.mark.usefixtures("demo_registry", "enable_reuse")
def test_agent_scoped_provider_follows_the_worker_scope(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
) -> None:
    """A provider without requester scoping uses the worker's scope: one shared connection for a shared agent."""
    connect_account(manager, _tool_target("@alice:example.org", "shared"), "shared-access", provider=_DEMO)

    alice = _resolve(config, runtime_paths, manager, _broker_target("@alice:example.org", "shared"), provider_id="demo")
    bob = _resolve(config, runtime_paths, manager, _broker_target("@bob:example.org", "shared"), provider_id="demo")
    private = _resolve(config, runtime_paths, manager, _broker_target("@alice:example.org"), provider_id="demo")

    assert alice == bob == Secret("shared-access")
    assert isinstance(private, SecretMissing)


@pytest.mark.usefixtures("demo_registry", "enable_reuse")
def test_agent_scoped_provider_on_a_shared_worker_is_not_gated(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
) -> None:
    """The shared-worker rule is for requester-scoped providers; an agent's shared connection is its own scope."""
    connect_account(manager, _tool_target("@alice:example.org", "shared"), "shared-access", provider=_DEMO)

    status = _status(config, runtime_paths, manager, _broker_target("@bob:example.org", "shared"), provider_id="demo")

    assert status is not None
    assert (status.connected, status.can_connect) == (True, True)
    assert (status.unavailable_reason, status.shared_worker_opt_in) == (None, False)


@pytest.mark.usefixtures("demo_registry", "enable_reuse")
def test_shared_scope_connect_links_are_never_handed_out(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A link for a connection several requesters share skips sign-in and any process in the worker can read it.

    So the broker offers none for a shared agent or one without a worker scope (all of those share one store).
    """
    shared = _broker_target("@alice:example.org", "shared")
    missing = _resolve(config, runtime_paths, manager, shared, provider_id="demo")
    unscoped = _resolve(config, runtime_paths, manager, _broker_target("@alice:example.org", None), provider_id="demo")
    connect_account(
        manager,
        _tool_target("@alice:example.org", "shared"),
        "stale-access",
        provider=_DEMO,
        expires_at=1.0,
    )
    serve_token_endpoint(monkeypatch, [httpx.Response(400, json={"error": "invalid_grant"})])
    revoked = _resolve(config, runtime_paths, manager, shared, provider_id="demo")
    private = _resolve(config, runtime_paths, manager, _broker_target("@alice:example.org"), provider_id="demo")

    assert revoked == SecretNeedsReconnect(provider="demo", connect_url=None)
    assert missing == SecretMissing()
    assert unscoped == SecretMissing()
    assert isinstance(private, SecretMissing)
    assert private.connect_url is not None
    assert "connect_token=" in urlsplit(private.connect_url).query


@pytest.mark.usefixtures("demo_registry", "enable_reuse")
def test_scoped_worker_never_reads_the_unscoped_store(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
) -> None:
    """Only unscoped targets drop their worker key: scoped targets keep it and never see the unscoped connection."""
    connect_account(manager, _tool_target("@alice:example.org", None), "unscoped-access", provider=_DEMO)

    results = [
        _resolve(config, runtime_paths, manager, _broker_target("@alice:example.org", scope), provider_id="demo")
        for scope in ("shared", "user", "user_agent")
    ]

    assert all(isinstance(result, SecretMissing) for result in results)


@pytest.mark.usefixtures("demo_registry", "enable_reuse")
def test_unscoped_worker_reads_the_store_its_tools_use(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
) -> None:
    """An unscoped call's token names its routed worker, but its OAuth scope is the tools' unscoped store."""
    tool_target = _tool_target("@alice:example.org", None)
    assert tool_target.worker_key is None
    connect_account(manager, tool_target, "unscoped-access", provider=_DEMO)
    claims = WorkerClaims.from_worker_target(replace(tool_target, worker_key="v1:local:unscoped:code"))
    assert claims is not None

    result = _resolve(config, runtime_paths, manager, claims.to_worker_target(), provider_id="demo")

    assert result == Secret("unscoped-access")


@pytest.mark.usefixtures("enable_reuse")
def test_status_reports_connection_without_refreshing(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Status reads the stored connection without a token request; an expired but refreshable token counts."""
    connect_account(
        manager,
        account_store("@alice:example.org"),
        "stale-access",
        expires_at=1.0,
        _oauth_claims={"email": "alice@example.org"},
        _oauth_claims_verified=True,
    )
    presented = serve_token_endpoint(monkeypatch, [])

    alice = _status(config, runtime_paths, manager, _broker_target("@alice:example.org"))
    bob = _status(config, runtime_paths, manager, _broker_target("@bob:example.org"))

    assert alice == OAuthStatus(
        provider="github",
        display_name="GitHub",
        connected=True,
        account_label="alice@example.org",
        can_connect=True,
        reset_required=False,
        reuse_enabled=True,
    )
    assert bob == OAuthStatus(
        provider="github",
        display_name="GitHub",
        connected=False,
        account_label=None,
        can_connect=True,
        reset_required=False,
        reuse_enabled=True,
    )
    assert presented == []


@pytest.mark.usefixtures("enable_reuse")
def test_status_with_a_service_account_matches_what_the_broker_injects(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service account is never connectable and not a token, but a stored personal token is still used."""
    monkeypatch.setattr(oauth_source, "oauth_provider_service_account_configured", lambda *_args: True)
    connect_account(manager, account_store("@alice:example.org"), "alice-access")

    alice = _status(config, runtime_paths, manager, _broker_target("@alice:example.org"))
    bob = _status(config, runtime_paths, manager, _broker_target("@bob:example.org"))

    assert alice is not None
    assert (alice.service_account, alice.connected, alice.can_connect) == (True, True, False)
    assert bob is not None
    assert (bob.service_account, bob.connected, bob.can_connect) == (True, False, False)
    assert _resolve(config, runtime_paths, manager, _broker_target("@alice:example.org")) == Secret("alice-access")
    assert _resolve(config, runtime_paths, manager, _broker_target("@bob:example.org")) == SecretMissing()


def _never(*_args: object, **_kwargs: object) -> None:
    pytest.fail("the broker must not read or refresh a connection Agent Keys may not use")


@pytest.mark.usefixtures("demo_registry")
@pytest.mark.parametrize(
    ("provider", "target", "requester_owned"),
    [
        (github_oauth_provider(), _broker_target("@alice:example.org"), True),
        (_DEMO, _broker_target("@alice:example.org", "shared"), False),
    ],
    ids=["requester-scoped", "agent-scoped"],
)
def test_without_an_opt_in_a_connection_is_never_read_refreshed_or_linked(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
    monkeypatch: pytest.MonkeyPatch,
    provider: OAuthProvider,
    target: ResolvedWorkerTarget,
    requester_owned: bool,
) -> None:
    """A connected account serves only its tools until someone allows Agent Keys to use it for the service.

    The broker neither reads nor refreshes it, not even for the status that placeholders and the agent tool read, and
    hands out no connect link; the result names the provider and says whether the account is the requester's own, so
    the refusal can say who may allow it.
    """
    connect_account(
        manager,
        _store_context(runtime_paths, manager, target, provider).worker_target,
        "stale",
        expires_at=1.0,
    )
    presented = serve_token_endpoint(monkeypatch, [])
    monkeypatch.setattr(oauth_source, "refresh_oauth_credentials_blocking", _never)
    monkeypatch.setattr(oauth_source, "load_oauth_credentials_snapshot_sync", _never)

    result = _resolve(config, runtime_paths, manager, target, provider_id=provider.id)
    status = _status(config, runtime_paths, manager, target, provider_id=provider.id)

    assert result == SecretMissing(provider=provider.id, reuse_off=True, requester_owned=requester_owned)
    assert presented == []
    assert status is not None
    assert (status.connected, status.account_label, status.reuse_enabled) == (False, None, False)


def test_an_opt_in_for_another_provider_counts_as_off(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
) -> None:
    """An opt-in names the provider it was given for, so changing a service's `oauth_provider` turns it off."""
    alice = _broker_target("@alice:example.org")
    connect_account(manager, account_store("@alice:example.org"), "alice-access")
    allow_oauth_reuse(manager, runtime_paths, account_store("@alice:example.org"), provider=_DEMO)

    result = _resolve(config, runtime_paths, manager, alice)
    status = _status(config, runtime_paths, manager, alice)

    assert result == SecretMissing(provider="github", reuse_off=True, requester_owned=True)
    assert status is not None
    assert (status.connected, status.reuse_enabled) == (False, False)


def test_one_requesters_opt_in_never_unlocks_anothers_account(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
) -> None:
    """Alice allowing her GitHub account leaves Bob's connected account to his tools only."""
    connect_account(manager, account_store("@alice:example.org"), "alice-access")
    connect_account(manager, account_store("@bob:example.org"), "bob-access")
    allow_oauth_reuse(manager, runtime_paths, account_store("@alice:example.org"))

    alice = _resolve(config, runtime_paths, manager, _broker_target("@alice:example.org"))
    bob = _resolve(config, runtime_paths, manager, _broker_target("@bob:example.org"))

    assert alice == Secret("alice-access")
    assert bob == SecretMissing(provider="github", reuse_off=True, requester_owned=True)


def test_a_requester_scoped_opt_in_lives_in_the_requesters_store_and_covers_their_agents(
    opted_in_config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
) -> None:
    """Allowing GitHub from one agent writes the requester's own store, where the connection lives.

    So it covers every agent of theirs with a service of that name, a shared one that opts in included.
    """
    connect_account(manager, account_store("@alice:example.org"), "alice-access")
    allow_oauth_reuse(manager, runtime_paths, worker_target(agent_name="code"))

    stored = load_egress_document(manager, account_store("@alice:example.org"), OAUTH_REUSE_CREDENTIAL_SERVICE)
    other_agent = WorkerClaims.from_worker_target(worker_target(agent_name="docs"))
    assert other_agent is not None
    on_other_agent = _resolve(opted_in_config, runtime_paths, manager, other_agent.to_worker_target())
    on_shared_agent = _resolve(opted_in_config, runtime_paths, manager, _broker_target("@alice:example.org", "shared"))

    assert stored is not None
    assert stored["services"]["github"]["provider"] == "github"
    assert stored["services"]["github"]["enabled_by"] == "@alice:example.org"
    assert on_other_agent == on_shared_agent == Secret("alice-access")


def test_status_says_whether_agent_keys_may_use_the_connection_and_revoking_stops_it(
    config: Config,
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
) -> None:
    """The status flag follows the opt-in, and removing the opt-in stops the token at once."""
    alice = _broker_target("@alice:example.org")
    connect_account(manager, account_store("@alice:example.org"), "alice-access")
    context = broker_credential_context(github_oauth_provider(), config, runtime_paths, manager, alice)

    before = _status(config, runtime_paths, manager, alice)
    set_oauth_reuse(context, "github", "github", enabled=True, by="@alice:example.org")
    allowed = (_status(config, runtime_paths, manager, alice), _resolve(config, runtime_paths, manager, alice))
    set_oauth_reuse(context, "github", "github", enabled=False, by="@alice:example.org")
    revoked = _resolve(config, runtime_paths, manager, alice)

    assert before is not None
    assert (before.connected, before.reuse_enabled) == (False, False)
    assert allowed[0] is not None
    assert (allowed[0].connected, allowed[0].reuse_enabled) == (True, True)
    assert allowed[1] == Secret("alice-access")
    assert revoked == SecretMissing(provider="github", reuse_off=True, requester_owned=True)
    assert load_egress_document(manager, account_store("@alice:example.org"), OAUTH_REUSE_CREDENTIAL_SERVICE) is None
