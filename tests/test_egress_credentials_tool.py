"""Tests for the egress_credentials agent tool."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest
from structlog.testing import capture_logs

from mindroom import secret_request_cards
from mindroom import secret_requests as secret_requests_module
from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.config.agent import AgentConfig
from mindroom.config.egress_broker import EgressService
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.custom_tools import egress_credentials as egress_credentials_module
from mindroom.custom_tools.egress_credentials import EgressCredentialsTools
from mindroom.egress_broker.secrets import save_secret
from mindroom.egress_broker.user_services import load_user_services, save_user_service
from mindroom.message_target import MessageTarget
from mindroom.oauth.credential_lifecycle import resolve_oauth_credential_context
from mindroom.oauth.credential_store import _oauth_credential_database_path
from mindroom.oauth.github import github_oauth_provider
from mindroom.oauth.google import google_oauth_provider
from mindroom.secret_requests import SECRET_REQUEST_CONTENT_KEY, secret_request_registry
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import BackgroundOutcome, register_background_runtime
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess, ToolManagedInitArg, ToolStatus
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    make_relation_lookup,
)
from tests.egress_broker.conftest import account_store, allow_oauth_reuse, connect_account, worker_target
from tests.oauth_test_utils import corrupt_oauth_credential_payload
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    ProcessRuntime,
    job_owner,
    start_job,
    tool_job_runtime,
    wait_for_status,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.tool_system.worker_routing import WorkerScope

# The responder access checks of a secret request run for real.
pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

_ALICE = "@alice:example.org"
_BOB = "@bob:example.org"
_ROOM = "!room:example.org"
_CARD = "$card"
_REASON = "to open the pull request you asked for"
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


# Serves the personal egress page: the broker is on behind trusted upstream auth.
_PAGE_SERVED = {"MINDROOM_EGRESS_BROKER_PORT": "8768", "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true"}


def _runtime_paths(tmp_path: Path, **env: str) -> RuntimePaths:
    """Return a runtime on the Docker worker backend unless `env` names another, so `user_agent` workers are private.

    The broker is off unless `env` sets its port.
    """
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MINDROOM_NAMESPACE": "",
            "MINDROOM_WORKER_BACKEND": "docker",
            "MINDROOM_EGRESS_BROKER_PORT": "off",
            **env,
        },
    )


def _context(
    runtime_paths: RuntimePaths,
    services: dict[str, object] | None = None,
    agents: dict[str, AgentConfig] | None = None,
    *,
    memberships: AgentReplyMembershipIndex | None = None,
    room_mode: bool = False,
) -> ToolRuntimeContext:
    """Return Alice's turn on agent `code` in thread `$thread`, or in the room itself with `room_mode`."""
    config = Config(agents=agents or {}, egress_broker={"services": _SERVICES if services is None else services})
    client = make_matrix_client_mock(user_id="@mindroom_code:example.org")
    client.room_send.return_value = nio.RoomSendResponse(_CARD, _ROOM)
    return ToolRuntimeContext(
        agent_name="code",
        target=MessageTarget.resolve(
            room_id=_ROOM,
            thread_id="$thread",
            reply_to_event_id="$request",
            room_mode=room_mode,
        ),
        requester_id=_ALICE,
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        plugins=config_plugins(config, runtime_paths),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        agent_reply_memberships=memberships,
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
        "oauth_access": None,
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
        "oauth_access": None,
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
    provider = google_oauth_provider("google_drive")
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
    ],
    ids=["broker-off", "chat-sign-in-off"],
)
def test_personal_scope_without_the_personal_page_is_sent_to_no_page(
    tmp_path: Path,
    worker_scope: WorkerScope,
    env: dict[str, str],
) -> None:
    """Only the personal page sets a personal scope's keys; where it is not served the tool names no page, as the broker."""
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
    assert "Never ask the user to paste a key into the chat: call `request_secret`" in note


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


def test_an_agent_whose_only_command_tool_is_a_primary_python_is_told_agent_keys_do_not_apply(tmp_path: Path) -> None:
    """A primary python runs in-process, so the tool lists no services and sends the agent to the operator, not a page."""
    runtime_paths = _runtime_paths(
        tmp_path,
        MINDROOM_PUBLIC_URL="https://mindroom.example",
        MINDROOM_EGRESS_BROKER_PORT="8768",
    )
    agent = AgentConfig(display_name="Code", tools=["python", "egress_credentials"], worker_tools=[])

    payload = _list(_tool(runtime_paths), _context(runtime_paths, agents={"code": agent}))

    assert payload["services"] == []
    assert payload["manage_url"] is None
    assert "worker_tools" in str(payload["note"])


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
    metadata = builtin_tool_registry().entries["egress_credentials"]
    assert metadata.display_name == "Egress Credentials"
    assert metadata.description == (
        "Opt this agent's shell calls and worker-routed python calls into Agent Keys, see which keys and accounts it "
        "can use, and ask the user for a missing key"
    )
    assert metadata.category is ToolCategory.INTEGRATIONS
    assert metadata.status is ToolStatus.AVAILABLE
    assert metadata.file_access is ToolFileAccess.NONE
    assert metadata.requires_primary_runtime is True
    assert metadata.managed_init_args == (ToolManagedInitArg.RUNTIME_PATHS, ToolManagedInitArg.WORKER_TARGET)
    assert metadata.function_names == ("list_egress_credentials", "request_secret")

    tool = get_tool_by_name(
        "egress_credentials",
        _runtime_paths(tmp_path),
        worker_target=worker_target(),
        tools=builtin_tool_registry(),
    )

    assert isinstance(tool, EgressCredentialsTools)
    assert [function.__name__ for function in tool.functions.values()] == [
        "list_egress_credentials",
        "request_secret",
    ]


@pytest.mark.parametrize("broker_on", [False, True])
def test_an_agent_gets_the_tool_only_while_the_broker_is_on(tmp_path: Path, *, broker_on: bool) -> None:
    """With the broker off a config that lists the tool stays valid, and the agent's tools leave it out."""
    runtime_paths = _runtime_paths(tmp_path, MINDROOM_EGRESS_BROKER_PORT="8768" if broker_on else "off")
    agent = AgentConfig(display_name="Code", tools=["shell", "egress_credentials"], worker_scope="user_agent")
    config = bind_runtime_paths(Config(agents={"code": agent}), runtime_paths)

    tools = config_plugins(config, runtime_paths).tools
    toolkit_names = [entry.name for entry in resolve_agent_tools(config, "code", tools).candidates()]

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
        "oauth_access": "read_write",
    }
    assert _entry(bob, "github")["configured"] is False


def test_list_reports_oauth_access(tmp_path: Path) -> None:
    """An allowed account says whether the agent may only read with it, so the agent knows why a write is refused."""
    runtime_paths = _runtime_paths(tmp_path, MINDROOM_PUBLIC_URL="https://mindroom.example")
    manager = get_runtime_credentials_manager(runtime_paths)
    _configure_github_client(manager)
    _connect_github(manager)
    allow_oauth_reuse(manager, runtime_paths, account_store(_ALICE), access="read")
    context = _context(runtime_paths, _PRESET_SERVICES)

    payload = _list(_tool(runtime_paths), context)

    assert _entry(payload, "github") == {
        "name": "github",
        "display_name": "GitHub",
        "source": "config",
        "configured": True,
        "active_source": "oauth",
        "oauth_reuse": True,
        "oauth_access": "read",
    }
    assert _entry(payload, "openai")["oauth_access"] is None
    assert '`oauth_access: "read"`' in str(payload["note"])


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
        oauth_access="read_write",
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
        oauth_access="read_write",
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
        provider=google_oauth_provider("google_drive"),
    )

    payload = _list(_tool(runtime_paths), _context(runtime_paths, _DRIVE_SERVICES))

    assert _entry(payload, "drive") == _unconfigured(
        "drive",
        "Google Drive",
        oauth_reuse=True,
        oauth_access="read_write",
    )


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
    assert "Never ask the user to paste a key into the chat: call `request_secret`" in note


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


def test_a_service_with_its_own_account_is_marked_and_the_note_names_the_header(tmp_path: Path) -> None:
    """The agent learns which service logs in with an account of its own and how a request picks a service."""
    runtime_paths = _runtime_paths(tmp_path, MINDROOM_PUBLIC_URL="https://mindroom.example")
    manager = get_runtime_credentials_manager(runtime_paths)
    context = _context(runtime_paths, _PRESET_SERVICES)
    save_user_service(
        manager,
        worker_target(),
        "mine",
        EgressService.model_validate({"preset": "google_drive", "oauth_separate_account": True}),
        config_services=context.config.egress_broker.services,
        oauth_providers={"google_drive"},
    )

    payload = _list(_tool(runtime_paths), context)

    assert _entry(payload, "mine")["separate_account"] is True
    assert "separate_account" not in _entry(payload, "github")
    assert "`X-Agent-Keys-Service: <name>`" in str(payload["note"])
    # A key wins elsewhere, but such a service takes none, so the agent never sends the user to add one for it.
    assert "a `separate_account: true` service takes no API key, only its own account" in str(payload["note"])


def test_an_administrator_key_is_listed_as_set_and_marked_as_the_administrators(tmp_path: Path) -> None:
    """A personal scope reads the administrator's key from the global store, and the entry says nobody else sets it."""
    runtime_paths = _runtime_paths(tmp_path)
    save_secret(get_runtime_credentials_manager(runtime_paths), None, "openai", _SECRET, admin_key=True)
    services = {**_SERVICES, "openai": {**_SERVICES["openai"], "admin_key": True}}

    payload = _list(_tool(runtime_paths), _context(runtime_paths, services))

    assert payload["services"] == [_unconfigured("github", "GitHub"), {**_keyed("openai"), "admin_key": True}]
    assert "`admin_key: true`" in payload["note"]


def test_a_github_app_service_lists_its_repositories(tmp_path: Path) -> None:
    """The agent learns which repositories the App covers and at what access, and that only an administrator changes it."""
    runtime_paths = _runtime_paths(tmp_path)
    get_runtime_credentials_manager(runtime_paths).save_credentials(
        "acme_app",
        {"auth_type": "github_app", "app_id": 1, "installation_id": 2, "private_key_file": "/run/secrets/app.pem"},
    )
    repositories = {"acme/api": "read_write", "acme/docs": "read"}
    app = {"credentials_service": "acme_app", "repositories": repositories}
    services = {**_SERVICES, "github": {**_SERVICES["github"], "github_app": app}}

    payload = _list(_tool(runtime_paths), _context(runtime_paths, services))

    assert payload["services"][0] == {
        "name": "github",
        "display_name": "GitHub",
        "source": "config",
        "configured": True,
        "active_source": "github_app",
        "oauth_reuse": None,
        "oauth_access": None,
        "github_app_repositories": repositories,
    }
    assert "`github_app_repositories`" in payload["note"]


def test_a_service_limited_to_other_agents_is_not_listed(tmp_path: Path) -> None:
    """The broker refuses this agent such a service, so it is not offered as one the user could fix."""
    runtime_paths = _runtime_paths(tmp_path)
    agents = {"code": AgentConfig(display_name="Code"), "other": AgentConfig(display_name="Other")}
    services = {**_SERVICES, "openai": {**_SERVICES["openai"], "agents": ["other"]}}

    payload = _list(_tool(runtime_paths), _context(runtime_paths, services, agents))

    assert [entry["name"] for entry in payload["services"]] == ["github"]


# The broker is on and the sandbox proxy runs shell in a worker, so agent `code` reaches Agent Keys, and MindRoom Chat
# may call this MindRoom to answer a card.
_BROKER_ENV = {
    "MINDROOM_SANDBOX_PROXY_URL": "http://sandbox-runner.test",
    "MINDROOM_EGRESS_BROKER_PORT": "8768",
    "MINDROOM_COMPUTER_ALLOWED_ORIGINS": '["https://chat.example.org"]',
}


@pytest.fixture(autouse=True)
def _homeserver_binds_openid(monkeypatch: pytest.MonkeyPatch) -> None:
    """The homeserver binds OpenID tokens, so Chat can sign in to this MindRoom with a client session."""
    monkeypatch.setattr(secret_request_cards, "homeserver_openid_binding", AsyncMock(return_value=True))


@pytest.fixture(autouse=True)
def _forget_secret_requests() -> Iterator[None]:
    """Leave the process-wide registry as each test found it."""
    before = set(secret_request_registry()._requests)
    yield
    for request_id in set(secret_request_registry()._requests) - before:
        secret_request_registry().discard(request_id)


def _asking(
    tmp_path: Path,
    services: dict[str, object] | None = None,
    *,
    worker_scope: WorkerScope = "user_agent",
    room_mode: bool = False,
    agent_fields: dict[str, object] | None = None,
    **env: str,
) -> tuple[EgressCredentialsTools, ToolRuntimeContext]:
    """Return the tool and Alice's turn on agent `code`: personal, or shared with Bob as its credential manager.

    `agent_fields` replace the agent's own.
    """
    runtime_paths = _runtime_paths(tmp_path, **{**_BROKER_ENV, **env})
    scope: dict[str, object] = (
        {"worker_scope": "shared", "credential_managers": [_BOB]}
        if worker_scope == "shared"
        else {"private": {"per": "user_agent"}}
    )
    agent = AgentConfig.model_validate(
        {
            "display_name": "Code",
            "tools": ["shell", "egress_credentials"],
            "include_default_tools": False,
            "access": {"users": [_ALICE, _BOB]},
            **scope,
            **(agent_fields or {}),
        },
    )
    context = _context(
        runtime_paths,
        services,
        {"code": agent},
        memberships=AgentReplyMembershipIndex(),
        room_mode=room_mode,
    )
    return _tool(runtime_paths, worker_scope=worker_scope), context


def _start(
    tool: EgressCredentialsTools,
    context: ToolRuntimeContext,
    service: str = "github",
    reason: str = _REASON,
    **shown: str,
) -> asyncio.Task[str]:
    with tool_runtime_context(context):
        return asyncio.create_task(tool.request_secret(service, reason, **shown))


async def _ask(
    tool: EgressCredentialsTools,
    context: ToolRuntimeContext,
    service: str = "github",
    reason: str = _REASON,
) -> dict[str, object]:
    return json.loads(await _start(tool, context, service, reason))


def _sent(context: ToolRuntimeContext) -> list[dict[str, object]]:
    """Return the content of every event the tool sent, in order."""
    return [call.kwargs["content"] for call in context.client.room_send.await_args_list]


async def _posted_card(context: ToolRuntimeContext, task: asyncio.Task[str]) -> dict[str, object]:
    """Return the card once the tool posted it and waits for the answer (it logs `secret_request_posted` then)."""
    with capture_logs() as logs:
        while not task.done() and not any(entry["event"] == "secret_request_posted" for entry in logs):  # noqa: ASYNC110 - the tool logs from its own task
            await asyncio.sleep(0)
    assert not task.done(), task.result()
    [card] = _sent(context)
    return card


def _request_id(card: dict[str, object]) -> str:
    request = card[SECRET_REQUEST_CONTENT_KEY]
    assert isinstance(request, dict)
    return request["request_id"]


def _final_card(context: ToolRuntimeContext, status: str) -> dict[str, object]:
    """Return the card's edit after checking it replaces the card with the same content in `status`."""
    card, edit = _sent(context)
    new_content = edit["m.new_content"]
    assert edit["m.relates_to"] == {"rel_type": "m.replace", "event_id": _CARD}
    assert new_content["msgtype"] == "m.notice"
    assert new_content[SECRET_REQUEST_CONTENT_KEY] == {**card[SECRET_REQUEST_CONTENT_KEY], "status": status}
    return new_content


@pytest.mark.asyncio
async def test_a_key_card_shows_a_link_and_a_field_hint(tmp_path: Path) -> None:
    """The agent can name the page that gives the key and hint at its form; both stay on the card's edit."""
    tool, context = _asking(tmp_path)

    task = _start(tool, context, url="https://github.com/settings/tokens", placeholder=" ghp_…\n")
    card = await _posted_card(context, task)
    secret_request_registry().resolve(_request_id(card), "declined")
    await task

    content = card[SECRET_REQUEST_CONTENT_KEY]
    assert (content["url"], content["placeholder"]) == ("https://github.com/settings/tokens", "ghp_…")
    assert card["body"].endswith("Where to get it: https://github.com/settings/tokens")
    _final_card(context, "declined")


@pytest.mark.asyncio
async def test_a_key_card_asks_for_the_overleaf_git_token(tmp_path: Path) -> None:
    """The Overleaf preset takes a key, so the card names Overleaf and the Git bridge host the token goes to."""
    tool, context = _asking(tmp_path, {"overleaf": {"preset": "overleaf"}})

    task = _start(tool, context, "overleaf", url="https://www.overleaf.com/user/settings")
    card = await _posted_card(context, task)
    secret_request_registry().resolve(_request_id(card), "declined")
    await task

    content = card[SECRET_REQUEST_CONTENT_KEY]
    assert (content["service"], content["display_name"]) == ("overleaf", "Overleaf")
    assert content["hosts"] == ["git.overleaf.com"]


@pytest.mark.asyncio
async def test_a_key_card_refuses_a_link_no_card_can_show(tmp_path: Path) -> None:
    """A link that is no `http` or `https` page posts no card and tells the agent why."""
    tool, context = _asking(tmp_path)

    result = json.loads(await _start(tool, context, url="javascript:alert(1)"))

    assert result["status"] == "error"
    assert "`url`" in result["message"]
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_card_for_a_service_with_a_key_says_it_replaces(tmp_path: Path) -> None:
    """Saving over a working key is the requester's choice, so the card tells them a key is already there."""
    tool, context = _asking(tmp_path)
    save_secret(get_runtime_credentials_manager(context.runtime_paths), worker_target(), "github", "ghp-old")

    task = _start(tool, context)
    card = await _posted_card(context, task)
    secret_request_registry().resolve(_request_id(card), "declined")
    await task

    assert card[SECRET_REQUEST_CONTENT_KEY]["replaces"] is True
    assert _final_card(context, "declined")[SECRET_REQUEST_CONTENT_KEY]["replaces"] is True


@pytest.mark.asyncio
async def test_a_key_in_another_scope_is_not_one_the_card_replaces(tmp_path: Path) -> None:
    """Only a key in the store the card's key goes to is replaced; Bob's own key for the agent is not."""
    tool, context = _asking(tmp_path)
    save_secret(get_runtime_credentials_manager(context.runtime_paths), worker_target(_BOB), "github", "ghp-bob")

    task = _start(tool, context)
    card = await _posted_card(context, task)
    secret_request_registry().resolve(_request_id(card), "declined")
    await task

    assert "replaces" not in card[SECRET_REQUEST_CONTENT_KEY]


@pytest.mark.asyncio
async def test_request_posts_card_and_returns_saved(tmp_path: Path) -> None:
    """The card goes into the thread with metadata only; the user's answer edits it and the agent learns `saved`."""
    tool, context = _asking(tmp_path)

    task = _start(tool, context)
    card = await _posted_card(context, task)
    request_id = _request_id(card)
    pending = secret_request_registry().claim(request_id, room_id=_ROOM, now=time.time())
    assert pending is not None
    secret_request_registry().resolve(request_id, "saved")
    result = json.loads(await task)

    assert (pending.requester_id, pending.agent_name, pending.target.service) == (_ALICE, "code", "github")
    assert pending.target.worker_target == worker_target()
    assert card["msgtype"] == "m.notice"
    assert card["body"] == "🔑 Code asks for your GitHub key. Open MindRoom Chat to enter it."
    assert card["m.relates_to"] == {
        "rel_type": "m.thread",
        "event_id": "$thread",
        "is_falling_back": False,
        "m.in_reply_to": {"event_id": "$request"},
    }
    assert card[SECRET_REQUEST_CONTENT_KEY] == {
        "request_id": request_id,
        "requester_id": _ALICE,
        "service": "github",
        "display_name": "GitHub",
        "hosts": ["api.github.com"],
        "reason": _REASON,
        "status": "pending",
        "expires_at": int(pending.expires_at * 1000),
    }
    assert _final_card(context, "saved")["body"] == "🔑 GitHub key saved."
    assert result["status"] == "saved"
    assert result["service"] == "github"
    assert "retry" in result["note"]
    assert "`X-Agent-Keys-Service: github`" in result["note"]


@pytest.mark.asyncio
async def test_request_declined_and_expired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A decline and an unanswered card each end the wait, edit the card, and tell the agent which it was."""
    tool, context = _asking(tmp_path)
    task = _start(tool, context)
    secret_request_registry().resolve(_request_id(await _posted_card(context, task)), "declined")
    declined = json.loads(await task)

    assert _final_card(context, "declined")["body"] == "🔑 GitHub key request declined."
    assert (declined["status"], declined["service"]) == ("declined", "github")
    assert "do not ask again" in declined["note"]

    monkeypatch.setattr(secret_requests_module, "SECRET_REQUEST_TTL_SECONDS", 0.01)
    tool, context = _asking(tmp_path)
    settled_during_edit: list[bool] = []

    async def send(**kwargs: object) -> nio.RoomSendResponse:
        content = kwargs["content"]
        assert isinstance(content, dict)
        if content.get("m.relates_to", {}).get("rel_type") == "m.replace":
            settled_during_edit.append(secret_request_registry()._requests[_request_id(content)].outcome.done())
        return nio.RoomSendResponse(_CARD, _ROOM)

    context.client.room_send.side_effect = send
    expired = await _ask(tool, context)

    assert _final_card(context, "expired")["body"] == "🔑 GitHub key request expired."
    assert (expired["status"], expired["service"]) == ("expired", "github")
    assert "15 minutes" in expired["note"]
    assert settled_during_edit == [True]


@pytest.mark.asyncio
async def test_cancelled_wait_edits_card_expired_and_discards(tmp_path: Path) -> None:
    """A stopped reply ends the wait: the card turns expired, the request is gone, and the cancellation propagates.

    The request is settled before the edit, so an answer arriving while the card turns expired is refused.
    """
    tool, context = _asking(tmp_path)
    claims_during_edit: list[object] = []

    async def send(**kwargs: object) -> nio.RoomSendResponse:
        content = kwargs["content"]
        assert isinstance(content, dict)
        if content.get("m.relates_to", {}).get("rel_type") == "m.replace":
            claim = secret_request_registry().claim(_request_id(content), room_id=_ROOM, now=time.time())
            claims_during_edit.append(claim)
        return nio.RoomSendResponse(_CARD, _ROOM)

    context.client.room_send.side_effect = send
    task = _start(tool, context)
    request_id = _request_id(await _posted_card(context, task))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    _final_card(context, "expired")
    assert claims_during_edit == [None]
    assert secret_request_registry().claim(request_id, room_id=_ROOM, now=time.time()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "service", "reason", "message"),
    [
        ("unknown", "gitlab", _REASON, "Unknown service 'gitlab'"),
        ("restrict-only", "github-limited", _REASON, "never takes a key"),
        ("admin-key", "openai", _REASON, "Only an administrator"),
        ("non-manager", "github", _REASON, "credential managers"),
        ("no-memberships", "github", _REASON, "cannot check"),
        ("empty-reason", "github", "  \n", "reason"),
        ("github-app", "acme", _REASON, "Acme takes no key"),
        ("separate-account", "mine", _REASON, "Google Drive takes no key; connect an account instead"),
        ("no-agent-keys", "github", _REASON, "This agent's commands do not reach Agent Keys"),
        ("not-a-user", "github", _REASON, "The requester may not use this agent"),
        ("not-a-person", "github", _REASON, "Only a person can answer a card"),
    ],
    ids=[
        "unknown",
        "restrict-only",
        "admin-key",
        "non-manager",
        "no-memberships",
        "empty-reason",
        "github-app",
        "separate-account",
        "no-agent-keys",
        "not-a-user",
        "not-a-person",
    ],
)
async def test_refuses_without_posting(
    tmp_path: Path,
    case: str,
    service: str,
    reason: str,
    message: str,
) -> None:
    """A service no key can be asked for, a requester who may not set the key, or no reason posts no card."""
    app = {"credentials_service": "acme_app", "repositories": {"acme/api": "read"}}
    services = {
        **_SERVICES,
        "openai": {**_SERVICES["openai"], "admin_key": True},
        "acme": {**_SERVICES["github"], "display_name": "Acme", "github_app": app},
    }
    agent_fields = {
        "no-agent-keys": {"tools": ["python", "egress_credentials"], "worker_tools": []},
        "not-a-user": {"access": {"users": [_BOB]}},
    }.get(case)
    tool, context = _asking(
        tmp_path,
        services,
        worker_scope="shared" if case == "non-manager" else "user_agent",
        agent_fields=agent_fields,
    )
    manager = get_runtime_credentials_manager(context.runtime_paths)
    if case == "restrict-only":
        limit = {"restrict_only": True, "rules": [{"host": "api.github.com", "path_prefix": "/repos/o/a"}]}
        _save_mine(manager, "github-limited", limit, context.config.egress_broker.services)
    if case == "separate-account":
        save_user_service(
            manager,
            worker_target(),
            "mine",
            EgressService.model_validate({"preset": "google_drive", "oauth_separate_account": True}),
            config_services=context.config.egress_broker.services,
            oauth_providers={"google_drive"},
        )
    if case == "no-memberships":
        context = replace(context, agent_reply_memberships=None)
    if case == "not-a-person":
        # A bot account asks: no person could answer its card.
        context = replace(
            context,
            requester_id="@bridge-bot:example.org",
            config=context.config.model_copy(update={"bot_accounts": ["@bridge-bot:example.org"]}),
        )

    result = await _ask(tool, context, service, reason)

    assert result["status"] == "error"
    assert message in result["message"]
    assert result["link"] is None
    context.client.room_send.assert_not_awaited()


# A service the agent proposes, as the card shows it.
_ACME: dict[str, object] = {
    "display_name": "Acme",
    "rules": [
        {"host": "api.acme.test", "path_prefix": "/v1/", "auth": {"type": "bearer"}},
        {"host": "git.acme.test", "port": 8443, "auth": {"type": "header", "name": "X-Key"}},
    ],
    "placeholder_env": {"ACME_TOKEN": "mindroom-brokered"},
}
_ACME_SHOWN = {
    "name": "acme",
    "display_name": "Acme",
    "rules": [
        {"host": "api.acme.test", "path_prefix": "/v1/", "auth": {"type": "bearer"}},
        {"host": "git.acme.test", "path_prefix": "/", "port": 8443, "auth": {"type": "header", "name": "X-Key"}},
    ],
    "placeholder_env": {"ACME_TOKEN": "mindroom-brokered"},
}


def _propose(
    tool: EgressCredentialsTools,
    context: ToolRuntimeContext,
    new_service: dict[str, object],
    service: str = "acme",
) -> asyncio.Task[str]:
    with tool_runtime_context(context):
        return asyncio.create_task(tool.request_secret(service, _REASON, new_service=new_service))


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_scope", ["user_agent", "shared"])
async def test_a_card_proposes_a_new_service_that_only_saving_creates(
    tmp_path: Path,
    worker_scope: WorkerScope,
) -> None:
    """The card shows the proposed service's rules and env; nothing is stored until the user saves."""
    tool, context = _asking(tmp_path, worker_scope=worker_scope)
    if worker_scope == "shared":
        # On the shared agent its credential manager Bob answers for its keys.
        context = replace(context, requester_id=_BOB)

    task = _propose(tool, context, _ACME)
    card = await _posted_card(context, task)
    pending = secret_request_registry().claim(_request_id(card), room_id=_ROOM, now=time.time())
    assert pending is not None
    secret_request_registry().resolve(pending.request_id, "declined")
    await task

    content = card[SECRET_REQUEST_CONTENT_KEY]
    assert (content["service"], content["display_name"]) == ("acme", "Acme")
    assert content["hosts"] == ["api.acme.test", "git.acme.test"]
    assert content["new_service"] == _ACME_SHOWN
    assert "replaces" not in content
    assert pending.target.new_service == EgressService.model_validate({**_ACME, "oauth_provider": None})
    manager = get_runtime_credentials_manager(context.runtime_paths)
    assert load_user_services(manager, pending.target.worker_target) == {}
    assert _final_card(context, "declined")[SECRET_REQUEST_CONTENT_KEY]["new_service"] == _ACME_SHOWN


@pytest.mark.asyncio
async def test_a_proposed_preset_service_takes_a_key_not_an_account(tmp_path: Path) -> None:
    """A preset's account login is dropped: a service proposed in chat only ever uses the key the user saves."""
    tool, context = _asking(tmp_path)

    task = _propose(tool, context, {"preset": "github", "display_name": "GitHub (acme)"}, "github-acme")
    card = await _posted_card(context, task)
    pending = secret_request_registry().claim(_request_id(card), room_id=_ROOM, now=time.time())
    assert pending is not None
    secret_request_registry().resolve(pending.request_id, "declined")
    await task

    assert pending.target.new_service is not None
    assert pending.target.new_service.oauth_provider is None
    assert pending.target.new_service.authored_model_dump() == {
        "preset": "github",
        "display_name": "GitHub (acme)",
        "oauth_provider": None,
    }
    assert card[SECRET_REQUEST_CONTENT_KEY]["new_service"]["display_name"] == "GitHub (acme)"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "new_service", "message"),
    [
        pytest.param("github", _ACME, "already used by a service your administrator configured", id="config-name"),
        pytest.param("mine", _ACME, "already used by a service of your own", id="own-name"),
        pytest.param("Not A Name", _ACME, "must match", id="bad-name"),
        pytest.param("acme", {"display_name": "Acme"}, "at least one rule", id="no-rules"),
        pytest.param("acme", {**_ACME, "rules": [{"host": "x://y"}]}, "host must not contain scheme", id="bad-rule"),
        pytest.param("acme", {**_ACME, "agents": ["code"]}, "cannot set agents", id="operator-field"),
        pytest.param("acme", {**_ACME, "oauth_provider": "github"}, "cannot set oauth_provider", id="account"),
        pytest.param("acme", {**_ACME, "restrict_only": True}, "cannot set restrict_only", id="restrict-only"),
        pytest.param(
            "acme",
            {**_ACME, "placeholder_env": {"LD_PRELOAD": "x"}},
            "LD_PRELOAD is not allowed as a placeholder",
            id="placeholder",
        ),
        pytest.param(
            "acme",
            {**_ACME, "rules": [{"host": f"h{index}.acme.test", "auth": {"type": "bearer"}} for index in range(51)]},
            "at most 50 rules",
            id="too-many-rules",
        ),
        pytest.param("acme", _ACME, "these hosts cannot be used: api.acme.test, git.acme.test", id="denied-hosts"),
    ],
)
async def test_refuses_a_proposed_service_without_posting(
    tmp_path: Path,
    name: str,
    new_service: dict[str, object],
    message: str,
) -> None:
    """A proposal the Agent Keys page would refuse, or one that takes a name in use, posts no card and says why."""
    tool, context = _asking(tmp_path)
    manager = get_runtime_credentials_manager(context.runtime_paths)
    _save_mine(manager, "mine", _SERVICES["openai"], context.config.egress_broker.services)
    if message.startswith("these hosts"):
        # The operator allows only hosts its own services cover.
        broker = context.config.egress_broker.model_copy(update={"unmatched_hosts": "deny"})
        context = replace(context, config=context.config.model_copy(update={"egress_broker": broker}))

    result = json.loads(await _propose(tool, context, new_service, name))

    assert result["status"] == "error"
    assert message in result["message"]
    context.client.room_send.assert_not_awaited()
    assert set(load_user_services(manager, worker_target())) == {"mine"}


@pytest.mark.asyncio
async def test_refusal_names_the_agent_keys_page(tmp_path: Path) -> None:
    """Where the user can set the key on a page, a refusal links it, here for a service that needs host rules first."""
    tool, context = _asking(tmp_path, **_PAGE_SERVED, MINDROOM_PUBLIC_URL="https://mindroom.example.org")

    result = await _ask(tool, context, "gitlab")

    assert result["status"] == "error"
    assert "Unknown service 'gitlab'" in result["message"]
    assert result["link"] == "https://mindroom.example.org/connections/egress"
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("no-chat-origin", "MINDROOM_COMPUTER_ALLOWED_ORIGINS"),
        ("no-openid-audience", "set `MINDROOM_PUBLIC_URL`"),
        ("no-openid-binding", "the homeserver binds OpenID tokens"),
        ("binding-check-failed", "the homeserver binds OpenID tokens"),
    ],
    ids=["no-chat-origin", "no-openid-audience", "no-openid-binding", "binding-check-failed"],
)
async def test_refuses_when_chat_cannot_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    message: str,
) -> None:
    """Without Chat allowed to call this MindRoom with a client session, nobody could answer, so no card is posted."""
    binding = {"no-openid-binding": False, "binding-check-failed": None}.get(case, True)
    monkeypatch.setattr(secret_request_cards, "homeserver_openid_binding", AsyncMock(return_value=binding))
    if case == "no-openid-audience":
        monkeypatch.setattr(secret_request_cards, "openid_audience", lambda _paths: None)
    tool, context = _asking(
        tmp_path,
        **_PAGE_SERVED,
        MINDROOM_PUBLIC_URL="https://mindroom.example.org",
        MINDROOM_COMPUTER_ALLOWED_ORIGINS="[]" if case == "no-chat-origin" else '["https://chat.example.org"]',
    )

    result = await _ask(tool, context)

    assert result["status"] == "error"
    assert message in result["message"]
    assert ("homeserver" in result["message"]) is (case in {"no-openid-binding", "binding-check-failed"})
    assert "The user can add the key on the Agent Keys page instead" in result["message"]
    assert result["link"] == "https://mindroom.example.org/connections/egress"
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_refusal_without_a_page_says_where_the_key_goes(tmp_path: Path) -> None:
    """With no Agent Keys page to link, the refusal still reads right: the operator names the page."""
    tool, context = _asking(tmp_path, MINDROOM_COMPUTER_ALLOWED_ORIGINS="[]")

    result = await _ask(tool, context)

    assert result["link"] is None
    assert "or, when it is null, the page the operator names" in result["message"]
    context.client.room_send.assert_not_awaited()


async def _published_job_runtime(context: ToolRuntimeContext) -> ProcessRuntime:
    """Start the background job runtime of the context's storage root, which a call running in one of its jobs finds."""
    runtime = await tool_job_runtime(context.runtime_paths.storage_root)
    pin_background_tool_jobs(context.config, context.runtime_paths)
    register_background_runtime(context.runtime_paths, runtime)
    return runtime


async def _start_reply_job(
    runtime: ProcessRuntime,
    context: ToolRuntimeContext,
    operation: Callable[[], Awaitable[BackgroundOutcome]],
) -> None:
    """Start `operation` as a job of the reply `reply`, inside the context's tool calls."""
    with tool_runtime_context(context):
        await start_job(
            runtime,
            "ask",
            tool_name="request_secret",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
            reply_id="reply",
        )


async def _sends(context: ToolRuntimeContext, count: int) -> list[dict[str, object]]:
    """Return the events the tool sent once there are `count` of them."""
    while context.client.room_send.await_count < count:  # noqa: ASYNC110 - the job sends from its own task
        await asyncio.sleep(0)
    return _sent(context)


@pytest.mark.asyncio
async def test_stop_before_the_card_is_posted_posts_none(tmp_path: Path) -> None:
    """A Stop of the reply that started the call's job, before the call posts its card, cancels the job without one."""
    tool, context = _asking(tmp_path)
    runtime = await _published_job_runtime(context)
    go = asyncio.Event()

    async def ask_later() -> BackgroundOutcome:
        await go.wait()
        return BackgroundOutcome("completed", await tool.request_secret("github", _REASON))

    try:
        await _start_reply_job(runtime, context, ask_later)
        await runtime.cancel_stopped_reply("reply")
        go.set()
        await asyncio.wait_for(wait_for_status(runtime, "ask", "cancelled"), JOB_TEST_TIMEOUT)
    finally:
        await runtime.shutdown()

    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_withdraws_both_cards_of_a_job_after_one_is_answered(tmp_path: Path) -> None:
    """Two cards in one job: answering one leaves the job waiting on the other, so Stop still expires that card."""
    tool, context = _asking(tmp_path)
    runtime = await _published_job_runtime(context)

    async def ask_both() -> BackgroundOutcome:
        results = await asyncio.gather(tool.request_secret("github", _REASON), tool.request_secret("openai", _REASON))
        return BackgroundOutcome("completed", json.dumps(results))

    try:
        await _start_reply_job(runtime, context, ask_both)
        cards = await asyncio.wait_for(_sends(context, 2), JOB_TEST_TIMEOUT)
        await asyncio.wait_for(wait_for_status(runtime, "ask", "awaiting_approval"), JOB_TEST_TIMEOUT)
        github, openai = sorted(cards, key=lambda card: card[SECRET_REQUEST_CONTENT_KEY]["service"])
        secret_request_registry().resolve(_request_id(github), "saved")
        answered = (await asyncio.wait_for(_sends(context, 3), JOB_TEST_TIMEOUT))[2]

        assert answered["m.new_content"][SECRET_REQUEST_CONTENT_KEY]["status"] == "saved"
        assert runtime._entries["ask"].job.status == "awaiting_approval"

        await runtime.cancel_stopped_reply("reply")
        await asyncio.wait_for(wait_for_status(runtime, "ask", "cancelled"), JOB_TEST_TIMEOUT)
    finally:
        await runtime.shutdown()

    withdrawn = _sent(context)[3]
    assert withdrawn["m.new_content"][SECRET_REQUEST_CONTENT_KEY] == {
        **openai[SECRET_REQUEST_CONTENT_KEY],
        "status": "expired",
    }
    # The answer route claims before anything else, so it answers this request 404.
    assert secret_request_registry().claim(_request_id(openai), room_id=_ROOM, now=time.time()) is None


@pytest.mark.asyncio
async def test_stop_withdraws_the_card_of_a_background_job(tmp_path: Path) -> None:
    """Run as a background job, the call waits on its card as an approval does, so the reply's Stop expires the card."""
    tool, context = _asking(tmp_path)
    runtime = await _published_job_runtime(context)

    async def ask() -> BackgroundOutcome:
        return BackgroundOutcome("completed", await tool.request_secret("github", _REASON))

    try:
        await _start_reply_job(runtime, context, ask)
        await asyncio.wait_for(wait_for_status(runtime, "ask", "awaiting_approval"), JOB_TEST_TIMEOUT)
        request_id = _request_id(_sent(context)[0])

        await runtime.cancel_stopped_reply("reply")
        await asyncio.wait_for(wait_for_status(runtime, "ask", "cancelled"), JOB_TEST_TIMEOUT)
    finally:
        await runtime.shutdown()

    _final_card(context, "expired")
    assert secret_request_registry().claim(request_id, room_id=_ROOM, now=time.time()) is None
    assert request_id not in secret_request_registry()._requests


@pytest.mark.asyncio
async def test_a_save_that_fails_after_expiry_ends_the_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Once the claimed answer's save fails and gives the claim back, nobody can answer, so the card expires at once."""
    monkeypatch.setattr(secret_requests_module, "SECRET_REQUEST_TTL_SECONDS", 0.2)
    tool, context = _asking(tmp_path)
    task = _start(tool, context)
    request_id = _request_id(await _posted_card(context, task))
    assert secret_request_registry().claim(request_id, room_id=_ROOM, now=time.time()) is not None

    await asyncio.sleep(0.3)  # The request expires while its claimed answer is still being saved.
    assert not task.done()
    secret_request_registry().release(request_id)
    result = json.loads(await asyncio.wait_for(task, 5))

    assert result["status"] == "expired"
    _final_card(context, "expired")


@pytest.mark.asyncio
async def test_an_answer_saving_as_the_wait_ends_is_not_expired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key the route claimed before the TTL and stores just after it is reported and shown as saved."""
    monkeypatch.setattr(secret_requests_module, "SECRET_REQUEST_TTL_SECONDS", 0.2)
    tool, context = _asking(tmp_path)
    task = _start(tool, context)
    request_id = _request_id(await _posted_card(context, task))
    assert secret_request_registry().claim(request_id, room_id=_ROOM, now=time.time()) is not None

    await asyncio.sleep(0.3)  # The TTL elapses while the claimed answer is still being saved.
    assert not task.done()
    secret_request_registry().resolve(request_id, "saved")
    result = json.loads(await task)

    assert result["status"] == "saved"
    assert _final_card(context, "saved")["body"] == "🔑 GitHub key saved."


@pytest.mark.asyncio
async def test_room_mode_posts_without_a_thread_relation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn in the room itself gets its card in the room."""
    monkeypatch.setattr(secret_requests_module, "SECRET_REQUEST_TTL_SECONDS", 0.01)
    tool, context = _asking(tmp_path, room_mode=True)

    await _ask(tool, context)

    assert "m.relates_to" not in _sent(context)[0]


@pytest.mark.asyncio
async def test_failed_post_discards_the_request(tmp_path: Path) -> None:
    """A card that could not be sent is an error, and nobody can answer its request."""
    tool, context = _asking(tmp_path)
    context.client.room_send.return_value = nio.RoomSendError("server unavailable")

    result = await _ask(tool, context)

    assert result["status"] == "error"
    assert secret_request_registry().claim(_request_id(_sent(context)[0]), room_id=_ROOM, now=time.time()) is None


@pytest.mark.asyncio
async def test_failed_edit_keeps_the_outcome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A card that cannot be edited is logged, without the reason, and the agent still learns the outcome."""
    monkeypatch.setattr(secret_requests_module, "SECRET_REQUEST_TTL_SECONDS", 0.01)
    tool, context = _asking(tmp_path)
    context.client.room_send.side_effect = [
        nio.RoomSendResponse(_CARD, _ROOM),
        nio.RoomSendError("server unavailable"),
    ]

    with capture_logs() as logs:
        result = await _ask(tool, context)

    assert result["status"] == "expired"
    assert [entry["event"] for entry in logs if entry["event"] == "secret_request_edit_failed"] == [
        "secret_request_edit_failed",
    ]
    assert [entry for entry in logs if _REASON in repr(entry)] == []
