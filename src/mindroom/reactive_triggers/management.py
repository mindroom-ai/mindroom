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
from mindroom.thread_tags import get_thread_tags

if TYPE_CHECKING:
    from collections.abc import Callable

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


type TriggerRecord = ExternalTriggerRecord | ReactiveTriggerRecord


def is_trigger_owner(actor_user_id: str, owner_user_id: str, config: Config, runtime_paths: RuntimePaths) -> bool:
    """Return whether ``actor_user_id`` owns a trigger recorded under ``owner_user_id``."""
    return owner_user_id in equivalent_requester_ids(actor_user_id, config, runtime_paths)


def get_trigger_record(runtime_paths: RuntimePaths, trigger_id: str) -> TriggerRecord | None:
    """Return one webhook or reactive trigger by ID, checking the external store first."""
    external = ExternalTriggerStore(runtime_paths).get_record(trigger_id)
    if external is not None:
        return external
    return ReactiveTriggerStore(runtime_paths).get_trigger(trigger_id)


async def read_thread_tag_present(client: nio.AsyncClient, source: ThreadTagSourceSpec) -> bool:
    """Return whether ``source.tag`` is currently present on ``source.thread_id`` in ``source.room_id``."""
    tag_state = await get_thread_tags(client, source.room_id, source.thread_id)
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


def _not_found(trigger_id: str) -> ExternalTriggerStoreError:
    return ExternalTriggerStoreError(f"external trigger not found: {trigger_id}")


def _load_trigger_for_mutation(
    runtime_paths: RuntimePaths,
    trigger_id: str,
    authorize_reactive: Callable[[ReactiveTriggerRecord], None],
) -> ExternalTriggerRecord | tuple[ReactiveTriggerStore, ReactiveTriggerRecord]:
    record = get_trigger_record(runtime_paths, trigger_id)
    if record is None:
        raise _not_found(trigger_id)
    if isinstance(record, ExternalTriggerRecord):
        return record
    authorize_reactive(record)
    return ReactiveTriggerStore(runtime_paths), record


def set_trigger_enabled_sync(
    runtime_paths: RuntimePaths,
    config: Config,
    trigger_id: str,
    *,
    enabled: bool,
    actor_user_id: str,
    authorize_reactive: Callable[[ReactiveTriggerRecord], None],
    refreshed_tag_present: bool | None = None,
) -> TriggerRecord:
    """Enable or disable a webhook or reactive trigger across both stores."""
    loaded = _load_trigger_for_mutation(runtime_paths, trigger_id, authorize_reactive)
    if isinstance(loaded, ExternalTriggerRecord):
        return ExternalTriggerStore(runtime_paths).set_enabled(
            trigger_id,
            enabled=enabled,
            actor_user_id=actor_user_id,
            config=config,
        )

    reactive_store, record = loaded
    if enabled and not record.enabled:
        refresh_reactive_state_on_reenable(
            reactive_store,
            record,
            now=time.time(),
            refreshed_tag_present=refreshed_tag_present,
        )
    updated = reactive_store.set_enabled(trigger_id, enabled, expected_rule_uid=record.rule_uid)
    if updated is None:
        raise _not_found(trigger_id)
    return updated


def delete_trigger_sync(
    runtime_paths: RuntimePaths,
    config: Config,
    trigger_id: str,
    *,
    actor_user_id: str,
    authorize_reactive: Callable[[ReactiveTriggerRecord], None],
) -> None:
    """Delete a webhook or reactive trigger across both stores."""
    loaded = _load_trigger_for_mutation(runtime_paths, trigger_id, authorize_reactive)
    if isinstance(loaded, ExternalTriggerRecord):
        ExternalTriggerStore(runtime_paths).delete_record(trigger_id, actor_user_id=actor_user_id, config=config)
        return

    reactive_store, record = loaded
    if not reactive_store.delete_trigger(trigger_id, expected_rule_uid=record.rule_uid):
        raise _not_found(trigger_id)
