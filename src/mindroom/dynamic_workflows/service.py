"""Dynamic Workflow service layer for run orchestration."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from functools import partial
from typing import TYPE_CHECKING

from mindroom.background_tasks import run_blocking_until_complete, wait_for_future_until_complete
from mindroom.dynamic_workflows.runner import execute_workflow_spec
from mindroom.dynamic_workflows.store import DynamicWorkflowRun
from mindroom.dynamic_workflows.validation import (
    validate_workflow_input,
    validate_workflow_spec,
    workflow_runtime_seconds,
)
from mindroom.failure_text import failure_text

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.dynamic_workflows.runner import ParticipantExecutor
    from mindroom.dynamic_workflows.store import DynamicWorkflowStore

_RUN_CANCELLED = "Workflow run was cancelled."


class DynamicWorkflowService:
    """Coordinate validation, execution, and persisted run state for Dynamic Workflows."""

    def __init__(
        self,
        store: DynamicWorkflowStore,
        *,
        participant_executor: ParticipantExecutor | None = None,
        spec_validator: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self._store = store
        self._participant_executor = participant_executor
        self._spec_validator = spec_validator

    async def run_workflow(
        self,
        *,
        workflow_id: str,
        scope: str,
        owner_id: str,
        input_data: dict[str, object],
        requested_by: str,
        base_url: str | None = None,
    ) -> DynamicWorkflowRun:
        """Start and complete one workflow run on the current event loop.

        Store file work runs on threads, and each call finishes before the run continues or handles a
        cancellation, so the loop stays free and the run's records are written in order.
        A run cancelled before its final record is written ends cancelled; one cancelled while that record
        is written keeps it.
        """
        preparing = asyncio.create_task(
            asyncio.to_thread(
                self._prepare_run,
                workflow_id=workflow_id,
                scope=scope,
                owner_id=owner_id,
                input_data=input_data,
                requested_by=requested_by,
                base_url=base_url,
            ),
        )
        try:
            prepared = await wait_for_future_until_complete(preparing)
        except asyncio.CancelledError:
            # Preparation has finished by now, so a run it started and admitted ends cancelled.
            if preparing.exception() is None and not isinstance(started := preparing.result(), DynamicWorkflowRun):
                await self._fail(started[0], _RUN_CANCELLED)
            raise
        if isinstance(prepared, DynamicWorkflowRun):
            return prepared

        run, spec = prepared
        return await self._execute_and_persist(run, spec, input_data)

    def _prepare_run(
        self,
        *,
        workflow_id: str,
        scope: str,
        owner_id: str,
        input_data: dict[str, object],
        requested_by: str,
        base_url: str | None,
    ) -> tuple[DynamicWorkflowRun, dict[str, object]] | DynamicWorkflowRun:
        """Admit a run and validate its pinned spec, or persist a preparation failure."""
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
            if self._spec_validator is not None:
                self._spec_validator(spec)
            validate_workflow_input(spec, input_data)
        except Exception as exc:  # Persist preparation failures only after the run exists.
            return self._store.fail_workflow_run(run, error=failure_text(exc))
        return run, spec

    async def _execute_and_persist(
        self,
        run: DynamicWorkflowRun,
        spec: dict[str, object],
        input_data: dict[str, object],
    ) -> DynamicWorkflowRun:
        timeout_seconds = workflow_runtime_seconds(spec)
        execution_task = asyncio.create_task(
            execute_workflow_spec(
                spec,
                input_data,
                participant_executor=self._participant_executor,
            ),
        )
        try:
            done, _pending = await asyncio.wait({execution_task}, timeout=timeout_seconds)
            execution = execution_task.result() if done else None
        except asyncio.CancelledError:
            execution_task.cancel()
            execution_task.add_done_callback(_discard_late_task_result)
            await self._fail(run, _RUN_CANCELLED)
            raise
        except Exception as exc:
            return await self._fail(run, failure_text(exc))
        if not done:
            execution_task.cancel()
            execution_task.add_done_callback(_discard_late_task_result)
            return await self._fail(run, _workflow_timeout_message(timeout_seconds))
        return await run_blocking_until_complete(self._complete_or_fail, run, execution)

    async def _fail(self, run: DynamicWorkflowRun, error: str) -> DynamicWorkflowRun:
        return await run_blocking_until_complete(partial(self._store.fail_workflow_run, run, error=error))

    def _complete_or_fail(self, run: DynamicWorkflowRun, execution: object) -> DynamicWorkflowRun:
        try:
            return self._store.complete_workflow_run(run, execution)
        except Exception as exc:
            return self._store.fail_workflow_run(run, error=failure_text(exc))


def _workflow_timeout_message(timeout_seconds: float) -> str:
    return f"Workflow run exceeded permissions.max_runtime_seconds ({timeout_seconds})."


def _discard_late_task_result(task: asyncio.Task[object]) -> None:
    with suppress(asyncio.CancelledError, Exception):
        task.result()
