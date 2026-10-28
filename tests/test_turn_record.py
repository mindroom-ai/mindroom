"""The immutable turn value: canonical candidates for writes, and the facts read back from rows."""

from __future__ import annotations

import pytest

from mindroom.history.types import HistoryScope
from mindroom.message_target import MessageTarget
from mindroom.turn_record import SourceEventMetadata, TurnRecord, canonicalize_turn_record


def test_turn_record_normalizes_ids_and_prompt_map() -> None:
    """A candidate drops empty and repeated ids, empty response ids, and prompts of events it does not own."""
    handled_turn = TurnRecord.create(
        ["$a", "", "$a", "$b"],
        response_event_id="",
        source_event_prompts={"$a": "prompt a", "$extra": "ignored"},
    )

    assert handled_turn.source_event_ids == ("$a", "$b")
    assert handled_turn.response_event_id is None
    assert handled_turn.source_event_prompts == {"$a": "prompt a"}
    assert handled_turn.anchor_event_id == "$b"
    assert handled_turn.is_coalesced
    assert not TurnRecord.create(["$single"]).is_coalesced


def test_turn_record_create_normalizes_coupled_source_state() -> None:
    """An alias that is also a source stays a source, and a malformed selection is dropped."""
    record = TurnRecord.create(
        ["source", "source", ""],
        discovery_event_ids=["source", "edit", "edit"],
        source_event_prompts={"source": "prompt"},
        source_event_revisions={"edit": [4, ""]},
    )

    assert record.source_event_ids == ("source",)
    assert record.discovery_event_ids == ("edit",)
    assert record.source_event_prompts == {"source": "prompt"}
    assert record.source_event_revisions is None


def test_canonicalize_turn_record_links_command_result_to_started_checkpoint() -> None:
    """A stored command result implies that execution started."""
    record = TurnRecord.create(["source"], command_execution_started=False)

    updated = canonicalize_turn_record(record, command_result_text="done")

    assert updated.command_execution_started is True
    assert updated.command_result_text == "done"


def test_turn_record_preserves_response_context() -> None:
    """Response owner, history scope, and conversation target stay intact."""
    conversation_target = MessageTarget.resolve(
        room_id="!room:example.com",
        thread_id="$thread:example.com",
        reply_to_event_id="$reply:example.com",
    )
    history_scope = HistoryScope(kind="team", scope_id="team_scope")

    handled_turn = TurnRecord.create(
        ["$event:example.com"],
        response_owner="test_agent",
        history_scope=history_scope,
        conversation_target=conversation_target,
    )

    assert handled_turn.response_owner == "test_agent"
    assert handled_turn.history_scope == history_scope
    assert handled_turn.conversation_target == conversation_target


@pytest.mark.parametrize(
    ("record", "deleted", "expected"),
    [
        (TurnRecord.create(["$single"], requester_id="@bob:localhost"), (), True),
        (TurnRecord.create(["$single"]), (), False),
        (
            TurnRecord.create(
                ["$first", "$second"],
                source_event_metadata={
                    "$first": SourceEventMetadata(sender="@bob:localhost"),
                    "$second": SourceEventMetadata(sender="@bob:localhost"),
                },
            ),
            (),
            True,
        ),
        (
            TurnRecord.create(
                ["$first", "$second"],
                source_event_metadata={
                    "$first": SourceEventMetadata(sender="@alice:localhost"),
                    "$second": SourceEventMetadata(sender="@bob:localhost"),
                },
            ),
            (),
            False,
        ),
        (
            TurnRecord.create(
                ["$first", "$second"],
                source_event_metadata={
                    "$first": SourceEventMetadata(sender="@alice:localhost"),
                    "$second": SourceEventMetadata(sender="@bob:localhost"),
                },
            ),
            ("$first",),
            True,
        ),
        (TurnRecord.create(["$first"], requester_id="@bob:localhost"), ("$first",), False),
    ],
)
def test_turn_record_proves_all_live_sources_belong_to_requester(
    record: TurnRecord,
    deleted: tuple[str, ...],
    *,
    expected: bool,
) -> None:
    """Whole-turn decisions fail closed unless every source nobody deleted has the requester."""
    assert record.sources_all_from_requester("@bob:localhost", deleted=deleted) is expected
