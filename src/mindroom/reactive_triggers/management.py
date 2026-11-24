"""Shared trigger ownership, re-enable refresh, enable/disable, and delete helpers across both trigger stores."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from mindroom.external_triggers.policy import is_external_trigger_administrator
from mindroom.external_triggers.store import (
    ExternalTriggerForbiddenError,
    ExternalTriggerNotFoundError,
    ExternalTriggerRecord,
    ExternalTriggerStore,
)
from mindroom.reactive_triggers.models import (
    ReactiveTriggerRecord,
    RoomCursor,
    RoomMessageSourceSpec,
    ThreadTagSourceSpec,
)
from mindroom.requester_identity import equivalent_requester_ids
from mindroom.thread_tags import ThreadTagsState, get_thread_tags

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.reactive_triggers.store import ReactiveTriggerStore


def is_trigger_owner(actor_user_id: str, owner_user_id: str, config: Config, runtime_paths: RuntimePaths) -> bool:
    """Return whether ``actor_user_id`` owns a trigger recorded under ``owner_user_id``."""
    return owner_user_id in equivalent_requester_ids(actor_user_id, config, runtime_paths)


def authorize_trigger_actor(
    actor_user_id: str,
    owner_user_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    allow_admin: bool,
    verb: str = "changed",
) -> None:
    """Require the actor to own the trigger, or administer external triggers when ``allow_admin`` is True."""
    if is_trigger_owner(actor_user_id, owner_user_id, config, runtime_paths):
        return
    if allow_admin and is_external_trigger_administrator(config, runtime_paths, actor_user_id):
        return
    suffix = " or an external trigger admin" if allow_admin else ""
    msg = f"trigger can only be {verb} by its owner{suffix}"
    raise ExternalTriggerForbiddenError(msg)


async def read_thread_tag_present(
    client: nio.AsyncClient,
    source: ThreadTagSourceSpec,
    *,
    fetch_tags: Callable[[nio.AsyncClient, str, str], Awaitable[ThreadTagsState | None]] = get_thread_tags,
) -> bool:
    """Return whether ``source.tag`` is currently present on ``source.thread_id`` in ``source.room_id``."""
    tag_state = await fetch_tags(client, source.room_id, source.thread_id)
    current_tags = tag_state.tags if tag_state is not None else {}
    return source.tag.lower() in {existing.lower() for existing in current_tags}


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


def _require_reactive_record(
    reactive_store: ReactiveTriggerStore,
    trigger_id: str,
    *,
    actor_user_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    allow_admin: bool,
    expected_rule_uid: str | None,
) -> ReactiveTriggerRecord:
    record = reactive_store.get_trigger(trigger_id)
    if record is None or (expected_rule_uid is not None and record.rule_uid != expected_rule_uid):
        msg = f"external trigger not found: {trigger_id}"
        raise ExternalTriggerNotFoundError(msg)
    authorize_trigger_actor(actor_user_id, record.owner_user_id, config, runtime_paths, allow_admin=allow_admin)
    return record


def set_trigger_enabled_sync(
    external_store: ExternalTriggerStore,
    reactive_store: ReactiveTriggerStore,
    trigger_id: str,
    *,
    enabled: bool,
    actor_user_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    allow_admin: bool,
    refreshed_tag_present: bool | None = None,
    expected_external_uid: str | None = None,
    expected_reactive_uid: str | None = None,
    now: float | None = None,
) -> ExternalTriggerRecord | ReactiveTriggerRecord:
    """Enable or disable a webhook or reactive trigger across both stores."""
    if expected_reactive_uid is None and (
        expected_external_uid is not None or external_store.get_record(trigger_id) is not None
    ):
        return external_store.set_enabled(
            trigger_id,
            enabled=enabled,
            actor_user_id=actor_user_id,
            config=config,
            allow_admin=allow_admin,
            expected_uid=expected_external_uid,
        )

    record = _require_reactive_record(
        reactive_store,
        trigger_id,
        actor_user_id=actor_user_id,
        config=config,
        runtime_paths=runtime_paths,
        allow_admin=allow_admin,
        expected_rule_uid=expected_reactive_uid,
    )
    if enabled and not record.enabled:
        refresh_reactive_state_on_reenable(
            reactive_store,
            record,
            now=time.time() if now is None else now,
            refreshed_tag_present=refreshed_tag_present,
        )
    updated = reactive_store.set_enabled(trigger_id, enabled, expected_rule_uid=record.rule_uid)
    if updated is None:
        msg = f"external trigger not found: {trigger_id}"
        raise ExternalTriggerNotFoundError(msg)
    return updated


def delete_trigger_sync(
    external_store: ExternalTriggerStore,
    reactive_store: ReactiveTriggerStore,
    trigger_id: str,
    *,
    actor_user_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    allow_admin: bool,
    expected_external_uid: str | None = None,
    expected_reactive_uid: str | None = None,
) -> None:
    """Delete a webhook or reactive trigger across both stores."""
    if expected_reactive_uid is None and (
        expected_external_uid is not None or external_store.get_record(trigger_id) is not None
    ):
        external_store.delete_record(
            trigger_id,
            actor_user_id=actor_user_id,
            config=config,
            allow_admin=allow_admin,
            expected_uid=expected_external_uid,
        )
        return

    record = _require_reactive_record(
        reactive_store,
        trigger_id,
        actor_user_id=actor_user_id,
        config=config,
        runtime_paths=runtime_paths,
        allow_admin=allow_admin,
        expected_rule_uid=expected_reactive_uid,
    )
    if not reactive_store.delete_trigger(trigger_id, expected_rule_uid=record.rule_uid):
        msg = f"external trigger not found: {trigger_id}"
        raise ExternalTriggerNotFoundError(msg)
