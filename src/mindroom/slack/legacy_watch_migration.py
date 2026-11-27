"""One-time migration of legacy Slack watches.json and replay watch thread roots."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from mindroom.file_locks import advisory_file_lock
from mindroom.logging_config import get_logger
from mindroom.reactive_triggers.models import (
    TRIGGER_ID_PATTERN,
    ExternalEventSourceSpec,
    ReactiveTriggerRecord,
    TriggerTargetSpec,
)
from mindroom.reactive_triggers.store import THREAD_KEY_TTL_SECONDS
from mindroom.slack.event_source import SLACK_EVENT_SOURCE_ID, SlackEventSource
from mindroom.slack.turns import slack_replay_store

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delivery_replay import DeliveryReplayStore
    from mindroom.event_sources import ExternalMatchValue
    from mindroom.reactive_triggers.store import ReactiveTriggerStore

logger = get_logger(__name__)

_LEGACY_MIGRATION_NAME = "slack_watches_v1"
_MIGRATED_WATCH_PROMPT = (
    "Deliver this Slack message to the user, summarize it clearly, and wait for their reply before posting to Slack."
)


def _convert_legacy_watch(
    watch_id: str,
    fields: Mapping[str, Any],
    *,
    max_fires: int,
    now: float,
    replay_store: DeliveryReplayStore,
) -> tuple[ReactiveTriggerRecord, list[tuple[str, str, str, float]]] | None:
    trigger_id = f"slack_watch_{watch_id}"
    if TRIGGER_ID_PATTERN.fullmatch(trigger_id) is None:
        logger.warning("Skipping legacy Slack watch with invalid ID during migration", watch_id=watch_id)
        return None
    kind = {
        (True, True): "message",
        (True, False): "mention",
        (False, True): "direct_message",
    }.get((bool(fields.get("mentions")), bool(fields.get("direct_messages"))))
    if kind is None:
        return None
    channels = list(
        dict.fromkeys(ch.strip() for ch in fields.get("channels") or () if isinstance(ch, str) and ch.strip()),
    )
    match: dict[str, ExternalMatchValue] = (
        {"channel_id": channels[0] if len(channels) == 1 else channels} if channels and kind != "direct_message" else {}
    )
    room_id = str(fields["room_id"])
    record = ReactiveTriggerRecord(
        trigger_id=trigger_id,
        owner_user_id=str(fields["owner_id"]),
        description="Migrated Slack watch",
        source=ExternalEventSourceSpec(
            source_id=SLACK_EVENT_SOURCE_ID,
            kind=kind,
            match=match,
            binding={"team_id": str(fields["team_id"]), "slack_user_id": str(fields["slack_user_id"])},
            source_origin="registered",
            private_room_only=SlackEventSource.private_room_only,
        ),
        target=TriggerTargetSpec(
            agent=str(fields["agent_name"]),
            room_id=room_id,
            new_thread=SlackEventSource.default_new_thread,
            thread_key_from_event=SlackEventSource.default_thread_key_from_event,
            reopen_on_fire=SlackEventSource.default_reopen_on_fire,
            prompt=_MIGRATED_WATCH_PROMPT,
        ),
        once=SlackEventSource.default_once,
        max_fires_per_hour=max_fires,
        created_at=float(fields.get("created_at", now)),
        updated_at=now,
    )
    prefix = f"{watch_id}:"
    bound_roots = replay_store.list_bound_thread_roots(f"slack:watch:{watch_id}", now=int(now))
    thread_keys = [
        (trigger_id, stripped_key, thread_root_id, min(now, float(expires_at - THREAD_KEY_TTL_SECONDS)))
        for raw_key, (entry_room_id, thread_root_id, expires_at) in bound_roots.items()
        if entry_room_id == room_id
        and expires_at > now
        and raw_key.startswith(prefix)
        and (stripped_key := raw_key.removeprefix(prefix))
    ]
    return record, thread_keys


def _archive_legacy_watches_file(watches_path: Path) -> None:
    try:
        watches_path.replace(watches_path.with_name("watches.json.migrated"))
    except OSError:
        logger.warning(
            "Migrated legacy Slack watches.json into SQLite, but archiving to watches.json.migrated failed; "
            "will retry archival on next startup",
            watches_path=str(watches_path),
            exc_info=True,
        )


# LEGACY_COMPAT: One-time migration of `<control_state>/slack/watches.json` and live replay watch thread roots.
# Legacy format: `watches.json` with `{"watches": {<id>: {owner_id, agent_name, room_id, team_id, slack_user_id,
#   mentions, direct_messages, channels, created_at}}}` plus `slack/replay` entries under scope `slack:watch:<id>`.
# Last legacy release: v2026.11.651; replacement: `reactive_triggers.db` rules with `source_id="slack"`.
# Handling: Atomically import into SQLite once under `watches.json.lock` with marker `slack_watches_v1`, then archive
#   `watches.json` to `watches.json.migrated` only after commit.
# Coverage: tests/slack/test_event_source.py::test_migrate_legacy_slack_watches_and_replay_roots_once.
def migrate_legacy_slack_watches(runtime_paths: RuntimePaths, store: ReactiveTriggerStore, config: Config) -> int:
    """Import legacy ``watches.json`` rules and unexpired replay watch thread roots once, then archive the file."""
    control_state_root = runtime_paths.control_state_root
    if control_state_root is None:
        return 0
    watches_path = control_state_root / "slack" / "watches.json"
    if not watches_path.exists():
        return 0

    with advisory_file_lock(watches_path.with_name("watches.json.lock")):
        if not watches_path.exists():
            return 0
        if store.has_migration(_LEGACY_MIGRATION_NAME):
            _archive_legacy_watches_file(watches_path)
            return 0
        stored = json.loads(watches_path.read_text(encoding="utf-8"))
        raw_watches = stored.get("watches") if isinstance(stored, dict) else None
        if not isinstance(raw_watches, dict):
            msg = "Legacy Slack watches.json has invalid structure"
            raise TypeError(msg)

        now = time.time()
        default_max = SlackEventSource.default_max_fires_per_hour or 60
        max_fires = min(default_max, config.external_trigger_policy.max_fires_per_hour)
        replay_store = slack_replay_store(control_state_root)
        records: list[ReactiveTriggerRecord] = []
        thread_keys: list[tuple[str, str, str, float]] = []
        for watch_id, fields in raw_watches.items():
            if (
                isinstance(fields, dict)
                and (
                    converted := _convert_legacy_watch(
                        watch_id,
                        fields,
                        max_fires=max_fires,
                        now=now,
                        replay_store=replay_store,
                    )
                )
                is not None
            ):
                records.append(converted[0])
                thread_keys.extend(converted[1])

        imported = store.import_triggers_once(_LEGACY_MIGRATION_NAME, records, thread_keys, now=now)
        _archive_legacy_watches_file(watches_path)
        return imported
