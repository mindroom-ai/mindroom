"""Tests for the egress_credentials agent tool."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from mindroom.config.egress_broker import EgressService
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.custom_tools import egress_credentials as egress_credentials_module
from mindroom.custom_tools.egress_credentials import EgressCredentialsTools
from mindroom.egress_broker.secrets import save_secret
from mindroom.egress_broker.user_services import save_user_service
from mindroom.message_target import MessageTarget
from mindroom.oauth.credential_lifecycle import resolve_oauth_credential_context
from mindroom.oauth.credential_store import _oauth_credential_database_path
from mindroom.oauth.github import github_oauth_provider
from mindroom.oauth.google_drive import google_drive_oauth_provider
from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess, ToolManagedInitArg, ToolStatus
from mindroom.tool_system.metadata import TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity, resolve_worker_target
from tests.conftest import make_conversation_reader_mock, make_relation_lookup
from tests.oauth_test_utils import corrupt_oauth_credential_payload, publish_oauth_credentials

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.oauth.providers import OAuthProvider

_SECRET = "ghp_super_secret_token_value"  # noqa: S105
_ACCESS_TOKEN = "gho_oauth_access_token_value"  # noqa: S105
_CONNECT_PATH = "/api/oauth/github/authorize"
_DRIVE_SERVICES: dict[str, object] = {"drive": {"preset": "google_drive"}}
_PRESET_SERVICES: dict[str, object] = {
    "github": {"preset": "github"},
    "openai": {"preset": "openai"},
}
_SERVICES: dict[str, object] = {
    "github": {
        "display_name": "GitHub",
        "rules": [{"host": "api.github.com", "auth": {"type": "bearer"}}],
    },
    "openai": {
        "rules": [{"host": "api.openai.com", "auth": {"type": "bearer"}}],
    },
}


def _runtime_paths(tmp_path: Path, **env: str) -> RuntimePaths:
    """Return a runtime on the Docker worker backend unless `env` names another, so `user_agent` workers are private."""
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MINDROOM_NAMESPACE": "", "MINDROOM_WORKER_BACKEND": "docker", **env},
    )


def _target(requester_id: str = "@alice:example.org", worker_scope: str | None = "user_agent") -> ResolvedWorkerTarget:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id=requester_id,
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
        tenant_id=None,
        account_id=None,
    )
    return resolve_worker_target(worker_scope, "code", identity)  # type: ignore[arg-type]


def _context(runtime_paths: RuntimePaths, services: dict[str, object] | None = None) -> ToolRuntimeContext:
    return ToolRuntimeContext(
        agent_name="code",
        target=MessageTarget.resolve(room_id="!room:example.org", thread_id="$thread", reply_to_event_id="$request"),
        requester_id="@alice:example.org",
        client=MagicMock(),
        config=Config(egress_broker={"services": _SERVICES if services is None else services}),
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )


def _list(tool: EgressCredentialsTools, context: ToolRuntimeContext | None) -> dict[str, object]:
    with tool_runtime_context(context):
        return json.loads(tool.list_egress_credentials())


def _statuses(payload: dict[str, object]) -> dict[str, bool]:
    services = payload["services"]
    assert isinstance(services, list)
    return {entry["name"]: entry["configured"] for entry in services}


def _entry(payload: dict[str, object], name: str) -> dict[str, object]:
    services = payload["services"]
    assert isinstance(services, list)
    return next(entry for entry in services if entry["name"] == name)


def _github_store(requester_id: str) -> ResolvedWorkerTarget:
    """Return the requester's own store, where GitHub connections live whatever the agent's scope."""
    return resolve_worker_target(
        "user",
        "code",
        ToolExecutionIdentity(
            channel="matrix",
            agent_name="code",
            requester_id=requester_id,
            room_id="!room:example.org",
            thread_id="$thread",
            resolved_thread_id="$thread",
            session_id="session-1",
            tenant_id=None,
            account_id=None,
        ),
    )


def _configure_github_client(manager: CredentialsManager) -> None:
    manager.save_credentials("github_oauth_client", {"client_id": "github-client-id", "client_secret": "gh-secret"})


def _connect_github(manager: CredentialsManager, requester_id: str) -> None:
    provider = github_oauth_provider()
    publish_oauth_credentials(
        provider,
        {
            "token": _ACCESS_TOKEN,
            "refresh_token": "github-refresh",
            "token_uri": provider.token_url,
            "client_id": "github-client-id",
            "scopes": list(provider.scopes),
            "expires_at": 4_102_444_800.0,
            "_source": "oauth",
            "_oauth_provider": provider.id,
        },
        credentials_manager=manager,
        worker_target=_github_store(requester_id),
    )


def _github_tool(runtime_paths: RuntimePaths, requester_id: str = "@alice:example.org") -> EgressCredentialsTools:
    return EgressCredentialsTools(runtime_paths=runtime_paths, worker_target=_target(requester_id))


def _configure_drive_client(manager: CredentialsManager) -> None:
    manager.save_credentials(
        "google_drive_oauth_client",
        {"client_id": "drive-client-id", "client_secret": "gd-secret"},
    )


def _connect_drive(
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
    requester_id: str,
    worker_scope: str | None,
) -> None:
    """Store a Google Drive connection where the provider's own scope policy puts it for that call."""
    provider: OAuthProvider = google_drive_oauth_provider()
    store = resolve_oauth_credential_context(
        provider,
        runtime_paths,
        manager,
        _target(requester_id, worker_scope),
    ).worker_target
    publish_oauth_credentials(
        provider,
        {
            "token": _ACCESS_TOKEN,
            "refresh_token": "drive-refresh",
            "token_uri": provider.token_url,
            "client_id": "drive-client-id",
            "scopes": list(provider.scopes),
            "expires_at": 4_102_444_800.0,
            "_source": "oauth",
            "_oauth_provider": provider.id,
        },
        credentials_manager=manager,
        worker_target=store,
    )


def _drive_tool(
    runtime_paths: RuntimePaths,
    requester_id: str = "@alice:example.org",
    worker_scope: str | None = "user_agent",
) -> EgressCredentialsTools:
    return EgressCredentialsTools(runtime_paths=runtime_paths, worker_target=_target(requester_id, worker_scope))


def test_reports_configured_status_per_requester_scope(tmp_path: Path) -> None:
    """Each requester sees only the secrets in their own scope, with the service's display name."""
    runtime_paths = _runtime_paths(tmp_path)
    save_secret(get_runtime_credentials_manager(runtime_paths), _target("@alice:example.org"), "github", _SECRET)
    context = _context(runtime_paths)

    alice = _list(
        EgressCredentialsTools(runtime_paths=runtime_paths, worker_target=_target("@alice:example.org")),
        context,
    )
    bob = _list(EgressCredentialsTools(runtime_paths=runtime_paths, worker_target=_target("@bob:example.org")), context)

    assert alice["tool"] == "egress_credentials"
    assert alice["services"] == [
        {"name": "github", "display_name": "GitHub", "configured": True, "active_source": "key"},
        {
            "name": "openai",
            "display_name": "openai",
            "configured": False,
            "active_source": None,
            "can_connect_account": False,
            "provider": None,
        },
    ]
    assert _statuses(bob) == {"github": False, "openai": False}


def test_lists_the_scopes_own_services_next_to_config_services(tmp_path: Path) -> None:
    """A service the user defined in this agent's scope is listed with its status; other requesters never see it."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    context = _context(runtime_paths)
    mine = EgressService.model_validate(
        {"display_name": "Mine", "rules": [{"host": "api.example.com", "auth": {"type": "bearer"}}]},
    )
    save_user_service(
        manager,
        _target(),
        "mine",
        mine,
        config_services=context.config.egress_broker.services,
        oauth_providers=(),
    )
    save_secret(manager, _target(), "mine", _SECRET)

    alice = _list(EgressCredentialsTools(runtime_paths=runtime_paths, worker_target=_target()), context)
    bob = _list(EgressCredentialsTools(runtime_paths=runtime_paths, worker_target=_target("@bob:example.org")), context)

    assert _entry(alice, "mine") == {"name": "mine", "display_name": "Mine", "configured": True, "active_source": "key"}
    assert _statuses(alice) == {"github": False, "openai": False, "mine": True}
    assert _statuses(bob) == {"github": False, "openai": False}


def test_user_services_are_listed_without_config_services(tmp_path: Path) -> None:
    """With no operator services, the scope's own services are still listed instead of the no-services note."""
    runtime_paths = _runtime_paths(tmp_path)
    mine = EgressService.model_validate({"rules": [{"host": "api.example.com", "auth": {"type": "bearer"}}]})
    manager = get_runtime_credentials_manager(runtime_paths)
    save_user_service(manager, _target(), "mine", mine, config_services={}, oauth_providers=())

    payload = _list(
        EgressCredentialsTools(runtime_paths=runtime_paths, worker_target=_target()),
        _context(runtime_paths, services={}),
    )

    assert _statuses(payload) == {"mine": False}
    assert "No egress services" not in str(payload["note"])


def test_unscoped_target_reads_the_global_store(tmp_path: Path) -> None:
    """An agent without a worker scope uses the global secrets, which a scoped secret does not satisfy."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    save_secret(manager, None, "openai", _SECRET)
    save_secret(manager, _target(worker_scope="user_agent"), "github", _SECRET)

    payload = _list(
        EgressCredentialsTools(runtime_paths=runtime_paths, worker_target=_target(worker_scope=None)),
        _context(runtime_paths),
    )

    assert _statuses(payload) == {"github": False, "openai": True}


def test_manage_url_points_at_the_dashboard_or_the_personal_page(tmp_path: Path) -> None:
    """The link follows the broker's rule: the personal page behind trusted upstream auth, else the dashboard."""
    dashboard = _runtime_paths(tmp_path, MINDROOM_PUBLIC_URL="https://mindroom.example/")
    personal = _runtime_paths(
        tmp_path,
        MINDROOM_PUBLIC_URL="https://mindroom.example",
        MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED="true",
    )

    dashboard_payload = _list(
        EgressCredentialsTools(runtime_paths=dashboard, worker_target=_target()),
        _context(dashboard),
    )
    personal_payload = _list(
        EgressCredentialsTools(runtime_paths=personal, worker_target=_target()),
        _context(personal),
    )

    assert dashboard_payload["manage_url"] == "https://mindroom.example/"
    assert personal_payload["manage_url"] == "https://mindroom.example/connections/egress"
    assert "https://mindroom.example/connections/egress" in str(personal_payload["note"])


def test_manage_url_is_null_without_a_public_url(tmp_path: Path) -> None:
    """Without a public URL the tool still answers and tells the agent to ask the operator."""
    runtime_paths = _runtime_paths(tmp_path)

    payload = _list(
        EgressCredentialsTools(runtime_paths=runtime_paths, worker_target=_target()),
        _context(runtime_paths),
    )

    assert payload["manage_url"] is None
    assert "operator" in str(payload["note"])


def test_output_never_contains_secret_values(tmp_path: Path) -> None:
    """Status output carries names and flags only, never a stored secret or its timestamp."""
    runtime_paths = _runtime_paths(tmp_path, MINDROOM_PUBLIC_URL="https://mindroom.example")
    manager = get_runtime_credentials_manager(runtime_paths)
    save_secret(manager, _target(), "github", _SECRET)
    save_secret(manager, _target(), "openai", _SECRET + "_two")
    tool = EgressCredentialsTools(runtime_paths=runtime_paths, worker_target=_target())

    with tool_runtime_context(_context(runtime_paths)):
        output = tool.list_egress_credentials()

    assert _statuses(json.loads(output)) == {"github": True, "openai": True}
    assert "ghp_" not in output
    assert "_updated_at" not in output
    assert "secret" not in set(json.loads(output))


def test_without_worker_target_explains_a_worker_scoped_agent_is_required(tmp_path: Path) -> None:
    """No worker target means no scope to check, so no services are listed."""
    runtime_paths = _runtime_paths(tmp_path, MINDROOM_PUBLIC_URL="https://mindroom.example")

    payload = _list(EgressCredentialsTools(runtime_paths=runtime_paths), _context(runtime_paths))

    assert payload["tool"] == "egress_credentials"
    assert payload["services"] == []
    assert payload["manage_url"] == "https://mindroom.example/"
    assert "worker-scoped" in str(payload["note"])


def test_without_runtime_context_returns_empty_services(tmp_path: Path) -> None:
    """Outside a live request there is no committed config, so the tool reports nothing instead of raising."""
    runtime_paths = _runtime_paths(tmp_path)

    payload = _list(EgressCredentialsTools(runtime_paths=runtime_paths, worker_target=_target()), None)

    assert payload["services"] == []
    assert "configuration" in str(payload["note"])


def test_no_configured_services_says_so(tmp_path: Path) -> None:
    """A config with no egress services gets a note saying none are set up."""
    runtime_paths = _runtime_paths(tmp_path)

    payload = _list(
        EgressCredentialsTools(runtime_paths=runtime_paths, worker_target=_target()),
        _context(runtime_paths, services={}),
    )

    assert payload["services"] == []
    assert "No egress services" in str(payload["note"])


def test_registers_and_builds_via_metadata(tmp_path: Path) -> None:
    """The tool builds through the registry with runtime paths and worker target injected."""
    metadata = TOOL_METADATA["egress_credentials"]
    assert metadata.display_name == "Egress Credentials"
    assert (
        metadata.description
        == "See which API keys and connected accounts this agent can use through the egress broker and where to add them"
    )
    assert metadata.category is ToolCategory.INTEGRATIONS
    assert metadata.status is ToolStatus.AVAILABLE
    assert metadata.file_access is ToolFileAccess.NONE
    assert metadata.requires_primary_runtime is True
    assert metadata.managed_init_args == (ToolManagedInitArg.RUNTIME_PATHS, ToolManagedInitArg.WORKER_TARGET)
    assert metadata.function_names == ("list_egress_credentials",)

    tool = get_tool_by_name("egress_credentials", _runtime_paths(tmp_path), worker_target=_target())

    assert isinstance(tool, EgressCredentialsTools)
    assert [function.__name__ for function in tool.tools] == ["list_egress_credentials"]


def test_connected_account_is_the_active_source_without_a_key(tmp_path: Path) -> None:
    """A service whose GitHub account is connected reports `oauth` and no connect hint; other requesters see none."""
    runtime_paths = _runtime_paths(tmp_path, MINDROOM_PUBLIC_URL="https://mindroom.example")
    manager = get_runtime_credentials_manager(runtime_paths)
    _configure_github_client(manager)
    _connect_github(manager, "@alice:example.org")
    context = _context(runtime_paths, _PRESET_SERVICES)

    alice = _list(_github_tool(runtime_paths), context)
    bob = _list(_github_tool(runtime_paths, "@bob:example.org"), context)

    assert _entry(alice, "github") == {
        "name": "github",
        "display_name": "GitHub",
        "configured": True,
        "active_source": "oauth",
    }
    assert _entry(bob, "github")["configured"] is False


@pytest.mark.parametrize("worker_scope", ["shared", None], ids=["shared", "unscoped"])
def test_github_account_is_not_offered_where_requesters_share_a_worker(
    tmp_path: Path,
    worker_scope: str | None,
) -> None:
    """The broker never uses a GitHub account on a shared or unscoped worker, so the tool neither counts nor offers it."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    _configure_github_client(manager)
    _connect_github(manager, "@alice:example.org")
    context = _context(runtime_paths, _PRESET_SERVICES)

    alice = _list(
        EgressCredentialsTools(runtime_paths=runtime_paths, worker_target=_target("@alice:example.org", worker_scope)),
        context,
    )

    assert _entry(alice, "github") == {
        "name": "github",
        "display_name": "GitHub",
        "configured": False,
        "active_source": None,
        "can_connect_account": False,
        "provider": None,
    }


def test_github_account_is_not_offered_on_the_static_runner_without_the_opt_in(tmp_path: Path) -> None:
    """Every call shares the static runner's process, so a `user_agent` agent gets no GitHub account there either."""
    runtime_paths = _runtime_paths(tmp_path, MINDROOM_WORKER_BACKEND="static_runner")
    manager = get_runtime_credentials_manager(runtime_paths)
    _configure_github_client(manager)
    _connect_github(manager, "@alice:example.org")
    opted_in = {**_PRESET_SERVICES, "github": {"preset": "github", "oauth_on_shared_workers": True}}

    refused = _list(_github_tool(runtime_paths), _context(runtime_paths, _PRESET_SERVICES))
    allowed = _list(_github_tool(runtime_paths), _context(runtime_paths, opted_in))

    assert _entry(refused, "github") == {
        "name": "github",
        "display_name": "GitHub",
        "configured": False,
        "active_source": None,
        "can_connect_account": False,
        "provider": None,
    }
    assert _entry(allowed, "github")["active_source"] == "oauth"


def test_github_account_is_per_requester_on_a_shared_agent_with_the_opt_in(tmp_path: Path) -> None:
    """With `oauth_on_shared_workers`, GitHub follows the requester on a shared agent: Alice's account is not Bob's."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    _configure_github_client(manager)
    _connect_github(manager, "@alice:example.org")
    context = _context(
        runtime_paths,
        {**_PRESET_SERVICES, "github": {"preset": "github", "oauth_on_shared_workers": True}},
    )

    alice = _list(
        EgressCredentialsTools(runtime_paths=runtime_paths, worker_target=_target("@alice:example.org", "shared")),
        context,
    )
    bob = _list(
        EgressCredentialsTools(runtime_paths=runtime_paths, worker_target=_target("@bob:example.org", "shared")),
        context,
    )

    assert _entry(alice, "github")["active_source"] == "oauth"
    assert _entry(bob, "github")["active_source"] is None
    assert _entry(bob, "github")["can_connect_account"] is True


def test_explicit_key_wins_over_a_connected_account(tmp_path: Path) -> None:
    """With both sources present the entry names the key, the one the broker injects."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    _configure_github_client(manager)
    _connect_github(manager, "@alice:example.org")
    save_secret(manager, _target(), "github", _SECRET)

    payload = _list(_github_tool(runtime_paths), _context(runtime_paths, _PRESET_SERVICES))

    assert _entry(payload, "github") == {
        "name": "github",
        "display_name": "GitHub",
        "configured": True,
        "active_source": "key",
    }


def test_unconfigured_service_says_whether_an_account_can_be_connected(tmp_path: Path) -> None:
    """Without a key or connection, a service with a connectable provider offers to connect it; one without does not."""
    runtime_paths = _runtime_paths(tmp_path)
    _configure_github_client(get_runtime_credentials_manager(runtime_paths))

    payload = _list(_github_tool(runtime_paths), _context(runtime_paths, _PRESET_SERVICES))

    assert _entry(payload, "github") == {
        "name": "github",
        "display_name": "GitHub",
        "configured": False,
        "active_source": None,
        "can_connect_account": True,
        "provider": "github",
    }
    assert _entry(payload, "openai") == {
        "name": "openai",
        "display_name": "OpenAI",
        "configured": False,
        "active_source": None,
        "can_connect_account": False,
        "provider": None,
    }


def test_provider_without_a_client_cannot_be_connected(tmp_path: Path) -> None:
    """A provider whose OAuth client is not configured offers no connection and is not named."""
    runtime_paths = _runtime_paths(tmp_path)

    payload = _list(_github_tool(runtime_paths), _context(runtime_paths, _PRESET_SERVICES))

    entry = _entry(payload, "github")
    assert (entry["can_connect_account"], entry["provider"]) == (False, None)


def test_shared_service_account_is_not_injected_so_it_is_not_connected(tmp_path: Path) -> None:
    """A Google service account never supplies a token, and personal accounts are not connectable beside it."""
    runtime_paths = _runtime_paths(tmp_path, GOOGLE_SERVICE_ACCOUNT_FILE=str(tmp_path / "service-account.json"))
    _configure_drive_client(get_runtime_credentials_manager(runtime_paths))

    payload = _list(_drive_tool(runtime_paths), _context(runtime_paths, _DRIVE_SERVICES))

    assert _entry(payload, "drive") == {
        "name": "drive",
        "display_name": "Google Drive",
        "configured": False,
        "active_source": None,
        "can_connect_account": False,
        "provider": None,
    }


def test_google_account_follows_the_agent_scope_not_the_requester(tmp_path: Path) -> None:
    """A provider that is not requester-scoped uses the agent's connection: shared for all, per requester otherwise."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    _configure_drive_client(manager)
    _connect_drive(runtime_paths, manager, "@alice:example.org", "shared")
    context = _context(runtime_paths, _DRIVE_SERVICES)

    shared_alice = _list(_drive_tool(runtime_paths, "@alice:example.org", "shared"), context)
    shared_bob = _list(_drive_tool(runtime_paths, "@bob:example.org", "shared"), context)
    per_requester_alice = _list(_drive_tool(runtime_paths, "@alice:example.org", "user_agent"), context)

    assert _entry(shared_alice, "drive")["active_source"] == "oauth"
    assert _entry(shared_bob, "drive")["active_source"] == "oauth"
    assert _entry(per_requester_alice, "drive")["active_source"] is None
    assert _entry(per_requester_alice, "drive")["can_connect_account"] is True
    assert _entry(per_requester_alice, "drive")["provider"] == "google_drive"

    _connect_drive(runtime_paths, manager, "@alice:example.org", "user_agent")
    alice = _list(_drive_tool(runtime_paths, "@alice:example.org", "user_agent"), context)
    bob = _list(_drive_tool(runtime_paths, "@bob:example.org", "user_agent"), context)

    assert _entry(alice, "drive")["active_source"] == "oauth"
    assert _entry(bob, "drive")["active_source"] is None


def test_one_failing_provider_does_not_fail_the_whole_listing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service whose account status cannot be read is reported as unconfigured; the others still answer."""
    runtime_paths = _runtime_paths(tmp_path)
    save_secret(get_runtime_credentials_manager(runtime_paths), _target(), "openai", _SECRET)

    def failing_status(*_args: object, **_kwargs: object) -> None:
        msg = f"provider exploded with {_ACCESS_TOKEN}"
        raise RuntimeError(msg)

    monkeypatch.setattr(egress_credentials_module, "oauth_status", failing_status)

    with capture_logs() as logs:
        payload = _list(_github_tool(runtime_paths), _context(runtime_paths, _PRESET_SERVICES))

    assert _entry(payload, "github") == {
        "name": "github",
        "display_name": "GitHub",
        "configured": False,
        "active_source": None,
        "can_connect_account": False,
        "provider": None,
    }
    assert _entry(payload, "openai")["active_source"] == "key"
    failures = [log for log in logs if log["event"] == "egress_credentials_status_failed"]
    assert [(log["service"], log["error_type"]) for log in failures] == [("github", "RuntimeError")]
    assert _ACCESS_TOKEN not in repr(logs)


def test_unreadable_connection_cannot_be_connected_until_reset(tmp_path: Path) -> None:
    """A stored credential that cannot be decoded needs a reset first, so the agent is not told to connect."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    _configure_github_client(manager)
    _connect_github(manager, "@alice:example.org")
    provider = github_oauth_provider()
    corrupt_oauth_credential_payload(
        _oauth_credential_database_path(
            resolve_oauth_credential_context(provider, runtime_paths, manager, _github_store("@alice:example.org")),
        ),
        base64.b64encode(b"not a credential"),
    )

    payload = _list(_github_tool(runtime_paths), _context(runtime_paths, _PRESET_SERVICES))

    entry = _entry(payload, "github")
    assert (entry["configured"], entry["can_connect_account"]) == (False, False)


def test_unknown_oauth_provider_is_ignored(tmp_path: Path) -> None:
    """A service naming a provider the registry lacks has no OAuth source, and the tool still answers."""
    runtime_paths = _runtime_paths(tmp_path)
    services = {"acme": {"oauth_provider": "acme", "rules": [{"host": "api.acme.test", "auth": {"type": "bearer"}}]}}

    payload = _list(_github_tool(runtime_paths), _context(runtime_paths, services))

    assert _entry(payload, "acme") == {
        "name": "acme",
        "display_name": "acme",
        "configured": False,
        "active_source": None,
        "can_connect_account": False,
        "provider": None,
    }


def test_output_carries_no_token_or_connect_link(tmp_path: Path) -> None:
    """Neither an access token nor a one-time connect link reaches the model; only the manage page does."""
    runtime_paths = _runtime_paths(tmp_path, MINDROOM_PUBLIC_URL="https://mindroom.example")
    manager = get_runtime_credentials_manager(runtime_paths)
    _configure_github_client(manager)
    _connect_github(manager, "@alice:example.org")
    context = _context(runtime_paths, _PRESET_SERVICES)

    connected = _github_tool(runtime_paths)
    with tool_runtime_context(context):
        connected_output = connected.list_egress_credentials()
        unconnected_output = _github_tool(runtime_paths, "@bob:example.org").list_egress_credentials()

    for output in (connected_output, unconnected_output):
        assert _ACCESS_TOKEN not in output
        assert "github-refresh" not in output
        assert _CONNECT_PATH not in output
        assert "token=" not in output
        assert json.loads(output)["manage_url"] == "https://mindroom.example/"


def test_note_points_at_connecting_an_account_or_adding_a_key(tmp_path: Path) -> None:
    """The note covers both fixes, where to do them, and that a key wins; it never asks for a pasted key."""
    runtime_paths = _runtime_paths(tmp_path, MINDROOM_PUBLIC_URL="https://mindroom.example")

    payload = _list(_github_tool(runtime_paths), _context(runtime_paths, _PRESET_SERVICES))

    note = str(payload["note"])
    assert "neither an API key nor a connected account" in note
    assert "an API key wins over a connected account" in note
    assert (
        "When `can_connect_account` is true, the user can connect their `provider` account at https://mindroom.example/"
        in note
    )
    assert "otherwise, or to use their own key instead, they can add an API key there" in note
    assert "Never ask the user to paste a key into the chat." in note


def test_note_without_a_public_url_sends_the_agent_to_the_operator(tmp_path: Path) -> None:
    """With no manage link the note still names both fixes and tells the agent to ask the operator where to do them."""
    runtime_paths = _runtime_paths(tmp_path)

    payload = _list(_github_tool(runtime_paths), _context(runtime_paths, _PRESET_SERVICES))

    note = str(payload["note"])
    assert "ask the operator for it" in note
    assert "http" not in note
