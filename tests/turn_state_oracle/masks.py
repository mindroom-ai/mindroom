"""Intended differences from the recordings, each named with its reason and asserted as the new behavior.

A mask names one observable (or ``"outcome"``), why it differs, the scenarios it covers (every one when empty),
``normalize(key, value, context)``, which both the recorded and the current value pass through before they compare
(``None`` treats a key as absent), and ``affirm(key, value, context)``, which asserts the new behavior on every current
value the mask covers. The context is the step's action and what the current store reports redacted, so a mask can
tell a deliberate consequence of a redaction from a lost fact.

A divergence ends one scenario's comparison at a step whose input the store now refuses or answers differently by
design, so everything after it differs for that reason alone; it affirms the new behavior at that step instead.

A later change declares a deliberate behavior change here instead of re-recording, so the recordings keep pinning
everything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tests.turn_state_oracle.actions import NAMED, SEEDS
from tests.turn_state_oracle.adapter import ROOMS

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class Context:
    """What a mask may consult about one step: its action, and what the current store reports redacted in each room."""

    action: dict[str, Any] | None
    redacted_in: dict[str, frozenset[str]]
    # Every (room key, event) a scenario action has redacted up to and including this step.
    redactions: frozenset[tuple[str, str]] = frozenset()
    # The current store's whole state after this step.
    state: dict[str, dict[str, Any]] | None = None
    # The room each event was first seen or recorded in, as the adapter tracks it.
    event_rooms: dict[str, str] | None = None
    # The latest echo a scenario action published for each source, up to and including this step.
    echoes: dict[str, str] | None = None
    # Every echo a scenario action published, up to and including this step.
    published_echoes: frozenset[str] = frozenset()

    @property
    def redacted(self) -> frozenset[str]:
        """Return every event the current store reports redacted in some room."""
        return frozenset().union(*self.redacted_in.values())

    def redacted_for(self, record: dict[str, Any]) -> frozenset[str]:
        """Return what is redacted in a record's room: a redaction in another room never reaches a turn.

        A record without a target is in the room its first source was seen in, as the adapter reads it.
        """
        target = record.get("target")
        room = target["room_id"] if target else (self.event_rooms or {}).get(record["sources"][0], ROOMS["a"])
        return self.redacted_in.get(room, frozenset())

    def room_of(self, event_id: str) -> str | None:
        """Return the room of the turn the current store reads ``event_id`` into, else where it was first seen."""
        record = (self.state or {}).get("turn_record", {}).get(event_id)
        target = record.get("target") if isinstance(record, dict) else None
        return target["room_id"] if target else (self.event_rooms or {}).get(event_id, ROOMS["a"])


@dataclass(frozen=True)
class Mask:
    """One deliberate difference."""

    observable: str
    reason: str
    normalize: Callable[[str, object, Context], object]
    affirm: Callable[[str, object, Context], None]
    scenarios: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Divergence:
    """One scenario whose comparison ends at ``step``, with the new behavior there asserted instead."""

    step: int
    reason: str
    affirm: Callable[[object, dict[str, dict[str, Any]]], None]


_RECORD_OBSERVABLES = ("turn_record", "record_for_response")
# The scenarios whose every redaction is an action; a legacy input also holds redactions no action made.
_ACTION_SCENARIOS = frozenset(
    {*(f"named/{name}" for name in NAMED), *(f"generated/seed_{seed:02d}" for seed in range(SEEDS))},
)


def _record_fields(value: object) -> dict[str, Any] | None:
    return value if isinstance(value, dict) and "sources" in value else None


def _without_inferred_attribution(_key: str, value: object, _context: Context) -> object:
    record = _record_fields(value)
    if record is None or "metadata" not in record:
        return value
    metadata, sources = record["metadata"], record["sources"]
    requester_only = len(sources) == 1 and metadata == {sources[0]: {"sender": record.get("requester")}}
    if metadata == {} or requester_only:
        return {name: field for name, field in record.items() if name != "metadata"}
    return value


def _attribution_is_a_map(_key: str, value: object, _context: Context) -> None:
    record = _record_fields(value)
    if record is not None:
        assert isinstance(record.get("metadata"), dict), f"a turn's attribution is always a map: {record}"


def _without_echo_response(_key: str, value: object, _context: Context) -> object:
    record = _record_fields(value)
    if record is None or record.get("completed") or record.get("response") is None:
        return value
    if record.get("response") == record.get("echo"):
        return {name: field for name, field in record.items() if name != "response"}
    return value


def _echo_is_never_an_answer(_key: str, value: object, _context: Context) -> None:
    record = _record_fields(value)
    if record is not None and not record.get("completed") and record.get("echo") is not None:
        assert record.get("response") != record["echo"], f"an echo became an unfinished turn's response: {record}"


def _without_requester_attribution(_key: str, value: object, _context: Context) -> object:
    """Drop source attributions that only name the turn's requester, which a reshape now carries along."""
    record = _record_fields(value)
    if record is None or not isinstance(record.get("metadata"), dict):
        return value
    kept = {
        source: entry for source, entry in record["metadata"].items() if entry != {"sender": record.get("requester")}
    }
    normalized = {name: field for name, field in record.items() if name != "metadata"}
    return {**normalized, "metadata": kept} if kept else normalized


def _every_attribution_names_a_sender(_key: str, value: object, _context: Context) -> None:
    record = _record_fields(value)
    if record is not None:
        for source, entry in (record.get("metadata") or {}).items():
            assert source in {*record["sources"], *record.get("aliases", ())}, f"foreign attribution: {record}"
            assert "sender" in entry, f"attribution without a sender: {record}"


def _without_echo(_key: str, value: object, _context: Context) -> object:
    """Drop a record's echo: it is a fact of each source it was published for, read separately."""
    record = _record_fields(value)
    if record is None:
        return value
    return {name: field for name, field in record.items() if name not in {"echo", "echo_fallback"}}


def _an_echo_belongs_to_one_of_the_turns_sources(_key: str, value: object, context: Context) -> None:
    record = _record_fields(value)
    if record is not None and record.get("echo") is not None:
        published = {(context.echoes or {}).get(source) for source in record["sources"]}
        assert record["echo"] in published, f"an echo not published for any of the turn's sources: {record}"


def _latest_echo_of_the_source(key: str, value: object, context: Context) -> object:
    """Read a source's echo as the latest one published for it: an echo no longer leaks to its batch siblings."""
    return (context.echoes or {}).get(key) if value is not None or key in (context.echoes or {}) else value


def _an_echo_is_its_own_sources(key: str, value: object, context: Context) -> None:
    assert value in {None, (context.echoes or {}).get(key)}, f"{key} reads an echo published for another source"


def _no_echo_lookup(key: str, value: object, context: Context) -> object:
    return None if key in context.published_echoes else value


def _an_echo_reads_the_turn_of_its_source(key: str, value: object, context: Context) -> None:
    record = _record_fields(value)
    if record is not None and key in context.published_echoes:
        sources = {source for source, echo in (context.echoes or {}).items() if echo == key}
        assert sources & set(record["sources"]), f"echo {key} reads a turn none of its sources is in: {record}"


def _revisions_by_physical_source(_key: str, value: object, _context: Context) -> object:
    """Key each selected revision by the physical source it was taken for, not the alias that named it."""
    record = _record_fields(value)
    if record is None or "revisions" not in record:
        return value
    alias_source = {
        entry["discovery_event_id"]: source
        for source, entry in (record.get("metadata") or {}).items()
        if entry.get("discovery_event_id")
    }
    revisions = {alias_source.get(source, source): revision for source, revision in record["revisions"].items()}
    return {**record, "revisions": revisions}


def _revisions_name_physical_sources(_key: str, value: object, _context: Context) -> None:
    record = _record_fields(value)
    if record is not None:
        assert set(record.get("revisions") or {}) <= set(record["sources"]), f"a revision keyed by an alias: {record}"


def _current_turn(key: str, context: Context) -> dict[str, Any] | None:
    return _record_fields((context.state or {}).get("turn_record", {}).get(key))


def _keeper(record: dict[str, Any] | None, key: str) -> bool:
    """Return whether ``record`` is what a detached source keeps for its echo or checkpoint: itself, nothing else."""
    return (
        record is not None
        and record["sources"] == [key]
        and not record.get("completed")
        and not any(field in record for field in ("target", "prompts", "response"))
    )


def _bare(record: dict[str, Any]) -> bool:
    return not any(record.get(field) for field in ("prompts", "prepared_voice", "response", "echo"))


def _without_stale_batch(key: str, value: object, context: Context) -> object:
    """Read a source detached from its unfinished batch as the store now keeps it: no turn, or a keeper.

    The old store left the stale batch, or a bare record of the source alone, readable under it.
    """
    record = _record_fields(value)
    if record is None or record.get("completed"):
        return value
    current = _current_turn(key, context)
    stale = bool(set(record["sources"]) - {key}) or (record["sources"] == [key] and _bare(record))
    if current is None and stale:
        return None
    if _keeper(current, key) and (stale or _keeper(record, key)):
        return "keeper"
    return value


def _a_detached_source_keeps_nothing_it_needs(key: str, value: object, context: Context) -> None:
    if value is None:
        state = context.state or {}
        assert state.get("prepared_voice", {}).get(key) is None, f"{key} lost the turn of its voice checkpoint"
        assert state.get("visible_echo", {}).get(key) is None, f"{key} lost the turn of its echo"


def _no_intent_without_a_turn(key: str, value: object, context: Context) -> object:
    current = _current_turn(key, context)
    return None if current is None or "target" not in current else value


def _a_source_without_context_owes_no_answer(key: str, value: object, context: Context) -> None:
    current = _current_turn(key, context)
    if current is None or "target" not in current:
        assert not value, f"{key} owes an answer without a turn that holds its context"


def _finalized_published_echo(key: str, value: object, context: Context) -> object:
    return None if key in (context.echoes or {}) else value


def _a_finalization_names_the_published_echo(key: str, value: object, context: Context) -> None:
    if isinstance(value, dict):
        assert value["event_id"] == (context.echoes or {}).get(key), f"{key} finalized an echo it never published"


def _without_suppressed_revisions(_key: str, value: object, _context: Context) -> object:
    record = _record_fields(value)
    if record is None:
        return value
    return {name: field for name, field in record.items() if name != "suppressed_revisions"}


def _no_suppressed_revisions(_key: str, value: object, _context: Context) -> None:
    record = _record_fields(value)
    assert record is None or "suppressed_revisions" not in record, f"a retired field came back: {record}"


def _refused_across_rooms(outcome: object, _state: dict[str, dict[str, Any]]) -> None:
    assert isinstance(outcome, dict), outcome
    assert outcome["error"] == "ValueError", outcome
    message = outcome["message"]
    assert "another room" in message or "cannot be attached to a turn in" in message, outcome


def _record(state: dict[str, dict[str, Any]], key: str) -> dict[str, Any]:
    record = _record_fields(state.get("turn_record", {}).get(key))
    assert record is not None, f"no turn reads {key}"
    return record


def _affirm_all(
    *checks: Callable[[object, dict[str, dict[str, Any]]], None],
) -> Callable[[object, dict[str, dict[str, Any]]], None]:
    def affirm(outcome: object, state: dict[str, dict[str, Any]]) -> None:
        for check in checks:
            check(outcome, state)

    return affirm


def _no_voice(source: str) -> Callable[[object, dict[str, dict[str, Any]]], None]:
    def affirm(_outcome: object, state: dict[str, dict[str, Any]]) -> None:
        assert source not in state.get("prepared_voice", {}), f"{source} kept its transcript"

    return affirm


def _prompt(key: str, source: str, prompt: str) -> Callable[[object, dict[str, dict[str, Any]]], None]:
    def affirm(_outcome: object, state: dict[str, dict[str, Any]]) -> None:
        assert _record(state, key).get("prompts", {}).get(source) == prompt, _record(state, key)

    return affirm


def _sources(key: str, sources: list[str]) -> Callable[[object, dict[str, dict[str, Any]]], None]:
    def affirm(_outcome: object, state: dict[str, dict[str, Any]]) -> None:
        assert _record(state, key)["sources"] == sources, _record(state, key)

    return affirm


def _handled(*event_ids: str) -> Callable[[object, dict[str, dict[str, Any]]], None]:
    def affirm(_outcome: object, state: dict[str, dict[str, Any]]) -> None:
        for event_id in event_ids:
            assert state.get("is_handled", {}).get(event_id) is True, f"{event_id} is not handled"

    return affirm


def _not_handled(*event_ids: str) -> Callable[[object, dict[str, dict[str, Any]]], None]:
    def affirm(_outcome: object, state: dict[str, dict[str, Any]]) -> None:
        for event_id in event_ids:
            assert event_id not in state.get("is_handled", {}), f"{event_id} is handled"

    return affirm


def _redacted_in(room: str, *event_ids: str) -> Callable[[object, dict[str, dict[str, Any]]], None]:
    def affirm(_outcome: object, state: dict[str, dict[str, Any]]) -> None:
        reported = set(state.get("redacted_event_ids", {}).get(room) or ())
        assert set(event_ids) <= reported, f"room {room} reports {sorted(reported)}"

    return affirm


def _deleted_sources(record: dict[str, Any], redacted: frozenset[str]) -> set[str]:
    """Return the record's sources deleted directly or through the message they answer for."""
    metadata = record.get("metadata") or {}
    return {
        source
        for source in record["sources"]
        if source in redacted or (metadata.get(source) or {}).get("discovery_event_id") in redacted
    }


def _as_after_redaction(_key: str, value: object, context: Context) -> object:
    """Return a record as it reads once every redaction the store reports in its room has taken effect on it."""
    record = _record_fields(value)
    if record is None:
        return value
    redacted = context.redacted_for(record)
    normalized = {name: field for name, field in record.items() if name != "redacted"}
    if "replay" in normalized:
        normalized["replay"] = {
            revision_id: {name: field for name, field in entry.items() if name != "redacted"}
            for revision_id, entry in normalized["replay"].items()
        }
    deleted = _deleted_sources(record, redacted)
    revision_deleted = {
        source for source, revision in (normalized.get("revisions") or {}).items() if revision[1] in redacted
    }
    for field in ("prompts", "prepared_voice"):
        if field in normalized:
            gone = deleted | revision_deleted if field == "prompts" else deleted
            kept = {source: item for source, item in normalized[field].items() if source not in gone}
            normalized[field] = kept
            if not kept:
                del normalized[field]
    if "revisions" in normalized:
        kept_revisions = {
            source: revision
            for source, revision in normalized["revisions"].items()
            if source not in deleted and revision[1] not in redacted
        }
        normalized["revisions"] = kept_revisions
        if not kept_revisions:
            del normalized["revisions"]
    return normalized


def _redaction_reads_back_from_its_record(_key: str, value: object, context: Context) -> None:
    record = _record_fields(value)
    if record is None:
        return
    redacted = context.redacted_for(record)
    indexed = {*record["sources"], *record.get("aliases", ())}
    reported = set(record.get("redacted", []))
    assert reported == indexed & redacted, f"redaction not read back: {record}"
    deleted = _deleted_sources(record, redacted)
    for field in ("prompts", "prepared_voice"):
        assert not deleted & set(record.get(field) or {}), f"a deleted source kept its {field}: {record}"
    for revision_id, entry in (record.get("replay") or {}).items():
        assert entry["redacted"] is (revision_id in redacted), f"revision {revision_id} misreported: {record}"


def _without_redaction_only_record(key: str, value: object, context: Context) -> object:
    """Drop a record whose only content was a redaction of its own event: a redaction alone writes no turn."""
    record = _record_fields(value)
    if record is None or key not in context.redacted:
        return value
    bare = {name: field for name, field in record.items() if name != "redacted"}
    return None if bare == {"anchor": key, "completed": False, "sources": [key]} else value


def _a_redacted_event_keeps_no_plaintext(key: str, value: object, context: Context) -> None:
    record = _record_fields(value)
    if record is not None and key in context.redacted_for(record) and key in record["sources"]:
        for field in ("prompts", "prepared_voice"):
            assert key not in (record.get(field) or {}), f"redacted {key} kept its {field}: {record}"


def _redacted_in_its_own_room(key: str, value: object, context: Context) -> object:
    """Keep only the events a scenario redacted in this room: a redaction is read in the room it was sent in."""
    if not isinstance(value, list):
        return value
    kept = [event_id for event_id in value if (key, event_id) in context.redactions]
    return kept or None


def _only_its_own_rooms_redactions_read_back(key: str, value: object, context: Context) -> None:
    if isinstance(value, list):
        foreign = [event_id for event_id in value if (key, event_id) not in context.redactions]
        assert not foreign, f"room {key} reports redactions sent elsewhere: {foreign}"


def _edits_a_redacted_revision(context: Context) -> bool:
    """Return whether this step commits an edit whose revision is redacted in its source's room."""
    action = context.action
    if action is None or action.get("op") != "edit" or not action.get("commit"):
        return False
    room = context.room_of(action["source"])
    return room is not None and action["edit"] in context.redacted_in.get(room, frozenset())


def _edit_outcome(_key: str, value: object, context: Context) -> object:
    if not _edits_a_redacted_revision(context):
        return value
    return "redacted revision" if value in {"ok", "redacted"} else value


def _an_edit_of_a_redacted_revision_is_refused(_key: str, value: object, context: Context) -> None:
    if _edits_a_redacted_revision(context):
        assert value == "redacted", f"an edit of a redacted revision was taken: {value}"


def _prepares_over_a_redacted_history(context: Context) -> bool:
    """Return whether this step prepares a reply over a history that still shows an event redacted in its room."""
    action = context.action
    if action is None or action.get("op") != "prepare":
        return False
    redacted = context.redacted_in.get(ROOMS[action["room"]], frozenset())
    return any(event_id in redacted or latest in redacted for event_id, latest, _ts in action.get("history", ()))


def _prepare_outcome(_key: str, value: object, context: Context) -> object:
    if not _prepares_over_a_redacted_history(context):
        return value
    retried = isinstance(value, dict) and value.get("error") == "RevisionSnapshotChangedError"
    return "proceed" if retried else value


def _a_stale_history_is_prepared_again(_key: str, value: object, context: Context) -> None:
    if _prepares_over_a_redacted_history(context):
        assert value != "proceed", "a reply proceeded over a history that still shows a redacted event"


def _redacted_event_is_deleted(key: str, value: object, context: Context) -> object:
    return "deleted" if key in context.redacted else value


def _a_redacted_event_is_handled(key: str, value: object, context: Context) -> None:
    if key in context.redacted:
        assert value is True, f"redacted {key} is not handled"


def _no_checkpoint_for_a_redacted_source(key: str, value: object, context: Context) -> object:
    return None if key in context.redacted else value


def _a_redacted_source_keeps_no_checkpoint(key: str, value: object, context: Context) -> None:
    if key in context.redacted:
        assert value is None, f"redacted {key} kept a voice checkpoint"


def _redaction_outcome(_key: str, value: object, context: Context) -> object:
    if context.action is None or context.action.get("op") != "redact":
        return value
    return "recorded" if value in {"ignored", "tombstoned"} else value


def _a_live_redaction_is_recorded(_key: str, value: object, context: Context) -> None:
    if context.action is not None and context.action.get("op") == "redact" and context.action.get("via") != "journal":
        assert value == "tombstoned", f"a live redaction was not recorded: {value}"


_DETACHED = (
    "A source left out of a re-formed unfinished batch, or a sibling of a source answered on its own, loses its "
    "turn unless it holds a voice checkpoint or an echo; it answers again as its own turn. The old store left the "
    "stale batch readable, and pending, under it."
)

_REDACTION_AT_ADMISSION = (
    "A redaction is recorded once for the agent when the journal admits it and takes effect there, whether it "
    "arrives live or from history, and whether or not a turn already holds the event; it used to wait for the "
    "live callback to find the event's record. The redacted event reads handled, and a turn reads its deletion "
    "back, its plaintext scrubbed, from then on."
)


MASKS: tuple[Mask, ...] = (
    *(
        Mask(
            observable,
            "Attribution is stored per source: a turn always reads back a sender map, and a one-source turn written "
            "without one names its requester (the old singleton fallback, now materialized); a coalesced turn without "
            "one names nobody, as {} did.",
            _without_inferred_attribution,
            _attribution_is_a_map,
        )
        for observable in _RECORD_OBSERVABLES
    ),
    *(
        Mask(
            observable,
            "Publishing a voice echo never becomes the answer of an unfinished turn; only a turn's own response or "
            "placeholder is its response.",
            _without_echo_response,
            _echo_is_never_an_answer,
        )
        for observable in _RECORD_OBSERVABLES
    ),
    *(
        Mask(observable, _REDACTION_AT_ADMISSION, _as_after_redaction, _redaction_reads_back_from_its_record)
        for observable in _RECORD_OBSERVABLES
    ),
    Mask("is_handled", _REDACTION_AT_ADMISSION, _redacted_event_is_deleted, _a_redacted_event_is_handled),
    Mask(
        "prepared_voice",
        _REDACTION_AT_ADMISSION,
        _no_checkpoint_for_a_redacted_source,
        _a_redacted_source_keeps_no_checkpoint,
    ),
    Mask("outcome", _REDACTION_AT_ADMISSION, _redaction_outcome, _a_live_redaction_is_recorded),
    Mask(
        "turn_record",
        "A redaction of an event no turn holds writes no turn: the agent's redaction record alone makes the event "
        "read handled and deleted, where the old store wrote a bare tombstone record for it.",
        _without_redaction_only_record,
        _a_redacted_event_keeps_no_plaintext,
    ),
    Mask(
        "redacted_event_ids",
        "A redaction is read only in the room it was sent in; the old store also reported an event redacted in a "
        "room whose turn held a tombstone of it from another room.",
        _redacted_in_its_own_room,
        _only_its_own_rooms_redactions_read_back,
        _ACTION_SCENARIOS,
    ),
    *(
        Mask(
            observable,
            "Proven attribution is stored per source and kept when a batch re-forms, so a source keeps naming its "
            "requester where the old merge dropped the proof. A coalesced turn without any proof still names "
            "nobody, which the fail-closed scenario keeps pinning.",
            _without_requester_attribution,
            _every_attribution_names_a_sender,
            _ACTION_SCENARIOS - {"named/50d89d0e53_unattributed_coalesced_turns_fail_closed"},
        )
        for observable in _RECORD_OBSERVABLES
    ),
    *(
        Mask(
            observable,
            "An echo is a fact of the voice source it was published for and survives a batch re-forming; the old "
            "turn-wide echo leaked to every source of a batch and was lost when the batch re-formed.",
            _without_echo,
            _an_echo_belongs_to_one_of_the_turns_sources,
            _ACTION_SCENARIOS,
        )
        for observable in _RECORD_OBSERVABLES
    ),
    Mask(
        "visible_echo",
        "An echo is a fact of the voice source it was published for and survives a batch re-forming; the old "
        "turn-wide echo leaked to every source of a batch and was lost when the batch re-formed.",
        _latest_echo_of_the_source,
        _an_echo_is_its_own_sources,
        _ACTION_SCENARIOS,
    ),
    Mask(
        "record_for_response",
        "An echo reads the turn of the source it was published for; with echoes per source that turn is the one "
        "that source belongs to now, not the batch the echo was first recorded on.",
        _no_echo_lookup,
        _an_echo_reads_the_turn_of_its_source,
        _ACTION_SCENARIOS,
    ),
    *(
        Mask(
            observable,
            "A selected revision is recorded for the physical source it edits, also when the edit names the relay's "
            "original as its alias; the old record kept the alias as the key.",
            _revisions_by_physical_source,
            _revisions_name_physical_sources,
        )
        for observable in _RECORD_OBSERVABLES
    ),
    Mask(
        "turn_record",
        _DETACHED,
        _without_stale_batch,
        _a_detached_source_keeps_nothing_it_needs,
        _ACTION_SCENARIOS,
    ),
    Mask(
        "pending_intent",
        _DETACHED,
        _no_intent_without_a_turn,
        _a_source_without_context_owes_no_answer,
        _ACTION_SCENARIOS,
    ),
    *(
        Mask(
            observable,
            "``suppressed_revisions`` is a retired field no current record carries; the conversion drops it.",
            _without_suppressed_revisions,
            _no_suppressed_revisions,
        )
        for observable in _RECORD_OBSERVABLES
    ),
    Mask(
        "finalized_echo",
        "An echo is a fact of the voice source it was published for, so its replacement is recorded even after the "
        "source's batch re-formed; the old store had lost the echo with the batch and recorded nothing.",
        _finalized_published_echo,
        _a_finalization_names_the_published_echo,
        _ACTION_SCENARIOS,
    ),
    Mask(
        "outcome",
        "A reply prepared over a history that still shows an event redacted in its room retries its preparation: "
        "the snapshot predates the redaction, so it would carry the deleted text. The old store proceeded and left "
        "the next response to reconcile it.",
        _prepare_outcome,
        _a_stale_history_is_prepared_again,
    ),
    Mask(
        "outcome",
        "An edit whose revision is already redacted is refused: its text never becomes a turn's prompt, where the "
        "old store took it and dropped it when the redaction was reconciled.",
        _edit_outcome,
        _an_edit_of_a_redacted_revision_is_refused,
    ),
)


_CROSS_ROOM = (
    "The generator names sources from both rooms in one turn, which Matrix never produces: an event belongs to one "
    "room. A turn is recorded in one room, so the store refuses a write that names another, and everything after "
    "differs for that reason alone."
)
_RETENTION_CLEARS_TRANSCRIPTS = (
    "Retention clears the transcript of a voice checkpoint whose source no longer owes an answer, so no plaintext "
    "outlives the turn that needed it; the old store kept it until the turn itself was dropped."
)
_PROMPT_FOLLOWS_ITS_REVISION = (
    "A source's prompt follows its selected revision: a re-formed batch, or a later pending write that carries the "
    "original text, keeps the newer edit and its watermark. The old store took the later write's older text."
)
_CONVERSION = (
    "Converting the stored records to turn rows applies the documented rules: disagreeing rows of one turn resolve "
    "to their owners, a room-less record keeps only its completion proof, a redaction is recorded once for the agent "
    "in its own room, and revision tombstones become the agent's redactions. test_turn_rows_conversion pins each rule; "
    "the converted state affirmed here differs from the old reader's from the start."
)

DIVERGENCES: dict[str, Divergence] = {
    **{
        f"generated/seed_{seed:02d}": Divergence(step, _CROSS_ROOM, _refused_across_rooms)
        for seed, step in ((0, 14), (2, 7), (4, 21), (6, 2), (9, 21), (10, 19), (12, 24), (16, 23), (18, 11))
    },
    "generated/seed_08": Divergence(15, _RETENTION_CLEARS_TRANSCRIPTS, _no_voice("$m3")),
    "named/bb62aea414_voice_checkpoint_survives_merges": Divergence(
        19,
        _RETENTION_CLEARS_TRANSCRIPTS,
        _no_voice("$m3"),
    ),
    "generated/seed_11": Divergence(
        9,
        _PROMPT_FOLLOWS_ITS_REVISION + " The pending write of $m2 alone re-formed the batch to exactly that source.",
        _affirm_all(_sources("$m2", ["$m2"]), _prompt("$m2", "$m2", "$m2 as of $e3")),
    ),
    "generated/seed_15": Divergence(11, _PROMPT_FOLLOWS_ITS_REVISION, _prompt("$m1", "$m2", "$m2 as of $e3")),
    "generated/seed_17": Divergence(14, _PROMPT_FOLLOWS_ITS_REVISION, _prompt("$m2", "$m2", "$m2 as of $e3")),
    "generated/seed_14": Divergence(
        7,
        "A source answered on its own leaves its batch together with its revisions: $m5's turn no longer carries "
        "$m2's edit, which the old record kept.",
        lambda _outcome, state: _record_without_revision(state, "$m5", "$e3"),
    ),
    "generated/seed_19": Divergence(
        9,
        "A re-formed batch takes over the placeholder of the unfinished turn it absorbs, which leaves that turn, so "
        "one turn owns the response; the old store kept both and could not say which turn owned it.",
        lambda _outcome, state: _record_for_response(state, "$p5", ["$m4"]),
    ),
    "named/f5aaea2b0e_late_write_keeps_committed_delivery": Divergence(
        3,
        "The FINAL acknowledgement completes the turn its answer was bound to in its own transaction, so there is no "
        "window after the commit in which the turn still reads unfinished, and no publication to lose.",
        _affirm_all(_handled("$m1"), lambda _outcome, state: _completed(state, "$m1")),
    ),
    "named/5197054f5c_conversation_tombstones_outlive_retention": Divergence(
        14,
        "Redactions are the agent's own records, kept apart from turns, so they outlive retention without holding "
        "an answered turn back: the old store kept $m5's turn because its revision's tombstone lived on it.",
        _affirm_all(_redacted_in("a", "$m1", "$m3", "$m4", "$e5"), _not_handled("$m5")),
    ),
    "legacy/cleanup_obligations": Divergence(0, _CONVERSION, _redacted_in("a", "$e2", "$m2")),
    "legacy/legacy_summary_provenance": Divergence(0, _CONVERSION, _redacted_in("a", "$e1", "$e2")),
    "legacy/room_less_tombstone": Divergence(
        0,
        _CONVERSION,
        _affirm_all(_redacted_in("a", "$x1", "$x2"), _not_handled("$m6")),
    ),
    "legacy/coalesced_rows_disagree": Divergence(0, _CONVERSION, _handled("$m4", "$m7")),
    "legacy/pre_journal_ledger": Divergence(0, _CONVERSION, _not_handled("$m5")),
    "legacy/no_revision_replay_key": Divergence(
        4,
        "A redaction is read only in the room it was sent in; the old store also reported a revision redacted in "
        "room a as redacted in room b.",
        _redacted_in("a", "$e1"),
    ),
}


def _record_without_revision(state: dict[str, dict[str, Any]], key: str, revision: str) -> None:
    assert revision not in (_record(state, key).get("replay") or {}), _record(state, key)


def _record_for_response(state: dict[str, dict[str, Any]], response: str, sources: list[str]) -> None:
    record = _record_fields(state.get("record_for_response", {}).get(response))
    assert record is not None, state.get("record_for_response", {}).get(response)
    assert record["sources"] == sources, record


def _completed(state: dict[str, dict[str, Any]], key: str) -> None:
    assert _record(state, key)["completed"] is True, _record(state, key)


def masked(
    scenario: str,
    observable: str,
    key: str,
    value: object,
    context: Context,
    *,
    current: bool = False,
) -> object:
    """Return ``value`` as every mask covering this scenario and observable compares it, affirming a current one."""
    for mask in MASKS:
        if mask.observable == observable and (not mask.scenarios or scenario in mask.scenarios):
            if current:
                mask.affirm(key, value, context)
            value = mask.normalize(key, value, context)
    return value
