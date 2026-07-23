"""Dynamic Workflow service layer for run orchestration."""

from __future__ import annotations

import asyncio
import signal
import threading
import time
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING

from mindroom.dynamic_workflows.runner import async_execute_workflow_spec, execute_workflow_spec
from mindroom.dynamic_workflows.validation import (
    DynamicWorkflowError,
    validate_workflow_input,
    validate_workflow_spec,
    workflow_runtime_seconds,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from mindroom.dynamic_workflows.runner import AsyncParticipantExecutor, ParticipantExecutor
    from mindroom.dynamic_workflows.store import DynamicWorkflowRun, DynamicWorkflowStore


class _SyncWorkflowTimeoutError(TimeoutError):
    """Raised when a synchronous Dynamic Workflow run exceeds its runtime cap."""


class DynamicWorkflowService:
    """Coordinate validation, execution, and persisted run state for Dynamic Workflows."""

    def __init__(
        self,
        store: DynamicWorkflowStore,
        *,
        participant_executor: ParticipantExecutor | None = None,
        async_participant_executor: AsyncParticipantExecutor | None = None,
        spec_validator: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self._store = store
        self._participant_executor = participant_executor
        self._async_participant_executor = async_participant_executor
        self._spec_validator = spec_validator

    def run_workflow(
        self,
        *,
        workflow_id: str,
        scope: str,
        owner_id: str,
        input_data: dict[str, object],
        requested_by: str,
        base_url: str | None = None,
    ) -> DynamicWorkflowRun:
        """Start and complete one workflow run on the synchronous main-thread call path.

        Synchronous execution uses SIGALRM to enforce permissions.max_runtime_seconds, so callers running in worker
        threads should use arun_workflow instead.
        """
        run = self._store.start_workflow_run(
            workflow_id=workflow_id,
            scope=scope,
            owner_id=owner_id,
            input_data=input_data,
            requested_by=requested_by,
            base_url=base_url,
        )
        try:
            spec = self._store.load_workflow_revision(
                workflow_id=workflow_id,
                scope=scope,
                owner_id=owner_id,
                revision=run.revision,
            )
            spec = validate_workflow_spec(spec)
            self._validate_spec_policy(spec)
            validate_workflow_input(spec, input_data)
        except Exception as exc:  # Persist validation failures as run records.
            return self._store.fail_workflow_run(run, error=str(exc))

        return self._execute_and_persist(run, spec, input_data)

    async def arun_workflow(
        self,
        *,
        workflow_id: str,
        scope: str,
        owner_id: str,
        input_data: dict[str, object],
        requested_by: str,
        base_url: str | None = None,
    ) -> DynamicWorkflowRun:
        """Start and complete one workflow run on the current event loop."""
        run = self._store.start_workflow_run(
            workflow_id=workflow_id,
            scope=scope,
            owner_id=owner_id,
            input_data=input_data,
            requested_by=requested_by,
            base_url=base_url,
        )
        try:
            spec = self._store.load_workflow_revision(
                workflow_id=workflow_id,
                scope=scope,
                owner_id=owner_id,
                revision=run.revision,
            )
            spec = validate_workflow_spec(spec)
            self._validate_spec_policy(spec)
            validate_workflow_input(spec, input_data)
        except Exception as exc:
            return self._store.fail_workflow_run(run, error=str(exc))

        return await self._aexecute_and_persist(run, spec, input_data)

    def _validate_spec_policy(self, spec: dict[str, object]) -> None:
        if self._spec_validator is not None:
            self._spec_validator(spec)

    def _execute_and_persist(
        self,
        run: DynamicWorkflowRun,
        spec: dict[str, object],
        input_data: dict[str, object],
    ) -> DynamicWorkflowRun:
        try:
            with _sync_workflow_runtime_limit(spec):
                execution = execute_workflow_spec(
                    spec,
                    input_data,
                    participant_executor=self._participant_executor,
                )
        except Exception as exc:  # Persist runtime failures from participant code.
            return self._store.fail_workflow_run(run, error=str(exc))
        return self._complete_or_fail(run, execution)

    async def _aexecute_and_persist(
        self,
        run: DynamicWorkflowRun,
        spec: dict[str, object],
        input_data: dict[str, object],
    ) -> DynamicWorkflowRun:
        timeout_seconds = workflow_runtime_seconds(spec)
        execution_task = asyncio.create_task(
            async_execute_workflow_spec(
                spec,
                input_data,
                participant_executor=self._async_participant_executor,
            ),
        )
        try:
            done, _pending = await asyncio.wait({execution_task}, timeout=timeout_seconds)
            if not done:
                execution_task.cancel()
                execution_task.add_done_callback(_discard_late_task_result)
                return self._store.fail_workflow_run(run, error=_workflow_timeout_message(timeout_seconds))
            execution = execution_task.result()
        except asyncio.CancelledError:
            execution_task.cancel()
            execution_task.add_done_callback(_discard_late_task_result)
            self._store.fail_workflow_run(run, error="Workflow run was cancelled.")
            raise
        except Exception as exc:
            return self._store.fail_workflow_run(run, error=str(exc))
        return self._complete_or_fail(run, execution)

    def _complete_or_fail(self, run: DynamicWorkflowRun, execution: object) -> DynamicWorkflowRun:
        try:
            return self._store.complete_workflow_run(run, execution)
        except Exception as exc:
            return self._store.fail_workflow_run(run, error=str(exc))


@contextmanager
def _sync_workflow_runtime_limit(spec: dict[str, object]) -> Iterator[None]:
    """Enforce permissions.max_runtime_seconds for synchronous main-thread workflow execution."""
    timeout_seconds = workflow_runtime_seconds(spec)
    if threading.current_thread() is not threading.main_thread():
        msg = (
            "Synchronous Dynamic Workflow runs require the async execution path "
            "to enforce permissions.max_runtime_seconds outside the main thread."
        )
        raise DynamicWorkflowError(msg)

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started_at = time.monotonic()

    def raise_timeout(_signum: int, _frame: object) -> None:
        raise _SyncWorkflowTimeoutError(_workflow_timeout_message(timeout_seconds))

    signal.signal(signal.SIGALRM, raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, float(timeout_seconds))
    try:
        yield
    except _SyncWorkflowTimeoutError as exc:
        raise DynamicWorkflowError(str(exc)) from exc
    finally:
        elapsed = time.monotonic() - started_at
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, max(previous_timer[0] - elapsed, 1e-6), previous_timer[1])


def _workflow_timeout_message(timeout_seconds: float) -> str:
    return f"Workflow run exceeded permissions.max_runtime_seconds ({timeout_seconds})."


def _discard_late_task_result(task: asyncio.Task[object]) -> None:
    with suppress(asyncio.CancelledError, Exception):
        task.result()
