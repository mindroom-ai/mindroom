"""Trusted zero-argument agent repository tool tests."""

from __future__ import annotations

import asyncio
import inspect
import json
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

import mindroom.agent_repositories as agent_repositories_module
from mindroom.agent_repositories import (
    RepositoryBindingStore,
    RepositoryEnsureRequest,
    RepositoryLease,
)
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.agent_repository import AgentRepositoryTools
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    resolve_worker_target,
    tool_stays_local,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _target() -> object:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="redwood",
        requester_id="@ignored:example.test",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
        tenant_id=None,
    )
    return resolve_worker_target("shared", "redwood", identity)


def _runtime_paths(tmp_path: Path, *, broker_config: bool = False) -> RuntimePaths:
    env: dict[str, str] = {}
    if broker_config:
        token_file = tmp_path / "broker-token"
        token_file.write_text("control-plane-secret", encoding="utf-8")
        env = {
            "MINDROOM_AGENT_REPOSITORY_BROKER_URL": "http://agent-vault:14321",
            "MINDROOM_AGENT_REPOSITORY_BROKER_TOKEN_FILE": str(token_file),
        }
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env=env,
    )


def _lease(repository_id: int = 42) -> RepositoryLease:
    return RepositoryLease(
        repository_id=repository_id,
        organization="example-org",
        repository_name="MindRoom-redwood",
        clone_url="https://github.com/example-org/MindRoom-redwood.git",
    )


@dataclass
class _FakeBroker:
    lease: RepositoryLease = field(default_factory=_lease)
    delay: float = 0.0
    requests: list[RepositoryEnsureRequest] = field(default_factory=list)

    async def ensure_repository(self, request: RepositoryEnsureRequest) -> RepositoryLease:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.lease


def _tool(
    tmp_path: Path,
    *,
    broker: _FakeBroker,
    workspace: Path | None = None,
) -> AgentRepositoryTools:
    return AgentRepositoryTools(
        organization="example-org",
        prefix="MindRoom",
        runtime_paths=_runtime_paths(tmp_path),
        worker_target=_target(),
        tool_output_workspace_root=workspace or tmp_path / "workspace",
        broker=broker,
    )


def _git(workspace: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_tool_exposes_only_argument_free_ensure() -> None:
    """The model must have no repository-management or naming arguments."""
    signature = inspect.signature(AgentRepositoryTools.ensure_my_repository)

    assert list(signature.parameters) == ["self"]
    assert tool_stays_local("agent_repository")


@pytest.mark.asyncio
async def test_ensure_binds_repository_and_configures_credential_free_origin(tmp_path: Path) -> None:
    """One call should bind the broker identity and initialize the current workspace."""
    workspace = tmp_path / "workspace"
    broker = _FakeBroker()
    tool = _tool(tmp_path, broker=broker, workspace=workspace)

    payload = json.loads(await tool.ensure_my_repository())

    assert payload == {
        "clone_url": "https://github.com/example-org/MindRoom-redwood.git",
        "organization": "example-org",
        "repository_id": 42,
        "repository_name": "MindRoom-redwood",
        "status": "ok",
        "tool": "agent_repository",
        "workspace": str(workspace),
    }
    assert broker.requests == [
        RepositoryEnsureRequest(
            worker_key=_target().worker_key,
            organization="example-org",
            repository_name="MindRoom-redwood",
        ),
    ]
    assert _git(workspace, "remote", "get-url", "origin") == _lease().clone_url
    git_config = (workspace / ".git" / "config").read_text(encoding="utf-8")
    assert "control-plane-secret" not in git_config
    assert "extraheader" not in git_config.casefold()

    binding = RepositoryBindingStore(_runtime_paths(tmp_path)).read(_target().worker_key)
    assert binding is not None
    assert binding.repository_id == 42


@pytest.mark.asyncio
async def test_ensure_runs_no_git_network_or_push_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provisioning only edits local Git metadata; workers perform scoped pushes later."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    commands: list[tuple[str, ...]] = []
    real_subprocess_run = agent_repositories_module.subprocess.run

    def record_subprocess_run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(tuple(arguments))
        return real_subprocess_run(arguments, **kwargs)

    monkeypatch.setattr(agent_repositories_module.subprocess, "run", record_subprocess_run)

    payload = json.loads(await _tool(tmp_path, broker=_FakeBroker(), workspace=workspace).ensure_my_repository())

    assert payload["status"] == "ok"
    assert commands
    assert all(command[:2] == ("git", "config") for command in commands)


@pytest.mark.asyncio
async def test_concurrent_ensure_calls_are_idempotent(tmp_path: Path) -> None:
    """Concurrent calls should converge on one binding and one canonical origin."""
    workspace = tmp_path / "workspace"
    broker = _FakeBroker(delay=0.01)
    tool = _tool(tmp_path, broker=broker, workspace=workspace)

    first, second = await asyncio.gather(
        tool.ensure_my_repository(),
        tool.ensure_my_repository(),
    )

    assert first == second
    assert len(broker.requests) == 2
    assert _git(workspace, "remote", "get-url", "origin") == _lease().clone_url


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        "https://GITHUB.com/EXAMPLE-ORG/MindRoom-redwood",
        "https://github.com:443/example-org/MindRoom-redwood.git/",
    ],
)
async def test_normalized_https_origin_is_idempotent(tmp_path: Path, origin: str) -> None:
    """Harmless HTTPS syntax variants for the bound repository are equivalent."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    _git(workspace, "remote", "add", "origin", origin)
    config_before = (workspace / ".git" / "config").read_bytes()
    tool = _tool(tmp_path, broker=_FakeBroker(), workspace=workspace)

    payload = json.loads(await tool.ensure_my_repository())

    assert payload["status"] == "ok"
    assert _git(workspace, "remote", "get-url", "origin") == origin
    assert (workspace / ".git" / "config").read_bytes() == config_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        "git@github.com:example-org/MindRoom-redwood.git",
        "ssh://git@github.com/example-org/MindRoom-redwood.git",
    ],
)
async def test_same_repository_over_ssh_is_an_origin_conflict(tmp_path: Path, origin: str) -> None:
    """SSH bypasses the HTTPS-only Agent Vault proxy and must fail unchanged."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    _git(workspace, "remote", "add", "origin", origin)
    config_before = (workspace / ".git" / "config").read_bytes()
    tool = _tool(tmp_path, broker=_FakeBroker(), workspace=workspace)

    payload = json.loads(await tool.ensure_my_repository())

    assert payload["status"] == "origin_conflict"
    assert _git(workspace, "remote", "get-url", "origin") == origin
    assert (workspace / ".git" / "config").read_bytes() == config_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        "git@github.com:example-org/MindRoom-redwood.git",
        "https://evil.example/other.git",
    ],
)
async def test_git_url_rewrite_cannot_disguise_conflicting_origin(tmp_path: Path, origin: str) -> None:
    """Git insteadOf expansion must not turn a raw hostile origin into trusted HTTPS."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    _git(workspace, "remote", "add", "origin", origin)
    _git(workspace, "config", "--local", f"url.{_lease().clone_url}.insteadOf", origin)
    assert _git(workspace, "remote", "get-url", "origin") == _lease().clone_url
    config_before = (workspace / ".git" / "config").read_bytes()
    tool = _tool(tmp_path, broker=_FakeBroker(), workspace=workspace)

    payload = json.loads(await tool.ensure_my_repository())

    assert payload["status"] == "origin_conflict"
    assert _git(workspace, "config", "--local", "--get", "remote.origin.url") == origin
    assert (workspace / ".git" / "config").read_bytes() == config_before


@pytest.mark.asyncio
async def test_mismatched_push_url_is_an_origin_conflict(tmp_path: Path) -> None:
    """A hidden push URL must not bypass a matching credential-free fetch URL."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    _git(workspace, "remote", "add", "origin", _lease().clone_url)
    _git(workspace, "remote", "set-url", "--add", "--push", "origin", "https://github.com/other/repository.git")
    config_before = (workspace / ".git" / "config").read_bytes()
    tool = _tool(tmp_path, broker=_FakeBroker(), workspace=workspace)

    payload = json.loads(await tool.ensure_my_repository())

    assert payload["status"] == "origin_conflict"
    assert (workspace / ".git" / "config").read_bytes() == config_before


@pytest.mark.asyncio
async def test_symlinked_git_metadata_fails_without_mutating_target(tmp_path: Path) -> None:
    """An agent-controlled .git link must not redirect trusted config writes elsewhere."""
    external = tmp_path / "external"
    external.mkdir()
    _git(external, "init", "--initial-branch=main")
    config_before = (external / ".git" / "config").read_bytes()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").symlink_to(external / ".git", target_is_directory=True)

    payload = json.loads(await _tool(tmp_path, broker=_FakeBroker(), workspace=workspace).ensure_my_repository())

    assert payload["status"] == "error"
    assert "Git metadata" in payload["error"]
    assert (external / ".git" / "config").read_bytes() == config_before


@pytest.mark.asyncio
async def test_symlinked_git_config_fails_without_mutating_target(tmp_path: Path) -> None:
    """Trusted origin setup must never follow a nested Git config link."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    external_config = tmp_path / "external-config"
    config_path = workspace / ".git" / "config"
    external_config.write_bytes(config_path.read_bytes())
    config_path.unlink()
    config_path.symlink_to(external_config)
    config_before = external_config.read_bytes()

    payload = json.loads(await _tool(tmp_path, broker=_FakeBroker(), workspace=workspace).ensure_my_repository())

    assert payload["status"] == "error"
    assert "Git metadata" in payload["error"]
    assert external_config.read_bytes() == config_before


@pytest.mark.asyncio
async def test_git_directory_swap_cannot_redirect_origin_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Origin mutation must stay bound to inspected Git metadata during a concurrent swap."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    external = tmp_path / "external"
    external.mkdir()
    _git(external, "init", "--initial-branch=main")
    external_config = external / ".git" / "config"
    external_before = external_config.read_bytes()
    original_read_git_config_entries = agent_repositories_module._read_git_config_entries
    swapped = False

    def swap_after_config_inspection(config_fd: int) -> tuple[tuple[str, str], ...]:
        nonlocal swapped
        result = original_read_git_config_entries(config_fd)
        if not swapped:
            (workspace / ".git").rename(workspace / ".git-original")
            (workspace / ".git").symlink_to(external / ".git", target_is_directory=True)
            swapped = True
        return result

    monkeypatch.setattr(agent_repositories_module, "_read_git_config_entries", swap_after_config_inspection)

    payload = json.loads(await _tool(tmp_path, broker=_FakeBroker(), workspace=workspace).ensure_my_repository())

    assert swapped
    assert payload["status"] == "error"
    assert external_config.read_bytes() == external_before


@pytest.mark.asyncio
async def test_symlinked_workspace_root_fails_without_mutating_target(tmp_path: Path) -> None:
    """An agent-controlled workspace link must not redirect trusted Git writes."""
    external = tmp_path / "external"
    external.mkdir()
    workspace = tmp_path / "workspace"
    workspace.symlink_to(external, target_is_directory=True)

    payload = json.loads(await _tool(tmp_path, broker=_FakeBroker(), workspace=workspace).ensure_my_repository())

    assert payload["status"] == "error"
    assert "workspace path" in payload["error"]
    assert not (external / ".git").exists()


@pytest.mark.asyncio
async def test_symlinked_workspace_parent_fails_without_mutating_target(tmp_path: Path) -> None:
    """Workspace parent links must not redirect trusted directory creation."""
    external = tmp_path / "external"
    external.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(external, target_is_directory=True)
    workspace = linked_parent / "workspace"

    payload = json.loads(await _tool(tmp_path, broker=_FakeBroker(), workspace=workspace).ensure_my_repository())

    assert payload["status"] == "error"
    assert "workspace path" in payload["error"]
    assert not (external / "workspace").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/other/repository.git",
        "git@github.com:other/repository.git",
    ],
)
async def test_existing_different_origin_fails_closed(tmp_path: Path, origin: str) -> None:
    """The trusted tool must never replace a workspace's existing origin."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    _git(workspace, "remote", "add", "origin", origin)
    config_before = (workspace / ".git" / "config").read_bytes()
    tool = _tool(tmp_path, broker=_FakeBroker(), workspace=workspace)

    payload = json.loads(await tool.ensure_my_repository())

    assert payload == {
        "error": "Agent repository workspace has an origin for a different repository",
        "status": "origin_conflict",
        "tool": "agent_repository",
    }
    assert _git(workspace, "remote", "get-url", "origin") == origin
    assert (workspace / ".git" / "config").read_bytes() == config_before


@pytest.mark.asyncio
async def test_immutable_binding_collision_is_not_disclosed_or_rebound(tmp_path: Path) -> None:
    """A different repository ID for one worker must fail without leaking broker authority."""
    target = _target()
    assert target.worker_key is not None
    request = RepositoryEnsureRequest(
        worker_key=target.worker_key,
        organization="example-org",
        repository_name="MindRoom-redwood",
    )
    RepositoryBindingStore(_runtime_paths(tmp_path)).bind(request, _lease(repository_id=7))
    tool = _tool(tmp_path, broker=_FakeBroker(lease=_lease(repository_id=99)))

    result = await tool.ensure_my_repository()
    payload = json.loads(result)

    assert payload["status"] == "error"
    assert "immutable repository binding" in payload["error"]
    assert "token" not in result.casefold()
    assert RepositoryBindingStore(_runtime_paths(tmp_path)).read(target.worker_key).repository_id == 7


def test_tool_builds_from_registry_with_trusted_runtime_inputs(tmp_path: Path) -> None:
    """Registry wiring should inject policy, identity, workspace, and primary RuntimePaths."""
    workspace = tmp_path / "workspace"
    tool = get_tool_by_name(
        "agent_repository",
        _runtime_paths(tmp_path, broker_config=True),
        runtime_overrides={"organization": "example-org", "prefix": "MindRoom"},
        tool_output_workspace_root=workspace,
        worker_target=_target(),
    )

    assert isinstance(tool, AgentRepositoryTools)
    assert set(tool.async_functions) == {"ensure_my_repository"}
    assert tool.functions == {}
