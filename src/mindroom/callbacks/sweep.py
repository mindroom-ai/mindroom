"""Expiry sweep for one-shot callbacks, piggybacked on API maintenance ticks."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast

from fastapi import HTTPException

from mindroom.api import config_lifecycle
from mindroom.authorization import is_authorized_sender, is_sender_allowed_for_agent_reply
from mindroom.callbacks.executor import execute_callback_expiry_notice
from mindroom.callbacks.store import CallbackRecordNotDeliverableError, CallbackStore, CallbackStoreError
from mindroom.external_triggers.executor import is_user_joined_room
from mindroom.external_triggers.models import TriggerDeliveryReadiness
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    import nio
    from fastapi import FastAPI

    from mindroom.callbacks.store import CallbackDeliverySnapshot, CallbackRecord
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol

logger = get_logger(__name__)

# Give a notify callback with unused fires one day of delivery retries past expiry
# before deleting it without a notice.
_EXPIRY_NOTICE_GIVE_UP_SECONDS = 86400
_CALLBACK_SWEEP_INTERVAL_SECONDS = 60.0


async def run_callback_sweep_loop(
    stop_event: asyncio.Event,
    api_app: FastAPI,
    *,
    interval_seconds: float = _CALLBACK_SWEEP_INTERVAL_SECONDS,
) -> None:
    """Sweep callbacks at a fixed cadence independent of worker cleanup."""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            break
        except TimeoutError:
            pass
        try:
            await _sweep_expired_callbacks(api_app)
        except Exception:
            logger.exception("Callback expiry sweep failed")


async def _sweep_expired_callbacks(api_app: FastAPI) -> None:
    """Notify and remove expired callback records using the app's committed runtime."""
    try:
        config, runtime_paths = config_lifecycle.read_app_committed_runtime_config(api_app)
    except (HTTPException, TypeError):
        return
    if not config.callback_policy.enabled or runtime_paths.control_state_root is None:
        return
    store = CallbackStore(runtime_paths)
    now = int(time.time())
    try:
        expired_records = await asyncio.to_thread(store.list_expired, now=now)
    except CallbackStoreError:
        logger.warning("Callback expiry sweep could not read the callback store", exc_info=True)
        return
    if not expired_records:
        return

    runtime = config_lifecycle.app_state(api_app).external_trigger_runtime
    for record in expired_records:
        if _needs_expiry_notice(record, now=now):
            delivered = await _try_deliver_expiry_notice(
                record,
                runtime=runtime,
                config=config,
                runtime_paths=runtime_paths,
                store=store,
            )
            if not delivered:
                # Leave the record for the next sweep tick.
                continue
        await asyncio.to_thread(_delete_record_and_script, store, record)


def _needs_expiry_notice(record: CallbackRecord, *, now: int) -> bool:
    """Return whether one expired record still owes its owner a timeout notice."""
    if record.on_expiry != "notify" or record.uses_left <= 0:
        return False
    return now - record.expires_at <= _EXPIRY_NOTICE_GIVE_UP_SECONDS


async def _try_deliver_expiry_notice(
    record: CallbackRecord,
    *,
    runtime: config_lifecycle.ExternalTriggerRuntime | None,
    config: Config,
    runtime_paths: RuntimePaths,
    store: CallbackStore,
) -> bool:
    """Deliver one expiry notice; True also covers permanently undeliverable records."""
    snapshot, permanently_undeliverable = await _expiry_notice_snapshot(
        record,
        runtime=runtime,
        config=config,
        runtime_paths=runtime_paths,
        store=store,
    )
    if snapshot is None:
        return permanently_undeliverable
    if runtime is None:
        return False
    return await _deliver_expiry_notice(
        record,
        snapshot=snapshot,
        runtime=runtime,
        config=config,
        runtime_paths=runtime_paths,
    )


async def _expiry_notice_snapshot(
    record: CallbackRecord,
    *,
    runtime: config_lifecycle.ExternalTriggerRuntime | None,
    config: Config,
    runtime_paths: RuntimePaths,
    store: CallbackStore,
) -> tuple[CallbackDeliverySnapshot | None, bool]:
    """Return one deliverable snapshot, or None with whether the record is dead.

    A record whose owner or target is no longer valid or authorized under
    current config can never receive its notice, so the sweep may delete it
    silently; store read failures are transient and retried next tick.
    """
    try:
        snapshot = await asyncio.to_thread(
            store.delivery_snapshot,
            record.callback_id,
            config=config,
            config_generation=runtime.config_generation if runtime is not None else 0,
        )
    except CallbackRecordNotDeliverableError:
        return None, True
    except CallbackStoreError:
        return None, False
    if snapshot is None:
        return None, True
    if not is_authorized_sender(snapshot.owner_user_id, config, snapshot.resolved_room_id, runtime_paths):
        return None, True
    if not is_sender_allowed_for_agent_reply(snapshot.owner_user_id, snapshot.target_agent, config, runtime_paths):
        return None, True
    return snapshot, False


async def _deliver_expiry_notice(
    record: CallbackRecord,
    *,
    snapshot: CallbackDeliverySnapshot,
    runtime: config_lifecycle.ExternalTriggerRuntime,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Post one expiry notice through the same readiness gates as callback fires."""
    readiness = TriggerDeliveryReadiness(
        enabled=True,
        target_agent=snapshot.target_agent,
        resolved_room_id=snapshot.resolved_room_id,
    )
    client = cast("nio.AsyncClient", runtime.client)
    try:
        if not await runtime.is_delivery_target_ready(readiness):
            return False
        if not await is_user_joined_room(client, snapshot.resolved_room_id, snapshot.owner_user_id):
            return False
        matrix_event_id = await execute_callback_expiry_notice(
            client=client,
            snapshot=snapshot,
            created_at=record.created_at,
            config=config,
            runtime_paths=runtime_paths,
            conversation_cache=cast("ConversationCacheProtocol", runtime.conversation_cache),
        )
    except Exception:
        logger.warning(
            "Callback expiry notice delivery failed",
            callback_id=record.callback_id,
            exc_info=True,
        )
        return False
    return matrix_event_id is not None


def _delete_record_and_script(store: CallbackStore, record: CallbackRecord) -> None:
    """Remove one expired record and best-effort delete its generated script."""
    store.delete_record_unchecked(record.callback_id)
    if record.script_path is not None:
        with suppress(OSError):
            Path(record.script_path).unlink(missing_ok=True)
