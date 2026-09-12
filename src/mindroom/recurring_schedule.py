"""Durable checkpoints for recurring task timers and their pending Matrix trigger."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from croniter import croniter
from pydantic import TypeAdapter, ValidationError

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.durable_write import create_directory_durable, write_json_file_durable
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)
_CHECKPOINT_RETRY_SECONDS = 30


class _CheckpointValidationError(ValueError):
    """A persisted cursor cannot safely be used until its storage is repaired."""


class RecurringDeliveryHeldError(RuntimeError):
    """A frozen trigger needs reconciliation before this device can resume it."""


class RecurringCheckpointUnavailableError(RuntimeError):
    """The scheduler must refresh time and task state before retrying preparation."""


async def _checkpoint_operation[Result](operation: Callable[[], Result]) -> Result:
    """Expose storage failures separately from invalid schedule definitions."""
    try:
        return await run_blocking_until_complete(operation)
    except (OSError, ValidationError, _CheckpointValidationError) as error:
        logger.exception("recurring_checkpoint_unavailable")
        msg = "Recurring checkpoint is unavailable"
        raise RecurringCheckpointUnavailableError(msg) from error


async def _retry_checkpoint_operation[Result](operation: Callable[[], Result]) -> Result:
    """Retry acknowledgement writes in place so delivered triggers are never resent."""
    while True:
        try:
            return await _checkpoint_operation(operation)
        except RecurringCheckpointUnavailableError:
            await asyncio.sleep(_CHECKPOINT_RETRY_SECONDS)


@dataclass(frozen=True)
class _PreparedRecurringDelivery:
    """Freeze content and the device whose Matrix transaction namespace owns it."""

    content: dict[str, Any]
    device_id: str


@dataclass(frozen=True)
class _RecurringCheckpoint:
    """One timer cursor, with at most one unacknowledged trigger."""

    workflow_key: str
    next_run_at: datetime
    prepared: _PreparedRecurringDelivery | None = None
    last_skipped_at: datetime | None = None
    skip_reason: str | None = None
    schema_version: Literal[1] = 1


_CHECKPOINT_ADAPTER = TypeAdapter(_RecurringCheckpoint)


@dataclass(frozen=True)
class RecurringOccurrence:
    """One persisted occurrence carried from its timer into Matrix delivery."""

    path: Path
    checkpoint: _RecurringCheckpoint

    @property
    def transaction_id(self) -> str:
        """Identify the schedule and intended time independently of retry time."""
        identity = f"{self.path.stem}:{self.checkpoint.next_run_at.isoformat()}"
        return f"schedule_{hashlib.sha256(identity.encode()).hexdigest()}"


def _save(path: Path, checkpoint: _RecurringCheckpoint) -> None:
    create_directory_durable(path.parent, mode=0o700)
    write_json_file_durable(
        path,
        _CHECKPOINT_ADAPTER.dump_python(checkpoint, mode="json"),
        strict_atomic_replace=True,
    )


def _plan(
    path: Path,
    workflow_key: str,
    cron: str,
    now: datetime,
    grace_seconds: int,
) -> RecurringOccurrence:
    checkpoint = _CHECKPOINT_ADAPTER.validate_json(path.read_bytes()) if path.exists() else None
    if checkpoint is not None and checkpoint.next_run_at.tzinfo is None:
        msg = "Recurring checkpoint requires an aware next_run_at"
        raise _CheckpointValidationError(msg)
    if checkpoint is None or checkpoint.workflow_key != workflow_key:
        # First adoption and edits establish a future cursor. We cannot prove
        # whether the old runtime fired an earlier occurrence.
        checkpoint = _RecurringCheckpoint(workflow_key, croniter(cron, now).get_next(datetime))
        _save(path, checkpoint)
    elif checkpoint.prepared is None and checkpoint.next_run_at <= now:
        # Coalesce downtime to its latest occurrence, including an exact cron boundary.
        latest = croniter(cron, now + timedelta(microseconds=1)).get_prev(datetime)
        if (now - latest).total_seconds() > grace_seconds:
            checkpoint = replace(
                checkpoint,
                next_run_at=croniter(cron, now).get_next(datetime),
                last_skipped_at=latest,
                skip_reason="outside catch-up grace window",
            )
            _save(path, checkpoint)
            logger.warning(
                "recurring_schedule_skipped",
                scheduled_at=latest.isoformat(),
                reason=checkpoint.skip_reason,
            )
        elif latest > checkpoint.next_run_at:
            skipped_at = croniter(cron, latest).get_prev(datetime)
            checkpoint = replace(
                checkpoint,
                next_run_at=latest,
                last_skipped_at=skipped_at,
                skip_reason="coalesced missed occurrences into latest run",
            )
            _save(path, checkpoint)
            logger.info(
                "recurring_schedule_coalesced",
                scheduled_at=latest.isoformat(),
                skipped_through=skipped_at.isoformat(),
            )
    return RecurringOccurrence(path, checkpoint)


async def plan_recurring_occurrence(
    runtime_paths: RuntimePaths,
    *,
    homeserver: str,
    sender: str,
    room_id: str,
    task_id: str,
    workflow_json: str,
    cron: str,
    now: datetime,
    grace_seconds: int,
) -> RecurringOccurrence:
    """Load or advance a timer without losing a previously attempted trigger."""
    identity = json.dumps([homeserver, sender, room_id, task_id])
    name = hashlib.sha256(identity.encode()).hexdigest()
    path = runtime_paths.storage_root / "tracking" / "recurring_schedules" / f"{name}.json"
    workflow_key = hashlib.sha256(workflow_json.encode()).hexdigest()
    return await _checkpoint_operation(partial(_plan, path, workflow_key, cron, now, grace_seconds))


async def prepare_recurring_delivery(
    occurrence: RecurringOccurrence,
    content: dict[str, Any],
    device_id: str | None,
) -> RecurringOccurrence:
    """Commit the exact trigger and return the saved state before any network attempt."""
    if not device_id:
        msg = "Recurring delivery requires an authenticated Matrix device"
        raise RuntimeError(msg)
    checkpoint = replace(occurrence.checkpoint, prepared=_PreparedRecurringDelivery(content, device_id))
    await _checkpoint_operation(partial(_save, occurrence.path, checkpoint))
    return replace(occurrence, checkpoint=checkpoint)


def recurring_delivery_content(occurrence: RecurringOccurrence, device_id: str | None) -> dict[str, Any] | None:
    """Resume frozen content only while Matrix can deduplicate its transaction."""
    prepared = occurrence.checkpoint.prepared
    if prepared is None:
        return None
    if prepared.device_id != device_id:
        msg = "Pending recurring trigger belongs to another Matrix device; delivery needs reconciliation"
        raise RecurringDeliveryHeldError(msg)
    return prepared.content


async def complete_recurring_occurrence(occurrence: RecurringOccurrence, cron: str, now: datetime) -> None:
    """Advance after delivery, hook suppression, or a terminal preparation failure."""
    completed_at = max(now, occurrence.checkpoint.next_run_at)
    latest = croniter(cron, completed_at + timedelta(microseconds=1)).get_prev(datetime)
    checkpoint = replace(
        occurrence.checkpoint,
        next_run_at=croniter(cron, completed_at).get_next(datetime),
        prepared=None,
    )
    if latest > occurrence.checkpoint.next_run_at:
        checkpoint = replace(
            checkpoint,
            last_skipped_at=latest,
            skip_reason="coalesced while previous trigger was pending",
        )
    await _retry_checkpoint_operation(partial(_save, occurrence.path, checkpoint))
    if latest > occurrence.checkpoint.next_run_at:
        assert checkpoint.last_skipped_at is not None
        logger.info(
            "recurring_schedule_coalesced",
            scheduled_at=occurrence.checkpoint.next_run_at.isoformat(),
            skipped_through=checkpoint.last_skipped_at.isoformat(),
            reason=checkpoint.skip_reason,
        )
