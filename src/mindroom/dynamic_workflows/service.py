"""Dynamic Workflow service layer for run orchestration."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mindroom.dynamic_workflows.runner import async_execute_workflow_spec, execute_workflow_spec
from mindroom.dynamic_workflows.store import (
    DynamicWorkflowStore,
    validate_workflow_input,
    validate_workflow_spec,
    workflow_runtime_seconds,
)

if TYPE_CHECKING:
    from mindroom.dynamic_workflows.runner import AsyncParticipantExecutor, ParticipantExecutor
    from mindroom.dynamic_workflows.store import DynamicWorkflowRun


class DynamicWorkflowService:
    """Coordinate validation, execution, and persisted run state for Dynamic Workflows."""

    def __init__(
        self,
        store: DynamicWorkflowStore,
        *,
        participant_executor: ParticipantExecutor | None = None,
        async_participant_executor: AsyncParticipantExecutor | None = None,
    ) -> None:
        self._store = store
        self._participant_executor = participant_executor
        self._async_participant_executor = async_participant_executor

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
        """Start and complete one workflow run on the current managed call path."""
        run = self._store.start_workflow_run(
            workflow_id=workflow_id,
            scope=scope,
            owner_id=owner_id,
            input_data=input_data,
            requested_by=requested_by,
            base_url=base_url,
        )
        spec = self._store.load_workflow_revision(
            workflow_id=workflow_id,
            scope=scope,
            owner_id=owner_id,
            revision=run.revision,
        )
        try:
            spec = validate_workflow_spec(spec)
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
        spec = self._store.load_workflow_revision(
            workflow_id=workflow_id,
            scope=scope,
            owner_id=owner_id,
            revision=run.revision,
        )
        try:
            spec = validate_workflow_spec(spec)
            validate_workflow_input(spec, input_data)
        except Exception as exc:
            return self._store.fail_workflow_run(run, error=str(exc))

        return await self._aexecute_and_persist(run, spec, input_data)

    def _execute_and_persist(
        self,
        run: DynamicWorkflowRun,
        spec: dict[str, object],
        input_data: dict[str, object],
    ) -> DynamicWorkflowRun:
        try:
            execution = execute_workflow_spec(
                spec,
                input_data,
                participant_executor=self._participant_executor,
            )
        except Exception as exc:  # Persist runtime failures from participant code.
            return self._store.fail_workflow_run(run, error=str(exc))
        return self._store.complete_workflow_run(run, execution)

    async def _aexecute_and_persist(
        self,
        run: DynamicWorkflowRun,
        spec: dict[str, object],
        input_data: dict[str, object],
    ) -> DynamicWorkflowRun:
        try:
            execution = await asyncio.wait_for(
                async_execute_workflow_spec(
                    spec,
                    input_data,
                    participant_executor=self._async_participant_executor,
                ),
                timeout=workflow_runtime_seconds(spec),
            )
        except Exception as exc:
            return self._store.fail_workflow_run(run, error=str(exc))
        return self._store.complete_workflow_run(run, execution)
