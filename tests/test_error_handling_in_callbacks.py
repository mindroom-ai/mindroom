"""Test that errors in event callbacks are properly handled and logged."""

from __future__ import annotations

import asyncio

import pytest
from structlog.testing import capture_logs

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot import _create_best_effort_task_wrapper


@pytest.mark.asyncio
async def test_best_effort_callback_error_is_logged_not_raised() -> None:
    """An explicitly best-effort consumer must not crash the sync loop."""
    owner = object()

    async def failing_callback() -> None:
        message = "consumer failed"
        raise ValueError(message)

    wrapped = _create_best_effort_task_wrapper(failing_callback, owner=owner)

    with capture_logs() as logs:
        await wrapped()
        await wait_for_background_tasks(timeout=1.0, owner=owner)

    assert [entry for entry in logs if entry["event"] == "Error in event callback"]


@pytest.mark.asyncio
async def test_cancelled_error_is_handled_silently() -> None:
    """Test that CancelledError is handled silently (expected during shutdown)."""

    # Create a callback that raises CancelledError
    async def callback_that_gets_cancelled(*args: object, **kwargs: object) -> None:  # noqa: ARG001
        raise asyncio.CancelledError

    owner = object()
    wrapped = _create_best_effort_task_wrapper(callback_that_gets_cancelled, owner=owner)

    with capture_logs() as logs:
        await wrapped()
        await wait_for_background_tasks(timeout=1.0, owner=owner)

    assert not [entry for entry in logs if entry["event"] == "Error in event callback"]
