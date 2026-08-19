"""Tests for durable background script run state."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.constants import RuntimePaths
from mindroom.script_runs.models import (
    ScriptCallClaim,
    ScriptCallState,
    ScriptRunRecord,
    ScriptRunState,
    ScriptToolGrant,
)
from mindroom.script_runs.store import (
    ScriptCallConflictError,
    ScriptCallRateLimitError,
    ScriptCapabilityError,
    ScriptRunStore,
    ScriptRunStoreError,
    mint_script_capability,
)

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
    authenticated = store.require_active_capability(run.run_id, token)
    assert authenticated.run_id == run.run_id
    assert len(store.pending_calls(run.run_id)) == 1


def test_snapshot_locator_is_durable_and_rejects_parent_traversal(runtime_paths: RuntimePaths) -> None:
    """Launch snapshot ownership is a storage-relative, containment-checked fact."""
    store = ScriptRunStore(runtime_paths)
    run = store.create_run(_new_run())

    updated = store.record_snapshot_locator(
        run.run_id,
        "workers/worker-1/workspace/.mindroom/script-runs/run-1",
    )

    assert store.get_run(run.run_id).snapshot_locator == updated.snapshot_locator
    with pytest.raises(ScriptRunStoreError, match="snapshot locator"):
        store.record_snapshot_locator(run.run_id, "../outside/run-1")


def test_observed_exit_revokes_and_retains_output_before_terminal_transition(runtime_paths: RuntimePaths) -> None:
    """One atomic durable mutation preserves process truth while cleanup remains pending."""
    store = ScriptRunStore(runtime_paths)
    created = store.create_run(_new_run())
    run = store.transition_run(created.run_id, state=ScriptRunState.RUNNING)

    observed = store.record_process_exit(
        run.run_id,
        exit_code=0,
        error=None,
        output="finished",
        cancellation_reason="Background script process exited.",
    )

    assert observed.state is ScriptRunState.RUNNING
    assert observed.cancel_requested_at is not None
    assert observed.finished_at is not None
    assert observed.output == "finished"
    terminal = store.transition_run(run.run_id, state=ScriptRunState.EXITED)
    assert terminal.output == "finished"


def test_run_store_rejects_oversized_terminal_output(runtime_paths: RuntimePaths) -> None:
    """Observed process output cannot exceed the durable control-state bound."""
    store = ScriptRunStore(runtime_paths)
    run = store.create_run(_new_run())

    with pytest.raises(ScriptRunStoreError, match="output exceeds"):
        store.record_process_exit(
            run.run_id,
            exit_code=0,
            error=None,
            output="x" * (128 * 1024),
            cancellation_reason="Background script process exited.",
        )


def test_run_store_migrates_existing_database_without_output_column(runtime_paths: RuntimePaths) -> None:
    """Opening a pre-output database adds the bounded terminal-output column conservatively."""
    initial = ScriptRunStore(runtime_paths)
    with sqlite3.connect(initial.database_path) as connection:
        connection.execute("ALTER TABLE script_runs DROP COLUMN output")

    migrated = ScriptRunStore(runtime_paths)
    created = migrated.create_run(_new_run())

    assert migrated.get_run(created.run_id).output == ""


def test_call_rate_limit_is_atomic_and_does_not_charge_stable_retries(runtime_paths: RuntimePaths) -> None:
    """Concurrent new claims share one durable quota while an identical retry remains free."""
    store = ScriptRunStore(runtime_paths)
    run = store.create_run(replace(_new_run(), max_tool_calls_per_minute=1))

    def claim(call_id: str) -> ScriptCallClaim | ScriptCallRateLimitError:
        try:
            return store.claim_call(
                run_id=run.run_id,
                call_id=call_id,
                grant=ScriptToolGrant("website", "read_url"),
                arguments_digest=f"digest-{call_id}",
            )
        except ScriptCallRateLimitError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(claim, ("call-a", "call-b")))

    accepted = [outcome for outcome in outcomes if not isinstance(outcome, ScriptCallRateLimitError)]
    rejected = [outcome for outcome in outcomes if isinstance(outcome, ScriptCallRateLimitError)]
    assert len(accepted) == 1
    assert len(rejected) == 1
    accepted_claim = accepted[0]
    assert isinstance(accepted_claim, ScriptCallClaim)
    duplicate = store.claim_call(
        run_id=run.run_id,
        call_id=accepted_claim.call.call_id,
        grant=ScriptToolGrant("website", "read_url"),
        arguments_digest=accepted_claim.call.arguments_digest,
    )
    assert duplicate.created is False
    assert len(store.pending_calls(run.run_id)) == 1


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


def test_run_store_rejects_call_grant_outside_launch_snapshot(runtime_paths: RuntimePaths) -> None:
    """A durable call cannot expand its run's captured grant surface."""
    store = ScriptRunStore(runtime_paths)
    store.create_run(_new_run())

    with pytest.raises(ScriptCapabilityError, match="not granted"):
        store.claim_call(
            run_id="run-1",
            call_id="call-1",
            grant=ScriptToolGrant("shell", "run_shell_command"),
            arguments_digest="digest-a",
        )

    assert store.pending_calls("run-1") == []


def test_run_store_replays_equivalent_serialized_terminal_receipt(runtime_paths: RuntimePaths) -> None:
    """Retries compare the durable JSON receipt rather than Python container types."""
    store = ScriptRunStore(runtime_paths)
    store.create_run(_new_run())
    store.claim_call(
        run_id="run-1",
        call_id="call-1",
        grant=ScriptToolGrant("website", "read_url"),
        arguments_digest="digest-a",
    )

    first = store.publish_call_result(
        run_id="run-1",
        call_id="call-1",
        state=ScriptCallState.COMPLETED,
        result=("page body",),
    )
    duplicate = store.publish_call_result(
        run_id="run-1",
        call_id="call-1",
        state=ScriptCallState.COMPLETED,
        result=["page body"],
    )

    assert first == duplicate
    assert duplicate.result == ["page body"]


def test_run_store_replays_terminal_receipt_with_different_mapping_order(runtime_paths: RuntimePaths) -> None:
    """Equivalent JSON mappings retain one durable terminal receipt."""
    store = ScriptRunStore(runtime_paths)
    store.create_run(_new_run())
    store.claim_call(
        run_id="run-1",
        call_id="call-1",
        grant=ScriptToolGrant("website", "read_url"),
        arguments_digest="digest-a",
    )

    first = store.publish_call_result(
        run_id="run-1",
        call_id="call-1",
        state=ScriptCallState.COMPLETED,
        result={"title": "Status", "body": "ok"},
    )
    duplicate = store.publish_call_result(
        run_id="run-1",
        call_id="call-1",
        state=ScriptCallState.COMPLETED,
        result={"body": "ok", "title": "Status"},
    )

    assert first == duplicate
    assert duplicate.result == {"title": "Status", "body": "ok"}


def test_run_store_normalizes_mixed_json_mapping_keys_before_replay(runtime_paths: RuntimePaths) -> None:
    """JSON-supported non-string keys replay as their canonical wire mapping."""
    store = ScriptRunStore(runtime_paths)
    store.create_run(_new_run())
    store.claim_call(
        run_id="run-1",
        call_id="call-1",
        grant=ScriptToolGrant("website", "read_url"),
        arguments_digest="digest-a",
    )

    first = store.publish_call_result(
        run_id="run-1",
        call_id="call-1",
        state=ScriptCallState.COMPLETED,
        result={2: "two", "one": 1},
    )
    duplicate = store.publish_call_result(
        run_id="run-1",
        call_id="call-1",
        state=ScriptCallState.COMPLETED,
        result={"one": 1, "2": "two"},
    )

    assert first == duplicate
    assert duplicate.result == {"2": "two", "one": 1}


def test_run_store_rejects_terminal_run_mutation(runtime_paths: RuntimePaths) -> None:
    """A terminal lifecycle record cannot change mutable process details."""
    store = ScriptRunStore(runtime_paths)
    store.create_run(_new_run())
    store.transition_run("run-1", state=ScriptRunState.FAILED, error="initial failure")

    with pytest.raises(ScriptRunStoreError, match=r"(?i)terminal"):
        store.transition_run("run-1", state=ScriptRunState.FAILED, error="rewritten failure")


def test_run_store_rejects_oversized_run_error(runtime_paths: RuntimePaths) -> None:
    """Run error text is bounded before it becomes durable control state."""
    store = ScriptRunStore(runtime_paths)
    store.create_run(_new_run())

    with pytest.raises(ScriptRunStoreError, match="error exceeds"):
        store.transition_run("run-1", state=ScriptRunState.FAILED, error="x" * (128 * 1024))


def test_run_store_rejects_terminal_run_transition(runtime_paths: RuntimePaths) -> None:
    """A terminal run cannot be silently resurrected."""
    store = ScriptRunStore(runtime_paths)
    store.create_run(_new_run())

    exited = store.transition_run("run-1", state=ScriptRunState.FAILED)

    assert exited.state is ScriptRunState.FAILED
    with pytest.raises(ValueError, match="cannot transition"):
        store.transition_run("run-1", state=ScriptRunState.RUNNING)


def test_run_store_rejects_running_transition_after_cancellation_intent(runtime_paths: RuntimePaths) -> None:
    """The durable cancellation barrier cannot be overwritten by a racing launcher."""
    store = ScriptRunStore(runtime_paths)
    store.create_run(_new_run())
    store.request_cancel("run-1", reason="stop before publication")

    with pytest.raises(ScriptRunStoreError, match="cancellation"):
        store.transition_run("run-1", state=ScriptRunState.RUNNING)

    run = store.get_run("run-1")
    assert run.state is ScriptRunState.STARTING
    assert run.cancel_requested_at is not None


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


@pytest.mark.parametrize("result", [float("nan"), float("inf"), float("-inf")])
def test_run_store_rejects_nonfinite_terminal_receipt(runtime_paths: RuntimePaths, result: float) -> None:
    """Durable receipts must always remain readable by strict JSON consumers."""
    store = ScriptRunStore(runtime_paths)
    store.create_run(_new_run())
    store.claim_call(
        run_id="run-1",
        call_id="call-1",
        grant=ScriptToolGrant("website", "read_url"),
        arguments_digest="digest-a",
    )

    with pytest.raises(ScriptRunStoreError, match="JSON serializable"):
        store.publish_call_result(
            run_id="run-1",
            call_id="call-1",
            state=ScriptCallState.COMPLETED,
            result=result,
        )
