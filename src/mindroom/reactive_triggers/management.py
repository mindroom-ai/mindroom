"""Shared trigger ownership, re-enable refresh, and cross-store toggle/delete helpers."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from mindroom.external_triggers.store import ExternalTriggerRecord, ExternalTriggerStore, ExternalTriggerStoreError
from mindroom.reactive_triggers.models import (
    ReactiveTriggerRecord,
    RoomCursor,
    RoomMessageSourceSpec,
    ThreadTagSourceSpec,
)
from mindroom.reactive_triggers.store import ReactiveTriggerStore
from mindroom.requester_identity import equivalent_requester_ids
from mindroom.thread_tags import ThreadTagsState, get_thread_tags

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


def is_trigger_owner(actor_user_id: str, owner_user_id: str, config: Config, runtime_paths: RuntimePaths) -> bool:
    """Return whether ``actor_user_id`` owns a trigger recorded under ``owner_user_id``."""
    return owner_user_id in equivalent_requester_ids(actor_user_id, config, runtime_paths)


async def read_thread_tag_present(
    client: nio.AsyncClient,
    source: ThreadTagSourceSpec,
    *,
    fetch_tags: Callable[[nio.AsyncClient, str, str], Awaitable[ThreadTagsState | None]] = get_thread_tags,
) -> bool:
    """Return whether ``source.tag`` is currently present on ``source.thread_id`` in ``source.room_id``."""
    tag_state = await fetch_tags(client, source.room_id, source.thread_id)
    return tag_state is not None and source.tag.lower() in {tag.lower() for tag in tag_state.tags}


def refresh_reactive_state_on_reenable(
    reactive_store: ReactiveTriggerStore,
    record: ReactiveTriggerRecord,
    *,
    now: float,
    refreshed_tag_present: bool | None,
) -> None:
    """Refresh room cursor or thread tag checkpoint before re-enabling a disabled reactive trigger."""
    if isinstance(record.source, RoomMessageSourceSpec):
        reactive_store.advance_room_cursor(
            record.trigger_id,
            RoomCursor(created_ts=int(now * 1000), logical_event_id=""),
            expected_rule_uid=record.rule_uid,
        )
    elif isinstance(record.source, ThreadTagSourceSpec) and refreshed_tag_present is not None:
        reactive_store.update_thread_checkpoint_and_enqueue(
            record.trigger_id,
            tag_present=refreshed_tag_present,
            state_event_id=None,
            expected_rule_uid=record.rule_uid,
        )


def set_trigger_enabled_sync(
    runtime_paths: RuntimePaths,
    config: Config,
    trigger_id: str,
    *,
    enabled: bool,
    actor_user_id: str,
    refreshed_tag_present: bool | None = None,
    authorize_reactive: Callable[[ReactiveTriggerRecord], None] | None = None,
) -> ExternalTriggerRecord | ReactiveTriggerRecord:
    """Enable or disable a webhook or reactive trigger across both stores."""
    external_store = ExternalTriggerStore(runtime_paths)
    if external_store.get_record(trigger_id) is not None:
        return external_store.set_enabled(trigger_id, enabled=enabled, actor_user_id=actor_user_id, config=config)

    reactive_store = ReactiveTriggerStore(runtime_paths)
    record = reactive_store.get_trigger(trigger_id)
    if record is None:
        msg = f"external trigger not found: {trigger_id}"
        raise ExternalTriggerStoreError(msg)
    if authorize_reactive is not None:
        authorize_reactive(record)
    if enabled and not record.enabled:
        refresh_reactive_state_on_reenable(
            reactive_store,
            record,
            now=time.time(),
            refreshed_tag_present=refreshed_tag_present,
        )
    updated = reactive_store.set_enabled(trigger_id, enabled, expected_rule_uid=record.rule_uid)
    if updated is None:
        msg = f"external trigger not found: {trigger_id}"
        raise ExternalTriggerStoreError(msg)
    return updated


def delete_trigger_sync(
    runtime_paths: RuntimePaths,
    config: Config,
    trigger_id: str,
    *,
    actor_user_id: str,
    authorize_reactive: Callable[[ReactiveTriggerRecord], None] | None = None,
) -> None:
    """Delete a webhook or reactive trigger across both stores."""
    external_store = ExternalTriggerStore(runtime_paths)
    if external_store.get_record(trigger_id) is not None:
        external_store.delete_record(trigger_id, actor_user_id=actor_user_id, config=config)
        return

    reactive_store = ReactiveTriggerStore(runtime_paths)
    record = reactive_store.get_trigger(trigger_id)
    if record is None:
        msg = f"external trigger not found: {trigger_id}"
        raise ExternalTriggerStoreError(msg)
    if authorize_reactive is not None:
        authorize_reactive(record)
    if not reactive_store.delete_trigger(trigger_id, expected_rule_uid=record.rule_uid):
        msg = f"external trigger not found: {trigger_id}"
        raise ExternalTriggerStoreError(msg)
