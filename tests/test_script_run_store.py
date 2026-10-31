"""Tests for durable background script run state."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.constants import RuntimePaths
from mindroom.script_runs import store as store_module
from mindroom.script_runs.models import (
    ScriptCallClaim,
    ScriptCallState,
    ScriptRunRecord,
    ScriptRunState,
    ScriptToolGrant,
    script_run_id_from_worker_key,
    script_worker_key_belongs_to_run,
    validated_script_run_id,
)
from mindroom.script_runs.store import (
    ScriptCallConflictError,
    ScriptCallRateLimitError,
    ScriptCapabilityError,
    ScriptRunStore,
    ScriptRunStoreError,
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


_GRANT = ScriptToolGrant("website", "read_url")


def _claim(
    store: ScriptRunStore,
    *,
    call_id: str = "call-1",
    arguments_digest: str = "digest-a",
) -> ScriptCallClaim:
    return store.claim_call(run_id="run-1", call_id=call_id, grant=_GRANT, arguments_digest=arguments_digest)


def _new_run() -> ScriptRunRecord:
    return ScriptRunRecord(
        run_id="run-1",
        agent_name="watcher",
        owner_user_id="@alice:example.test",
        room_id="!room:example.test",
        source_digest="source-digest",
        grants=(_GRANT,),
        token_hash="capability-digest",  # noqa: S106
    )


def test_run_store_preserves_recovery_contract_across_reopen(runtime_paths: RuntimePaths) -> None:
    """Restart can recover the launch contract without granting recovery to older rows."""
    store = ScriptRunStore(runtime_paths)
    recoverable = replace(_new_run(), recovery_signature="launch-contract-digest")
    legacy = replace(_new_run(), run_id="legacy-run")
    store.create_run(recoverable)
    store.create_run(legacy)

    reopened = ScriptRunStore(runtime_paths)

    assert reopened.get_run(recoverable.run_id).recovery_signature == "launch-contract-digest"
    assert reopened.get_run(legacy.run_id).recovery_signature is None


def test_run_store_claims_one_logical_call_once(runtime_paths: RuntimePaths) -> None:
    """A retry with the same logical call returns its original claim."""
    store = ScriptRunStore(runtime_paths)
    run = store.create_run(_new_run())

    first = _claim(store)
    duplicate = _claim(store)

    assert first.created is True
    assert duplicate.created is False
    assert duplicate.call.call_id == first.call.call_id
    assert len(store.pending_calls(run.run_id)) == 1


def test_run_store_migrates_existing_table_for_resource_snapshots(runtime_paths: RuntimePaths) -> None:
    """An existing script database gains profile columns before the first profiled launch."""
    database_path = runtime_paths.control_state_root / "script_runs" / "script_runs.sqlite3"
    database_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            """
            CREATE TABLE script_runs (
                run_id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                owner_user_id TEXT NOT NULL,
                room_id TEXT NOT NULL,
                thread_root_event_id TEXT,
                execution_identity_json TEXT NOT NULL,
                source_digest TEXT NOT NULL,
                grants_json TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                preapprove_launch_grants INTEGER NOT NULL,
                worker_key TEXT,
                worker_id TEXT,
                worker_backend_locator TEXT,
                snapshot_locator TEXT,
                name TEXT,
                local_unsafe INTEGER NOT NULL,
                max_tool_calls_per_minute INTEGER NOT NULL,
                max_runtime_seconds INTEGER NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                exit_code INTEGER,
                error TEXT,
                output TEXT NOT NULL,
                cancel_requested_at TEXT,
                cancellation_reason TEXT
            )
            """,
        )
        connection.execute(
            """
            INSERT INTO script_runs (
                run_id, agent_name, owner_user_id, room_id, thread_root_event_id,
                execution_identity_json, source_digest, grants_json, token_hash,
                preapprove_launch_grants, worker_key, worker_id, worker_backend_locator,
                snapshot_locator, name, local_unsafe,
                max_tool_calls_per_minute, max_runtime_seconds,
                state, created_at, started_at, finished_at, exit_code, error, output,
                cancel_requested_at, cancellation_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-run",
                "watcher",
                "@alice:example.test",
                "!room:example.test",
                "$thread-root",
                '{"requester_id":"@alice:example.test"}',
                "source-digest",
                '[["website","read_url"]]',
                "capability-digest",
                1,
                "worker-key",
                "worker-id",
                "sandbox://worker",
                "snapshot://legacy",
                "Nightly check",
                1,
                17,
                3_600,
                "exited",
                "2026-08-20T00:00:00Z",
                "2026-08-20T00:00:01Z",
                "2026-08-20T00:00:10Z",
                0,
                None,
                "legacy output",
                None,
                None,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    store = ScriptRunStore(runtime_paths)
    legacy = store.get_run("legacy-run")
    expected_legacy = ScriptRunRecord(
        run_id="legacy-run",
        agent_name="watcher",
        owner_user_id="@alice:example.test",
        room_id="!room:example.test",
        thread_root_event_id="$thread-root",
        execution_identity={"requester_id": "@alice:example.test"},
        source_digest="source-digest",
        grants=(_GRANT,),
        token_hash="capability-digest",  # noqa: S106
        preapprove_launch_grants=True,
        worker_key="worker-key",
        worker_id="worker-id",
        worker_backend_locator="sandbox://worker",
        snapshot_locator="snapshot://legacy",
        name="Nightly check",
        local_unsafe=True,
        max_tool_calls_per_minute=17,
        max_runtime_seconds=3_600,
        state=ScriptRunState.EXITED,
        created_at="2026-08-20T00:00:00Z",
        started_at="2026-08-20T00:00:01Z",
        finished_at="2026-08-20T00:00:10Z",
        exit_code=0,
        output="legacy output",
    )
    profiled = replace(
        _new_run(),
        recovery_signature="new-worker-contract",
        resource_profile="standard",
        resource_requests={"cpu": "250m", "memory": "512Mi"},
        resource_limits={"cpu": "1", "memory": "2Gi"},
    )

    store.create_run(profiled)

    assert legacy == expected_legacy
    assert store.get_run(profiled.run_id) == profiled
    reopened = ScriptRunStore(runtime_paths)
    assert reopened.get_run("legacy-run") == expected_legacy
    assert reopened.get_run(profiled.run_id) == profiled


def test_run_store_migrates_existing_table_without_recovery_signature(runtime_paths: RuntimePaths) -> None:
    """A database from before durable recovery gains the column, and its running rows stay unrecoverable."""
    database_path = runtime_paths.control_state_root / "script_runs" / "script_runs.sqlite3"
    database_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            """
            CREATE TABLE script_runs (
                run_id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                owner_user_id TEXT NOT NULL,
                room_id TEXT NOT NULL,
                thread_root_event_id TEXT,
                execution_identity_json TEXT NOT NULL,
                source_digest TEXT NOT NULL,
                grants_json TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                preapprove_launch_grants INTEGER NOT NULL,
                worker_key TEXT,
                worker_id TEXT,
                worker_backend_locator TEXT,
                snapshot_locator TEXT,
                name TEXT,
                local_unsafe INTEGER NOT NULL,
                resource_profile TEXT,
                resource_requests_json TEXT NOT NULL DEFAULT '{}',
                resource_limits_json TEXT NOT NULL DEFAULT '{}',
                max_tool_calls_per_minute INTEGER NOT NULL,
                max_runtime_seconds INTEGER NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                exit_code INTEGER,
                error TEXT,
                output TEXT NOT NULL,
                cancel_requested_at TEXT,
                cancellation_reason TEXT
            )
            """,
        )
        connection.execute(
            """
            INSERT INTO script_runs (
                run_id, agent_name, owner_user_id, room_id, execution_identity_json, source_digest, grants_json,
                token_hash, preapprove_launch_grants, worker_key, worker_backend_locator, local_unsafe,
                resource_profile, resource_requests_json, resource_limits_json,
                max_tool_calls_per_minute, max_runtime_seconds, state, created_at, output
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "running-before-recovery",
                "watcher",
                "@alice:example.test",
                "!room:example.test",
                "{}",
                "source-digest",
                '[["website","read_url"]]',
                "capability-digest",
                0,
                "worker-key",
                "kubernetes:test",
                0,
                "standard",
                '{"cpu":"250m","memory":"512Mi"}',
                '{"cpu":"1","memory":"2Gi"}',
                30,
                3_600,
                "running",
                "2026-09-13T00:00:00Z",
                "",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    store = ScriptRunStore(runtime_paths)
    legacy = store.get_run("running-before-recovery")
    recoverable = store.create_run(replace(_new_run(), recovery_signature="launch-contract-digest"))

    assert legacy.state is ScriptRunState.RUNNING
    assert legacy.recovery_signature is None
    assert legacy.resource_requests == {"cpu": "250m", "memory": "512Mi"}
    assert ScriptRunStore(runtime_paths).get_run(recoverable.run_id).recovery_signature == "launch-contract-digest"


def test_write_transaction_closes_connection_when_begin_fails(
    runtime_paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLite lock failure during BEGIN cannot leak its connection."""
    store = ScriptRunStore(runtime_paths)

    class BeginFailure:
        closed = False

        def execute(self, statement: str) -> None:
            assert statement == "BEGIN IMMEDIATE"
            msg = "database is locked"
            raise sqlite3.OperationalError(msg)

        def close(self) -> None:
            self.closed = True

    connection = BeginFailure()
    monkeypatch.setattr(store_module, "_connect", lambda _path: connection)

    with pytest.raises(sqlite3.OperationalError, match="locked"), store._write_transaction():
        pass

    assert connection.closed is True


def test_write_transaction_rolls_back_when_its_body_fails(runtime_paths: RuntimePaths) -> None:
    """A failed write leaves no partial change behind."""
    store = ScriptRunStore(runtime_paths)
    run = store.create_run(_new_run())

    def delete_then_fail() -> None:
        with store._write_transaction() as connection:
            connection.execute("DELETE FROM script_runs WHERE run_id = ?", (run.run_id,))
            msg = "body failed"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="body failed"):
        delete_then_fail()

    assert store.get_run(run.run_id) == run


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


def test_terminal_transition_atomically_clears_cleanup_ownership(runtime_paths: RuntimePaths) -> None:
    """A crash cannot expose cleared ownership before the terminal state is durable."""
    store = ScriptRunStore(runtime_paths)
    created = store.create_run(
        replace(
            _new_run(),
            worker_key="v1:t:user_agent:alice:run-1:watcher",
            worker_id="worker-1",
            worker_backend_locator="locator-a",
            snapshot_locator="workers/worker-1/workspace/.mindroom/script-runs/run-1",
        ),
    )
    store.record_process_exit(
        created.run_id,
        exit_code=0,
        error=None,
        output="finished",
        cancellation_reason="Background script process exited.",
    )

    terminal = store.finalize_cleaned_run(created.run_id, state=ScriptRunState.EXITED)

    assert terminal.state is ScriptRunState.EXITED
    assert terminal.worker_key is None
    assert terminal.worker_id is None
    assert terminal.worker_backend_locator is None
    assert terminal.snapshot_locator is None


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


def test_call_rate_limit_is_atomic_and_does_not_charge_stable_retries(runtime_paths: RuntimePaths) -> None:
    """Concurrent new claims share one durable quota while an identical retry remains free."""
    store = ScriptRunStore(runtime_paths)
    run = store.create_run(replace(_new_run(), max_tool_calls_per_minute=1))

    def claim(call_id: str) -> ScriptCallClaim | ScriptCallRateLimitError:
        try:
            return _claim(store, call_id=call_id, arguments_digest=f"digest-{call_id}")
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
    duplicate = _claim(
        store,
        call_id=accepted_claim.call.call_id,
        arguments_digest=accepted_claim.call.arguments_digest,
    )
    assert duplicate.created is False
    assert len(store.pending_calls(run.run_id)) == 1


def test_run_store_rejects_call_id_reuse_with_different_arguments(runtime_paths: RuntimePaths) -> None:
    """A call ID cannot change its immutable arguments after acceptance."""
    store = ScriptRunStore(runtime_paths)
    store.create_run(_new_run())
    _claim(store)

    with pytest.raises(ScriptCallConflictError):
        _claim(store, arguments_digest="digest-b")


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


@pytest.mark.parametrize(
    ("first_result", "duplicate_result", "stored_result"),
    [
        pytest.param(("page body",), ["page body"], ["page body"], id="container-type"),
        pytest.param(
            {"title": "Status", "body": "ok"},
            {"body": "ok", "title": "Status"},
            {"title": "Status", "body": "ok"},
            id="mapping-order",
        ),
        pytest.param({2: "two", "one": 1}, {"one": 1, "2": "two"}, {"2": "two", "one": 1}, id="mixed-mapping-keys"),
    ],
)
def test_run_store_replays_an_equivalent_terminal_receipt(
    runtime_paths: RuntimePaths,
    first_result: object,
    duplicate_result: object,
    stored_result: object,
) -> None:
    """Retries compare the canonical durable JSON receipt, not Python container types, mapping order, or key types."""
    store = ScriptRunStore(runtime_paths)
    store.create_run(_new_run())
    _claim(store)

    first = store.publish_call_result(
        run_id="run-1",
        call_id="call-1",
        state=ScriptCallState.COMPLETED,
        result=first_result,
    )
    duplicate = store.publish_call_result(
        run_id="run-1",
        call_id="call-1",
        state=ScriptCallState.COMPLETED,
        result=duplicate_result,
    )

    assert first == duplicate
    assert duplicate.result == stored_result


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
    _claim(store)

    published = store.publish_call_result(
        run_id="run-1",
        call_id="call-1",
        state=ScriptCallState.COMPLETED,
        result={"body": "ok"},
    )

    assert published.state is ScriptCallState.COMPLETED
    assert published.result == {"body": "ok"}
    assert published.error is None


@pytest.mark.parametrize("result", [float("nan"), float("inf"), float("-inf"), "\udcff"])
def test_run_store_rejects_a_terminal_receipt_strict_json_cannot_encode(
    runtime_paths: RuntimePaths,
    result: object,
) -> None:
    """Durable receipts stay readable by strict JSON consumers, so non-finite numbers and invalid Unicode get the store's typed error."""
    store = ScriptRunStore(runtime_paths)
    store.create_run(_new_run())
    _claim(store)

    with pytest.raises(ScriptRunStoreError, match="JSON serializable"):
        store.publish_call_result(
            run_id="run-1",
            call_id="call-1",
            state=ScriptCallState.COMPLETED,
            result=result,
        )


@pytest.mark.parametrize(
    "locator",
    [
        "workers/worker-1/workspace",
        "workers/worker-1/workspace/.mindroom/script-runs/run-2",
        "workers/worker-1/workspace/other/script-runs/run-1",
    ],
)
def test_snapshot_locator_must_name_the_runs_own_snapshot_directory(runtime_paths: RuntimePaths, locator: str) -> None:
    """Cleanup removes the recorded directory, so only this run's snapshot directory may be recorded."""
    store = ScriptRunStore(runtime_paths)
    run = store.create_run(_new_run())

    with pytest.raises(ScriptRunStoreError, match="snapshot locator"):
        store.record_snapshot_locator(run.run_id, locator)


def test_running_run_cannot_return_to_starting(runtime_paths: RuntimePaths) -> None:
    """A run that reported running never re-enters the pre-spawn state."""
    store = ScriptRunStore(runtime_paths)
    run = store.create_run(_new_run())
    store.transition_run(run.run_id, state=ScriptRunState.RUNNING)

    with pytest.raises(ScriptRunStoreError, match="cannot transition"):
        store.transition_run(run.run_id, state=ScriptRunState.STARTING)


def test_cancel_request_leaves_a_terminal_run_unchanged(runtime_paths: RuntimePaths) -> None:
    """Revoking a finished run is a no-op on its durable record."""
    store = ScriptRunStore(runtime_paths)
    run = store.create_run(_new_run())
    exited = store.transition_run(run.run_id, state=ScriptRunState.EXITED, exit_code=0)

    assert store.request_cancel(run.run_id, reason="owner") == exited
    assert store.get_run(run.run_id).cancel_requested_at is None


@pytest.mark.parametrize("run_id", [f"script-{'a' * 32}/x", f"script-{'A' * 32}", f"script-{'a' * 31}"])
def test_script_run_id_must_be_exactly_the_generated_form(run_id: str) -> None:
    """A run ID names a handle and a directory, so nothing may follow the generated hex digits."""
    with pytest.raises(ValueError, match="Script run ID"):
        validated_script_run_id(run_id)


_PINNED_RUN_ID = f"script-{'a' * 32}"


@pytest.mark.parametrize(
    ("worker_key", "run_id"),
    [
        pytest.param(f"v1:default:user_agent:alice:{_PINNED_RUN_ID}:watcher", _PINNED_RUN_ID, id="pinned"),
        pytest.param(f"v1:default:shared:alice:{_PINNED_RUN_ID}:watcher", None, id="not-user-agent"),
        pytest.param("v1:default:user_agent:alice:script-not-hex:watcher", None, id="not-a-run-id"),
        pytest.param(f"v1:default:user_agent:{_PINNED_RUN_ID}:watcher", None, id="too-short"),
    ],
)
def test_script_worker_key_names_only_its_pinned_run(worker_key: str, run_id: str | None) -> None:
    """Only a user-agent key pinned to a generated run ID names that run, and it belongs to no other run."""
    assert script_run_id_from_worker_key(worker_key) == run_id
    assert script_worker_key_belongs_to_run(worker_key, _PINNED_RUN_ID) is (run_id == _PINNED_RUN_ID)
    assert script_worker_key_belongs_to_run(worker_key, f"script-{'b' * 32}") is False
