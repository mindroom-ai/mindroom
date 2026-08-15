"""Domain tests for constrained agent-owned repositories."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.agent_repositories import (
    RepositoryBindingError,
    RepositoryBindingStore,
    RepositoryEnsureRequest,
    RepositoryLease,
    derive_repository_name,
)
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


def _target(
    *,
    agent_name: str = "redwood",
    worker_scope: str = "shared",
    requester_id: str | None = None,
    private: bool = False,
) -> ResolvedWorkerTarget:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent_name,
        requester_id=requester_id,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
        tenant_id=None,
    )
    return resolve_worker_target(
        worker_scope,  # type: ignore[arg-type]
        agent_name,
        identity,
        private_agent_names=frozenset({agent_name}) if private else frozenset(),
    )


def _request(target: ResolvedWorkerTarget, name: str) -> RepositoryEnsureRequest:
    assert target.worker_key is not None
    return RepositoryEnsureRequest(
        worker_key=target.worker_key,
        organization="example-org",
        repository_name=name,
    )


def _lease(name: str = "MindRoom-redwood", repository_id: str = "42") -> RepositoryLease:
    return RepositoryLease(
        repository_id=repository_id,
        organization="example-org",
        repository_name=name,
        clone_url=f"https://github.com/example-org/{name}.git",
    )


def test_shared_repository_name_has_no_requester_suffix() -> None:
    """Shared agents should own exactly one repository across requesters."""
    target = _target(requester_id="@ignored:example.test")

    assert derive_repository_name(prefix="MindRoom", worker_target=target) == "MindRoom-redwood"


def test_private_repository_name_uses_matrix_requester_localpart() -> None:
    """Private user-agent workers should use their authoritative Matrix localpart."""
    target = _target(
        agent_name="mind",
        worker_scope="user_agent",
        requester_id="@basnijholt:example.test",
        private=True,
    )

    assert derive_repository_name(prefix="MindRoom", worker_target=target) == "MindRoom-mind-basnijholt"


def test_repository_name_sanitizes_hostile_slug_characters() -> None:
    """Names derived inside the control plane must never preserve path or URL syntax."""
    hostile = replace(_target(), routing_agent_name="../../Security Agent?token=secret")

    assert derive_repository_name(prefix="MindRoom", worker_target=hostile) == "MindRoom-security-agent-token-secret"


def test_repository_name_hashes_long_values_before_github_limit() -> None:
    """Distinct oversized identities should remain deterministic and collision-resistant."""
    first = replace(_target(), routing_agent_name="a" * 110 + "one")
    second = replace(_target(), routing_agent_name="a" * 110 + "two")

    first_name = derive_repository_name(prefix="MindRoom", worker_target=first)
    second_name = derive_repository_name(prefix="MindRoom", worker_target=second)

    assert len(first_name) <= 100
    assert first_name != second_name
    assert first_name.startswith("MindRoom-")


@pytest.mark.parametrize(
    "target",
    [
        _target(agent_name="mind", worker_scope="user_agent", requester_id=None, private=True),
        _target(agent_name="mind", worker_scope="user_agent", requester_id="", private=True),
        _target(agent_name="mind", worker_scope="user_agent", requester_id="not-matrix", private=True),
    ],
)
def test_private_repository_name_requires_matrix_username(target: ResolvedWorkerTarget) -> None:
    """Missing or non-Matrix requester identities must fail closed."""
    with pytest.raises(RepositoryBindingError, match="Matrix requester"):
        derive_repository_name(prefix="MindRoom", worker_target=target)


def test_repository_binding_uses_configured_storage_root(tmp_path: Path) -> None:
    """Bindings must live on the configured, backed-up runtime storage root."""
    storage_root = tmp_path / "retained-agent-data"
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=storage_root,
        process_env={},
    )
    target = _target()
    request = _request(target, "MindRoom-redwood")
    store = RepositoryBindingStore(runtime_paths)

    binding = store.bind(request, _lease())

    binding_files = list((storage_root / "repository_bindings").glob("*.json"))
    assert len(binding_files) == 1
    assert binding_files[0].is_file()
    assert store.read(request.worker_key) == binding


def test_repository_binding_is_idempotent_and_write_once(tmp_path: Path) -> None:
    """A worker identity can replay one exact repository ID but cannot rebind."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={},
    )
    target = _target()
    request = _request(target, "MindRoom-redwood")
    store = RepositoryBindingStore(runtime_paths)

    first = store.bind(request, _lease(repository_id="42"))
    replay = store.bind(request, _lease(repository_id="42"))

    assert replay == first
    with pytest.raises(RepositoryBindingError, match="immutable repository binding"):
        store.bind(request, _lease(repository_id="99"))


def test_repository_binding_serializes_concurrent_writers(tmp_path: Path) -> None:
    """Concurrent idempotent calls should publish one complete binding."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={},
    )
    target = _target()
    request = _request(target, "MindRoom-redwood")
    store = RepositoryBindingStore(runtime_paths)

    with ThreadPoolExecutor(max_workers=8) as executor:
        bindings = list(executor.map(lambda _: store.bind(request, _lease()), range(32)))

    assert len(set(bindings)) == 1
    assert store.read(request.worker_key) == bindings[0]
    assert list((runtime_paths.storage_root / "repository_bindings").glob("*.tmp")) == []


@pytest.mark.parametrize(
    "lease",
    [
        replace(_lease(), organization="other"),
        replace(_lease(), repository_name="MindRoom-other"),
        replace(_lease(), clone_url="https://token@github.com/example-org/MindRoom-redwood.git"),
        replace(_lease(), clone_url="https://evil.test/example-org/MindRoom-redwood.git"),
        replace(_lease(), clone_url=" https://github.com/example-org/MindRoom-redwood.git"),
        replace(_lease(), clone_url="https://github.com/example-org/MindRoom-redwood.git\n"),
        replace(_lease(), clone_url="https://github.com/example-org/MindRoom-redwood.git?"),
        replace(_lease(), clone_url="https://github.com/example-org/MindRoom-redwood.git#"),
        replace(_lease(), clone_url="https://GITHUB.com/example-org/MindRoom-redwood.git"),
    ],
)
def test_repository_binding_rejects_untrusted_lease_fields(
    tmp_path: Path,
    lease: RepositoryLease,
) -> None:
    """Broker responses cannot redirect ownership or smuggle credentials into Git state."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={},
    )
    request = _request(_target(), "MindRoom-redwood")

    with pytest.raises(RepositoryBindingError):
        RepositoryBindingStore(runtime_paths).bind(request, lease)
