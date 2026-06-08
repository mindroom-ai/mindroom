"""Dynamic Workflow service layer for run orchestration."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from mindroom.dynamic_workflows.runner import execute_workflow_spec
from mindroom.dynamic_workflows.store import (
    DynamicWorkflowStore,
    validate_workflow_input,
    validate_workflow_spec,
)

if TYPE_CHECKING:
    from mindroom.dynamic_workflows.runner import ParticipantExecutor
    from mindroom.dynamic_workflows.store import DynamicWorkflowRun


class DynamicWorkflowService:
    """Coordinate validation, execution, and persisted run state for Dynamic Workflows."""

    def __init__(
        self,
        store: DynamicWorkflowStore,
        *,
        participant_executor: ParticipantExecutor | None = None,
    ) -> None:
        self._store = store
        self._participant_executor = participant_executor

    def run_workflow(
        self,
        *,
        workflow_id: str,
        scope: str,
        owner_id: str,
        input_data: dict[str, object],
        requested_by: str,
        base_url: str | None = None,
        background: bool = True,
    ) -> DynamicWorkflowRun:
        """Start one workflow run and optionally complete it in the background."""
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

        if background:
            threading.Thread(
                target=self._execute_and_persist,
                args=(run, spec, input_data),
                name=f"mindroom-dynamic-workflow-{run.run_id}",
                daemon=True,
            ).start()
            return run
        return self._execute_and_persist(run, spec, input_data)

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
