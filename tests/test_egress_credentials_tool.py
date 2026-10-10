"""Tests for the egress_credentials agent tool."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.custom_tools.egress_credentials import EgressCredentialsTools
from mindroom.egress_broker import oauth_source
from mindroom.egress_broker.secrets import save_secret
from mindroom.message_target import MessageTarget
from mindroom.oauth.credential_lifecycle import resolve_oauth_credential_context
from mindroom.oauth.credential_store import _oauth_credential_database_path
from mindroom.oauth.github import github_oauth_provider
from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess, ToolManagedInitArg, ToolStatus
from mindroom.tool_system.metadata import TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity, resolve_worker_target
from tests.conftest import make_conversation_reader_mock, make_relation_lookup
from tests.oauth_test_utils import corrupt_oauth_credential_payload, publish_oauth_credentials

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager

_SECRET = "ghp_super_secret_token_value"  # noqa: S105
_ACCESS_TOKEN = "gho_oauth_access_token_value"  # noqa: S105
_CONNECT_PATH = "/api/oauth/github/authorize"
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
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MINDROOM_NAMESPACE": "", **env},
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
        metadata.description == "See which API keys this agent can use through the egress broker and where to add them"
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


def test_github_account_is_per_requester_on_a_shared_agent(tmp_path: Path) -> None:
    """GitHub follows the requester even on a shared agent: Alice's connection serves Alice and never Bob."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    _configure_github_client(manager)
    _connect_github(manager, "@alice:example.org")
    context = _context(runtime_paths, _PRESET_SERVICES)

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


def test_shared_service_account_is_not_injected_so_it_is_not_connected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared service account never supplies a token and personal accounts are not connectable beside it."""
    monkeypatch.setattr(oauth_source, "oauth_provider_service_account_configured", lambda *_args: True)
    runtime_paths = _runtime_paths(tmp_path)
    _configure_github_client(get_runtime_credentials_manager(runtime_paths))

    payload = _list(_github_tool(runtime_paths), _context(runtime_paths, _PRESET_SERVICES))

    entry = _entry(payload, "github")
    assert (entry["configured"], entry["active_source"], entry["can_connect_account"]) == (False, None, False)


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
    """The note covers both fixes and still sends users to the manage page instead of the chat."""
    runtime_paths = _runtime_paths(tmp_path, MINDROOM_PUBLIC_URL="https://mindroom.example")

    payload = _list(_github_tool(runtime_paths), _context(runtime_paths, _PRESET_SERVICES))

    note = str(payload["note"])
    assert "can_connect_account" in note
    assert "connect" in note
    assert "key" in note
    assert "https://mindroom.example/" in note
    assert "paste" in note
