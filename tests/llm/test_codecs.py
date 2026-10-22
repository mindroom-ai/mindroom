"""Dict codecs of ``mindroom.llm`` reproduce the shapes Agno 3.0.9 wrote.

Every expected dict here is one Agno 3.0.9 wrote: ``tests/fixtures/agno_parity/codecs.json`` holds the values real
Agno objects produced, and the stored-run fixtures under ``tests/fixtures/agno_parity/runs`` hold the values MindRoom
stored through Agno, so the owned readers and writers are proven against Agno's own ``to_dict``; the stored data they
must keep decoding was written that way.
Keys the owned records deliberately drop are listed once per type and removed before comparing.
"""
# ruff: noqa: D103

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.llm.media import Audio, File, Image, Video
from mindroom.llm.messages import Message
from mindroom.llm.runs import (
    RunRecord,
    RunRequirement,
    RunStatus,
    TeamRunRecord,
    ToolExecution,
    run_record_from_dict,
)
from mindroom.llm.usage import MessageMetrics, ModelMetrics, RunMetrics, SessionMetrics, ToolCallMetrics
from tests.llm.agno_dict_helpers import (
    DROPPED_RUN_KEYS,
    DROPPED_TOOL_EXECUTION_KEYS,
    expected_requirement_dict,
    expected_run_dict,
    without,
)

if TYPE_CHECKING:
    from collections.abc import Callable

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "agno_parity"
# What Agno 3.0.9 objects wrote, keyed by the case that built them; see the fixtures README.
AGNO: dict[str, Any] = json.loads((FIXTURES / "codecs.json").read_text(encoding="utf-8"))

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(32))
PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"


def agno(key: str) -> Any:  # noqa: ANN401 - a JSON value
    """Return a fresh copy of one frozen Agno value, so a test may change it."""
    return copy.deepcopy(AGNO[key])


# --- media -----------------------------------------------------------------------------------------------------

MEDIA_TYPES: dict[str, type[Image | Audio | Video | File]] = {
    "image": Image,
    "audio": Audio,
    "video": Video,
    "file": File,
}


@pytest.mark.parametrize("name", list(AGNO["media"]))
def test_media_round_trips_agno_dict(name: str) -> None:
    expected = agno("media")[name]["dict"]

    assert MEDIA_TYPES[name.split("-", maxsplit=1)[0]].from_dict(expected).to_dict() == expected


@pytest.mark.parametrize("name", list(AGNO["media"]))
def test_media_to_dict_without_content_matches_agno(name: str) -> None:
    case = agno("media")[name]

    owned = MEDIA_TYPES[name.split("-", maxsplit=1)[0]].from_dict(case["dict"])

    assert owned.to_dict(include_content=False) == case["dict_without_content"]


def test_media_from_dict_decodes_content_bytes_as_agno_did() -> None:
    image = Image.from_dict(agno("media_content_image"))
    text_file = File.from_dict(agno("media_content_text_file"))
    binary_file = File.from_dict(agno("media_content_binary_file"))

    assert image.content == PNG_BYTES
    assert text_file.content == b"plain"
    assert binary_file.content == PDF_BYTES


def test_media_from_dict_ignores_agno_only_keys() -> None:
    image = Image.from_dict(
        {"id": "i", "url": "https://example.invalid/i.png", "media_reference": {"storage_key": "k"}},
    )
    document = File.from_dict(
        {"id": "f", "filepath": "/data/f.pdf", "external": {"uri": "gemini://f"}, "citations": True},
    )

    assert image.to_dict() == {"id": "i", "url": "https://example.invalid/i.png"}
    assert document.to_dict() == {"id": "f", "filepath": "/data/f.pdf"}


# --- metrics ---------------------------------------------------------------------------------------------------


def test_message_metrics_round_trip_agno_dict() -> None:
    expected = agno("message_metrics")

    assert MessageMetrics.from_dict(expected).to_dict() == expected


def test_message_metrics_to_dict_omits_zero_and_none_as_agno() -> None:
    owned = MessageMetrics(input_tokens=1, cost=0.0, duration=0.0, provider_metrics={})

    assert owned.to_dict() == {"input_tokens": 1, "cost": 0.0, "duration": 0.0}
    assert MessageMetrics().to_dict() == {}


def test_message_metrics_from_dict_ignores_timer_and_unknown_keys() -> None:
    assert MessageMetrics.from_dict({"input_tokens": 3, "timer": {"start": 1}, "mystery": 1}) == MessageMetrics(
        input_tokens=3,
    )


def test_message_metrics_addition_matches_agno() -> None:
    owned = MessageMetrics.from_dict(agno("message_metrics")) + MessageMetrics.from_dict(agno("message_metrics_addend"))

    assert owned.to_dict() == agno("message_metrics_sum")
    assert (MessageMetrics(input_tokens=1) + MessageMetrics()).to_dict() == {"input_tokens": 1}


def test_tool_call_metrics_round_trip_agno_dict() -> None:
    expected = agno("tool_call_metrics")

    assert ToolCallMetrics.from_dict(expected).to_dict() == expected
    assert ToolCallMetrics.from_dict({**expected, "timer": None}).to_dict() == expected


def test_run_metrics_round_trip_agno_dict() -> None:
    expected = agno("run_metrics")

    assert RunMetrics.from_dict(expected).to_dict() == expected
    assert RunMetrics().to_dict() == {}


def test_run_metrics_add_matches_agno_accumulate_model_metrics() -> None:
    reports = [
        (
            "claude-opus-5-5",
            "Anthropic",
            {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12, "cost": 0.1, "provider_metrics": {"n": 1}},
        ),
        (
            "claude-opus-5-5",
            "Anthropic",
            {
                "input_tokens": 5,
                "output_tokens": 1,
                "total_tokens": 6,
                "cache_read_tokens": 4,
                "provider_metrics": {"n": 2, "tier": "a"},
            },
        ),
        ("gpt-6-luna", "OpenAI Chat", {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}),
    ]
    usages = [MessageMetrics.from_dict(report) for _, _, report in reports]
    owned = RunMetrics()
    for (model_id, provider, _), usage in zip(reports, usages, strict=True):
        owned.add(usage, model_id=model_id, provider=provider)

    assert owned.to_dict() == agno("run_metrics_accumulated")
    assert [entry.id for entry in owned.details["model"]] == ["claude-opus-5-5", "gpt-6-luna"]  # type: ignore[index]
    # Agno aliased the request's provider_metrics into the details entry and rewrote it on the next request.
    assert usages[0].provider_metrics == {"n": 1}


def test_run_metrics_addition_matches_agno() -> None:
    owned = RunMetrics.from_dict(agno("run_metrics")) + RunMetrics.from_dict(agno("run_metrics_addend"))

    assert owned.to_dict() == agno("run_metrics_sum")
    assert (RunMetrics(input_tokens=1) + RunMetrics()).to_dict() == {"input_tokens": 1}


def test_model_metrics_round_trip_agno_dict() -> None:
    expected = agno("model_metrics")

    assert ModelMetrics.from_dict(expected).to_dict() == expected


# --- messages --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("message_id", list(AGNO["messages"]))
def test_message_round_trips_agno_dict(message_id: str) -> None:
    expected = agno("messages")[message_id]

    assert Message.from_dict(expected).to_dict() == expected


def test_message_dict_keys_match_agno_for_every_field() -> None:
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
        compressed_content="cc",
        reasoning_content="r",
        redacted_reasoning_content="rr",
        provider_data={"k": "v"},
        metrics=MessageMetrics(input_tokens=1),
        from_history=True,
        images=[Image(id="i", url="https://example.invalid/i")],
        audio=[Audio(id="a", url="https://example.invalid/a")],
        videos=[Video(id="v", url="https://example.invalid/v")],
        files=[File(id="f", url="https://example.invalid/f")],
    )

    assert owned.to_dict() == agno("message_every_field")


def test_message_from_dict_ignores_agno_only_keys() -> None:
    stored = agno("message_with_agno_only_fields")
    stored["citations"] = {"urls": [{"url": "https://example.invalid"}]}
    stored["references"] = {"query": "q", "references": []}
    stored["unexpected_future_key"] = {"nested": True}

    message = Message.from_dict(stored)

    assert message.to_dict() == {
        "id": "m",
        "content": "hi",
        "from_history": False,
        "stop_after_tool_call": False,
        "role": "assistant",
        "created_at": 1,
    }


def test_message_from_dict_keeps_media_scalars_agno_reader_dropped() -> None:
    # Agno's own reader lost these image fields; the owned one must not.
    [owned_image] = Message.from_dict(agno("message_with_media_scalars")).images or []

    assert (owned_image.detail, owned_image.alt_text, owned_image.metadata) == ("high", "cat", {"k": "v"})


def test_message_from_dict_reads_non_persisted_flags_from_model_dump_payloads() -> None:
    message = Message.from_dict(agno("message_model_dump_with_non_persisted_flags"))

    assert message.add_to_agent_memory is False


# --- tool executions and requirements --------------------------------------------------------------------------


def test_tool_execution_round_trips_agno_dict_minus_dropped_keys() -> None:
    agno_dict = agno("tool_execution")
    expected = without(agno_dict, DROPPED_TOOL_EXECUTION_KEYS)

    owned = ToolExecution.from_dict(agno_dict).to_dict()

    assert owned == expected
    assert set(agno_dict) - set(owned) == DROPPED_TOOL_EXECUTION_KEYS


def test_tool_execution_default_dict_matches_agno_minus_dropped_keys() -> None:
    owned = ToolExecution(tool_call_id="c", tool_name="t", tool_args={}, created_at=7)

    assert owned.to_dict() == without(agno("tool_execution_default"), DROPPED_TOOL_EXECUTION_KEYS)
    assert ToolExecution.from_dict({"tool_name": "t"}).created_at > 0


def test_run_requirement_round_trips_agno_dict() -> None:
    expected = expected_requirement_dict(agno("run_requirement"))

    assert RunRequirement.from_dict(expected).to_dict() == expected


@pytest.mark.parametrize("decision", ["confirm", "reject", "reject_with_note"])
def test_run_requirement_confirmation_updates_match_agno(decision: str) -> None:
    owned = RunRequirement.from_dict(agno("run_requirement"))
    if decision == "confirm":
        owned.confirm()
    elif decision == "reject":
        owned.reject()
    else:
        owned.reject("Not approved by requester")

    assert owned.to_dict() == expected_requirement_dict(agno("run_requirement_decided")[decision])
    assert owned.is_resolved() is True
    assert owned.needs_confirmation is False


def test_run_requirement_external_result_matches_agno() -> None:
    owned = RunRequirement.from_dict(agno("run_requirement_external"))
    assert owned.needs_external_execution is True

    owned.set_external_execution_result("subagent said hi")

    assert owned.to_dict() == expected_requirement_dict(agno("run_requirement_external_resolved"))
    assert owned.tool_execution.result == "subagent said hi"
    assert owned.is_resolved() is True


def test_run_requirement_from_dict_ignores_user_input_schemas() -> None:
    stored = agno("run_requirement")
    stored["user_input_schema"] = [{"name": "x", "field_type": "str", "description": None, "value": None}]
    stored["user_feedback_schema"] = [
        {"question": "q", "header": None, "multi_select": False, "selected_options": None},
    ]

    assert RunRequirement.from_dict(stored).to_dict() == expected_requirement_dict(agno("run_requirement"))


# --- run records -----------------------------------------------------------------------------------------------


def test_run_record_round_trips_agno_dict_minus_dropped_keys() -> None:
    agno_dict = agno("paused_agent_run")
    expected = expected_run_dict(agno_dict)

    owned = RunRecord.from_dict(agno_dict).to_dict()

    assert owned == expected
    assert set(agno_dict) - set(owned) <= DROPPED_RUN_KEYS
    assert {"input", "session_state", "content_type"} <= set(agno_dict)


def test_run_record_minimal_dict_matches_agno_minus_dropped_keys() -> None:
    owned = RunRecord(
        run_id="r",
        agent_id="a",
        session_id="s",
        status=RunStatus.completed,
        metrics=RunMetrics(),
        created_at=1,
    )

    assert owned.to_dict() == expected_run_dict(agno("minimal_agent_run"))
    assert owned.to_dict()["metrics"] == {}
    assert "tools" not in owned.to_dict()
    assert "requirements" not in owned.to_dict()


def test_run_record_writes_tools_requirements_and_metrics_exactly_when_set() -> None:
    # Agno's loop left ``tools = []`` on runs without tool calls and ``requirements = []`` after resolving a
    # pause; MindRoom's interrupted-turn records never set ``metrics``. Each key must survive as stored.
    agno_dict = agno("agent_run_with_empty_tools_and_requirements")
    assert (agno_dict["tools"], agno_dict["requirements"]) == ([], [])
    assert "metrics" not in agno_dict

    owned = RunRecord(run_id="r", agent_id="a", tools=[], requirements=[], created_at=1)

    assert owned.to_dict() == expected_run_dict(agno_dict)
    assert RunRecord.from_dict(agno_dict).to_dict() == expected_run_dict(agno_dict)
    assert RunRecord.from_dict(agno_dict).metrics is None
    absent = RunRecord(run_id="r", agent_id="a", created_at=1).to_dict()
    assert {"tools", "requirements", "metrics"}.isdisjoint(absent)


def test_team_run_record_round_trips_agno_dict_minus_dropped_keys() -> None:
    agno_dict = agno("team_run")
    expected = expected_run_dict(agno_dict)

    record = TeamRunRecord.from_dict(agno_dict)

    assert record.to_dict() == expected
    assert isinstance(record.member_responses[0], RunRecord)
    assert record.member_responses[0].parent_run_id == "team-run-1"
    # Agno wrote an empty ``member_responses`` list on a team run without members.
    assert TeamRunRecord(run_id="t", team_id="x", created_at=1).to_dict()["member_responses"] == []


def test_run_record_from_dict_discriminates_on_team_id() -> None:
    outer_dict = agno("team_run_with_nested_team")

    record = run_record_from_dict(outer_dict)

    assert isinstance(record, TeamRunRecord)
    assert [type(member) for member in record.member_responses] == [RunRecord, TeamRunRecord]
    assert isinstance(run_record_from_dict(agno("member_run")), RunRecord)
    assert record.to_dict() == expected_run_dict(outer_dict)
    assert isinstance(run_record_from_dict(agno("usage_only_run")), RunRecord)


def test_run_status_values_and_names_match_agno() -> None:
    assert [[status.name, status.value] for status in RunStatus] == AGNO["run_statuses"]
    for status in RunStatus:
        # ``.name`` is persisted as ``mindroom_original_status``, so the member names must match too.
        assert status.name == status.value.lower()
        assert status == str(status)


@pytest.mark.parametrize("status", ["PENDING", "REGENERATED"])
def test_run_record_reads_every_status_agno_could_store(status: str) -> None:
    agno_dict = agno("runs_by_status")[status]

    assert RunRecord.from_dict(agno_dict).to_dict() == expected_run_dict(agno_dict)


def test_run_requirement_reads_rows_agno_read_leniently() -> None:
    """A requirement without a tool execution or with an unreadable timestamp still reads, as with Agno."""
    stored = {"id": "req-9", "created_at": "not a timestamp", "confirmation": True}

    owned = RunRequirement.from_dict(stored)

    assert (owned.tool_execution.tool_name, owned.tool_execution.tool_args) == ("unknown", {})
    assert owned.created_at.tzinfo is not None
    agno_dict, owned_dict = expected_requirement_dict(agno("run_requirement_read_leniently")), owned.to_dict()
    for result in (agno_dict, owned_dict):
        del result["created_at"], result["tool_execution"]["created_at"]
    assert owned_dict == agno_dict


# Stored requirement keys beside a pending tool execution, by case.
DECISIONS_ON_THE_REQUIREMENT: dict[str, dict[str, Any]] = {
    "rejection": {"confirmation": False, "confirmation_note": "No."},
    "approval": {"confirmation": True},
    "external-result": {"external_execution_result": "done"},
    "explicit-tool-decision-wins": {
        "confirmation": False,
        "tool_execution": {"tool_name": "t", "requires_confirmation": True, "confirmed": True, "created_at": 3},
    },
}


@pytest.mark.parametrize("case", list(DECISIONS_ON_THE_REQUIREMENT))
def test_run_requirement_moves_a_decision_on_the_requirement_onto_its_tool_execution(case: str) -> None:
    data = {
        "id": "req-1",
        "created_at": "2026-01-02T03:04:05+00:00",
        "tool_execution": {"tool_name": "t", "requires_confirmation": True, "created_at": 3},
        **DECISIONS_ON_THE_REQUIREMENT[case],
    }

    owned = RunRequirement.from_dict(data).to_dict()

    assert owned == expected_requirement_dict(agno("run_requirement_decision_moved")[case])


@pytest.mark.parametrize(
    "stored",
    [{"id": "provider-file"}, {"id": "gemini-file", "external": {"uri": "gemini://f"}, "mime_type": "application/pdf"}],
    ids=["id-only", "external-only"],
)
def test_message_reads_files_agno_accepted_without_a_source(stored: dict[str, Any]) -> None:
    message = {"role": "user", "content": "see file", "files": [stored], "created_at": 1}

    [owned_file] = Message.from_dict(message).files or []

    assert owned_file.id == stored["id"]
    assert owned_file.to_dict() == {key: value for key, value in stored.items() if key != "external"}


# --- stored fixtures -------------------------------------------------------------------------------------------

FIXTURE_RUNS = FIXTURES / "runs"

# Fixture file -> (reader, expected dict for one stored case); every file under ``runs/`` has an entry.
FIXTURE_CODECS: dict[str, tuple[Callable[[dict[str, Any]], Any], Callable[[dict[str, Any]], dict[str, Any]]]] = {
    "run_output": (run_record_from_dict, expected_run_dict),
    "team_run_output": (run_record_from_dict, expected_run_dict),
    "message": (Message.from_dict, dict),
    "image": (Image.from_dict, dict),
    "audio": (Audio.from_dict, dict),
    "video": (Video.from_dict, dict),
    "file": (File.from_dict, dict),
    "tool_execution": (ToolExecution.from_dict, lambda stored: without(stored, DROPPED_TOOL_EXECUTION_KEYS)),
    "run_requirement": (RunRequirement.from_dict, expected_requirement_dict),
    "message_metrics": (MessageMetrics.from_dict, dict),
    "run_metrics": (RunMetrics.from_dict, dict),
    "model_metrics": (ModelMetrics.from_dict, dict),
    "tool_call_metrics": (ToolCallMetrics.from_dict, dict),
    "session_metrics": (SessionMetrics.from_dict, dict),
}


def test_every_stored_fixture_file_has_a_codec() -> None:
    assert {path.stem for path in FIXTURE_RUNS.glob("*.json")} == FIXTURE_CODECS.keys()


@pytest.mark.parametrize("fixture", sorted(FIXTURE_CODECS))
def test_stored_fixture_dicts_round_trip_minus_dropped_keys(fixture: str) -> None:
    read, expected_for = FIXTURE_CODECS[fixture]
    cases: dict[str, dict[str, Any]] = json.loads((FIXTURE_RUNS / f"{fixture}.json").read_text())
    assert cases

    mismatches: dict[str, list[str]] = {}
    for case, stored in cases.items():
        value = read(stored)
        if fixture in {"run_output", "team_run_output"}:
            assert type(value) is (TeamRunRecord if fixture == "team_run_output" else RunRecord), case
        written = value.to_dict()
        expected = expected_for(stored)
        if written != expected or list(written) != list(expected):
            mismatches[case] = sorted(
                key for key in set(written) | set(expected) if written.get(key) != expected.get(key)
            ) or ["<key order>"]

    assert mismatches == {}


# --- session totals --------------------------------------------------------------------------------------------


def _run_metrics_cases() -> list[dict[str, Any]]:
    return [
        agno("run_metrics"),
        agno("run_metrics_with_empty_details"),
        {},
        *json.loads((FIXTURE_RUNS / "run_metrics.json").read_text()).values(),
    ]


def test_session_metrics_count_runs_as_agno_accumulated_them() -> None:
    totals = agno("session_metrics_totals")
    owned = SessionMetrics()
    for case, agno_totals in zip(_run_metrics_cases(), totals, strict=True):
        owned.count_run(RunMetrics.from_dict(case))

        assert owned.to_dict() == agno_totals
    assert SessionMetrics.from_dict(totals[-1]).to_dict() == totals[-1]


def test_session_metrics_count_a_saved_again_run_once() -> None:
    """A run saved when it paused and again when it finished adds only its final usage."""
    paused = RunMetrics.from_dict(agno("run_metrics"))
    finished = RunMetrics.from_dict(agno("run_metrics"))
    finished.add(
        MessageMetrics(input_tokens=7, output_tokens=3, total_tokens=10, cost=0.25),
        model_id="m",
        provider="P",
    )
    once = SessionMetrics()
    once.count_run(finished)

    twice = SessionMetrics()
    twice.count_run(paused)
    twice.count_run(finished, replacing=paused)

    assert twice.input_tokens == finished.input_tokens
    # Undoing a model entry that reported an empty ``provider_metrics`` leaves that empty dict, as Agno's merge would.
    assert _without_empty_provider_metrics(twice.to_dict()) == _without_empty_provider_metrics(once.to_dict())


def _without_empty_provider_metrics(totals: dict[str, Any]) -> dict[str, Any]:
    details = {
        model_type: [{key: value for key, value in entry.items() if value != {}} for entry in entries]
        for model_type, entries in totals.get("details", {}).items()
    }
    return {**totals, "details": details}


def test_session_metrics_from_dict_ignores_unreadable_details() -> None:
    assert SessionMetrics.from_dict({"input_tokens": 3, "details": "garbage", "timer": 1}).to_dict() == {
        "input_tokens": 3,
    }
    assert SessionMetrics.from_dict({"details": {"model": "garbage"}}).to_dict() == {}
