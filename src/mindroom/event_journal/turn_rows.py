"""Turn state as rows: every statement on the turn tables and the redaction table.

Each fact about a turn has a column and a narrow writer. The only whole-turn
operation is ``_reshape``: forming a turn whose ordered sources differ from
every stored one.

Three rules carry the design, and each is easy to get backwards.

A turn is named by ``turn_key``, an immutable identity for one ordered tuple of
physical sources. A write with a different tuple makes a new key; nothing
changes an existing key's sources or their order, so an edit or a delivery that
captured a key can never be redirected to a different turn.

Physical membership is exclusive and alias membership is advisory. A source
belongs to exactly one turn, while an alias may be indexed by several, because
an original and the router relay answering for it are both live turns. Lookups
read aliases; claims, checkpoints, and reshape move physical membership only.

"Redacted" is stored once, in ``event_redactions``, and every content-bearing
write checks it in its own transaction against the state after the write. So a
redaction that lands before a row exists, and a payload built before the
redaction, both fail to store deleted text.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, replace
from itertools import batched, chain
from typing import TYPE_CHECKING

from mindroom.history.types import HistoryScope
from mindroom.message_target import MessageTarget
from mindroom.model_selection import command_result_content_to_dict, freeze_command_result_content
from mindroom.turn_record import (
    PreparedVoiceSource,
    RevisionReplay,
    SourceEventMetadata,
    SourceEventRevision,
    SourceVisibleEcho,
    TurnRecord,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Sequence
    from typing import Concatenate

    from .backend import Backend, Row, Transaction

# Bound the parameters of one IN list well below either backend's limit.
_CHUNK = 500

_TURN_COLUMNS = (
    "turn_key, anchor_event_id, room_id, completed, updated_at, response_event_id, final_delivery_id, "
    "response_owner, requester_id, history_scope_json, conversation_target_json, "
    "command_execution_started, command_result_text, command_result_extra_content_json"
)
_SOURCE_COLUMNS = (
    "turn_key, event_id, prompt, selected_rev_ts, selected_rev_id, sender, timestamp_ms, speaker, "
    "discovery_event_id, prepared_voice_json, visible_echo_event_id, visible_echo_is_fallback"
)

# A revision a turn learns about: (revision event id, the event it edits, its timestamp in ms).
type RevisionRegistration = tuple[str, str, int]


@dataclass(frozen=True, slots=True)
class Accepted:
    """The write landed; ``record`` is the turn as stored after it."""

    record: TurnRecord


@dataclass(frozen=True, slots=True)
class Answered:
    """The turn is already answered or handed to its final delivery; nothing was written."""


@dataclass(frozen=True, slots=True)
class Refused:
    """An answered or delivery-bound turn of another key owns a candidate source; nothing was written."""


type ReshapeResult = Accepted | Answered | Refused


@dataclass(frozen=True, slots=True)
class RetentionResult:
    """What one retention pass removed."""

    dropped_turn_count: int
    cleared_voice_turn_count: int


@dataclass(slots=True)
class _Turn:
    turn_key: str
    anchor_event_id: str
    room_id: str | None
    completed: bool
    updated_at: float
    response_event_id: str | None = None
    final_delivery_id: str | None = None
    response_owner: str | None = None
    requester_id: str | None = None
    history_scope_json: str | None = None
    conversation_target_json: str | None = None
    command_execution_started: bool = False
    command_result_text: str | None = None
    command_result_extra_content_json: str | None = None

    @property
    def frozen(self) -> bool:
        """Answered, or handed to a final delivery whose acknowledgement will answer it."""
        return self.completed or self.final_delivery_id is not None


@dataclass(slots=True)
class _Source:
    event_id: str
    prompt: str | None = None
    selected_rev_ts: int | None = None
    selected_rev_id: str | None = None
    sender: str | None = None
    timestamp_ms: float | None = None
    speaker: str | None = None
    discovery_event_id: str | None = None
    prepared_voice_json: str | None = None
    visible_echo_event_id: str | None = None
    visible_echo_is_fallback: bool | None = None

    @property
    def selected(self) -> SourceEventRevision | None:
        if self.selected_rev_ts is None or self.selected_rev_id is None:
            return None
        return (self.selected_rev_ts, self.selected_rev_id)


@dataclass(frozen=True, slots=True)
class _Revision:
    revision_id: str
    source_event_id: str
    timestamp_ms: int
    response_event_id: str | None = None


@dataclass(slots=True)
class _Rows:
    turn: _Turn
    sources: list[_Source]
    aliases: list[str] = field(default_factory=list)
    revisions: list[_Revision] = field(default_factory=list)


def agent_of_principal(principal_id: str) -> str:
    """Return the agent a journal principal belongs to; principals are ``agent@mxid`` and agent names hold no ``@``."""
    return principal_id.partition("@")[0]


def _placeholders(values: Collection[object]) -> str:
    return ", ".join("?" for _ in values)


def _fetch_in(transaction: Transaction, sql: str, prefix: tuple[object, ...], values: Iterable[object]) -> list[Row]:
    """Run ``sql`` once per bounded batch of ``values``; ``{in}`` marks where the batch's placeholders go."""
    rows: list[Row] = []
    for batch in batched(dict.fromkeys(values), _CHUNK):
        rows.extend(transaction.fetchall(sql.format(**{"in": _placeholders(batch)}), (*prefix, *batch)))
    return rows


def _optional_bool(value: object) -> bool | None:
    return None if value is None else bool(value)


def _turn_from_row(row: Row) -> _Turn:
    return _Turn(
        turn_key=str(row["turn_key"]),
        anchor_event_id=str(row["anchor_event_id"]),
        room_id=row["room_id"],
        completed=bool(row["completed"]),
        updated_at=float(row["updated_at"]),
        response_event_id=row["response_event_id"],
        final_delivery_id=row["final_delivery_id"],
        response_owner=row["response_owner"],
        requester_id=row["requester_id"],
        history_scope_json=row["history_scope_json"],
        conversation_target_json=row["conversation_target_json"],
        command_execution_started=bool(row["command_execution_started"]),
        command_result_text=row["command_result_text"],
        command_result_extra_content_json=row["command_result_extra_content_json"],
    )


def _source_from_row(row: Row) -> _Source:
    return _Source(
        event_id=str(row["event_id"]),
        prompt=row["prompt"],
        selected_rev_ts=None if row["selected_rev_ts"] is None else int(row["selected_rev_ts"]),
        selected_rev_id=row["selected_rev_id"],
        sender=row["sender"],
        timestamp_ms=None if row["timestamp_ms"] is None else float(row["timestamp_ms"]),
        speaker=row["speaker"],
        discovery_event_id=row["discovery_event_id"],
        prepared_voice_json=row["prepared_voice_json"],
        visible_echo_event_id=row["visible_echo_event_id"],
        visible_echo_is_fallback=_optional_bool(row["visible_echo_is_fallback"]),
    )


def _turns(transaction: Transaction, agent_name: str, keys: Iterable[str]) -> dict[str, _Turn]:
    rows = _fetch_in(
        transaction,
        f"SELECT {_TURN_COLUMNS} FROM turns WHERE agent_name = ? AND turn_key IN ({{in}})",  # noqa: S608
        (agent_name,),
        keys,
    )
    return {str(row["turn_key"]): _turn_from_row(row) for row in rows}


def _load_rows(transaction: Transaction, agent_name: str, keys: Iterable[str]) -> dict[str, _Rows]:
    """Read every row of these turns."""
    turns = _turns(transaction, agent_name, keys)
    loaded = {key: _Rows(turn=turn, sources=[]) for key, turn in turns.items()}
    for row in _fetch_in(
        transaction,
        f"SELECT {_SOURCE_COLUMNS} FROM turn_sources WHERE agent_name = ? AND turn_key IN ({{in}}) "  # noqa: S608
        "ORDER BY turn_key, ordinal",
        (agent_name,),
        turns,
    ):
        loaded[str(row["turn_key"])].sources.append(_source_from_row(row))
    for row in _fetch_in(
        transaction,
        "SELECT turn_key, event_id FROM turn_aliases WHERE agent_name = ? AND turn_key IN ({in}) "
        "ORDER BY turn_key, event_id/*bytes*/",
        (agent_name,),
        turns,
    ):
        loaded[str(row["turn_key"])].aliases.append(str(row["event_id"]))
    for row in _fetch_in(
        transaction,
        "SELECT turn_key, revision_id, source_event_id, timestamp_ms, response_event_id FROM turn_revisions "
        "WHERE agent_name = ? AND turn_key IN ({in}) ORDER BY turn_key, revision_id/*bytes*/",
        (agent_name,),
        turns,
    ):
        loaded[str(row["turn_key"])].revisions.append(
            _Revision(
                str(row["revision_id"]),
                str(row["source_event_id"]),
                int(row["timestamp_ms"]),
                row["response_event_id"],
            ),
        )
    return loaded


def _physical_owners(transaction: Transaction, agent_name: str, event_ids: Iterable[str]) -> dict[str, str]:
    rows = _fetch_in(
        transaction,
        "SELECT event_id, turn_key FROM turn_sources WHERE agent_name = ? AND event_id IN ({in})",
        (agent_name,),
        event_ids,
    )
    return {str(row["event_id"]): str(row["turn_key"]) for row in rows}


def _alias_owners(transaction: Transaction, agent_name: str, event_ids: Iterable[str]) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = {}
    for row in _fetch_in(
        transaction,
        "SELECT event_id, turn_key FROM turn_aliases WHERE agent_name = ? AND event_id IN ({in})",
        (agent_name,),
        event_ids,
    ):
        owners.setdefault(str(row["event_id"]), set()).add(str(row["turn_key"]))
    return owners


def _newest(turns: Iterable[_Turn]) -> _Turn | None:
    return max(turns, key=lambda turn: (turn.updated_at, turn.turn_key), default=None)


def _assemble(rows: _Rows) -> TurnRecord:
    """Build the immutable read value of one turn from its rows."""
    turn = rows.turn
    sources = rows.sources
    return TurnRecord.create(
        [source.event_id for source in sources],
        discovery_event_ids=rows.aliases,
        anchor_event_id=turn.anchor_event_id,
        response_event_id=turn.response_event_id,
        completed=turn.completed,
        source_event_prompts={source.event_id: source.prompt for source in sources if source.prompt is not None},
        source_event_revisions={
            source.event_id: selected for source in sources if (selected := source.selected) is not None
        },
        revision_replay={
            revision.revision_id: RevisionReplay(
                revision.source_event_id,
                revision.timestamp_ms,
                response_event_id=revision.response_event_id,
            )
            for revision in rows.revisions
        },
        # Always a map: a source without a sender has no proof of who sent it, even in a one-source turn.
        source_event_metadata={
            source.event_id: SourceEventMetadata(
                source.sender,
                source.timestamp_ms,
                source.discovery_event_id,
                source.speaker,
            )
            for source in sources
            if source.sender is not None
        },
        prepared_voice_sources={
            source.event_id: json.loads(source.prepared_voice_json)
            for source in sources
            if source.prepared_voice_json is not None
        },
        response_owner=turn.response_owner,
        requester_id=turn.requester_id,
        command_execution_started=turn.command_execution_started,
        command_result_text=turn.command_result_text,
        command_result_extra_content=(
            None
            if turn.command_result_extra_content_json is None
            else freeze_command_result_content(json.loads(turn.command_result_extra_content_json))
        ),
        history_scope=(
            None if turn.history_scope_json is None else HistoryScope.from_metadata(json.loads(turn.history_scope_json))
        ),
        conversation_target=(
            None
            if turn.conversation_target_json is None
            else MessageTarget.from_metadata(json.loads(turn.conversation_target_json))
        ),
        turn_key=turn.turn_key,
        visible_echoes={
            source.event_id: SourceVisibleEcho(source.visible_echo_event_id, source.visible_echo_is_fallback)
            for source in sources
            if source.visible_echo_event_id is not None
        },
    )


def _record(transaction: Transaction, agent_name: str, turn_key: str) -> TurnRecord:
    return _assemble(_load_rows(transaction, agent_name, (turn_key,))[turn_key])


# Redaction reads.


def redacted_events(
    transaction: Transaction,
    agent_name: str,
    room_id: str,
    event_ids: Iterable[str],
) -> frozenset[str]:
    """Return which of these physical events are redacted in ``room_id``."""
    rows = _fetch_in(
        transaction,
        "SELECT event_id FROM event_redactions WHERE agent_name = ? AND room_id = ? AND event_id IN ({in})",
        (agent_name, room_id),
        event_ids,
    )
    return frozenset(str(row["event_id"]) for row in rows)


def _deleted_given(sources: Sequence[_Source], redacted: Collection[str]) -> frozenset[str]:
    """Return the sources deleted because they, or the discovery alias mapped to them, are in ``redacted``.

    An id that is a physical source of the turn is never read as another
    source's alias: physical membership takes precedence over alias mapping.
    """
    physical = {source.event_id for source in sources}
    return frozenset(
        source.event_id
        for source in sources
        if source.event_id in redacted
        or (source.discovery_event_id not in physical and source.discovery_event_id in redacted)
    )


def _deleted_among(
    transaction: Transaction,
    agent_name: str,
    room_id: str | None,
    sources: Sequence[_Source],
) -> frozenset[str]:
    """Return the sources deleted by the redactions recorded in ``room_id``."""
    if room_id is None:
        return frozenset()
    ids = chain.from_iterable((source.event_id, source.discovery_event_id or source.event_id) for source in sources)
    return _deleted_given(sources, redacted_events(transaction, agent_name, room_id, ids))


def deleted_sources(transaction: Transaction, agent_name: str, source_event_ids: Iterable[str]) -> frozenset[str]:
    """Return which of these physical sources are deleted, each as its stored turn reads it.

    A source is deleted when it, or the discovery alias mapped to it, is
    redacted. A redacted alias of its turn that maps to no source, such as the
    question a selection answers, deletes every source of that turn.
    """
    owners = _physical_owners(transaction, agent_name, source_event_ids)
    deleted: set[str] = set()
    for rows in _load_rows(transaction, agent_name, tuple(set(owners.values()))).values():
        room_id = rows.turn.room_id
        mapped = {source.discovery_event_id for source in rows.sources}
        unmapped = [alias for alias in rows.aliases if alias not in mapped]
        if room_id is not None and redacted_events(transaction, agent_name, room_id, unmapped):
            deleted.update(source.event_id for source in rows.sources)
        else:
            deleted |= _deleted_among(transaction, agent_name, room_id, rows.sources)
    return frozenset(event_id for event_id in owners if event_id in deleted)


def deleted_events(
    transaction: Transaction,
    agent_name: str,
    room_id: str,
    event_ids: Iterable[str],
) -> frozenset[str]:
    """Return which of these events are deleted in ``room_id``: redacted, or a source whose discovery alias is.

    A reply names its logical sources, and deleting the human message a router
    relayed deletes the relay that answers for it, as deleting a directly sent
    message does.
    """
    ids = tuple(dict.fromkeys(event_ids))
    redacted = redacted_events(transaction, agent_name, room_id, ids)
    aliased = {
        str(row["event_id"]): (str(row["discovery_event_id"]), str(row["turn_key"]))
        for row in _fetch_in(
            transaction,
            "SELECT s.event_id AS event_id, s.discovery_event_id AS discovery_event_id, s.turn_key AS turn_key "
            "FROM turn_sources s JOIN turns t ON t.agent_name = s.agent_name AND t.turn_key = s.turn_key "
            "WHERE s.agent_name = ? AND t.room_id = ? AND s.discovery_event_id IS NOT NULL AND s.event_id IN ({in})",
            (agent_name, room_id),
            (event_id for event_id in ids if event_id not in redacted),
        )
    }
    aliases = {alias for alias, _turn_key in aliased.values()}
    redacted_aliases = redacted_events(transaction, agent_name, room_id, aliases)
    physical = _physical_owners(transaction, agent_name, redacted_aliases)
    return redacted | {
        event_id
        for event_id, (alias, turn_key) in aliased.items()
        if alias in redacted_aliases and physical.get(alias) != turn_key
    }


def sources_deleted_by(transaction: Transaction, agent_name: str, room_id: str, event_id: str) -> tuple[str, ...]:
    """Return the sources in ``room_id`` a redaction of ``event_id`` deletes through their discovery alias."""
    owner = _physical_owners(transaction, agent_name, (event_id,)).get(event_id)
    rows = transaction.fetchall(
        "SELECT s.event_id AS event_id, s.turn_key AS turn_key FROM turn_sources s JOIN turns t "
        "ON t.agent_name = s.agent_name AND t.turn_key = s.turn_key "
        "WHERE s.agent_name = ? AND t.room_id = ? AND s.discovery_event_id = ? ORDER BY s.event_id/*bytes*/",
        (agent_name, room_id, event_id),
    )
    return tuple(str(row["event_id"]) for row in rows if str(row["turn_key"]) != owner)


# Turn reads.


def _lookup_key(transaction: Transaction, agent_name: str, event_id: str) -> str | None:
    """Return the turn one event id answers to.

    A completed owner wins: the physical owner if it is completed, else a
    completed alias owner. Then the physical owner, then the most recently
    updated alias owner. A relay that completed for an original whose own turn
    is unfinished is therefore what the original reads, while physical
    ownership stays with the original for claims and checkpoints.
    """
    physical = _physical_owners(transaction, agent_name, (event_id,)).get(event_id)
    aliases = _alias_owners(transaction, agent_name, (event_id,)).get(event_id, set())
    turns = _turns(transaction, agent_name, {*aliases, *((physical,) if physical else ())})
    if physical is not None and turns[physical].completed:
        return physical
    completed_alias = _newest(turns[key] for key in aliases if turns[key].completed)
    if completed_alias is not None:
        return completed_alias.turn_key
    if physical is not None:
        return physical
    newest_alias = _newest(turns[key] for key in aliases)
    return None if newest_alias is None else newest_alias.turn_key


def get_turn_record(transaction: Transaction, agent_name: str, event_id: str) -> TurnRecord | None:
    """Return the turn one source or alias answers to, by the lookup precedence of ``_lookup_key``."""
    key = _lookup_key(transaction, agent_name, event_id)
    return None if key is None else _record(transaction, agent_name, key)


def load_turn(transaction: Transaction, agent_name: str, turn_key: str) -> TurnRecord | None:
    """Return one turn by its key, as an edit that captured it reads it again."""
    rows = _load_rows(transaction, agent_name, (turn_key,)).get(turn_key)
    return None if rows is None else _assemble(rows)


def _answered_among(transaction: Transaction, agent_name: str, event_ids: Collection[str]) -> frozenset[str]:
    answered = {
        str(row["event_id"])
        for row in _fetch_in(
            transaction,
            "SELECT s.event_id AS event_id FROM turn_sources s JOIN turns t "
            "ON t.agent_name = s.agent_name AND t.turn_key = s.turn_key "
            "WHERE s.agent_name = ? AND t.completed AND s.event_id IN ({in})",
            (agent_name,),
            event_ids,
        )
    }
    answered.update(
        str(row["event_id"])
        for row in _fetch_in(
            transaction,
            "SELECT a.event_id AS event_id FROM turn_aliases a JOIN turns t "
            "ON t.agent_name = a.agent_name AND t.turn_key = a.turn_key "
            "WHERE a.agent_name = ? AND t.completed AND a.event_id IN ({in})",
            (agent_name,),
            event_ids,
        )
    )
    return frozenset(answered)


def is_answered(transaction: Transaction, agent_name: str, event_id: str) -> bool:
    """Return whether this event's physical owner, or any turn indexing it as an alias, is completed."""
    return bool(_answered_among(transaction, agent_name, (event_id,)))


def handled_among(
    transaction: Transaction,
    agent_name: str,
    room_id: str,
    event_ids: Collection[str],
) -> frozenset[str]:
    """Return which of these events are answered, or physically redacted in ``room_id``."""
    return _answered_among(transaction, agent_name, event_ids) | redacted_events(
        transaction,
        agent_name,
        room_id,
        event_ids,
    )


def turn_for_response_event(transaction: Transaction, agent_name: str, event_id: str) -> TurnRecord | None:
    """Return the sole turn whose visible response, or one of whose source echoes, is this event."""
    keys = {
        str(row["turn_key"])
        for row in transaction.fetchall(
            "SELECT turn_key FROM turns WHERE agent_name = ? AND response_event_id = ?",
            (agent_name, event_id),
        )
    }
    keys.update(
        str(row["turn_key"])
        for row in transaction.fetchall(
            "SELECT turn_key FROM turn_sources WHERE agent_name = ? AND visible_echo_event_id = ?",
            (agent_name, event_id),
        )
    )
    if len(keys) > 1:
        msg = f"Multiple turns own visible response {event_id!r}"
        raise RuntimeError(msg)
    return None if not keys else _record(transaction, agent_name, keys.pop())


def _source_row(transaction: Transaction, agent_name: str, event_id: str) -> tuple[_Source, str] | None:
    row = transaction.fetchone(
        f"SELECT {_SOURCE_COLUMNS} FROM turn_sources WHERE agent_name = ? AND event_id = ?",  # noqa: S608
        (agent_name, event_id),
    )
    return None if row is None else (_source_from_row(row), str(row["turn_key"]))


def prepared_voice(transaction: Transaction, agent_name: str, source_event_id: str) -> PreparedVoiceSource | None:
    """Return one physical voice source's normalization checkpoint."""
    found = _source_row(transaction, agent_name, source_event_id)
    if found is None or found[0].prepared_voice_json is None:
        return None
    return PreparedVoiceSource._from_raw(json.loads(found[0].prepared_voice_json))


def visible_echo(transaction: Transaction, agent_name: str, source_event_id: str) -> SourceVisibleEcho | None:
    """Return the echo published for one physical source, and how its replacement ended."""
    found = _source_row(transaction, agent_name, source_event_id)
    if found is None or found[0].visible_echo_event_id is None:
        return None
    return SourceVisibleEcho(found[0].visible_echo_event_id, found[0].visible_echo_is_fallback)


# Row writes.


def _insert_rows(transaction: Transaction, agent_name: str, rows: _Rows) -> None:
    turn = rows.turn
    transaction.execute(
        f"INSERT INTO turns (agent_name, {_TURN_COLUMNS}) VALUES (?, {_placeholders(range(14))})",  # noqa: S608
        (
            agent_name,
            turn.turn_key,
            turn.anchor_event_id,
            turn.room_id,
            turn.completed,
            turn.updated_at,
            turn.response_event_id,
            turn.final_delivery_id,
            turn.response_owner,
            turn.requester_id,
            turn.history_scope_json,
            turn.conversation_target_json,
            turn.command_execution_started,
            turn.command_result_text,
            turn.command_result_extra_content_json,
        ),
    )
    for ordinal, source in enumerate(rows.sources):
        transaction.execute(
            f"INSERT INTO turn_sources (agent_name, ordinal, {_SOURCE_COLUMNS}) "  # noqa: S608
            f"VALUES (?, ?, {_placeholders(range(12))})",
            (
                agent_name,
                ordinal,
                turn.turn_key,
                source.event_id,
                source.prompt,
                source.selected_rev_ts,
                source.selected_rev_id,
                source.sender,
                source.timestamp_ms,
                source.speaker,
                source.discovery_event_id,
                source.prepared_voice_json,
                source.visible_echo_event_id,
                source.visible_echo_is_fallback,
            ),
        )
    for alias in rows.aliases:
        _insert_alias(transaction, agent_name, turn.turn_key, alias)
    for revision in rows.revisions:
        _upsert_revision(transaction, agent_name, turn.turn_key, revision)


def _insert_alias(transaction: Transaction, agent_name: str, turn_key: str, event_id: str) -> None:
    transaction.execute(
        "INSERT INTO turn_aliases (agent_name, turn_key, event_id) VALUES (?, ?, ?) "
        "ON CONFLICT (agent_name, turn_key, event_id) DO NOTHING",
        (agent_name, turn_key, event_id),
    )


def _upsert_revision(transaction: Transaction, agent_name: str, turn_key: str, revision: _Revision) -> None:
    """Record a revision for one turn; a stored row is kept and only gains a missing response proof."""
    transaction.execute(
        """
        INSERT INTO turn_revisions (agent_name, turn_key, revision_id, source_event_id, timestamp_ms, response_event_id)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (agent_name, turn_key, revision_id) DO UPDATE SET
            response_event_id = COALESCE(turn_revisions.response_event_id, excluded.response_event_id)
        """,
        (
            agent_name,
            turn_key,
            revision.revision_id,
            revision.source_event_id,
            revision.timestamp_ms,
            revision.response_event_id,
        ),
    )


def _write_source(transaction: Transaction, agent_name: str, turn_key: str, source: _Source) -> None:
    transaction.execute(
        """
        UPDATE turn_sources SET
            prompt = ?, selected_rev_ts = ?, selected_rev_id = ?, sender = ?, timestamp_ms = ?, speaker = ?,
            discovery_event_id = ?, prepared_voice_json = ?, visible_echo_event_id = ?, visible_echo_is_fallback = ?
        WHERE agent_name = ? AND turn_key = ? AND event_id = ?
        """,
        (
            source.prompt,
            source.selected_rev_ts,
            source.selected_rev_id,
            source.sender,
            source.timestamp_ms,
            source.speaker,
            source.discovery_event_id,
            source.prepared_voice_json,
            source.visible_echo_event_id,
            source.visible_echo_is_fallback,
            agent_name,
            turn_key,
            source.event_id,
        ),
    )


def _touch(transaction: Transaction, agent_name: str, turn_key: str) -> None:
    transaction.execute(
        "UPDATE turns SET updated_at = ? WHERE agent_name = ? AND turn_key = ?",
        (time.time(), agent_name, turn_key),
    )


def _complete(transaction: Transaction, agent_name: str, turn_key: str) -> None:
    """Record one turn answered; its voice checkpoints are plaintext nothing replays any more."""
    transaction.execute(
        "UPDATE turns SET completed = TRUE, updated_at = ? WHERE agent_name = ? AND turn_key = ?",
        (time.time(), agent_name, turn_key),
    )
    transaction.execute(
        "UPDATE turn_sources SET prepared_voice_json = NULL WHERE agent_name = ? AND turn_key = ?",
        (agent_name, turn_key),
    )


def _new_one_source_turn(room_id: str | None, event_id: str) -> _Rows:
    return _Rows(
        turn=_Turn(uuid.uuid4().hex, event_id, room_id, completed=False, updated_at=time.time()),
        sources=[_Source(event_id)],
    )


def _require_room(room_id: str) -> None:
    if not room_id:
        msg = "A turn write requires the room its sources were sent in"
        raise ValueError(msg)


def _require_same_room(turn: _Turn, room_id: str) -> None:
    if turn.room_id is not None and turn.room_id != room_id:
        msg = f"Turn {turn.turn_key} is recorded in another room than {room_id!r}"
        raise ValueError(msg)


# Narrow writes.


def record_prepared_voice(
    transaction: Transaction,
    agent_name: str,
    *,
    room_id: str,
    source_event_id: str,
    prepared: PreparedVoiceSource,
) -> PreparedVoiceSource | None:
    """Keep the first normalization of one voice source; return the stored one, or None when terminal truth forbids it.

    Refused when the source's turn is answered or handed to its delivery, or
    the source is deleted, before any row is created for it.
    """
    _require_room(room_id)
    owner = _physical_owners(transaction, agent_name, (source_event_id,)).get(source_event_id)
    if owner is None:
        if redacted_events(transaction, agent_name, room_id, (source_event_id,)):
            return None
        rows = _new_one_source_turn(room_id, source_event_id)
        _insert_rows(transaction, agent_name, rows)
        source = rows.sources[0]
    else:
        rows = _load_rows(transaction, agent_name, (owner,))[owner]
        _require_same_room(rows.turn, room_id)
        if rows.turn.frozen or source_event_id in _deleted_among(transaction, agent_name, room_id, rows.sources):
            return None
        source = next(source for source in rows.sources if source.event_id == source_event_id)
    if source.prepared_voice_json is None:
        source.prepared_voice_json = json.dumps(prepared.to_record(), sort_keys=True)
        transaction.execute(
            "UPDATE turn_sources SET prepared_voice_json = ? WHERE agent_name = ? AND event_id = ?",
            (source.prepared_voice_json, agent_name, source_event_id),
        )
        _touch(transaction, agent_name, rows.turn.turn_key)
    return PreparedVoiceSource._from_raw(json.loads(source.prepared_voice_json))


def _prompt_source(rows: _Rows, event_id: str) -> str:
    """Return the physical source an event edits: the source itself, or the source its discovery alias maps to."""
    physical = {source.event_id for source in rows.sources}
    if event_id in physical:
        return event_id
    return next((source.event_id for source in rows.sources if source.discovery_event_id == event_id), event_id)


def record_revisions(
    transaction: Transaction,
    agent_name: str,
    *,
    turn_key: str,
    revisions: Sequence[RevisionRegistration],
) -> bool:
    """Record edits this turn selected or consumed as context, before their text reaches a prompt.

    Return False when the turn is gone, as retention may have evicted it.
    A revision row is an id and a watermark, kept even when the revision is redacted.
    """
    rows = _load_rows(transaction, agent_name, (turn_key,)).get(turn_key)
    if rows is None:
        return False
    for revision_id, edited_event_id, timestamp_ms in revisions:
        source = _prompt_source(rows, edited_event_id)
        if revision_id != source:
            _upsert_revision(transaction, agent_name, turn_key, _Revision(revision_id, source, timestamp_ms))
    _touch(transaction, agent_name, turn_key)
    return True


def record_edit(
    transaction: Transaction,
    agent_name: str,
    *,
    turn_key: str,
    edited_event_id: str,
    prompt: str,
    revision: SourceEventRevision,
) -> None:
    """Commit one edit's text to its source as its regeneration claims the reply.

    The revision is always recorded. Its text is selected only when the source
    is not deleted, the revision is not redacted, and nothing newer is selected.
    Whether the turn was answered stays as it was.
    """
    rows = _load_rows(transaction, agent_name, (turn_key,)).get(turn_key)
    if rows is None:
        return
    source_id = _prompt_source(rows, edited_event_id)
    source = next((source for source in rows.sources if source.event_id == source_id), None)
    if source is None:
        return
    timestamp_ms, revision_id = revision
    if revision_id != source_id:
        _upsert_revision(transaction, agent_name, turn_key, _Revision(revision_id, source_id, timestamp_ms))
    _touch(transaction, agent_name, turn_key)
    room_id = rows.turn.room_id
    if (
        room_id is None
        or source_id in _deleted_among(transaction, agent_name, room_id, rows.sources)
        or redacted_events(transaction, agent_name, room_id, (revision_id,))
        or (source.selected is not None and source.selected >= revision)
    ):
        return
    transaction.execute(
        "UPDATE turn_sources SET prompt = ?, selected_rev_ts = ?, selected_rev_id = ? "
        "WHERE agent_name = ? AND turn_key = ? AND event_id = ?",
        (prompt, timestamp_ms, revision_id, agent_name, turn_key, source_id),
    )


def record_visible_echo(
    transaction: Transaction,
    agent_name: str,
    *,
    room_id: str,
    source_event_id: str,
    echo_event_id: str,
) -> None:
    """Track the echo published for one physical source, without touching its turn's answer."""
    _require_room(room_id)
    owner = _physical_owners(transaction, agent_name, (source_event_id,)).get(source_event_id)
    if owner is None:
        rows = _new_one_source_turn(room_id, source_event_id)
        _insert_rows(transaction, agent_name, rows)
        owner = rows.turn.turn_key
    else:
        _require_same_room(_turns(transaction, agent_name, (owner,))[owner], room_id)
    transaction.execute(
        "UPDATE turn_sources SET visible_echo_event_id = ? WHERE agent_name = ? AND event_id = ?",
        (echo_event_id, agent_name, source_event_id),
    )
    transaction.execute(
        "UPDATE turns SET room_id = COALESCE(room_id, ?), updated_at = ? WHERE agent_name = ? AND turn_key = ?",
        (room_id, time.time(), agent_name, owner),
    )


def record_finalized_visible_echo(
    transaction: Transaction,
    agent_name: str,
    *,
    source_event_id: str,
    echo_event_id: str,
    is_fallback: bool,
) -> None:
    """Mark one source's tracked echo replaced; a transcript is never overwritten by a fallback.

    Echo publication never becomes the turn's answer: its response stays as it
    was. A router that takes an echo as its outcome records that explicitly.
    """
    found = _source_row(transaction, agent_name, source_event_id)
    if found is None:
        return
    source, turn_key = found
    if source.visible_echo_event_id != echo_event_id or (source.visible_echo_is_fallback is False and is_fallback):
        return
    transaction.execute(
        "UPDATE turn_sources SET visible_echo_is_fallback = ? WHERE agent_name = ? AND event_id = ?",
        (is_fallback, agent_name, source_event_id),
    )
    _touch(transaction, agent_name, turn_key)


def record_placeholder(transaction: Transaction, agent_name: str, *, turn_key: str, response_event_id: str) -> None:
    """Record the visible placeholder an unfinished turn's reply streams into."""
    transaction.execute(
        "UPDATE turns SET response_event_id = ?, updated_at = ? "
        "WHERE agent_name = ? AND turn_key = ? AND NOT completed",
        (response_event_id, time.time(), agent_name, turn_key),
    )


def record_command_started(transaction: Transaction, agent_name: str, *, turn_key: str) -> None:
    """Checkpoint that a command began executing, so a replay does not run it twice."""
    transaction.execute(
        "UPDATE turns SET command_execution_started = TRUE, updated_at = ? WHERE agent_name = ? AND turn_key = ?",
        (time.time(), agent_name, turn_key),
    )


def record_command_result(
    transaction: Transaction,
    agent_name: str,
    *,
    turn_key: str,
    text: str,
    extra_content: object = None,
) -> None:
    """Checkpoint a command's first result; a later one never replaces it."""
    frozen = freeze_command_result_content(extra_content)
    transaction.execute(
        """
        UPDATE turns SET
            command_execution_started = TRUE, command_result_text = ?, command_result_extra_content_json = ?,
            updated_at = ?
        WHERE agent_name = ? AND turn_key = ? AND command_result_text IS NULL
        """,
        (
            text,
            None if frozen is None else json.dumps(command_result_content_to_dict(frozen), sort_keys=True),
            time.time(),
            agent_name,
            turn_key,
        ),
    )


# Reshape and the writes that go through it.


def _reshape(
    transaction: Transaction,
    agent_name: str,
    candidate: TurnRecord,
    *,
    room_id: str,
) -> _Turn | Refused:
    """Give the candidate's ordered sources one turn and return it.

    This is the one whole-turn operation, and it is merge policy written out:

    1. Every physical owner and alias owner of a candidate source is read.
    2. A completed or delivery-bound turn of another key that physically owns
       a candidate source, or indexes one as its alias, refuses the write.
    3. The target is the stored turn with exactly this ordered tuple, else a
       new key; an existing key never changes its sources or their order.
    4. Every other physical owner is unfinished and is absorbed: the
       candidate's own source rows move into the target with their payload;
       a revision follows what it describes, so an edit of a retained source
       or another message's edit consumed as context moves into the target and
       an edit of a detached source follows that source; aliases move unless
       mapped to a detached source; and the target's turn columns come from the
       single most recently updated absorbed turn, never from two.
    5. A detached source keeps a one-source turn only for a voice checkpoint
       or echo, the unsettled work it still owns; any other loses its record,
       since a stale row would read as handled and drop a message. Absorbed
       turns are then deleted.

    Alias owners only supply conflict and lookup evidence: they never give up
    or receive physical membership, so an original and its relay coexist. A
    context that differs from an unfinished owner's is not a conflict: the
    dispatch holding the claim decides it, and the candidate's context wins.
    """
    _require_room(room_id)
    sources = candidate.source_event_ids
    if not sources:
        msg = "A turn write requires at least one source"
        raise ValueError(msg)
    owners = _physical_owners(transaction, agent_name, sources)
    alias_owners = _alias_owners(transaction, agent_name, sources)
    turns = _turns(transaction, agent_name, {*owners.values(), *chain.from_iterable(alias_owners.values())})
    target: _Turn | None = None
    if len(owners) == len(sources) and len(set(owners.values())) == 1:
        key = owners[sources[0]]
        if tuple(source.event_id for source in _load_rows(transaction, agent_name, (key,))[key].sources) == sources:
            target = turns[key]
    target_key = None if target is None else target.turn_key
    if any(
        turns[key].frozen
        for source in sources
        for key in {owners.get(source), *alias_owners.get(source, ())}
        if key is not None and key != target_key
    ):
        return Refused()
    for key in set(owners.values()):
        _require_same_room(turns[key], room_id)
    if target is not None:
        return target
    return _absorb(transaction, agent_name, candidate, room_id=room_id, owner_keys=set(owners.values()))


def _absorb(
    transaction: Transaction,
    agent_name: str,
    candidate: TurnRecord,
    *,
    room_id: str,
    owner_keys: set[str],
) -> _Turn:
    """Write a new turn for the candidate's tuple from the unfinished turns that owned its sources."""
    absorbed = _load_rows(transaction, agent_name, sorted(owner_keys))
    candidate_ids = set(candidate.source_event_ids)
    turn = _backfilled_turn(candidate.source_event_ids[-1], room_id, _newest(rows.turn for rows in absorbed.values()))
    moved = {source.event_id: source for rows in absorbed.values() for source in rows.sources}
    revisions: dict[str, _Revision] = {}
    aliases: dict[str, None] = {}
    detached: list[_Rows] = []
    for rows in absorbed.values():
        own = {source.event_id for source in rows.sources}
        kept = {
            source.event_id: _detached_turn(rows.turn.room_id, source)
            for source in rows.sources
            if source.event_id not in candidate_ids
            and (source.prepared_voice_json is not None or source.visible_echo_event_id is not None)
        }
        for revision in rows.revisions:
            if revision.source_event_id in candidate_ids or revision.source_event_id not in own:
                stored = revisions.setdefault(revision.revision_id, revision)
                if stored.response_event_id is None:
                    revisions[revision.revision_id] = replace(stored, response_event_id=revision.response_event_id)
            elif revision.source_event_id in kept:
                kept[revision.source_event_id].revisions.append(revision)
        mapped = {source.discovery_event_id: source.event_id for source in rows.sources if source.discovery_event_id}
        for alias in rows.aliases:
            mapped_source = mapped.get(alias)
            if alias not in candidate_ids and (mapped_source is None or mapped_source in candidate_ids):
                aliases[alias] = None
        detached.extend(kept.values())
    for key in absorbed:
        transaction.execute("DELETE FROM turns WHERE agent_name = ? AND turn_key = ?", (agent_name, key))
    for rows in detached:
        _insert_rows(transaction, agent_name, rows)
    _insert_rows(
        transaction,
        agent_name,
        _Rows(
            turn=turn,
            sources=[moved.get(event_id) or _Source(event_id) for event_id in candidate.source_event_ids],
            aliases=list(aliases),
            revisions=list(revisions.values()),
        ),
    )
    return turn


def _backfilled_turn(anchor_event_id: str, room_id: str, backfill: _Turn | None) -> _Turn:
    """Return a new unfinished turn carrying the turn columns of the one absorbed turn it backfills from."""
    turn = _Turn(uuid.uuid4().hex, anchor_event_id, room_id, completed=False, updated_at=time.time())
    if backfill is not None:
        turn.response_event_id = backfill.response_event_id
        turn.response_owner = backfill.response_owner
        turn.requester_id = backfill.requester_id
        turn.history_scope_json = backfill.history_scope_json
        turn.conversation_target_json = backfill.conversation_target_json
        turn.command_execution_started = backfill.command_execution_started
        turn.command_result_text = backfill.command_result_text
        turn.command_result_extra_content_json = backfill.command_result_extra_content_json
    return turn


def _detached_turn(room_id: str | None, source: _Source) -> _Rows:
    """Return the one-source turn a detached source keeps for its voice checkpoint or echo, and nothing else."""
    rows = _new_one_source_turn(room_id, source.event_id)
    rows.sources[0].prepared_voice_json = source.prepared_voice_json
    rows.sources[0].visible_echo_event_id = source.visible_echo_event_id
    rows.sources[0].visible_echo_is_fallback = source.visible_echo_is_fallback
    return rows


def _add_aliases(transaction: Transaction, agent_name: str, turn_key: str, candidate: TurnRecord) -> None:
    """Index the candidate's aliases, unless a completed turn of another key owns one."""
    aliases = candidate.discovery_event_ids
    if not aliases:
        return
    physical = _physical_owners(transaction, agent_name, aliases)
    indexed = _alias_owners(transaction, agent_name, aliases)
    turns = _turns(transaction, agent_name, {*physical.values(), *chain.from_iterable(indexed.values())})
    for alias in aliases:
        owners = {physical.get(alias), *indexed.get(alias, ())} - {None, turn_key}
        if not any(turns[key].completed for key in owners if key is not None):
            _insert_alias(transaction, agent_name, turn_key, alias)


def _candidate_selection(candidate: TurnRecord, source_event_id: str) -> SourceEventRevision | None:
    """Return the newest revision the candidate selected for one physical source, under its own or its alias's id."""
    return max(
        (
            revision
            for event_id, revision in (candidate.source_event_revisions or {}).items()
            if candidate.prompt_source_event_id(event_id) == source_event_id
        ),
        default=None,
    )


def _candidate_revisions(record: TurnRecord) -> list[_Revision]:
    """Return the revisions a record names: its replay entries, then each selected edit not among them.

    A selected edit is also a watermark that keeps older late writes stale.
    """
    revisions = {
        revision_id: _Revision(revision_id, replay.source_event_id, replay.timestamp_ms, replay.response_event_id)
        for revision_id, replay in (record.revision_replay or {}).items()
    }
    for event_id, (timestamp_ms, revision_id) in (record.source_event_revisions or {}).items():
        source_id = record.prompt_source_event_id(event_id)
        if revision_id != source_id:
            revisions.setdefault(revision_id, _Revision(revision_id, source_id, timestamp_ms))
    return list(revisions.values())


def _apply_candidate(
    transaction: Transaction,
    agent_name: str,
    turn: _Turn,
    candidate: TurnRecord,
) -> None:
    """Write the candidate's context, revisions, and per-source payload into its reshaped turn."""
    _apply_context(transaction, agent_name, turn, candidate)
    for revision in _candidate_revisions(candidate):
        _upsert_revision(transaction, agent_name, turn.turn_key, revision)
    sources = _load_rows(transaction, agent_name, (turn.turn_key,))[turn.turn_key].sources
    _attribute(sources, candidate)
    _apply_payload(transaction, agent_name, turn, sources, candidate)
    for source in sources:
        _write_source(transaction, agent_name, turn.turn_key, source)


@dataclass(frozen=True, slots=True)
class _Context:
    """A record's response context as the turn columns store it."""

    history_scope_json: str | None
    conversation_target_json: str | None
    command_result_extra_content_json: str | None


def _context(record: TurnRecord, room_id: str | None) -> _Context:
    target = record.conversation_target
    if target is not None and room_id is not None and target.room_id != room_id:
        msg = f"A conversation target in {target.room_id!r} cannot be attached to a turn in {room_id!r}"
        raise ValueError(msg)
    scope = record.history_scope
    extra = record.command_result_extra_content
    return _Context(
        history_scope_json=None if scope is None else json.dumps(scope.to_metadata(), sort_keys=True),
        conversation_target_json=None if target is None else json.dumps(target.to_metadata(), sort_keys=True),
        command_result_extra_content_json=(
            None if extra is None else json.dumps(command_result_content_to_dict(extra), sort_keys=True)
        ),
    )


def _apply_context(transaction: Transaction, agent_name: str, turn: _Turn, candidate: TurnRecord) -> None:
    """Write the candidate's response context: present values win, absent ones are kept, checkpoints only grow."""
    context = _context(candidate, turn.room_id)
    transaction.execute(
        """
        UPDATE turns SET
            response_event_id = COALESCE(?, response_event_id),
            response_owner = COALESCE(?, response_owner),
            requester_id = COALESCE(?, requester_id),
            history_scope_json = COALESCE(?, history_scope_json),
            conversation_target_json = COALESCE(?, conversation_target_json),
            command_execution_started = (command_execution_started OR ?),
            command_result_extra_content_json = CASE WHEN command_result_text IS NULL
                THEN ? ELSE command_result_extra_content_json END,
            command_result_text = COALESCE(command_result_text, ?),
            updated_at = ?
        WHERE agent_name = ? AND turn_key = ?
        """,
        (
            candidate.response_event_id,
            candidate.response_owner,
            candidate.requester_id,
            context.history_scope_json,
            context.conversation_target_json,
            candidate.command_execution_started,
            context.command_result_extra_content_json,
            candidate.command_result_text,
            time.time(),
            agent_name,
            turn.turn_key,
        ),
    )


def _attribute(sources: Sequence[_Source], candidate: TurnRecord) -> None:
    """Apply the candidate's per-source attribution.

    Where the candidate names a source, it wins, keeping fields it leaves out.
    A one-source candidate without any attribution names its requester; a
    coalesced one proves nothing about who sent what and keeps what is stored,
    so a new source stays unattributed rather than inferred from group size.
    """
    metadata = candidate.source_event_metadata
    for source in sources:
        attribution = None if metadata is None else metadata.get(source.event_id)
        if attribution is not None:
            source.sender = attribution.sender
            if attribution.timestamp_ms is not None:
                source.timestamp_ms = attribution.timestamp_ms
            source.speaker = attribution.speaker or source.speaker
            source.discovery_event_id = attribution.discovery_event_id or source.discovery_event_id
        elif metadata is None and len(sources) == 1 and candidate.requester_id is not None:
            source.sender = candidate.requester_id


def _apply_payload(
    transaction: Transaction,
    agent_name: str,
    turn: _Turn,
    sources: Sequence[_Source],
    candidate: TurnRecord,
) -> None:
    """Apply the candidate's prompts, voice checkpoints, and echoes, dropping what redaction makes ineligible.

    Eligibility is judged against the state after this write: a deleted source
    keeps no prompt or checkpoint, and a prompt taken from a redacted revision
    is never stored, while a newer valid edit still is. A prompt follows its
    revision, so a stored selection newer than the candidate's is kept. A
    stored echo or checkpoint is kept, as its own writer may have moved it on
    since the candidate was read.
    """
    room_id = turn.room_id
    deleted = _deleted_among(transaction, agent_name, room_id, sources)
    selections = {source.event_id: _candidate_selection(candidate, source.event_id) for source in sources}
    redacted_revisions = (
        frozenset()
        if room_id is None
        else redacted_events(
            transaction,
            agent_name,
            room_id,
            (selection[1] for selection in selections.values() if selection is not None),
        )
    )
    for source in sources:
        echo = (candidate.visible_echoes or {}).get(source.event_id)
        if echo is not None and source.visible_echo_event_id is None:
            source.visible_echo_event_id = echo.event_id
            source.visible_echo_is_fallback = echo.is_fallback
        if source.event_id in deleted:
            source.prompt = None
            source.prepared_voice_json = None
            continue
        prompt = (candidate.source_event_prompts or {}).get(source.event_id)
        selection = selections[source.event_id]
        stored = source.selected
        if (
            prompt is not None
            and (stored is None or (selection is not None and selection >= stored))
            and (selection is None or selection[1] not in redacted_revisions)
        ):
            source.prompt = prompt
            source.selected_rev_ts, source.selected_rev_id = selection or (None, None)
        voice = (candidate.prepared_voice_sources or {}).get(source.event_id)
        if voice is not None and source.prepared_voice_json is None and not turn.completed:
            source.prepared_voice_json = json.dumps(voice.to_record(), sort_keys=True)


def insert_turn(
    transaction: Transaction,
    agent_name: str,
    record: TurnRecord,
    *,
    room_id: str | None,
    updated_at: float,
) -> str:
    """Store one turn none of whose sources has a turn yet, as an upgrade converts a stored record; return its key.

    Attribution is exactly what the record's source map proves, and payload
    passes the same eligibility guard as every runtime write. ``room_id`` may
    be None only for content-free completion evidence an upgrade cannot place.
    """
    sources = record.source_event_ids
    if not sources:
        msg = "A turn requires at least one source"
        raise ValueError(msg)
    if owned := _physical_owners(transaction, agent_name, sources):
        msg = f"Sources {sorted(owned)} already belong to a turn"
        raise ValueError(msg)
    context = _context(record, room_id)
    turn = _Turn(
        uuid.uuid4().hex,
        sources[-1],
        room_id,
        completed=record.completed,
        updated_at=updated_at,
        response_event_id=record.response_event_id,
        response_owner=record.response_owner,
        requester_id=record.requester_id,
        history_scope_json=context.history_scope_json,
        conversation_target_json=context.conversation_target_json,
        command_execution_started=record.command_execution_started,
        command_result_text=record.command_result_text,
        command_result_extra_content_json=context.command_result_extra_content_json,
    )
    rows = _Rows(
        turn=turn,
        sources=[_Source(event_id) for event_id in sources],
        aliases=list(record.discovery_event_ids),
        revisions=_candidate_revisions(record),
    )
    _attribute(rows.sources, record)
    _apply_payload(transaction, agent_name, turn, rows.sources, record)
    _insert_rows(transaction, agent_name, rows)
    return turn.turn_key


def record_pending_turn(
    transaction: Transaction,
    agent_name: str,
    record: TurnRecord,
    *,
    room_id: str,
) -> ReshapeResult:
    """Persist a turn's response context before generation starts."""
    turn = _reshape(transaction, agent_name, record, room_id=room_id)
    if isinstance(turn, Refused):
        return turn
    if turn.frozen:
        return Answered()
    _add_aliases(transaction, agent_name, turn.turn_key, record)
    _apply_candidate(transaction, agent_name, turn, record)
    return Accepted(_record(transaction, agent_name, turn.turn_key))


def record_turn(
    transaction: Transaction,
    agent_name: str,
    record: TurnRecord,
    *,
    room_id: str,
) -> str | None:
    """Record one turn answered without a visible response of its own, as precheck denial and ignores do.

    Returns its key, or None when the write was refused.
    """
    turn = _reshape(transaction, agent_name, record, room_id=room_id)
    if isinstance(turn, Refused):
        return None
    _add_aliases(transaction, agent_name, turn.turn_key, record)
    # A turn handed to its final delivery keeps its context; only the acknowledgement's answer is still owed.
    if turn.final_delivery_id is None:
        transaction.execute(
            "UPDATE turns SET room_id = COALESCE(room_id, ?) WHERE agent_name = ? AND turn_key = ?",
            (room_id, agent_name, turn.turn_key),
        )
        turn.room_id = turn.room_id or room_id
        _apply_candidate(transaction, agent_name, turn, record)
    _complete(transaction, agent_name, turn.turn_key)
    return turn.turn_key


def record_responded_turn(
    transaction: Transaction,
    agent_name: str,
    record: TurnRecord,
    *,
    room_id: str,
) -> None:
    """Record one turn answered by a visible response, attributing it to the revisions the record selected."""
    response_event_id = record.response_event_id
    if not response_event_id:
        msg = "A responded turn requires a visible Matrix response event ID"
        raise ValueError(msg)
    turn_key = record_turn(transaction, agent_name, record, room_id=room_id)
    if turn_key is None:
        return
    transaction.execute(
        "UPDATE turns SET response_event_id = ? WHERE agent_name = ? AND turn_key = ?",
        (response_event_id, agent_name, turn_key),
    )
    consumed = tuple(revision_id for _timestamp, revision_id in (record.source_event_revisions or {}).values())
    if consumed:
        transaction.execute(
            f"UPDATE turn_revisions SET response_event_id = ? "  # noqa: S608
            f"WHERE agent_name = ? AND turn_key = ? AND response_event_id IS NULL "
            f"AND revision_id IN ({_placeholders(consumed)})",
            (response_event_id, agent_name, turn_key, *consumed),
        )


def bind_final_delivery(
    transaction: Transaction,
    agent_name: str,
    *,
    delivery_id: str,
    room_id: str,
    turn_key: str,
) -> tuple[str, ...] | None:
    """Bind a non-AI FINAL delivery to the turn it answers, in the transaction that makes the delivery durable.

    The turn is found by its anchor, as the outbox keys it, and must still be
    the key and room the producer recorded; a reshape since then leaves the
    delivery unbound. Returns the turn's physical sources and aliases for the
    same transaction to settle, or None when nothing was bound.
    """
    row = transaction.fetchone(
        "SELECT turn_key, room_id FROM turns WHERE agent_name = ? AND anchor_event_id = ?",
        (agent_name, delivery_id),
    )
    if row is None or row["turn_key"] != turn_key or row["room_id"] != room_id:
        return None
    transaction.execute(
        "UPDATE turns SET final_delivery_id = ? WHERE agent_name = ? AND turn_key = ?",
        (delivery_id, agent_name, turn_key),
    )
    rows = _load_rows(transaction, agent_name, (turn_key,))[turn_key]
    return (*(source.event_id for source in rows.sources), *rows.aliases)


def complete_bound_delivery(
    transaction: Transaction,
    agent_name: str,
    *,
    delivery_id: str,
    response_event_id: str,
) -> bool:
    """Complete the turn a non-AI FINAL delivery was bound to, in its acknowledgement's transaction.

    Returns whether one was. A placeholder the turn already names stays its
    response: the acknowledged event is only the edit that finished it.
    """
    row = transaction.fetchone(
        "UPDATE turns SET response_event_id = COALESCE(response_event_id, ?) "
        "WHERE agent_name = ? AND final_delivery_id = ? AND NOT completed RETURNING turn_key",
        (response_event_id, agent_name, delivery_id),
    )
    if row is None:
        return False
    _complete(transaction, agent_name, str(row["turn_key"]))
    return True


def complete_turn_of(
    transaction: Transaction,
    agent_name: str,
    logical_event_ids: Sequence[str],
) -> bool:
    """Complete the turn that physically owns these logical sources, as a reply's settlement does.

    Returns False when nothing changed: it was already complete, or no turn
    owns them, which only the crash window before ingress recorded it leaves.
    """
    owners = _physical_owners(transaction, agent_name, logical_event_ids)
    key = next((owners[event_id] for event_id in logical_event_ids if event_id in owners), None)
    if key is None or _turns(transaction, agent_name, (key,))[key].completed:
        return False
    _complete(transaction, agent_name, key)
    return True


# Redactions.


def record_redaction(transaction: Transaction, agent_name: str, *, room_id: str, event_id: str) -> None:
    """Record that ``event_id`` was redacted in ``room_id`` and clear the plaintext it left in the agent's turns there.

    A source that is the event, or whose discovery alias it is, loses its
    prompt and voice checkpoint; a source that selected it as its revision
    loses those and the selection, so a newer edit or the earlier body can be
    selected again. Structural ids stay, because deletion and stale-write
    refusal are derived from them: the alias binding, attribution, and the
    revision rows that hold edit watermarks. The projection, pending journal
    rows, and replies of the agent's principals are the caller's to scrub in
    the same transaction (``journal.redact_for_agent``).
    """
    transaction.execute(
        "INSERT INTO event_redactions (agent_name, room_id, event_id) VALUES (?, ?, ?) "
        "ON CONFLICT (agent_name, room_id, event_id) DO NOTHING",
        (agent_name, room_id, event_id),
    )
    room_turns = "SELECT turn_key FROM turns WHERE agent_name = ? AND room_id = ?"
    transaction.execute(
        f"""
        UPDATE turn_sources SET prompt = NULL, prepared_voice_json = NULL
        WHERE agent_name = ? AND turn_key IN ({room_turns})
          AND (
              event_id = ?
              OR (
                  discovery_event_id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM turn_sources AS physical
                      WHERE physical.agent_name = turn_sources.agent_name
                        AND physical.turn_key = turn_sources.turn_key
                        AND physical.event_id = ?
                  )
              )
          )
        """,  # noqa: S608 - a fixed subquery
        (agent_name, agent_name, room_id, event_id, event_id, event_id),
    )
    transaction.execute(
        f"""
        UPDATE turn_sources
        SET prompt = NULL, prepared_voice_json = NULL, selected_rev_ts = NULL, selected_rev_id = NULL
        WHERE agent_name = ? AND turn_key IN ({room_turns}) AND selected_rev_id = ?
        """,  # noqa: S608 - a fixed subquery
        (agent_name, agent_name, room_id, event_id),
    )


# The runtime's view.


@dataclass(frozen=True, slots=True)
class TurnRecordStore:
    """One agent's turn rows, each call one transaction on the journal's database."""

    backend: Backend
    agent_name: str

    @property
    def state_key(self) -> str:
        """Identify the database these rows live in, so in-process state is shared exactly per database."""
        return f"{id(self.backend):x}"

    async def read[**P, T](
        self,
        operation: Callable[Concatenate[Transaction, str, P], T],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        """Run one statement function of this module for this agent in a read snapshot."""
        return await self.backend.read(lambda transaction: operation(transaction, self.agent_name, *args, **kwargs))

    async def write[**P, T](
        self,
        operation: Callable[Concatenate[Transaction, str, P], T],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        """Run one statement function of this module for this agent on the writer, ordered after every queued write."""
        return await self.backend.write(lambda transaction: operation(transaction, self.agent_name, *args, **kwargs))


# Retention.


def _pure_voice_checkpoint(rows: _Rows) -> bool:
    """Recognize transient voice preparation without overlooking any other unfinished fact."""
    turn = rows.turn
    return (
        not rows.aliases
        and not rows.revisions
        and turn.response_event_id is None
        and turn.final_delivery_id is None
        and turn.response_owner is None
        and turn.requester_id is None
        and turn.history_scope_json is None
        and turn.conversation_target_json is None
        and not turn.command_execution_started
        and turn.command_result_text is None
        and any(source.prepared_voice_json is not None for source in rows.sources)
        and all(
            source.prompt is None
            and source.selected_rev_id is None
            and source.sender is None
            and source.timestamp_ms is None
            and source.speaker is None
            and source.discovery_event_id is None
            and source.visible_echo_event_id is None
            for source in rows.sources
        )
    )


def _has_response_intent(turn: _Turn) -> bool:
    return turn.response_owner is not None and turn.conversation_target_json is not None


def retain(
    transaction: Transaction,
    agent_name: str,
    *,
    now: float,
    max_age_seconds: float,
    max_turns: int,
    unsettled_event_ids: Collection[str],
) -> RetentionResult:
    """Drop turns past the age limit or beyond the count cap that own no unfinished work, then old plaintext.

    Kept regardless of age: a turn any of whose sources, aliases, or revisions
    the journal still has unsettled, and an unfinished turn with a source left
    to replay, unless it is only a voice checkpoint. Every other candidate
    goes with its rows. Separately, a kept unfinished turn none of whose sources
    is unsettled and that no response intends to answer loses its voice
    checkpoints, keeping its echo ids for publication readers.
    """
    unsettled = frozenset(unsettled_event_ids)
    keys = [
        str(row["turn_key"])
        for row in transaction.fetchall("SELECT turn_key FROM turns WHERE agent_name = ?", (agent_name,))
    ]
    loaded = _load_rows(transaction, agent_name, keys)
    by_room: dict[str | None, list[_Rows]] = {}
    for rows in loaded.values():
        by_room.setdefault(rows.turn.room_id, []).append(rows)
    redacted: dict[str | None, frozenset[str]] = {
        room_id: frozenset()
        if room_id is None
        else redacted_events(
            transaction,
            agent_name,
            room_id,
            chain.from_iterable(
                (source.event_id, source.discovery_event_id or source.event_id)
                for rows in room_rows
                for source in rows.sources
            ),
        )
        for room_id, room_rows in by_room.items()
    }

    def replayable(rows: _Rows) -> bool:
        deleted = _deleted_given(rows.sources, redacted[rows.turn.room_id])
        return any(source.event_id not in deleted for source in rows.sources)

    def required(rows: _Rows) -> bool:
        ids = chain(
            (source.event_id for source in rows.sources),
            rows.aliases,
            (revision.revision_id for revision in rows.revisions),
        )
        return not unsettled.isdisjoint(ids) or (
            not rows.turn.completed and replayable(rows) and not _pure_voice_checkpoint(rows)
        )

    ordered = sorted(loaded.values(), key=lambda rows: (rows.turn.updated_at, rows.turn.turn_key))
    needed = {rows.turn.turn_key for rows in ordered if required(rows)}
    fresh = [rows for rows in ordered if rows.turn.turn_key in needed or now - rows.turn.updated_at < max_age_seconds]
    if len(fresh) > max_turns:
        ordinary = [rows for rows in fresh if rows.turn.turn_key not in needed]
        kept_ordinary = ordinary[-max_turns:] if max_turns else []
        fresh = [rows for rows in fresh if rows.turn.turn_key in needed] + kept_ordinary
    kept = {rows.turn.turn_key for rows in fresh}
    dropped = [key for key in keys if key not in kept]
    for batch in batched(dropped, _CHUNK):
        transaction.execute(
            f"DELETE FROM turns WHERE agent_name = ? AND turn_key IN ({_placeholders(batch)})",  # noqa: S608
            (agent_name, *batch),
        )
    cleared = [
        rows.turn.turn_key
        for rows in fresh
        if not rows.turn.completed
        and not _has_response_intent(rows.turn)
        and unsettled.isdisjoint(source.event_id for source in rows.sources)
        and any(source.prepared_voice_json is not None for source in rows.sources)
    ]
    for batch in batched(cleared, _CHUNK):
        transaction.execute(
            f"UPDATE turn_sources SET prepared_voice_json = NULL "  # noqa: S608
            f"WHERE agent_name = ? AND turn_key IN ({_placeholders(batch)})",
            (agent_name, *batch),
        )
    return RetentionResult(dropped_turn_count=len(dropped), cleared_voice_turn_count=len(cleared))


__all__ = [
    "Accepted",
    "Answered",
    "Refused",
    "ReshapeResult",
    "RetentionResult",
    "RevisionRegistration",
    "TurnRecordStore",
    "agent_of_principal",
    "bind_final_delivery",
    "complete_bound_delivery",
    "complete_turn_of",
    "deleted_events",
    "deleted_sources",
    "get_turn_record",
    "handled_among",
    "insert_turn",
    "is_answered",
    "load_turn",
    "prepared_voice",
    "record_command_result",
    "record_command_started",
    "record_edit",
    "record_finalized_visible_echo",
    "record_pending_turn",
    "record_placeholder",
    "record_prepared_voice",
    "record_redaction",
    "record_responded_turn",
    "record_revisions",
    "record_turn",
    "record_visible_echo",
    "redacted_events",
    "retain",
    "sources_deleted_by",
    "turn_for_response_event",
    "visible_echo",
]
