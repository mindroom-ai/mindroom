"""One-time migration of legacy Slack watches.json and replay watch thread roots."""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any

from mindroom.file_locks import advisory_file_lock
from mindroom.reactive_triggers.models import (
    TRIGGER_ID_PATTERN,
    ExternalEventSourceSpec,
    ReactiveTriggerRecord,
    TriggerTargetSpec,
)
from mindroom.slack.event_source import SLACK_EVENT_SOURCE_ID
from mindroom.slack.turns import slack_replay_store

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delivery_replay import DeliveryReplayStore
    from mindroom.event_sources import ExternalMatchValue
    from mindroom.reactive_triggers.store import ReactiveTriggerStore

_LEGACY_MIGRATION_NAME = "slack_watches_v1"
_LEGACY_THREAD_TTL_SECONDS = 7 * 86_400
_MIGRATED_WATCH_PROMPT = (
    "Deliver this Slack message to the user, summarize it clearly, and wait for their reply before posting to Slack."
)
_SAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def _legacy_watch_trigger_id(watch_id: str) -> str:
    candidate = f"slack_watch_{watch_id}"
    if TRIGGER_ID_PATTERN.fullmatch(candidate) is not None:
        return candidate
    sanitized = _SAFE_ID_CHARS.sub("_", watch_id)[:52].strip("_") or "migrated"
    return f"slack_watch_{sanitized}"


def _convert_legacy_watch(
    watch_id: str,
    fields: Mapping[str, Any],
    *,
    max_fires: int,
    now: float,
    replay_store: DeliveryReplayStore,
) -> tuple[ReactiveTriggerRecord, list[tuple[str, str, str, float]]] | None:
    mentions, direct_messages = bool(fields.get("mentions")), bool(fields.get("direct_messages"))
    if mentions and direct_messages:
        kind = "message"
    elif mentions:
        kind = "mention"
    elif direct_messages:
        kind = "direct_message"
    else:
        return None
    channels = list(
        dict.fromkeys(ch.strip() for ch in fields.get("channels") or () if isinstance(ch, str) and ch.strip()),
    )
    match: dict[str, ExternalMatchValue] = (
        {"channel_id": channels[0] if len(channels) == 1 else channels} if channels and kind != "direct_message" else {}
    )
    trigger_id, room_id = _legacy_watch_trigger_id(watch_id), str(fields["room_id"])
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
        ),
        target=TriggerTargetSpec(
            agent=str(fields["agent_name"]),
            room_id=room_id,
            new_thread=True,
            thread_key_from_event=True,
            reopen_on_fire=True,
            prompt=_MIGRATED_WATCH_PROMPT,
        ),
        once=False,
        max_fires_per_hour=max_fires,
        created_at=float(fields.get("created_at", now)),
        updated_at=now,
    )
    thread_keys: list[tuple[str, str, str, float]] = []
    prefix = f"{watch_id}:"
    bound_roots = replay_store.list_bound_thread_roots(f"slack:watch:{watch_id}", now=int(now))
    for raw_key, (entry_room_id, thread_root_id, expires_at) in bound_roots.items():
        if (
            entry_room_id == room_id
            and expires_at > now
            and raw_key.startswith(prefix)
            and (stripped_key := raw_key.removeprefix(prefix))
        ):
            updated_at = min(now, float(expires_at - _LEGACY_THREAD_TTL_SECONDS))
            thread_keys.append((trigger_id, stripped_key, thread_root_id, updated_at))
    return record, thread_keys


# LEGACY_COMPAT: One-time migration of `<control_state>/slack/watches.json` and live replay watch thread roots.
# Legacy format: `watches.json` with `{"watches": {<id>: {owner_id, agent_name, room_id, team_id, slack_user_id,
#   mentions, direct_messages, channels, created_at}}}` plus `slack/replay` entries under scope `slack:watch:<id>`.
# Last legacy release: v2026.11; replacement: `reactive_triggers.db` rules with `source_id="slack"`.
# Handling: Atomically import into SQLite once under `watches.json.lock` with marker `slack_watches_v1`, then archive
#   `watches.json` to `watches.json.migrated` only after commit.
# Coverage: tests/slack/test_event_source.py::test_migrate_legacy_slack_watches_and_replay_roots_once.
def migrate_legacy_slack_watches(runtime_paths: RuntimePaths, store: ReactiveTriggerStore, config: Config) -> int:
    """Import legacy ``watches.json`` rules and unexpired replay watch thread roots once, then archive the file."""
    control_state_root = runtime_paths.control_state_root
    if control_state_root is None or store.has_migration(_LEGACY_MIGRATION_NAME):
        return 0
    watches_path = control_state_root / "slack" / "watches.json"
    if not watches_path.exists():
        return 0

    with advisory_file_lock(watches_path.with_name("watches.json.lock")):
        if not watches_path.exists() or store.has_migration(_LEGACY_MIGRATION_NAME):
            return 0
        stored = json.loads(watches_path.read_text(encoding="utf-8"))
        raw_watches = stored.get("watches") if isinstance(stored, dict) else None
        if not isinstance(raw_watches, dict):
            msg = "Legacy Slack watches.json has invalid structure"
            raise TypeError(msg)

        now = time.time()
        max_fires = max(1, min(60, config.external_trigger_policy.max_fires_per_hour))
        replay_store = slack_replay_store(control_state_root)
        records: list[ReactiveTriggerRecord] = []
        thread_keys: list[tuple[str, str, str, float]] = []
        for watch_id, fields in raw_watches.items():
            if not isinstance(watch_id, str) or not isinstance(fields, dict):
                continue
            converted = _convert_legacy_watch(watch_id, fields, max_fires=max_fires, now=now, replay_store=replay_store)
            if converted is not None:
                records.append(converted[0])
                thread_keys.extend(converted[1])

        imported = store.import_triggers_once(_LEGACY_MIGRATION_NAME, records, thread_keys, now=now)
        watches_path.replace(watches_path.with_name("watches.json.migrated"))
        return imported
