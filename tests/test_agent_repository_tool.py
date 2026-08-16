"""Trusted zero-argument agent repository tool tests."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from threading import Event, Timer
from typing import TYPE_CHECKING

import pytest

import mindroom.agent_repositories as agent_repositories_module
import mindroom.custom_tools.agent_repository as agent_repository_tool_module
from mindroom.agent_repositories import (
    RepositoryBindingError,
    RepositoryBindingStore,
    RepositoryEnsureRequest,
    RepositoryLease,
    RepositoryOriginConflictError,
    configure_repository_workspace,
)
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.agent_repository import AgentRepositoryTools
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.sandbox_proxy import ensure_worker_target_ready
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    resolve_worker_target,
    tool_stays_local,
)
from mindroom.workers.backend import WorkerBackendError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


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


def _lease(repository_id: str = "42") -> RepositoryLease:
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
    events: list[str] | None = None

    async def ensure_repository(self, request: RepositoryEnsureRequest) -> RepositoryLease:
        if self.events is not None:
            self.events.append("broker")
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.lease


def _tool(
    tmp_path: Path,
    *,
    broker: _FakeBroker,
    workspace: Path | None = None,
    worker_preparer: Callable[[ResolvedWorkerTarget], None] | None = None,
) -> AgentRepositoryTools:
    return AgentRepositoryTools(
        organization="example-org",
        prefix="MindRoom",
        runtime_paths=_runtime_paths(tmp_path),
        worker_target=_target(),
        tool_output_workspace_root=workspace or tmp_path / "workspace",
        broker=broker,
        worker_preparer=worker_preparer or (lambda _target: None),
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
        "repository_id": "42",
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
    assert binding.repository_id == "42"


@pytest.mark.asyncio
async def test_ensure_prepares_worker_before_calling_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh workers must create their Agent Vault vault before repository ensure."""
    events: list[str] = []
    broker = _FakeBroker(events=events)

    def prepare_worker(target: ResolvedWorkerTarget) -> None:
        assert target.worker_key == _target().worker_key
        events.append("worker")

    monkeypatch.setattr(
        agent_repository_tool_module,
        "ensure_worker_target_ready",
        lambda _runtime_paths, target: prepare_worker(target),
    )
    tool = AgentRepositoryTools(
        organization="example-org",
        prefix="MindRoom",
        runtime_paths=_runtime_paths(tmp_path),
        worker_target=_target(),
        tool_output_workspace_root=tmp_path / "workspace",
        broker=broker,
    )

    payload = json.loads(await tool.ensure_my_repository())

    assert payload["status"] == "ok"
    assert events == ["worker", "broker"]


@pytest.mark.asyncio
async def test_worker_preparation_failure_stops_before_broker(tmp_path: Path) -> None:
    """Repository creation must not race ahead when its scoped worker vault is unavailable."""
    broker = _FakeBroker()

    def fail_worker_preparation(_target: ResolvedWorkerTarget) -> None:
        msg = "worker unavailable"
        raise WorkerBackendError(msg)

    payload = json.loads(
        await _tool(tmp_path, broker=broker, worker_preparer=fail_worker_preparation).ensure_my_repository(),
    )

    assert payload["status"] == "error"
    assert payload["error"] == "worker unavailable"
    assert broker.requests == []


@pytest.mark.parametrize(
    "process_env",
    [
        {"MINDROOM_WORKER_BACKEND": "docker"},
        {"MINDROOM_WORKER_BACKEND": "kubernetes"},
    ],
)
def test_worker_preparation_requires_kubernetes_agent_vault(
    tmp_path: Path,
    process_env: dict[str, str],
) -> None:
    """Repository readiness must fail before provisioning without the HTTPS credential path."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env=process_env,
    )

    with pytest.raises(WorkerBackendError, match="Kubernetes workers with Agent Vault"):
        ensure_worker_target_ready(runtime_paths, _target())


@pytest.mark.asyncio
async def test_binding_and_workspace_configuration_do_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Durable binding locks and Git setup must run outside the shared event loop."""
    release = Event()
    heartbeat_seen = Event()
    observed_heartbeat: list[bool] = []
    real_bind = RepositoryBindingStore.bind

    def blocking_bind(
        store: RepositoryBindingStore,
        request: RepositoryEnsureRequest,
        lease: RepositoryLease,
    ) -> object:
        release.wait(timeout=2)
        return real_bind(store, request, lease)

    monkeypatch.setattr(RepositoryBindingStore, "bind", blocking_bind)
    tool = _tool(tmp_path, broker=_FakeBroker())

    async def heartbeat() -> None:
        await asyncio.sleep(0.01)
        heartbeat_seen.set()

    def observe_heartbeat_and_release() -> None:
        observed_heartbeat.append(heartbeat_seen.is_set())
        release.set()

    timer = Timer(0.1, observe_heartbeat_and_release)
    timer.start()
    try:
        ensure_task = asyncio.create_task(tool.ensure_my_repository())
        heartbeat_task = asyncio.create_task(heartbeat())
        await ensure_task
        await heartbeat_task
    finally:
        release.set()
        timer.cancel()

    assert observed_heartbeat == [True]


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
        _lease().clone_url.replace("https://github.com/", "https://github.com:443/") + "/",
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
        _lease().clone_url.removesuffix(".git"),
        _lease().clone_url.replace("github.com", "GITHUB.com"),
        _lease().clone_url.replace("/example-org/", "/EXAMPLE-ORG/"),
        _lease().clone_url.replace("/MindRoom-", "/mindroom-"),
    ],
)
async def test_uncredentialed_https_origin_is_an_origin_conflict(tmp_path: Path, origin: str) -> None:
    """Origins outside Agent Vault's exact Git path must fail unchanged."""
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
@pytest.mark.parametrize("suffix", ["?", "#", "\n", " "])
async def test_malformed_canonical_https_origin_is_an_origin_conflict(tmp_path: Path, suffix: str) -> None:
    """URL parsing must not normalize malformed raw origin syntax into readiness."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    origin = _lease().clone_url + suffix
    _git(workspace, "remote", "add", "origin", origin)
    config_before = (workspace / ".git" / "config").read_bytes()

    payload = json.loads(await _tool(tmp_path, broker=_FakeBroker(), workspace=workspace).ensure_my_repository())

    assert payload["status"] == "origin_conflict"
    assert (workspace / ".git" / "config").read_bytes() == config_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("remote.origin.proxy", ""),
        ("remote.origin.proxyAuthMethod", "anyauth"),
        ("remote.origin.vcs", "ext"),
        ("http.https://github.com/.proxy", ""),
        ("http.https://github.com/.proxyAuthMethod", "anyauth"),
    ],
)
async def test_git_transport_override_cannot_bypass_bound_origin(
    tmp_path: Path,
    key: str,
    value: str,
) -> None:
    """Repository-local transport policy must not bypass the Agent Vault route."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    _git(workspace, "remote", "add", "origin", _lease().clone_url)
    _git(workspace, "config", "--local", key, value)
    config_before = (workspace / ".git" / "config").read_bytes()

    payload = json.loads(await _tool(tmp_path, broker=_FakeBroker(), workspace=workspace).ensure_my_repository())

    assert payload["status"] == "origin_conflict"
    assert (workspace / ".git" / "config").read_bytes() == config_before


def test_differently_cased_remote_name_does_not_replace_lowercase_origin(tmp_path: Path) -> None:
    """A distinct `Origin` remote must not satisfy the required lowercase `origin`."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    _git(workspace, "remote", "add", "Origin", _lease().clone_url)
    configure_repository_workspace(
        workspace=workspace,
        clone_url=_lease().clone_url,
        lock_path=tmp_path / "workspace.lock",
    )

    assert _git(workspace, "remote", "get-url", "Origin") == _lease().clone_url
    assert _git(workspace, "remote", "get-url", "origin") == _lease().clone_url


def test_push_url_without_fetch_url_is_an_origin_conflict(tmp_path: Path) -> None:
    """A push-only remote is not a usable bound origin."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    _git(workspace, "config", "--local", "remote.origin.pushurl", _lease().clone_url)
    config_before = (workspace / ".git" / "config").read_bytes()

    with pytest.raises(RepositoryOriginConflictError):
        configure_repository_workspace(
            workspace=workspace,
            clone_url=_lease().clone_url,
            lock_path=tmp_path / "workspace.lock",
        )
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
async def test_git_push_url_rewrite_cannot_redirect_bound_origin(tmp_path: Path) -> None:
    """Git pushInsteadOf must not redirect a canonical stored origin."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    _git(workspace, "remote", "add", "origin", _lease().clone_url)
    hostile_url = "https://attacker.example/exfil.git"
    _git(workspace, "config", "--local", f"url.{hostile_url}.pushInsteadOf", _lease().clone_url)
    assert _git(workspace, "remote", "get-url", "--push", "origin") == hostile_url
    config_before = (workspace / ".git" / "config").read_bytes()

    payload = json.loads(await _tool(tmp_path, broker=_FakeBroker(), workspace=workspace).ensure_my_repository())

    assert payload["status"] == "origin_conflict"
    assert (workspace / ".git" / "config").read_bytes() == config_before


@pytest.mark.asyncio
@pytest.mark.parametrize("indirect_config", ["config.worktree", "commondir"])
async def test_indirect_git_configuration_is_an_origin_conflict(tmp_path: Path, indirect_config: str) -> None:
    """Git metadata indirection must not hide the effective push target."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    _git(workspace, "remote", "add", "origin", _lease().clone_url)
    hostile_url = "https://attacker.example/exfil.git"

    if indirect_config == "config.worktree":
        _git(workspace, "config", "--local", "extensions.worktreeConfig", "true")
        _git(workspace, "config", "--worktree", "remote.origin.pushurl", hostile_url)
    else:
        external = tmp_path / "external"
        external.mkdir()
        _git(external, "init", "--initial-branch=main")
        _git(external, "remote", "add", "origin", hostile_url)
        (workspace / ".git" / "commondir").write_text(str(external / ".git"), encoding="utf-8")

    assert _git(workspace, "remote", "get-url", "--push", "origin") == hostile_url
    config_before = (workspace / ".git" / "config").read_bytes()

    payload = json.loads(await _tool(tmp_path, broker=_FakeBroker(), workspace=workspace).ensure_my_repository())

    assert payload["status"] == "origin_conflict"
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
async def test_git_config_swap_cannot_redirect_origin_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Origin mutation must not replace a config that changed after inspection."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    config_path = workspace / ".git" / "config"
    inspected_config = config_path.with_name("config-inspected")
    config_before = config_path.read_bytes()
    hostile_config = b'[remote "origin"]\n\turl = git@github.com:other/repository.git\n'
    original_read_git_config_payload = agent_repositories_module._read_git_config_payload
    inspection_count = 0

    def swap_after_final_config_inspection(config_fd: int) -> bytes:
        nonlocal inspection_count
        result = original_read_git_config_payload(config_fd)
        inspection_count += 1
        if inspection_count == 2:
            config_path.rename(inspected_config)
            config_path.write_bytes(hostile_config)
        return result

    monkeypatch.setattr(agent_repositories_module, "_read_git_config_payload", swap_after_final_config_inspection)

    payload = json.loads(await _tool(tmp_path, broker=_FakeBroker(), workspace=workspace).ensure_my_repository())

    assert inspection_count == 2
    assert payload["status"] == "error"
    assert inspected_config.read_bytes() == config_before
    assert config_path.read_bytes() == hostile_config


@pytest.mark.asyncio
async def test_matching_origin_config_swap_cannot_return_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config path replacement after approval must invalidate workspace readiness."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    _git(workspace, "remote", "add", "origin", _lease().clone_url)
    config_path = workspace / ".git" / "config"
    approved_config = config_path.with_name("config-approved")
    config_before = config_path.read_bytes()
    hostile_config = config_before.replace(_lease().clone_url.encode(), b"https://github.com/other/repository.git")
    original_read_git_config_entries = agent_repositories_module._read_git_config_entries
    swapped = False

    def swap_after_origin_approval(config_fd: int) -> tuple[tuple[str, str], ...]:
        nonlocal swapped
        result = original_read_git_config_entries(config_fd)
        if not swapped:
            config_path.rename(approved_config)
            config_path.write_bytes(hostile_config)
            swapped = True
        return result

    monkeypatch.setattr(agent_repositories_module, "_read_git_config_entries", swap_after_origin_approval)

    payload = json.loads(await _tool(tmp_path, broker=_FakeBroker(), workspace=workspace).ensure_my_repository())

    assert swapped
    assert payload["status"] == "error"
    assert approved_config.read_bytes() == config_before
    assert config_path.read_bytes() == hostile_config


@pytest.mark.asyncio
async def test_matching_origin_content_change_cannot_return_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-inode config change after final approval must invalidate readiness."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    _git(workspace, "remote", "add", "origin", _lease().clone_url)
    config_path = workspace / ".git" / "config"
    config_inode = config_path.stat().st_ino
    hostile_config = config_path.read_bytes().replace(
        _lease().clone_url.encode(),
        b"https://github.com/other/repository.git",
    )
    original_read_git_config_entries = agent_repositories_module._read_git_config_entries
    inspection_count = 0

    def change_after_final_origin_approval(config_fd: int) -> tuple[tuple[str, str], ...]:
        nonlocal inspection_count
        result = original_read_git_config_entries(config_fd)
        inspection_count += 1
        if inspection_count == 2:
            with config_path.open("r+b") as config_file:
                config_file.truncate()
                config_file.write(hostile_config)
                config_file.flush()
                os.fsync(config_file.fileno())
        return result

    monkeypatch.setattr(agent_repositories_module, "_read_git_config_entries", change_after_final_origin_approval)

    payload = json.loads(await _tool(tmp_path, broker=_FakeBroker(), workspace=workspace).ensure_my_repository())

    assert inspection_count == 2
    assert payload["status"] == "error"
    assert config_path.stat().st_ino == config_inode
    assert config_path.read_bytes() == hostile_config


@pytest.mark.asyncio
async def test_workspace_path_swap_cannot_return_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readiness must refer to the workspace path, not a renamed descriptor target."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    _git(workspace, "remote", "add", "origin", _lease().clone_url)
    original_workspace = tmp_path / "workspace-original"
    original_git_directory_is_current = agent_repositories_module._git_directory_is_current
    hostile_origin = "https://github.com/other/repository.git"
    replacement_config: bytes | None = None
    swapped = False

    def swap_workspace_before_success(workspace_fd: int, git_fd: int) -> bool:
        nonlocal replacement_config, swapped
        result = original_git_directory_is_current(workspace_fd, git_fd)
        if not swapped:
            workspace.rename(original_workspace)
            workspace.mkdir()
            _git(workspace, "init", "--initial-branch=main")
            _git(workspace, "remote", "add", "origin", hostile_origin)
            replacement_config = (workspace / ".git" / "config").read_bytes()
            swapped = True
        return result

    monkeypatch.setattr(agent_repositories_module, "_git_directory_is_current", swap_workspace_before_success)

    payload = json.loads(await _tool(tmp_path, broker=_FakeBroker(), workspace=workspace).ensure_my_repository())

    assert swapped
    assert payload["status"] == "error"
    assert replacement_config is not None
    assert (workspace / ".git" / "config").read_bytes() == replacement_config
    assert _git(workspace, "remote", "get-url", "origin") == hostile_origin


@pytest.mark.asyncio
async def test_failed_git_initialization_leaves_no_partial_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed first initialization must leave a clean retryable workspace."""
    workspace = tmp_path / "workspace"
    real_atomic_write_at = agent_repositories_module._atomic_write_at

    def fail_config_write(directory_fd: int, name: str, payload: bytes, *, mode: int) -> None:
        if name == "config":
            msg = "injected config write failure"
            raise OSError(msg)
        real_atomic_write_at(directory_fd, name, payload, mode=mode)

    monkeypatch.setattr(agent_repositories_module, "_atomic_write_at", fail_config_write)

    payload = json.loads(await _tool(tmp_path, broker=_FakeBroker(), workspace=workspace).ensure_my_repository())

    assert payload["status"] == "error"
    assert not (workspace / ".git").exists()
    assert list(workspace.iterdir()) == []


def test_concurrent_git_config_write_is_not_erased_during_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed origin publication must preserve bytes written by a concurrent owner."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    config_path = workspace / ".git" / "config"
    concurrent_change = b"# concurrent owner change\n"
    original_write_all = agent_repositories_module._write_all
    concurrent_write_seen = False

    def write_concurrently_during_origin_publication(file_fd: int, payload: bytes) -> None:
        nonlocal concurrent_write_seen
        original_write_all(file_fd, payload)
        if not concurrent_write_seen and _lease().clone_url.encode() in payload:
            with config_path.open("ab") as config_file:
                config_file.write(concurrent_change)
                config_file.flush()
                os.fsync(config_file.fileno())
            concurrent_write_seen = True

    monkeypatch.setattr(agent_repositories_module, "_write_all", write_concurrently_during_origin_publication)

    with pytest.raises(RepositoryBindingError, match="changed during configuration"):
        configure_repository_workspace(
            workspace=workspace,
            clone_url=_lease().clone_url,
            lock_path=tmp_path / "workspace.lock",
        )

    assert concurrent_write_seen
    config_after = config_path.read_bytes()
    assert concurrent_change in config_after
    assert _lease().clone_url.encode() not in config_after


@pytest.mark.parametrize(
    ("crash_point", "exit_code"),
    [
        ("before_backup", 21),
        ("before_publication", 22),
        ("after_publication", 23),
    ],
)
def test_interrupted_git_config_publication_recovers_on_retry(
    tmp_path: Path,
    crash_point: str,
    exit_code: int,
) -> None:
    """Process death at each publication boundary must leave a retryable workspace."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    lock_path = tmp_path / "workspace.lock"
    crash_script = """
import os
import sys
from pathlib import Path

import mindroom.agent_repositories as repositories

real_rename = repositories._rename_at_no_replace

def crash_during_config_publication(source_fd, source, destination_fd, destination):
    point = sys.argv[4]
    publishing_backup = source == "config" and destination.startswith(".config.mindroom-backup")
    publishing_config = destination == "config" and source.startswith(".config.mindroom-stage")
    if point == "before_backup" and publishing_backup:
        os._exit(21)
    if point == "before_publication" and publishing_config:
        os._exit(22)
    real_rename(source_fd, source, destination_fd, destination)
    if point == "after_publication" and publishing_config:
        os._exit(23)

repositories._rename_at_no_replace = crash_during_config_publication
repositories.configure_repository_workspace(
    workspace=Path(sys.argv[1]),
    clone_url=sys.argv[3],
    lock_path=Path(sys.argv[2]),
)
"""

    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            crash_script,
            str(workspace),
            str(lock_path),
            _lease().clone_url,
            crash_point,
        ],
        check=False,
    )

    assert crashed.returncode == exit_code
    assert any(path.name.startswith(".config.mindroom-") for path in (workspace / ".git").iterdir())

    configure_repository_workspace(
        workspace=workspace,
        clone_url=_lease().clone_url,
        lock_path=lock_path,
    )

    assert _git(workspace, "remote", "get-url", "origin") == _lease().clone_url
    assert not any(path.name.startswith(".config.mindroom-") for path in (workspace / ".git").iterdir())


def test_partial_interrupted_git_config_stage_is_discarded_on_retry(tmp_path: Path) -> None:
    """Process death while writing the stage must not permanently wedge the workspace."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    lock_path = tmp_path / "workspace.lock"
    crash_script = """
import os
import sys
from pathlib import Path

import mindroom.agent_repositories as repositories

real_write_all = repositories._write_all

def crash_during_stage_write(file_fd, payload):
    if sys.argv[3].encode() in payload:
        os.write(file_fd, payload[: max(1, len(payload) // 2)])
        os._exit(24)
    real_write_all(file_fd, payload)

repositories._write_all = crash_during_stage_write
repositories.configure_repository_workspace(
    workspace=Path(sys.argv[1]),
    clone_url=sys.argv[3],
    lock_path=Path(sys.argv[2]),
)
"""

    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            crash_script,
            str(workspace),
            str(lock_path),
            _lease().clone_url,
        ],
        check=False,
    )

    assert crashed.returncode == 24
    assert (workspace / ".git" / "config").exists()
    assert any(path.name.startswith(".config.mindroom-stage") for path in (workspace / ".git").iterdir())

    configure_repository_workspace(
        workspace=workspace,
        clone_url=_lease().clone_url,
        lock_path=lock_path,
    )

    assert _git(workspace, "remote", "get-url", "origin") == _lease().clone_url
    assert not any(path.name.startswith(".config.mindroom-") for path in (workspace / ".git").iterdir())


def test_origin_publication_preserves_existing_git_config_mode(tmp_path: Path) -> None:
    """Adding origin must not broaden an existing Git config's permissions."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    config_path = workspace / ".git" / "config"
    config_path.chmod(0o600)

    configure_repository_workspace(
        workspace=workspace,
        clone_url=_lease().clone_url,
        lock_path=tmp_path / "workspace.lock",
    )

    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_directory_fsync_failure_preserves_retryable_config_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A durability error after backup publication must keep enough state for retry."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    lock_path = tmp_path / "workspace.lock"
    real_rename = agent_repositories_module._rename_at_no_replace
    real_fsync = agent_repositories_module.os.fsync
    backup_published = False
    fsync_failed = False

    def track_backup_publication(source_fd: int, source: str, destination_fd: int, destination: str) -> None:
        nonlocal backup_published
        real_rename(source_fd, source, destination_fd, destination)
        if source == "config" and destination.startswith(".config.mindroom-backup"):
            backup_published = True

    def fail_first_directory_fsync_after_backup(file_fd: int) -> None:
        nonlocal fsync_failed
        if backup_published and not fsync_failed and stat.S_ISDIR(os.fstat(file_fd).st_mode):
            fsync_failed = True
            msg = "injected directory fsync failure"
            raise OSError(msg)
        real_fsync(file_fd)

    monkeypatch.setattr(agent_repositories_module, "_rename_at_no_replace", track_backup_publication)
    monkeypatch.setattr(agent_repositories_module.os, "fsync", fail_first_directory_fsync_after_backup)

    with pytest.raises(OSError, match="injected directory fsync failure"):
        configure_repository_workspace(
            workspace=workspace,
            clone_url=_lease().clone_url,
            lock_path=lock_path,
        )

    assert backup_published
    assert fsync_failed
    assert not (workspace / ".git" / "config").exists()

    configure_repository_workspace(
        workspace=workspace,
        clone_url=_lease().clone_url,
        lock_path=lock_path,
    )

    assert _git(workspace, "remote", "get-url", "origin") == _lease().clone_url
    assert not any(path.name.startswith(".config.mindroom-") for path in (workspace / ".git").iterdir())


@pytest.mark.asyncio
async def test_oversized_git_config_fails_without_mutation(tmp_path: Path) -> None:
    """Worker-controlled Git config must have a strict control-plane memory bound."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    config_path = workspace / ".git" / "config"
    config_path.write_bytes(config_path.read_bytes() + b"#" + (b"x" * (2 * 1024 * 1024)) + b"\n")
    config_before = config_path.read_bytes()

    payload = json.loads(await _tool(tmp_path, broker=_FakeBroker(), workspace=workspace).ensure_my_repository())

    assert payload["status"] == "error"
    assert "too large" in payload["error"]
    assert config_path.read_bytes() == config_before


def test_git_config_parser_has_processing_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git config parsing must not consume an unbounded control-plane worker thread."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    real_run = subprocess.run
    observed_timeouts: list[float] = []

    def require_timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        timeout = kwargs.get("timeout")
        assert isinstance(timeout, float)
        observed_timeouts.append(timeout)
        return real_run(*args, **kwargs)  # type: ignore[call-overload,return-value]

    monkeypatch.setattr(agent_repositories_module.subprocess, "run", require_timeout)

    configure_repository_workspace(
        workspace=workspace,
        clone_url=_lease().clone_url,
        lock_path=tmp_path / "workspace.lock",
    )

    assert observed_timeouts
    assert all(timeout <= 5 for timeout in observed_timeouts)


def test_git_config_parser_does_not_follow_include_paths(tmp_path: Path) -> None:
    """Inspection must reject includes without opening worker-selected control-plane paths."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--initial-branch=main")
    included_fifo = tmp_path / "included-config"
    os.mkfifo(included_fifo)
    config_path = workspace / ".git" / "config"
    config_path.write_text(f"[include]\n\tpath = {included_fifo}\n", encoding="utf-8")
    config_before = config_path.read_bytes()

    started = time.monotonic()
    with pytest.raises(RepositoryOriginConflictError):
        configure_repository_workspace(
            workspace=workspace,
            clone_url=_lease().clone_url,
            lock_path=tmp_path / "workspace.lock",
        )

    assert time.monotonic() - started < 1
    assert config_path.read_bytes() == config_before


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
        "https://[bad",
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
    RepositoryBindingStore(_runtime_paths(tmp_path)).bind(request, _lease(repository_id="7"))
    tool = _tool(tmp_path, broker=_FakeBroker(lease=_lease(repository_id="99")))

    result = await tool.ensure_my_repository()
    payload = json.loads(result)

    assert payload["status"] == "error"
    assert "immutable repository binding" in payload["error"]
    assert "token" not in result.casefold()
    assert RepositoryBindingStore(_runtime_paths(tmp_path)).read(target.worker_key).repository_id == "7"


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
    function = tool.async_functions["ensure_my_repository"].model_copy(deep=True)
    function.process_entrypoint()
    assert function.parameters["properties"] == {}


@pytest.mark.asyncio
async def test_incomplete_existing_git_directory_fails_without_mutation(tmp_path: Path) -> None:
    """Interrupted local initialization must not report a repository-ready workspace."""
    workspace = tmp_path / "workspace"
    git_directory = workspace / ".git"
    git_directory.mkdir(parents=True)
    config = git_directory / "config"
    config.write_text(
        '[remote "origin"]\n\turl = https://github.com/example-org/MindRoom-redwood.git\n',
        encoding="utf-8",
    )
    config_before = config.read_bytes()

    payload = json.loads(await _tool(tmp_path, broker=_FakeBroker(), workspace=workspace).ensure_my_repository())

    assert payload["status"] == "error"
    assert "incomplete Git metadata" in payload["error"]
    assert config.read_bytes() == config_before
    assert {path.name for path in git_directory.iterdir()} == {"config"}
