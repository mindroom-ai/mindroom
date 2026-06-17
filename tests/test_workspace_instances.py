"""Tests for durable workspace instance records."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    resolve_worker_key,
    serialize_tool_execution_identity,
)
from mindroom.workspace_instances import (
    WorkspaceInstanceRecord,
    load_workspace_instance_records,
    record_workspace_instance,
    workspace_instance_registry_path,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Create isolated runtime paths for workspace instance records."""
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "CUSTOMER_ID": "tenant-123",
            "ACCOUNT_ID": "account-456",
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


def _config(runtime_paths: RuntimePaths, *, private_per: str = "user") -> Config:
    return Config.validate_with_runtime(
        {
            "memory": {"backend": "none"},
            "agents": {
                "mind": {
                    "display_name": "Mind",
                    "private": {"per": private_per, "root": "mind_data"},
                },
            },
        },
        runtime_paths,
    )


def _shared_workspace_config(runtime_paths: RuntimePaths) -> Config:
    return Config.validate_with_runtime(
        {
            "memory": {"backend": "none"},
            "agents": {
                "mind": {
                    "display_name": "Mind",
                    "memory_backend": "file",
                    "worker_scope": "user",
                },
            },
        },
        runtime_paths,
    )


def _identity(requester_id: str, *, session_id: str = "session-1") -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id=requester_id,
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=session_id,
        tenant_id="tenant-123",
        account_id="account-456",
        transport_agent_name="mindroom_mind",
    )


def _expected_record(
    *,
    runtime_paths: RuntimePaths,
    identity: ToolExecutionIdentity,
    session_id: str = "session-1",
) -> WorkspaceInstanceRecord:
    config = _config(runtime_paths)
    runtime = resolve_agent_runtime(
        "mind",
        config,
        runtime_paths,
        execution_identity=identity,
        create=True,
    )
    assert runtime.workspace is not None
    assert runtime.worker_key is not None
    assert runtime.execution_identity is not None
    assert runtime.execution_identity.session_id == session_id
    return WorkspaceInstanceRecord(
        agent_name="mind",
        workspace_root=runtime.workspace.root,
        state_root=runtime.state_root,
        execution_scope="user",
        execution_identity=runtime.execution_identity,
        worker_key=runtime.worker_key,
        is_private=True,
    )


def test_resolving_private_runtime_writes_one_durable_workspace_instance_record(
    runtime_paths: RuntimePaths,
) -> None:
    """Private runtime materialization should persist one concrete workspace instance."""
    identity = _identity("@alice:example.org")
    expected = _expected_record(runtime_paths=runtime_paths, identity=identity)

    registry_path = workspace_instance_registry_path(runtime_paths)
    records = load_workspace_instance_records(runtime_paths)

    assert registry_path == runtime_paths.storage_root / "workspace_instances" / "instances.json"
    assert registry_path.is_file()
    assert records == [expected]


def test_resolving_shared_requester_scoped_workspace_does_not_persist_execution_identity(
    runtime_paths: RuntimePaths,
) -> None:
    """Only private workspace instances should enter the automation registry."""
    config = _shared_workspace_config(runtime_paths)
    identity = _identity("@alice:example.org")

    runtime = resolve_agent_runtime(
        "mind",
        config,
        runtime_paths,
        execution_identity=identity,
        create=True,
    )

    assert runtime.workspace is not None
    assert runtime.is_private is False
    assert runtime.execution_identity == identity
    assert load_workspace_instance_records(runtime_paths) == []
    assert not workspace_instance_registry_path(runtime_paths).exists()


def test_workspace_instance_record_round_trips_serialized_execution_identity_and_paths(
    runtime_paths: RuntimePaths,
) -> None:
    """Registry records should preserve the complete execution and workspace identity."""
    identity = _identity("@alice:example.org")
    expected = _expected_record(runtime_paths=runtime_paths, identity=identity)

    raw_payload = json.loads(workspace_instance_registry_path(runtime_paths).read_text(encoding="utf-8"))
    instance_payloads = raw_payload["instances"]
    assert isinstance(instance_payloads, dict)
    assert len(instance_payloads) == 1
    stored_payload = next(iter(instance_payloads.values()))

    assert stored_payload["agent_name"] == "mind"
    assert stored_payload["workspace_root"] == str(expected.workspace_root)
    assert stored_payload["state_root"] == str(expected.state_root)
    assert stored_payload["execution_scope"] == "user"
    assert stored_payload["execution_identity"] == serialize_tool_execution_identity(identity)
    assert stored_payload["worker_key"] == expected.worker_key
    assert stored_payload["is_private"] is True
    assert load_workspace_instance_records(runtime_paths) == [expected]


def test_workspace_instance_writes_replace_same_logical_instance_without_duplicates(
    runtime_paths: RuntimePaths,
) -> None:
    """Repeated materialization for the same agent/workspace/worker key should replace the record."""
    config = _config(runtime_paths)
    first_identity = _identity("@alice:example.org", session_id="session-1")
    second_identity = _identity("@alice:example.org", session_id="session-2")
    first_worker_key = resolve_worker_key("user", first_identity, agent_name="mind")
    second_worker_key = resolve_worker_key("user", second_identity, agent_name="mind")
    assert first_worker_key == second_worker_key

    resolve_agent_runtime("mind", config, runtime_paths, execution_identity=first_identity, create=True)
    resolve_agent_runtime("mind", config, runtime_paths, execution_identity=second_identity, create=True)

    records = load_workspace_instance_records(runtime_paths)

    assert len(records) == 1
    assert records[0].execution_identity is not None
    assert records[0].execution_identity.session_id == "session-2"


def test_malformed_workspace_instance_entries_are_skipped(runtime_paths: RuntimePaths) -> None:
    """Discovery should tolerate malformed persisted records."""
    identity = _identity("@alice:example.org")
    expected = _expected_record(runtime_paths=runtime_paths, identity=identity)
    registry_path = workspace_instance_registry_path(runtime_paths)
    raw_payload = json.loads(registry_path.read_text(encoding="utf-8"))
    valid_payload = next(iter(raw_payload["instances"].values()))
    raw_payload["instances"] = {
        "valid": valid_payload,
        "not-an-object": "broken",
        "bad-execution-identity": {
            **valid_payload,
            "execution_identity": {"channel": "bad"},
        },
        "bad-workspace-root": {
            **valid_payload,
            "workspace_root": 123,
        },
    }
    registry_path.write_text(json.dumps(raw_payload), encoding="utf-8")

    assert load_workspace_instance_records(runtime_paths) == [expected]


def test_invalid_utf8_registry_is_treated_as_malformed_and_recovered(
    runtime_paths: RuntimePaths,
) -> None:
    """Invalid registry bytes should not break runtime materialization."""
    config = _config(runtime_paths)
    identity = _identity("@alice:example.org")
    registry_path = workspace_instance_registry_path(runtime_paths)
    registry_path.parent.mkdir(parents=True)
    registry_path.write_bytes(b"\xff\xfeinvalid-json")

    assert load_workspace_instance_records(runtime_paths) == []

    runtime = resolve_agent_runtime(
        "mind",
        config,
        runtime_paths,
        execution_identity=identity,
        create=True,
    )

    assert runtime.workspace is not None
    assert runtime.worker_key is not None
    records = load_workspace_instance_records(runtime_paths)
    assert len(records) == 1
    assert records[0].worker_key == runtime.worker_key
    assert records[0].workspace_root == runtime.workspace.root


def test_concurrent_private_runtime_materializations_do_not_lose_workspace_instance_records(
    runtime_paths: RuntimePaths,
) -> None:
    """The registry read-modify-write sequence should be protected against local thread races."""
    config = _config(runtime_paths)
    identities = [_identity(f"@user-{index}:example.org") for index in range(12)]

    def materialize(identity: ToolExecutionIdentity) -> str:
        runtime = resolve_agent_runtime(
            "mind",
            config,
            runtime_paths,
            execution_identity=identity,
            create=True,
        )
        assert runtime.worker_key is not None
        assert runtime.workspace is not None
        return runtime.worker_key

    with ThreadPoolExecutor(max_workers=6) as executor:
        expected_worker_keys = set(executor.map(materialize, identities))

    records = load_workspace_instance_records(runtime_paths)

    assert {record.worker_key for record in records} == expected_worker_keys
    assert len(records) == len(expected_worker_keys)
    assert all(record.is_private for record in records)
    assert all(record.workspace_root.is_dir() for record in records)


def test_record_workspace_instance_allows_direct_round_trip(runtime_paths: RuntimePaths) -> None:
    """The registry writer should support callers that already have a typed record."""
    identity = _identity("@alice:example.org")
    worker_key = resolve_worker_key("user", identity, agent_name="mind")
    assert worker_key is not None
    workspace_root = runtime_paths.storage_root / "custom" / "mind_data"
    state_root = runtime_paths.storage_root / "custom"
    workspace_root.mkdir(parents=True)
    record = WorkspaceInstanceRecord(
        agent_name="mind",
        workspace_root=workspace_root,
        state_root=state_root,
        execution_scope="user",
        execution_identity=identity,
        worker_key=worker_key,
        is_private=True,
    )

    record_workspace_instance(runtime_paths, record)

    assert load_workspace_instance_records(runtime_paths) == [record]
