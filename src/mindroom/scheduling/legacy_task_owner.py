"""Owner of a scheduled task saved before tasks stored their owner."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.scheduling.ownership import task_owner

if TYPE_CHECKING:
    import nio

    from mindroom.scheduling.tasks import ScheduledWorkflow


# LEGACY_COMPAT: Scheduled tasks without a stored owner.
# Legacy format: Scheduled-task state content without an "owner" field; every task saved before owners were stored.
# Last legacy release: v2026.10.668 wrote scheduled-task state without an owner; replacement: the next release
#   stores the owner's Matrix ID in the task's state content when the task is saved.
# Handling: The owner is decided by the current rule over the room's full state, with the task's latest state writer
#   standing in for its creator. The field is never written back, so a rewrite keeps the task on this rule.
# Coverage: tests/test_scheduled_task_runtime.py::test_task_saved_without_an_owner_restores_under_the_rule_over_its_writer.
def legacy_task_owner(room: nio.MatrixRoom, workflow: ScheduledWorkflow, *, sender: str, router_id: str) -> str:
    """Return the owner of a task whose state names none, from the room's full state and the state's sender."""
    return task_owner(room, workflow, creator_id=sender, router_id=router_id)
