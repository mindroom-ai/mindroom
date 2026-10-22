"""Tests for the egress_credentials agent tool."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from mindroom.config.agent import AgentConfig
from mindroom.config.egress_broker import EgressService
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
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
from mindroom.tool_system.dynamic_toolkits import visible_tool_surface
from mindroom.tool_system.metadata import TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import bind_runtime_paths, make_conversation_reader_mock, make_relation_lookup
from tests.egress_broker.conftest import account_store, allow_oauth_reuse, connect_account, worker_target
from tests.oauth_test_utils import corrupt_oauth_credential_payload

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.tool_system.worker_routing import WorkerScope

_ALICE = "@alice:example.org"
_BOB = "@bob:example.org"
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


# Serves the personal egress page: the broker is on behind trusted upstream auth with JWT.
_PAGE_SERVED = {
    "MINDROOM_EGRESS_BROKER_PORT": "8768",
    "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
    "MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT": "true",
}


def _runtime_paths(tmp_path: Path, **env: str) -> RuntimePaths:
    """Return a runtime on the Docker worker backend unless `env` names another, so `user_agent` workers are private."""
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MINDROOM_NAMESPACE": "", "MINDROOM_WORKER_BACKEND": "docker", **env},
    )


def _context(runtime_paths: RuntimePaths, services: dict[str, object] | None = None) -> ToolRuntimeContext:
    return ToolRuntimeContext(
        agent_name="code",
        target=MessageTarget.resolve(room_id="!room:example.org", thread_id="$thread", reply_to_event_id="$request"),
        requester_id=_ALICE,
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


def _unconfigured(name: str, display_name: str | None = None, **changes: object) -> dict[str, object]:
    """Return the entry of a config service with no key, no account, and no account to connect, plus `changes`."""
    return {
        "name": name,
        "display_name": display_name or name,
        "source": "config",
        "configured": False,
        "active_source": None,
        "oauth_reuse": None,
        "can_connect_account": False,
        "provider": None,
        **changes,
    }


def _keyed(name: str, display_name: str | None = None, source: str = "config") -> dict[str, object]:
    """Return the entry of a service without an account login whose key is set."""
    return {
        "name": name,
        "display_name": display_name or name,
        "source": source,
        "configured": True,
        "active_source": "key",
        "oauth_reuse": None,
    }


def _tool(
    runtime_paths: RuntimePaths,
    requester_id: str = _ALICE,
    worker_scope: WorkerScope | None = "user_agent",
) -> EgressCredentialsTools:
    return EgressCredentialsTools(
        runtime_paths=runtime_paths,
        worker_target=worker_target(requester_id, scope=worker_scope),
    )


def _save_mine(
    manager: CredentialsManager,
    name: str,
    body: dict[str, object],
    services: dict[str, EgressService],
) -> None:
    """Save `body` as Alice's own service `name` on agent `code`, checked against the config's `services`."""
    service = EgressService.model_validate(body)
    save_user_service(manager, worker_target(), name, service, config_services=services, oauth_providers=())


def _configure_github_client(manager: CredentialsManager) -> None:
    manager.save_credentials("github_oauth_client", {"client_id": "github-client-id", "client_secret": "gh-secret"})


def _connect_github(manager: CredentialsManager) -> None:
    connect_account(manager, account_store(_ALICE), _ACCESS_TOKEN)


def _allow_github(runtime_paths: RuntimePaths, requester_id: str = _ALICE) -> None:
    """Let Agent Keys use the requester's GitHub account for the `github` service."""
    allow_oauth_reuse(get_runtime_credentials_manager(runtime_paths), runtime_paths, account_store(requester_id))


def _configure_drive_client(manager: CredentialsManager) -> None:
    manager.save_credentials(
        "google_drive_oauth_client",
        {"client_id": "drive-client-id", "client_secret": "gd-secret"},
    )


def _connect_drive(
    runtime_paths: RuntimePaths,
    manager: CredentialsManager,
    worker_scope: WorkerScope | None,
) -> None:
    """Store Alice's Google Drive connection where the provider's own scope policy puts it for that call."""
    provider = google_drive_oauth_provider()
    target = worker_target(_ALICE, scope=worker_scope)
    store = resolve_oauth_credential_context(provider, runtime_paths, manager, target).worker_target
    connect_account(
        manager,
        store,
        _ACCESS_TOKEN,
        provider=provider,
        client_id="drive-client-id",
        refresh_token="drive-refresh",  # noqa: S106 - test credential
    )
    allow_oauth_reuse(manager, runtime_paths, target, "drive", provider=provider)


def test_reports_configured_status_per_requester_scope(tmp_path: Path) -> None:
    """Each requester sees only the secrets in their own scope, with the service's display name."""
    runtime_paths = _runtime_paths(tmp_path)
    save_secret(get_runtime_credentials_manager(runtime_paths), worker_target(), "github", _SECRET)
    context = _context(runtime_paths)

    alice = _list(_tool(runtime_paths), context)
    bob = _list(_tool(runtime_paths, _BOB), context)

    assert alice["tool"] == "egress_credentials"
    assert alice["services"] == [_keyed("github", "GitHub"), _unconfigured("openai")]
    assert _statuses(bob) == {"github": False, "openai": False}


def test_lists_the_scopes_own_services_next_to_config_services(tmp_path: Path) -> None:
    """A service the user defined in this agent's scope is listed with its status; other requesters never see it."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    context = _context(runtime_paths)
    _save_mine(
        manager,
        "mine",
        {"display_name": "Mine", "rules": [{"host": "api.example.com", "auth": {"type": "bearer"}}]},
        context.config.egress_broker.services,
    )
    save_secret(manager, worker_target(), "mine", _SECRET)

    alice = _list(_tool(runtime_paths), context)
    bob = _list(_tool(runtime_paths, _BOB), context)

    assert _entry(alice, "mine") == _keyed("mine", "Mine", source="user")
    assert _entry(alice, "github")["source"] == "config"
    assert _statuses(alice) == {"github": False, "openai": False, "mine": True}
    assert _statuses(bob) == {"github": False, "openai": False}


def test_a_restrict_only_limit_is_not_listed_as_a_missing_credential(tmp_path: Path) -> None:
    """A limit never takes a credential, so the agent is not told to ask for one; the service it limits stays listed."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    context = _context(runtime_paths)
    _save_mine(
        manager,
        "github-limited",
        {"restrict_only": True, "rules": [{"host": "api.github.com", "path_prefix": "/repos/o/a"}]},
        context.config.egress_broker.services,
    )

    payload = _list(_tool(runtime_paths), context)

    assert _statuses(payload) == {"github": False, "openai": False}


def test_user_services_are_listed_without_config_services(tmp_path: Path) -> None:
    """With no operator services, the scope's own services are still listed instead of the no-services note."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    _save_mine(manager, "mine", {"rules": [{"host": "api.example.com", "auth": {"type": "bearer"}}]}, {})

    payload = _list(_tool(runtime_paths), _context(runtime_paths, services={}))

    assert _statuses(payload) == {"mine": False}
    assert "No egress services" not in str(payload["note"])


def test_every_entry_says_who_defined_the_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`source` is `config` for the administrator's services and `user` for the scope's own, also when a status fails."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    context = _context(runtime_paths)
    mine = {"rules": [{"host": "api.example.com", "auth": {"type": "bearer"}}]}
    _save_mine(manager, "mine", mine, context.config.egress_broker.services)
    tool = _tool(runtime_paths)

    payload = _list(tool, context)

    assert {entry["name"]: entry["source"] for entry in payload["services"]} == {
        "github": "config",
        "openai": "config",
        "mine": "user",
    }
    assert "`source` is `config` for a service the administrator defined and `user` for one the user added" in str(
        payload["note"],
    )

    def failing_status(*_args: object, **_kwargs: object) -> None:
        msg = "status exploded"
        raise RuntimeError(msg)

    monkeypatch.setattr(egress_credentials_module, "service_status", failing_status)

    fallback = _list(tool, context)

    assert {entry["name"]: entry["source"] for entry in fallback["services"]} == {
        "github": "config",
        "openai": "config",
        "mine": "user",
    }
    assert all(entry["configured"] is False for entry in fallback["services"])


def test_unscoped_target_reads_the_global_store(tmp_path: Path) -> None:
    """An agent without a worker scope uses the global secrets, which a scoped secret does not satisfy."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    save_secret(manager, None, "openai", _SECRET)
    save_secret(manager, worker_target(), "github", _SECRET)

    payload = _list(_tool(runtime_paths, worker_scope=None), _context(runtime_paths))

    assert _statuses(payload) == {"github": False, "openai": True}


def test_manage_url_points_at_the_dashboard_or_the_personal_page(tmp_path: Path) -> None:
    """The link follows the broker's rule: the personal page where it is served, else the dashboard."""
    dashboard = _runtime_paths(tmp_path, MINDROOM_PUBLIC_URL="https://mindroom.example/")
    personal = _runtime_paths(tmp_path, MINDROOM_PUBLIC_URL="https://mindroom.example", **_PAGE_SERVED)

    dashboard_payload = _list(_tool(dashboard, worker_scope="shared"), _context(dashboard))
    personal_payload = _list(_tool(personal), _context(personal))

    assert dashboard_payload["manage_url"] == "https://mindroom.example/"
    assert personal_payload["manage_url"] == "https://mindroom.example/connections/egress"
    assert "https://mindroom.example/connections/egress" in str(personal_payload["note"])


# What the tool says where a personal scope's keys cannot be set: the broker's refusal says the same.
_PAGE_NOT_SERVED_NOTE = (
    "Each user sets this agent's keys and accounts on the Connections page, which nobody can sign in to here. "
    "An administrator can enable Connections sign-in or make the agent shared."
)


@pytest.mark.parametrize("worker_scope", ["user", "user_agent"])
@pytest.mark.parametrize(
    "env",
    [
        {},
        {"MINDROOM_EGRESS_BROKER_PORT": "8768", "MINDROOM_CONNECTIONS_ALLOWED_ORIGINS": "[]"},
        {
            "MINDROOM_EGRESS_BROKER_PORT": "8768",
            "MINDROOM_CONNECTIONS_ALLOWED_ORIGINS": "[]",
            "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
        },
    ],
    ids=["broker-off", "chat-sign-in-off", "trusted-without-jwt"],
)
def test_personal_scope_without_the_personal_page_is_sent_to_no_page(
    tmp_path: Path,
    worker_scope: WorkerScope,
    env: dict[str, str],
) -> None:
    """Only the personal page sets a personal scope's keys; where it is not served the tool names no page, as the broker.

    With sign-in from Chat off, trusted upstream auth without JWT does not serve it either: the personal API answers
    403 without signed requests.
    """
    runtime_paths = _runtime_paths(tmp_path, MINDROOM_PUBLIC_URL="https://mindroom.example", **env)
    tool = _tool(runtime_paths, worker_scope=worker_scope)

    payload = _list(tool, _context(runtime_paths, _PRESET_SERVICES))
    no_services = _list(tool, _context(runtime_paths, services={}))

    note = str(payload["note"])
    assert payload["manage_url"] is None
    assert no_services["manage_url"] is None
    assert _PAGE_NOT_SERVED_NOTE in note
    assert "When `can_connect_account` is true, the user can still connect their `provider` account at" in note
    assert "connect_url" in note
    assert "https://" not in note
    assert "Never ask the user to paste a key into the chat." in note


@pytest.mark.parametrize("worker_scope", ["shared", None])
def test_shared_scope_without_the_personal_page_keeps_the_dashboard(
    tmp_path: Path,
    worker_scope: WorkerScope | None,
) -> None:
    """The dashboard sets shared and unscoped keys, so the tool still sends their users there."""
    runtime_paths = _runtime_paths(tmp_path, MINDROOM_PUBLIC_URL="https://mindroom.example")

    payload = _list(_tool(runtime_paths, worker_scope=worker_scope), _context(runtime_paths))

    assert payload["manage_url"] == "https://mindroom.example/"
    assert "at https://mindroom.example/" in str(payload["note"])
    assert _PAGE_NOT_SERVED_NOTE not in str(payload["note"])


def test_manage_url_is_null_without_a_public_url(tmp_path: Path) -> None:
    """Without a public URL the tool still answers and tells the agent to ask the operator."""
    runtime_paths = _runtime_paths(tmp_path, **_PAGE_SERVED)

    payload = _list(_tool(runtime_paths), _context(runtime_paths))

    assert payload["manage_url"] is None
    assert "operator" in str(payload["note"])


def test_output_never_contains_secret_values(tmp_path: Path) -> None:
    """Status output carries names and flags only, never a stored secret or its timestamp."""
    runtime_paths = _runtime_paths(tmp_path, MINDROOM_PUBLIC_URL="https://mindroom.example")
    manager = get_runtime_credentials_manager(runtime_paths)
    save_secret(manager, worker_target(), "github", _SECRET)
    save_secret(manager, worker_target(), "openai", _SECRET + "_two")
    tool = _tool(runtime_paths)

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

    payload = _list(_tool(runtime_paths), None)

    assert payload["services"] == []
    assert "configuration" in str(payload["note"])


def test_no_configured_services_says_so(tmp_path: Path) -> None:
    """A config with no egress services gets a note saying none are set up."""
    runtime_paths = _runtime_paths(tmp_path)

    payload = _list(_tool(runtime_paths), _context(runtime_paths, services={}))

    assert payload["services"] == []
    assert "No egress services" in str(payload["note"])
    assert "users can add services of their own" in str(payload["note"])


def test_registers_and_builds_via_metadata(tmp_path: Path) -> None:
    """The tool builds through the registry with runtime paths and worker target injected."""
    metadata = TOOL_METADATA["egress_credentials"]
    assert metadata.display_name == "Egress Credentials"
    assert metadata.description == (
        "Opt this agent's shell and python calls into Agent Keys and see which keys and accounts it can use"
    )
    assert metadata.category is ToolCategory.INTEGRATIONS
    assert metadata.status is ToolStatus.AVAILABLE
    assert metadata.file_access is ToolFileAccess.NONE
    assert metadata.requires_primary_runtime is True
    assert metadata.managed_init_args == (ToolManagedInitArg.RUNTIME_PATHS, ToolManagedInitArg.WORKER_TARGET)
    assert metadata.function_names == ("list_egress_credentials",)

    tool = get_tool_by_name("egress_credentials", _runtime_paths(tmp_path), worker_target=worker_target())

    assert isinstance(tool, EgressCredentialsTools)
    assert [function.__name__ for function in tool.tools] == ["list_egress_credentials"]


@pytest.mark.parametrize("broker_on", [False, True])
def test_an_agent_gets_the_tool_only_while_the_broker_is_on(tmp_path: Path, *, broker_on: bool) -> None:
    """With the broker off a config that lists the tool stays valid, and the agent's tools leave it out."""
    runtime_paths = _runtime_paths(tmp_path, **({"MINDROOM_EGRESS_BROKER_PORT": "8768"} if broker_on else {}))
    agent = AgentConfig(display_name="Code", tools=["shell", "egress_credentials"], worker_scope="user_agent")
    config = bind_runtime_paths(Config(agents={"code": agent}), runtime_paths)

    toolkit_names = [
        entry.name for entry in visible_tool_surface(agent_name="code", config=config).runtime_tool_configs
    ]

    assert config.agents["code"].tool_names == ["shell", "egress_credentials"]
    assert "shell" in toolkit_names
    assert ("egress_credentials" in toolkit_names) is broker_on


def test_connected_account_is_the_active_source_without_a_key(tmp_path: Path) -> None:
    """A service whose GitHub account is connected and allowed reports `oauth` and no connect hint.

    Other requesters see none.
    """
    runtime_paths = _runtime_paths(tmp_path, MINDROOM_PUBLIC_URL="https://mindroom.example")
    manager = get_runtime_credentials_manager(runtime_paths)
    _configure_github_client(manager)
    _connect_github(manager)
    _allow_github(runtime_paths)
    context = _context(runtime_paths, _PRESET_SERVICES)

    alice = _list(_tool(runtime_paths), context)
    bob = _list(_tool(runtime_paths, _BOB), context)

    assert _entry(alice, "github") == {
        "name": "github",
        "display_name": "GitHub",
        "source": "config",
        "configured": True,
        "active_source": "oauth",
        "oauth_reuse": True,
    }
    assert _entry(bob, "github")["configured"] is False


def test_a_connected_account_agent_keys_may_not_use_is_named_until_it_is_allowed(tmp_path: Path) -> None:
    """An account serves only its tools until it is allowed, so the entry names it as not usable yet.

    The agent can then tell the user what allowing it means; nothing is offered to connect, and the connection is
    not read to say whether there is one.
    """
    runtime_paths = _runtime_paths(tmp_path, MINDROOM_PUBLIC_URL="https://mindroom.example")
    manager = get_runtime_credentials_manager(runtime_paths)
    _configure_github_client(manager)
    _connect_github(manager)
    context = _context(runtime_paths, _PRESET_SERVICES)

    connected = _list(_tool(runtime_paths), context)
    unconnected = _list(_tool(runtime_paths, _BOB), context)
    _allow_github(runtime_paths, _BOB)
    allowed_unconnected = _list(_tool(runtime_paths, _BOB), context)

    assert _entry(connected, "github") == _unconfigured("github", "GitHub", oauth_reuse=False, provider="github")
    assert _entry(unconnected, "github") == _unconfigured("github", "GitHub", oauth_reuse=False, provider="github")
    assert _entry(allowed_unconnected, "github") == _unconfigured(
        "github",
        "GitHub",
        oauth_reuse=True,
        can_connect_account=True,
        provider="github",
    )


@pytest.mark.parametrize("worker_scope", ["shared", None], ids=["shared", "unscoped"])
def test_github_account_is_not_offered_where_requesters_share_a_worker(
    tmp_path: Path,
    worker_scope: str | None,
) -> None:
    """The broker never uses a GitHub account on a shared or unscoped worker, so the tool neither counts nor offers it."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    _configure_github_client(manager)
    _connect_github(manager)
    context = _context(runtime_paths, _PRESET_SERVICES)

    alice = _list(_tool(runtime_paths, worker_scope=worker_scope), context)

    assert _entry(alice, "github") == _unconfigured("github", "GitHub")


def test_github_account_is_not_offered_on_the_static_runner_without_the_opt_in(tmp_path: Path) -> None:
    """Every call shares the static runner's process, so a `user_agent` agent gets no GitHub account there either."""
    runtime_paths = _runtime_paths(tmp_path, MINDROOM_WORKER_BACKEND="static_runner")
    manager = get_runtime_credentials_manager(runtime_paths)
    _configure_github_client(manager)
    _connect_github(manager)
    opted_in = {**_PRESET_SERVICES, "github": {"preset": "github", "oauth_on_shared_workers": True}}

    _allow_github(runtime_paths)

    refused = _list(_tool(runtime_paths), _context(runtime_paths, _PRESET_SERVICES))
    allowed = _list(_tool(runtime_paths), _context(runtime_paths, opted_in))

    assert _entry(refused, "github") == _unconfigured("github", "GitHub")
    assert _entry(allowed, "github")["active_source"] == "oauth"


def test_github_account_is_per_requester_on_a_shared_agent_with_the_opt_in(tmp_path: Path) -> None:
    """With `oauth_on_shared_workers`, GitHub follows the requester on a shared agent: Alice's account is not Bob's."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    _configure_github_client(manager)
    _connect_github(manager)
    _allow_github(runtime_paths)
    _allow_github(runtime_paths, _BOB)
    context = _context(
        runtime_paths,
        {**_PRESET_SERVICES, "github": {"preset": "github", "oauth_on_shared_workers": True}},
    )

    alice = _list(_tool(runtime_paths, worker_scope="shared"), context)
    bob = _list(_tool(runtime_paths, _BOB, "shared"), context)

    assert _entry(alice, "github")["active_source"] == "oauth"
    assert _entry(bob, "github")["active_source"] is None
    assert _entry(bob, "github")["can_connect_account"] is True


def test_explicit_key_wins_over_a_connected_account(tmp_path: Path) -> None:
    """With both sources present the entry names the key, the one the broker injects."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    _configure_github_client(manager)
    _connect_github(manager)
    save_secret(manager, worker_target(), "github", _SECRET)

    payload = _list(_tool(runtime_paths), _context(runtime_paths, _PRESET_SERVICES))

    assert _entry(payload, "github") == {**_keyed("github", "GitHub"), "oauth_reuse": False}


def test_unconfigured_service_says_whether_an_account_can_be_connected(tmp_path: Path) -> None:
    """Without a key or connection, a service with a connectable provider it may use offers to connect it.

    One without a provider does not.
    """
    runtime_paths = _runtime_paths(tmp_path)
    _configure_github_client(get_runtime_credentials_manager(runtime_paths))
    _allow_github(runtime_paths)

    payload = _list(_tool(runtime_paths), _context(runtime_paths, _PRESET_SERVICES))

    assert _entry(payload, "github") == _unconfigured(
        "github",
        "GitHub",
        oauth_reuse=True,
        can_connect_account=True,
        provider="github",
    )
    assert _entry(payload, "openai") == _unconfigured("openai", "OpenAI")


def test_provider_without_a_client_cannot_be_connected(tmp_path: Path) -> None:
    """A provider whose OAuth client is not configured offers no connection and is not named."""
    runtime_paths = _runtime_paths(tmp_path)
    _allow_github(runtime_paths)

    payload = _list(_tool(runtime_paths), _context(runtime_paths, _PRESET_SERVICES))

    entry = _entry(payload, "github")
    assert (entry["can_connect_account"], entry["provider"]) == (False, None)


def test_shared_service_account_is_not_injected_so_it_is_not_connected(tmp_path: Path) -> None:
    """A Google service account never supplies a token, and personal accounts are not connectable beside it."""
    runtime_paths = _runtime_paths(tmp_path, GOOGLE_SERVICE_ACCOUNT_FILE=str(tmp_path / "service-account.json"))
    _configure_drive_client(get_runtime_credentials_manager(runtime_paths))

    allow_oauth_reuse(
        get_runtime_credentials_manager(runtime_paths),
        runtime_paths,
        worker_target(),
        "drive",
        provider=google_drive_oauth_provider(),
    )

    payload = _list(_tool(runtime_paths), _context(runtime_paths, _DRIVE_SERVICES))

    assert _entry(payload, "drive") == _unconfigured("drive", "Google Drive", oauth_reuse=True)


def test_google_account_follows_the_agent_scope_not_the_requester(tmp_path: Path) -> None:
    """A provider that is not requester-scoped uses the agent's connection: shared for all, per requester otherwise."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    _configure_drive_client(manager)
    _connect_drive(runtime_paths, manager, "shared")
    context = _context(runtime_paths, _DRIVE_SERVICES)

    shared_alice = _list(_tool(runtime_paths, worker_scope="shared"), context)
    shared_bob = _list(_tool(runtime_paths, _BOB, "shared"), context)
    per_requester_alice = _list(_tool(runtime_paths), context)

    assert _entry(shared_alice, "drive")["active_source"] == "oauth"
    assert _entry(shared_bob, "drive")["active_source"] == "oauth"
    assert _entry(per_requester_alice, "drive")["active_source"] is None
    assert _entry(per_requester_alice, "drive")["oauth_reuse"] is False
    assert _entry(per_requester_alice, "drive")["provider"] == "google_drive"

    _connect_drive(runtime_paths, manager, "user_agent")
    alice = _list(_tool(runtime_paths), context)
    bob = _list(_tool(runtime_paths, _BOB), context)

    assert _entry(alice, "drive")["active_source"] == "oauth"
    assert _entry(bob, "drive")["active_source"] is None


def test_one_failing_provider_does_not_fail_the_whole_listing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service whose account status cannot be read is reported as unconfigured; the others still answer."""
    runtime_paths = _runtime_paths(tmp_path)
    save_secret(get_runtime_credentials_manager(runtime_paths), worker_target(), "openai", _SECRET)

    def failing_status(*_args: object, **_kwargs: object) -> None:
        msg = f"provider exploded with {_ACCESS_TOKEN}"
        raise RuntimeError(msg)

    monkeypatch.setattr(egress_credentials_module, "oauth_status", failing_status)

    with capture_logs() as logs:
        payload = _list(_tool(runtime_paths), _context(runtime_paths, _PRESET_SERVICES))

    assert _entry(payload, "github") == _unconfigured("github", "GitHub")
    assert _entry(payload, "openai")["active_source"] == "key"
    failures = [log for log in logs if log["event"] == "egress_credentials_status_failed"]
    assert [(log["service"], log["error_type"]) for log in failures] == [("github", "RuntimeError")]
    assert _ACCESS_TOKEN not in repr(logs)


def test_unreadable_connection_cannot_be_connected_until_reset(tmp_path: Path) -> None:
    """A stored credential that cannot be decoded needs a reset first, so the agent is not told to connect."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    _configure_github_client(manager)
    _connect_github(manager)
    _allow_github(runtime_paths)
    provider = github_oauth_provider()
    corrupt_oauth_credential_payload(
        _oauth_credential_database_path(
            resolve_oauth_credential_context(provider, runtime_paths, manager, account_store(_ALICE)),
        ),
        base64.b64encode(b"not a credential"),
    )

    payload = _list(_tool(runtime_paths), _context(runtime_paths, _PRESET_SERVICES))

    entry = _entry(payload, "github")
    assert (entry["configured"], entry["can_connect_account"]) == (False, False)


def test_unknown_oauth_provider_is_ignored(tmp_path: Path) -> None:
    """A service naming a provider the registry lacks has no OAuth source, and the tool still answers."""
    runtime_paths = _runtime_paths(tmp_path)
    services = {"acme": {"oauth_provider": "acme", "rules": [{"host": "api.acme.test", "auth": {"type": "bearer"}}]}}

    payload = _list(_tool(runtime_paths), _context(runtime_paths, services))

    assert _entry(payload, "acme") == _unconfigured("acme")


def test_output_carries_no_token_or_connect_link(tmp_path: Path) -> None:
    """Neither an access token nor a one-time connect link reaches the model; only the manage page does."""
    runtime_paths = _runtime_paths(tmp_path, MINDROOM_PUBLIC_URL="https://mindroom.example", **_PAGE_SERVED)
    manager = get_runtime_credentials_manager(runtime_paths)
    _configure_github_client(manager)
    _connect_github(manager)
    context = _context(runtime_paths, _PRESET_SERVICES)

    connected = _tool(runtime_paths)
    with tool_runtime_context(context):
        connected_output = connected.list_egress_credentials()
        unconnected_output = _tool(runtime_paths, _BOB).list_egress_credentials()

    for output in (connected_output, unconnected_output):
        assert _ACCESS_TOKEN not in output
        assert "github-refresh" not in output
        assert _CONNECT_PATH not in output
        assert "token=" not in output
        assert json.loads(output)["manage_url"] == "https://mindroom.example/connections/egress"


def test_note_points_at_connecting_an_account_or_adding_a_key(tmp_path: Path) -> None:
    """The note covers both fixes, where to do them, and that a key wins; it never asks for a pasted key."""
    runtime_paths = _runtime_paths(tmp_path, MINDROOM_PUBLIC_URL="https://mindroom.example", **_PAGE_SERVED)

    payload = _list(_tool(runtime_paths), _context(runtime_paths, _PRESET_SERVICES))

    note = str(payload["note"])
    assert "neither an API key nor a connected account Agent Keys may use" in note
    assert "an API key wins over a connected account" in note
    assert (
        "`oauth_reuse: false` means Agent Keys may not use the `provider` account for that service: allowing it "
        "there lets this agent's shell and python commands act as that account with its full access, without the "
        "approval prompts its tools ask for. "
        "Only the user can allow it."
    ) in note
    assert (
        "When `can_connect_account` is true, the user can connect their `provider` account at "
        "https://mindroom.example/connections/egress"
    ) in note
    assert "otherwise, or to use their own key instead, they can add an API key there" in note
    assert "Never ask the user to paste a key into the chat." in note


def test_note_without_a_public_url_sends_the_agent_to_the_operator(tmp_path: Path) -> None:
    """With no manage link the note still names both fixes and tells the agent to ask the operator where to do them."""
    runtime_paths = _runtime_paths(tmp_path, **_PAGE_SERVED)

    payload = _list(_tool(runtime_paths), _context(runtime_paths, _PRESET_SERVICES))

    note = str(payload["note"])
    assert "ask the operator for it" in note
    assert "http" not in note


@pytest.mark.parametrize("worker_scope", ["shared", None])
def test_note_on_a_shared_agent_says_who_can_allow_an_account(tmp_path: Path, worker_scope: WorkerScope | None) -> None:
    """A shared agent's accounts are its managers' to allow, but a per-user account is each user's own."""
    runtime_paths = _runtime_paths(tmp_path, MINDROOM_PUBLIC_URL="https://mindroom.example")

    payload = _list(_tool(runtime_paths, worker_scope=worker_scope), _context(runtime_paths, _PRESET_SERVICES))

    assert (
        "On this shared agent only its credential managers or an administrator can allow its accounts, "
        "except a per-user account such as GitHub, which each user allows for themselves."
    ) in str(payload["note"])
