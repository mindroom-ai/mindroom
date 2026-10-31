"""The dicts ``mindroom.llm`` values are stored as: MindRoom's own schema, and each value read back from it.

Every expected dict here is one a MindRoom release writes. The dicts Agno 3.0.9 wrote, which the same readers still
decode, are ``tests/llm/test_codecs.py``'s.
"""
# ruff: noqa: D103

from __future__ import annotations

from dataclasses import replace

import pytest

from mindroom.llm import usage as llm_usage
from mindroom.llm.media import Audio, File, Image, Video
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunRequirement, RunStatus, TeamRunRecord, ToolExecution
from mindroom.llm.usage import MessageMetrics, ModelMetrics, RunMetrics, TokenCounts

# --- media -----------------------------------------------------------------------------------------------------


def test_a_file_known_only_by_its_id_stores_its_id_and_type() -> None:
    assert File(id="provider-file", mime_type="application/pdf").to_dict() == {
        "id": "provider-file",
        "mime_type": "application/pdf",
    }


def test_media_to_dict_without_content_keeps_scalars() -> None:
    audio = Audio(id="a", content=b"x", mime_type="audio/wav", transcript="hi")

    assert audio.to_dict(include_content=False) == {
        "id": "a",
        "mime_type": "audio/wav",
        "sample_rate": 24000,
        "channels": 1,
        "transcript": "hi",
    }


def test_a_text_file_that_is_not_utf8_reads_back_as_its_bytes() -> None:
    """A ``text/*`` file whose bytes are not UTF-8 is stored as base64 that says so, and reads back as those bytes."""
    latin1 = File(id="f", content="café".encode("latin-1"), mime_type="text/plain")

    stored = latin1.to_dict()

    assert File.from_dict(stored).content == "café".encode("latin-1")
    assert stored == {"id": "f", "mime_type": "text/plain", "content": "Y2Fm6Q==", "content_encoding": "base64"}
    # UTF-8 text keeps its text form, which reads back as the same bytes.
    utf8 = File(id="f", content="café".encode(), mime_type="text/plain")
    assert utf8.to_dict() == {"id": "f", "mime_type": "text/plain", "content": "café"}
    assert File.from_dict(utf8.to_dict()).content == "café".encode()
    # Text that looks like base64 still reads back as its own bytes.
    assert File.from_dict(File(content=b"TestData", mime_type="text/plain").to_dict()).content == b"TestData"
    # Content a tool gave as text is stored as that text.
    assert File(content="plain text").to_dict()["content"] == "plain text"


def test_a_binary_file_is_stored_as_base64_and_reads_back_as_its_bytes() -> None:
    binary = File(content=b"\x00\x01", mime_type="application/octet-stream")

    assert binary.to_dict()["content"] == "AAE="
    assert File.from_dict(binary.to_dict()).content == b"\x00\x01"


# --- metrics ---------------------------------------------------------------------------------------------------


def test_message_metrics_store_only_what_they_hold() -> None:
    """Zero counters and unset values are left out; a zero duration is a value and is kept."""
    owned = MessageMetrics(tokens=TokenCounts(input_tokens=1), duration=0.0)

    assert owned.to_dict() == {"input_tokens": 1, "duration": 0.0}
    assert MessageMetrics().to_dict() == {}


def test_a_request_s_context_stores_the_counters_it_holds_after_the_rest() -> None:
    """A context reports each of its counters, so a zero one is left out and reads back as zero."""
    owned = MessageMetrics(
        tokens=TokenCounts(input_tokens=30, output_tokens=5, total_tokens=35, cache_read_tokens=900),
        duration=1.5,
        context=TokenCounts(input_tokens=8, cache_read_tokens=400),
    )

    stored = owned.to_dict()

    assert list(stored.items()) == [
        ("input_tokens", 30),
        ("output_tokens", 5),
        ("total_tokens", 35),
        ("cache_read_tokens", 900),
        ("duration", 1.5),
        ("context", {"input_tokens": 8, "cache_read_tokens": 400}),
    ]
    assert MessageMetrics.from_dict(stored) == owned


def test_message_metrics_add_counters_and_duration_and_keep_the_first_time_to_first_token() -> None:
    first = MessageMetrics(
        tokens=TokenCounts(input_tokens=120, output_tokens=30, total_tokens=150, cache_read_tokens=100),
        duration=1.25,
        time_to_first_token=0.4,
        context=TokenCounts(input_tokens=40),
    )
    second = MessageMetrics(
        tokens=TokenCounts(input_tokens=5, output_tokens=7),
        duration=0.5,
        time_to_first_token=9.0,
        context=TokenCounts(input_tokens=5, cache_write_tokens=2),
    )

    assert (first + second).to_dict() == {
        "input_tokens": 125,
        "output_tokens": 37,
        "total_tokens": 150,
        "cache_read_tokens": 100,
        "duration": 1.75,
        "time_to_first_token": 0.4,
        # The context is the later report's, as a retried request's is the answering attempt's.
        "context": {"input_tokens": 5, "cache_write_tokens": 2},
    }
    assert (MessageMetrics(tokens=TokenCounts(input_tokens=1)) + MessageMetrics()).to_dict() == {"input_tokens": 1}


def test_run_metrics_count_each_request_in_the_totals_and_under_its_model() -> None:
    reports = [
        (
            "claude-opus-5-5",
            "Anthropic",
            MessageMetrics(
                tokens=TokenCounts(input_tokens=10, output_tokens=2, total_tokens=12),
                context=TokenCounts(input_tokens=10),
            ),
        ),
        (
            "claude-opus-5-5",
            "Anthropic",
            MessageMetrics(tokens=TokenCounts(input_tokens=5, output_tokens=1, total_tokens=6, cache_read_tokens=4)),
        ),
        (
            "gpt-6-luna",
            "OpenAI Chat",
            MessageMetrics(tokens=TokenCounts(input_tokens=1, output_tokens=1, total_tokens=2)),
        ),
    ]
    owned = RunMetrics()
    for model_id, provider, usage in reports:
        owned.add(usage, model_id=model_id, provider=provider)

    # A request's context describes that request alone, so neither the totals nor a model's entry keep one.
    assert owned.to_dict() == {
        "input_tokens": 16,
        "output_tokens": 4,
        "total_tokens": 20,
        "cache_read_tokens": 4,
        "details": {
            "model": [
                {
                    "input_tokens": 15,
                    "output_tokens": 3,
                    "total_tokens": 18,
                    "cache_read_tokens": 4,
                    "id": "claude-opus-5-5",
                    "provider": "Anthropic",
                },
                {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                    "id": "gpt-6-luna",
                    "provider": "OpenAI Chat",
                },
            ],
        },
    }


def test_run_metrics_add_their_totals_and_each_model_s_entry_and_keep_the_earliest_first_token() -> None:
    def opus(input_tokens: int, **counters: int) -> ModelMetrics:
        tokens = TokenCounts(input_tokens=input_tokens, **counters)
        return ModelMetrics(tokens=tokens, id="claude-opus-5-5", provider="Anthropic")

    luna = ModelMetrics(tokens=TokenCounts(input_tokens=100), id="gpt-6-luna", provider="OpenAI Chat")
    first = RunMetrics(
        tokens=TokenCounts(input_tokens=300, output_tokens=60),
        time_to_first_token=0.3,
        duration=4.5,
        details={"model": [opus(200, output_tokens=40), luna]},
    )
    second = RunMetrics(
        tokens=TokenCounts(input_tokens=10, output_tokens=1),
        time_to_first_token=0.1,
        duration=1.0,
        details={"model": [opus(10)]},
    )

    assert first + second == RunMetrics(
        tokens=TokenCounts(input_tokens=310, output_tokens=61),
        time_to_first_token=0.1,
        duration=5.5,
        details={"model": [opus(210, output_tokens=40), luna]},
    )
    # Summing leaves both runs' entries as they were.
    assert first.details == {"model": [opus(200, output_tokens=40), luna]}
    assert (RunMetrics(tokens=TokenCounts(input_tokens=1)) + RunMetrics()).to_dict() == {"input_tokens": 1}


def test_stored_metrics_keep_a_counter_the_stored_order_omits(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stored order only orders the counters, so a counter it does not name is still stored after the rest."""
    monkeypatch.setattr(
        llm_usage,
        "_STORED_COUNTER_ORDER",
        tuple(name for name in llm_usage._STORED_COUNTER_ORDER if name != "input_tokens"),
    )
    run = RunMetrics()
    run.add(MessageMetrics(tokens=TokenCounts(input_tokens=5, output_tokens=2)), model_id="m", provider="P")

    stored = run.to_dict()

    assert list(stored) == ["output_tokens", "input_tokens", "details"]
    assert list(stored["details"]["model"][0]) == ["output_tokens", "input_tokens", "id", "provider"]
    assert RunMetrics.from_dict(stored).to_dict() == stored


# --- messages --------------------------------------------------------------------------------------------------


def test_a_message_stores_each_field_it_holds_under_its_key_and_reads_back_as_itself() -> None:
    owned = Message(
        role="assistant",
        content="x",
        id="same",
        created_at=5,
        name="n",
        tool_call_id="c",
        tool_calls=[{"id": "c"}],
        tool_name="t",
        tool_args={"a": 1},
        tool_call_error=True,
        stop_after_tool_call=True,
        reasoning_content="r",
        redacted_reasoning_content="rr",
        provider_data={"k": "v"},
        metrics=MessageMetrics(tokens=TokenCounts(input_tokens=1)),
        from_history=True,
        images=[Image(id="i", url="https://example.invalid/i")],
        audio=[Audio(id="a", url="https://example.invalid/a")],
        videos=[Video(id="v", url="https://example.invalid/v")],
        files=[File(id="f", url="https://example.invalid/f")],
    )

    stored = owned.to_dict()

    assert stored == {
        "id": "same",
        "content": "x",
        "reasoning_content": "r",
        "from_history": True,
        "stop_after_tool_call": True,
        "role": "assistant",
        "name": "n",
        "tool_call_id": "c",
        "tool_name": "t",
        "tool_args": {"a": 1},
        "tool_call_error": True,
        "tool_calls": [{"id": "c"}],
        "redacted_reasoning_content": "rr",
        "provider_data": {"k": "v"},
        "images": [{"id": "i", "url": "https://example.invalid/i"}],
        "audio": [{"id": "a", "url": "https://example.invalid/a", "sample_rate": 24000, "channels": 1}],
        "videos": [{"id": "v", "url": "https://example.invalid/v"}],
        "files": [{"id": "f", "url": "https://example.invalid/f"}],
        "metrics": {"input_tokens": 1},
        "created_at": 5,
    }
    assert Message.from_dict(stored) == owned


def test_a_message_with_only_a_role_stores_its_id_role_and_creation_time() -> None:
    assert Message.from_dict({"role": "user"}).to_dict().keys() == {"id", "role", "created_at"}


def test_message_to_dict_drops_empty_values_and_default_flags_but_keeps_false_and_zero() -> None:
    message = Message(
        role="tool",
        content="",
        tool_args={},
        tool_calls=[],
        provider_data={},
        tool_call_error=False,
        created_at=0,
        id="m",
    )

    assert message.to_dict() == {
        "id": "m",
        "content": "",
        "role": "tool",
        "tool_call_error": False,
        "created_at": 0,
    }


def test_a_message_stores_its_flags_only_when_they_differ_from_their_defaults() -> None:
    """A stop-after-call result and a history copy store their flag as True; ``from_dict`` reads an absent one as False."""
    stopped = Message(role="tool", content="done", stop_after_tool_call=True, id="t", created_at=1)
    copy = Message(role="assistant", content="earlier", from_history=True, id="a", created_at=1)

    assert stopped.to_dict() == {
        "id": "t",
        "content": "done",
        "stop_after_tool_call": True,
        "role": "tool",
        "created_at": 1,
    }
    assert copy.to_dict() == {
        "id": "a",
        "content": "earlier",
        "from_history": True,
        "role": "assistant",
        "created_at": 1,
    }
    assert [Message.from_dict(message.to_dict()) for message in (stopped, copy)] == [stopped, copy]


def test_only_context_no_history_keeps_stores_its_flag_and_reads_back_as_it() -> None:
    """A paused run's turn context stores ``add_to_agent_memory: false``; every other message stores no such key."""
    context = Message(role="user", content="<memory>likes tea</memory>", add_to_agent_memory=False)
    prompt = Message(role="user", content="Hi")

    assert context.to_dict()["add_to_agent_memory"] is False
    assert "add_to_agent_memory" not in prompt.to_dict()
    assert [Message.from_dict(message.to_dict()) for message in (context, prompt)] == [context, prompt]


# --- tool executions and the requirements read from them ------------------------------------------------------


def test_a_tool_execution_stores_its_id_name_and_the_fields_it_sets() -> None:
    owned = ToolExecution(tool_call_id="c", tool_name="t", tool_args={})

    assert owned.to_dict() == {"tool_call_id": "c", "tool_name": "t", "tool_args": {}}
    assert ToolExecution.from_dict(owned.to_dict()) == owned


def test_a_tool_execution_stores_every_set_field_under_its_stored_key_false_included() -> None:
    """A denied call keeps ``confirmed: false`` and a failed or answered one its ``tool_call_error``, unlike unset ones."""
    owned = ToolExecution(
        tool_call_id="c",
        tool_name="t",
        tool_args={"x": 1},
        is_error=False,
        result="",
        child_run_id="child",
        stop_after_call=True,
        requires_confirmation=False,
        confirmed=False,
        confirmation_note="No.",
        external_execution_required=False,
        external_execution_silent=False,
        approval_type="mindroom_policy",
    )

    assert owned.to_dict() == {
        "tool_call_id": "c",
        "tool_name": "t",
        "tool_args": {"x": 1},
        "tool_call_error": False,
        "result": "",
        "child_run_id": "child",
        "stop_after_tool_call": True,
        "requires_confirmation": False,
        "confirmed": False,
        "confirmation_note": "No.",
        "external_execution_required": False,
        "external_execution_silent": False,
        "approval_type": "mindroom_policy",
    }
    assert ToolExecution.from_dict(owned.to_dict()) == owned


def test_a_run_v2026_10_664_stored_with_every_default_reads_as_the_run_stored_now() -> None:
    """Rows that spell out the defaults, as v2026.10.664 and every earlier release wrote them, read unchanged.

    The rows written now leave those keys out, and the same reader, which v2026.10.664 shipped, reads them as that run.
    """
    earlier = {
        "run_id": "r",
        "agent_id": "code",
        "created_at": 1,
        "status": "PAUSED",
        "messages": [
            {
                "id": "u",
                "content": "Hi",
                "from_history": False,
                "stop_after_tool_call": False,
                "role": "user",
                "created_at": 1,
            },
            {
                "id": "t",
                "content": "done",
                "from_history": False,
                "stop_after_tool_call": True,
                "role": "tool",
                "tool_call_id": "c1",
                "tool_name": "stop",
                "created_at": 1,
            },
        ],
        "tools": [
            {
                "tool_call_id": "c1",
                "tool_name": "stop",
                "tool_args": {},
                "tool_call_error": False,
                "result": "done",
                "child_run_id": None,
                "stop_after_tool_call": True,
                "requires_confirmation": None,
                "confirmed": None,
                "confirmation_note": None,
                "external_execution_required": None,
                "external_execution_silent": None,
                "approval_type": None,
            },
        ],
        "requirements": [
            {
                "id": "q",
                "tool_execution": {
                    "tool_call_id": "c2",
                    "tool_name": "add",
                    "tool_args": {"a": 1},
                    "tool_call_error": None,
                    "result": None,
                    "child_run_id": None,
                    "stop_after_tool_call": False,
                    "requires_confirmation": True,
                    "confirmed": None,
                    "confirmation_note": None,
                    "external_execution_required": None,
                    "external_execution_silent": None,
                    "approval_type": "mindroom_policy",
                },
            },
        ],
    }
    now = {
        **earlier,
        "messages": [
            {"id": "u", "content": "Hi", "role": "user", "created_at": 1},
            {
                "id": "t",
                "content": "done",
                "stop_after_tool_call": True,
                "role": "tool",
                "tool_call_id": "c1",
                "tool_name": "stop",
                "created_at": 1,
            },
        ],
        "tools": [
            {
                "tool_call_id": "c1",
                "tool_name": "stop",
                "tool_args": {},
                "tool_call_error": False,
                "result": "done",
                "stop_after_tool_call": True,
            },
        ],
    }
    # Requirements are no longer stored with the run; layout version 8 folded a paused row's into its tool rows.
    del now["requirements"]

    assert RunRecord.from_dict(earlier).to_dict() == now
    assert RunRecord.from_dict(now) == RunRecord.from_dict(earlier)


def test_new_tool_executions_store_no_timing() -> None:
    stored = ToolExecution(tool_call_id="c", tool_name="t", tool_args={}).to_dict()

    assert {"metrics", "created_at"}.isdisjoint(stored)


def _member_confirmation() -> RunRequirement:
    """Return a team member's call waiting for the requester's confirmation."""
    waiting = ToolExecution(
        tool_call_id="call_2",
        tool_name="delegate",
        tool_args={"task": "x"},
        requires_confirmation=True,
    )
    return RunRequirement(
        waiting,
        member_agent_id="writer",
        member_display_name="Writer",
        member_run_id="member-run-1",
    )


def test_a_run_stores_a_paused_call_once_and_reads_its_requirement_and_decision_from_it() -> None:
    """The paused tool execution is the only stored copy of the call: its requirement and decision read from it."""
    done = ToolExecution(tool_call_id="call_1", tool_name="t", tool_args={}, result="ok")
    waiting = ToolExecution(
        tool_call_id="call_2",
        tool_name="delegate",
        tool_args={"task": "x"},
        requires_confirmation=True,
    )
    record = RunRecord(run_id="r", agent_id="writer", status=RunStatus.paused, tools=[done, waiting], created_at=1)
    [requirement] = record.requirements
    assert requirement.tool_execution is waiting

    requirement.confirm()
    stored = record.to_dict()

    assert "requirements" not in stored
    assert stored["tools"][1] == {
        "tool_call_id": "call_2",
        "tool_name": "delegate",
        "tool_args": {"task": "x"},
        "requires_confirmation": True,
        "confirmed": True,
    }
    assert RunRecord.from_dict(stored).requirements == [requirement]


def test_a_paused_team_run_waits_on_its_own_calls_then_its_paused_members_naming_each_member() -> None:
    own = ToolExecution(tool_call_id="call_1", tool_name="t", requires_confirmation=True)
    member_call = ToolExecution(tool_call_id="call_1", tool_name="delegate", requires_confirmation=True)
    member = RunRecord(
        run_id="member-run-1",
        agent_id="writer",
        agent_display_name="Writer",
        status=RunStatus.paused,
        tools=[member_call],
    )
    team = TeamRunRecord(run_id="t", team_id="x", status=RunStatus.paused, tools=[own], member_responses=[member])

    assert team.requirements == [RunRequirement(own), replace(_member_confirmation(), tool_execution=member_call)]
    # The team row stores neither the requirements nor its members, whose rows hold their own calls.
    assert {"requirements", "member_responses"} & set(team.to_dict()) == {"member_responses"}
    assert team.to_dict()["member_responses"] == [member.to_dict()]


@pytest.mark.parametrize(
    ("decision", "note"),
    [("confirm", None), ("reject", "Alice declined it."), ("reject", None), ("reject", "")],
)
def test_a_decision_is_recorded_on_the_call(decision: str, note: str | None) -> None:
    """A denial always records the note the model is told: the requester's reason, or that the call was not approved."""
    owned = _member_confirmation()
    if decision == "confirm":
        owned.confirm()
    else:
        owned.reject(note)

    approved = decision == "confirm"
    recorded_note = None if approved else note or "Not approved by requester"
    execution = owned.tool_execution
    assert owned.confirmation == approved
    assert (execution.confirmed, execution.confirmation_note) == (approved, recorded_note)
    # The call still records that it needed confirmation; a resume clears that once it acts on the decision.
    assert execution.requires_confirmation is True
    assert owned.is_resolved() is True
    assert owned.needs_confirmation is False


@pytest.mark.parametrize(
    ("reason", "note"),
    [("Alice declined it.", "Alice declined it."), (None, "Not approved by requester")],
)
def test_a_denied_requirement_gives_the_note_the_model_is_told(reason: str | None, note: str) -> None:
    """A reader of a recorded denial gets its note as a string, whether or not the requester gave a reason."""
    owned = _member_confirmation()
    owned.reject(reason)

    assert owned.denial_note == note


@pytest.mark.parametrize("decision", ["pending", "confirmed"])
def test_a_requirement_that_was_not_denied_has_no_denial_note(decision: str) -> None:
    """Reading a denial's note from a call nobody denied is a mistake, not an empty note."""
    owned = _member_confirmation()
    if decision == "confirmed":
        owned.confirm()

    with pytest.raises(ValueError, match="not a recorded denial"):
        _ = owned.denial_note


def test_an_external_result_is_recorded_on_the_call() -> None:
    delegation = ToolExecution(tool_call_id="call_3", tool_name="run_subagent", external_execution_required=True)
    owned = RunRequirement(delegation)
    assert owned.needs_external_execution is True

    owned.set_external_execution_result("subagent said hi")

    assert delegation.result == "subagent said hi"
    assert owned.is_resolved() is True


# --- run records -----------------------------------------------------------------------------------------------


def test_a_minimal_run_stores_its_ids_status_metrics_and_creation_time() -> None:
    owned = RunRecord(
        run_id="r",
        agent_id="a",
        session_id="s",
        status=RunStatus.completed,
        metrics=RunMetrics(),
        created_at=1,
    )

    assert owned.to_dict() == {
        "run_id": "r",
        "agent_id": "a",
        "session_id": "s",
        "created_at": 1,
        "status": "COMPLETED",
        "metrics": {},
        "messages": [],
    }
    assert RunRecord.from_dict(owned.to_dict()) == owned


def test_a_run_stores_tools_and_metrics_exactly_when_set() -> None:
    """``[]`` stays apart from none, so a run with no tool calls reads back as it was stored."""
    empty = RunRecord(run_id="r", agent_id="a", tools=[], created_at=1)
    absent = RunRecord(run_id="r", agent_id="a", created_at=1)

    assert empty.to_dict()["tools"] == []
    assert {"tools", "requirements", "metrics"}.isdisjoint(absent.to_dict())
    assert "requirements" not in empty.to_dict()
    # A new run stores its starting status.
    assert absent.to_dict()["status"] == "RUNNING"
    assert [RunRecord.from_dict(run.to_dict()) for run in (empty, absent)] == [empty, absent]
    # A team run always stores its member list, empty when no member run is paused with it, and no agent id.
    team = TeamRunRecord(run_id="t", team_id="x", created_at=1).to_dict()
    assert (team["member_responses"], "agent_id" in team) == ([], False)


def test_run_statuses_are_stored_by_value_and_named_in_lower_case() -> None:
    assert [[status.name, status.value] for status in RunStatus] == [
        ["running", "RUNNING"],
        ["completed", "COMPLETED"],
        ["paused", "PAUSED"],
        ["cancelled", "CANCELLED"],
        ["error", "ERROR"],
    ]
    for status in RunStatus:
        # ``.name`` is persisted as ``mindroom_original_status``, so the member names are stored values too.
        assert status.name == status.value.lower()
        assert status == str(status)
