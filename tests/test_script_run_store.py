"""Tests for durable background script run state."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.constants import RuntimePaths
from mindroom.script_runs.models import ScriptCallState, ScriptRunRecord, ScriptRunState, ScriptToolGrant
from mindroom.script_runs.store import ScriptCallConflictError, ScriptRunStore, mint_script_capability

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Provide primary-runtime paths with writable control state."""
    return RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "storage",
        control_state_root=tmp_path / "control_state",
    )


def _new_run(*, token_hash: str | None = None) -> ScriptRunRecord:
    return ScriptRunRecord(
        run_id="run-1",
        agent_name="watcher",
        owner_user_id="@alice:example.test",
        room_id="!room:example.test",
        source_digest="source-digest",
        grants=(ScriptToolGrant("website", "read_url"),),
        token_hash=token_hash or "capability-digest",
    )


def test_run_store_claims_one_logical_call_once(runtime_paths: RuntimePaths) -> None:
    """A retry with the same logical call returns its original claim."""
    store = ScriptRunStore(runtime_paths)
    token, token_hash = mint_script_capability()
    run = store.create_run(_new_run(token_hash=token_hash))

    first = store.claim_call(
        run_id=run.run_id,
        call_id="call-1",
        grant=ScriptToolGrant("website", "read_url"),
        arguments_digest="digest-a",
    )
    duplicate = store.claim_call(
        run_id=run.run_id,
        call_id="call-1",
        grant=ScriptToolGrant("website", "read_url"),
        arguments_digest="digest-a",
    )

    assert first.created is True
    assert duplicate.created is False
    assert duplicate.call.call_id == first.call.call_id
    assert store.capability_matches(run.run_id, token) is True


def test_run_store_rejects_call_id_reuse_with_different_arguments(runtime_paths: RuntimePaths) -> None:
    """A call ID cannot change its immutable arguments after acceptance."""
    store = ScriptRunStore(runtime_paths)
    run = store.create_run(_new_run())
    store.claim_call(
        run_id=run.run_id,
        call_id="call-1",
        grant=ScriptToolGrant("website", "read_url"),
        arguments_digest="digest-a",
    )

    with pytest.raises(ScriptCallConflictError):
        store.claim_call(
            run_id=run.run_id,
            call_id="call-1",
            grant=ScriptToolGrant("website", "read_url"),
            arguments_digest="digest-b",
        )


def test_run_store_rejects_terminal_run_transition(runtime_paths: RuntimePaths) -> None:
    """A terminal run cannot be silently resurrected."""
    store = ScriptRunStore(runtime_paths)
    store.create_run(_new_run())

    exited = store.transition_run("run-1", state=ScriptRunState.FAILED)

    assert exited.state is ScriptRunState.FAILED
    with pytest.raises(ValueError, match="cannot transition"):
        store.transition_run("run-1", state=ScriptRunState.RUNNING)


def test_run_store_publishes_one_bounded_terminal_receipt(runtime_paths: RuntimePaths) -> None:
    """A claimed call retains its one terminal result for duplicate polling."""
    store = ScriptRunStore(runtime_paths)
    store.create_run(_new_run())
    store.claim_call(
        run_id="run-1",
        call_id="call-1",
        grant=ScriptToolGrant("website", "read_url"),
        arguments_digest="digest-a",
    )

    published = store.publish_call_result(
        run_id="run-1",
        call_id="call-1",
        state=ScriptCallState.COMPLETED,
        result={"body": "ok"},
    )

    assert published.state is ScriptCallState.COMPLETED
    assert published.result == {"body": "ok"}
    assert published.error is None
