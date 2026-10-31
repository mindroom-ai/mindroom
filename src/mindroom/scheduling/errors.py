"""The failure a scheduling operation reports, which each surface renders in its own way.

Chat commands prefix the reason with ❌, the scheduler tool fails its call with it, and the schedules API maps it to
an HTTP status, so no caller reads success or failure from the wording.
"""


class SchedulingError(Exception):
    """A scheduling operation that did not happen; its text is the reason shown to whoever asked."""


class ScheduledTaskNotFoundError(SchedulingError):
    """The task an operation names is not stored in the room."""

    def __init__(self, task_id: str) -> None:
        super().__init__(f"Task `{task_id}` not found.")


class ScheduledTaskStorageError(SchedulingError):
    """Reading or writing a task's room state, or withdrawing its call approval, failed; a retry may succeed."""


class ScheduledTaskStateReadError(ScheduledTaskStorageError):
    """A scheduled-task state read failed without proving the task absent."""
