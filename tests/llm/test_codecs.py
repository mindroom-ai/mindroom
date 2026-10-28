"""Dict codecs of ``mindroom.llm`` read the dicts Agno 3.0.9 wrote, keeping every value the owned records model.

These are legacy inputs and never change: ``tests/fixtures/agno_parity/codecs.json`` holds what real Agno objects
produced, and the stored-run fixtures under ``tests/fixtures/agno_parity/runs`` hold what MindRoom stored through Agno.
Each one read and written back is the dict it was, minus the keys the owned records deliberately drop, which
``tests.llm.agno_dict_helpers`` lists once per type. The schema MindRoom itself writes is
``tests/llm/test_stored_dicts.py``'s.
"""
# ruff: noqa: D103

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.history.message_content import render_message_content
from mindroom.llm import usage as llm_usage
from mindroom.llm.media import Audio, File, Image, Video
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunRequirement, RunStatus, TeamRunRecord, ToolExecution
from mindroom.llm.usage import MessageMetrics, ModelMetrics, RunMetrics, SessionMetrics, TokenCounts
from tests.llm.agno_dict_helpers import (
    DROPPED_MESSAGE_KEYS,
    DROPPED_METRICS_KEYS,
    DROPPED_RUN_KEYS,
    DROPPED_TOOL_EXECUTION_KEYS,
    expected_requirement_dict,
    expected_run_dict,
    stored_run_record,
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


def test_a_text_file_stored_before_its_encoding_was_recorded_reads_as_before() -> None:
    """An unmarked ``text/*`` row reads as UTF-8 text, so an earlier non-UTF-8 file still reads as its base64 text."""
    earlier = File.from_dict({"id": "f", "mime_type": "text/plain", "content": "Y2Fm6Q=="})

    assert earlier.content == b"Y2Fm6Q=="


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


def test_message_metrics_from_dict_ignores_timer_and_unknown_keys() -> None:
    assert MessageMetrics.from_dict({"input_tokens": 3, "timer": {"start": 1}, "mystery": 1}) == MessageMetrics(
        tokens=TokenCounts(input_tokens=3),
    )


def test_run_metrics_round_trip_agno_dict_minus_dropped_keys() -> None:
    expected = without(agno("run_metrics"), DROPPED_METRICS_KEYS)

    assert RunMetrics.from_dict(agno("run_metrics")).to_dict() == expected
    assert RunMetrics().to_dict() == {}


def test_model_metrics_round_trip_agno_dict() -> None:
    expected = agno("model_metrics")

    assert ModelMetrics.from_dict(expected).to_dict() == expected


# --- messages --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("message_id", list(AGNO["messages"]))
def test_message_round_trips_agno_dict(message_id: str) -> None:
    stored = agno("messages")[message_id]

    assert Message.from_dict(stored).to_dict() == without(stored, DROPPED_MESSAGE_KEYS)


def test_a_message_agno_stored_with_a_compressed_copy_reads_as_its_full_content() -> None:
    """Agno's retired ``compress_tool_results`` stored a shortened copy beside the content; only the content reads.

    Since the owned runtime replaced Agno, every provider has been sent the content, so estimates and summary input
    read it too.
    """
    stored = agno("messages")["m-assistant"]
    assert stored["compressed_content"] == "short"

    message = Message.from_dict(stored)

    assert render_message_content(message) == stored["content"] == "Running it."
    assert "compressed_content" not in message.to_dict()


def test_message_from_dict_ignores_agno_only_keys() -> None:
    stored = agno("message_with_agno_only_fields")
    stored["citations"] = {"urls": [{"url": "https://example.invalid"}]}
    stored["references"] = {"query": "q", "references": []}
    stored["unexpected_future_key"] = {"nested": True}

    assert Message.from_dict(stored) == Message(role="assistant", content="hi", id="m", created_at=1)


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


@pytest.mark.parametrize("stored_id", [None, "", "absent"])
def test_a_tool_execution_stored_without_a_call_id_reads_with_the_same_id(stored_id: str | None) -> None:
    """Agno 2.4.7 stored Ollama's calls without ids; each read gives such a call the same id, the rest as stored."""
    stored = agno("tool_execution")
    if stored_id == "absent":
        del stored["tool_call_id"]
    else:
        stored["tool_call_id"] = stored_id

    first, second = ToolExecution.from_dict(stored), ToolExecution.from_dict(stored)

    assert first.tool_call_id
    assert second.tool_call_id == first.tool_call_id
    # A store that hands the keys back in another order reads the same id.
    assert ToolExecution.from_dict(dict(reversed(stored.items()))).tool_call_id == first.tool_call_id
    written = first.to_dict()
    assert written.pop("tool_call_id") == first.tool_call_id
    assert written == without(stored, DROPPED_TOOL_EXECUTION_KEYS | {"tool_call_id"})


def test_a_run_whose_tool_executions_have_no_call_ids_still_loads() -> None:
    """A run Agno 2.4.7 stored for Ollama, whose calls had no ids, loads with an id on each, the same on every load."""
    stored = json.loads((FIXTURE_RUNS / "run_output.json").read_text())["agent_code:run-tools"]
    for tool in stored["tools"]:
        tool["tool_call_id"] = None
    for message in stored["messages"]:
        for call in message.get("tool_calls") or ():
            del call["id"]
        if message["role"] == "tool":
            del message["tool_call_id"]

    written = RunRecord.from_dict(stored).to_dict()

    assert RunRecord.from_dict(stored).to_dict() == written
    new_ids = {tool.pop("tool_call_id") for tool in written["tools"]}
    assert len(new_ids) == len(stored["tools"]) == 3
    assert all(isinstance(new_id, str) and new_id for new_id in new_ids)
    expected = expected_run_dict(stored)
    for tool in expected["tools"]:
        del tool["tool_call_id"]
    assert written == expected


def test_run_requirement_round_trips_agno_dict() -> None:
    expected = expected_requirement_dict(agno("run_requirement"))

    assert RunRequirement.from_dict(expected).to_dict() == expected


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


def test_a_run_agno_stored_with_empty_tools_and_requirements_and_no_metrics_reads_as_stored() -> None:
    # Agno's loop left ``tools = []`` on runs without tool calls and ``requirements = []`` after resolving a
    # pause; MindRoom's interrupted-turn records never set ``metrics``. Each key must survive as stored.
    agno_dict = agno("agent_run_with_empty_tools_and_requirements")
    assert (agno_dict["tools"], agno_dict["requirements"]) == ([], [])
    assert "metrics" not in agno_dict

    record = RunRecord.from_dict(agno_dict)

    assert (record.tools, record.requirements, record.metrics) == ([], [], None)
    assert record.to_dict() == expected_run_dict(agno_dict)


def test_team_run_record_round_trips_agno_dict_minus_dropped_keys() -> None:
    agno_dict = agno("team_run")
    expected = expected_run_dict(agno_dict)

    record = TeamRunRecord.from_dict(agno_dict)

    assert record.to_dict() == expected
    # The stored label of the team's members had no reader, so a rewritten row drops it.
    assert "team_name" in agno_dict
    assert "team_name" not in record.to_dict()
    assert isinstance(record.member_responses[0], RunRecord)
    assert record.member_responses[0].parent_run_id == "team-run-1"


@pytest.mark.parametrize("status", ["PENDING", "REGENERATED"])
def test_run_record_reads_every_status_agno_could_store(status: str) -> None:
    """A run Agno stored as not started or as replaced by a regeneration reads as a run that did not finish."""
    agno_dict = agno("runs_by_status")[status]

    record = RunRecord.from_dict(agno_dict)

    assert record.status is RunStatus.error
    assert record.to_dict() == {**expected_run_dict(agno_dict), "status": "ERROR"}


def test_run_requirement_reads_rows_agno_read_leniently() -> None:
    """A requirement without a tool execution or with an unreadable timestamp still reads, as with Agno."""
    stored = {"id": "req-9", "created_at": "not a timestamp", "confirmation": True}

    owned = RunRequirement.from_dict(stored)

    assert (owned.tool_execution.tool_name, owned.tool_execution.tool_args) == ("unknown", {})
    agno_dict, owned_dict = expected_requirement_dict(agno("run_requirement_read_leniently")), owned.to_dict()
    # Agno's placeholder had no call id; the owned one reads with an id, the same on every read.
    assert agno_dict["tool_execution"]["tool_call_id"] is None
    assert owned_dict["tool_execution"]["tool_call_id"] == owned.tool_execution.tool_call_id
    assert owned.tool_execution.tool_call_id
    assert RunRequirement.from_dict(stored).tool_execution.tool_call_id == owned.tool_execution.tool_call_id
    for result in (agno_dict, owned_dict):
        del result["tool_execution"]["tool_call_id"]
    assert owned_dict == agno_dict


# Stored requirement keys beside a pending tool execution, by case.
DECISIONS_ON_THE_REQUIREMENT: dict[str, dict[str, Any]] = {
    "rejection": {"confirmation": False, "confirmation_note": "No."},
    "approval": {"confirmation": True},
    "external-result": {"external_execution_result": "done"},
    "explicit-tool-decision-wins": {
        "confirmation": False,
        "tool_execution": {
            "tool_call_id": "call-1",
            "tool_name": "t",
            "requires_confirmation": True,
            "confirmed": True,
            "created_at": 3,
        },
    },
}


@pytest.mark.parametrize("case", list(DECISIONS_ON_THE_REQUIREMENT))
def test_run_requirement_moves_a_decision_on_the_requirement_onto_its_tool_execution(case: str) -> None:
    data = {
        "id": "req-1",
        "created_at": "2026-01-02T03:04:05+00:00",
        "tool_execution": {"tool_call_id": "call-1", "tool_name": "t", "requires_confirmation": True, "created_at": 3},
        **DECISIONS_ON_THE_REQUIREMENT[case],
    }

    owned = RunRequirement.from_dict(data).to_dict()

    # Agno's goldens were recorded for these entries without a call id.
    expected = expected_requirement_dict(agno("run_requirement_decision_moved")[case])
    expected["tool_execution"]["tool_call_id"] = "call-1"
    assert owned == expected


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
    "run_output": (stored_run_record, expected_run_dict),
    "team_run_output": (stored_run_record, expected_run_dict),
    "message": (Message.from_dict, lambda stored: without(stored, DROPPED_MESSAGE_KEYS)),
    "image": (Image.from_dict, dict),
    "audio": (Audio.from_dict, dict),
    "video": (Video.from_dict, dict),
    "file": (File.from_dict, dict),
    "tool_execution": (ToolExecution.from_dict, lambda stored: without(stored, DROPPED_TOOL_EXECUTION_KEYS)),
    "run_requirement": (RunRequirement.from_dict, expected_requirement_dict),
    "message_metrics": (MessageMetrics.from_dict, dict),
    "run_metrics": (RunMetrics.from_dict, dict),
    "model_metrics": (ModelMetrics.from_dict, dict),
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


_METRIC_CODECS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "message_metrics": MessageMetrics.from_dict,
    "run_metrics": RunMetrics.from_dict,
    "model_metrics": ModelMetrics.from_dict,
    "session_metrics": SessionMetrics.from_dict,
}


@pytest.mark.parametrize("fixture", sorted(_METRIC_CODECS))
def test_stored_metrics_keep_agno_key_order_at_every_level(fixture: str) -> None:
    """The counters sit first in Agno's order, in run totals and in each per-model entry alike."""
    stored = list(json.loads((FIXTURE_RUNS / f"{fixture}.json").read_text()).values())
    for key, value in AGNO.items():
        if key.startswith(fixture):
            stored.extend(
                without(item, DROPPED_METRICS_KEYS) for item in (value if isinstance(value, list) else [value])
            )

    for value in stored:
        assert json.dumps(_METRIC_CODECS[fixture](value).to_dict()) == json.dumps(value)


# --- session totals --------------------------------------------------------------------------------------------


def _run_metrics_cases() -> list[dict[str, Any]]:
    return [
        agno("run_metrics"),
        agno("run_metrics_with_empty_details"),
        {},
        *json.loads((FIXTURE_RUNS / "run_metrics.json").read_text()).values(),
    ]


def _counted_totals(totals: dict[str, Any]) -> dict[str, Any]:
    """Return the token counters of stored session totals, in total and for each model entry."""
    counters = set(llm_usage.TOKEN_FIELDS)
    counted: dict[str, Any] = {key: value for key, value in totals.items() if key in counters}
    if "details" in totals:
        counted["details"] = {
            model_type: [
                {key: value for key, value in entry.items() if key in counters | {"id", "provider"}}
                for entry in entries
            ]
            for model_type, entries in totals["details"].items()
        }
    return counted


def test_session_metrics_count_token_counters_as_agno_accumulated_them() -> None:
    """The session totals add each run's token counters, in total and per model, as Agno summed them."""
    totals = agno("session_metrics_totals")
    owned = SessionMetrics()
    for case, agno_totals in zip(_run_metrics_cases(), totals, strict=True):
        owned.count_run(RunMetrics.from_dict(case), replacing=None)

        assert owned.to_dict() == _counted_totals(agno_totals)
    assert SessionMetrics.from_dict(totals[-1]).to_dict() == without(totals[-1], DROPPED_METRICS_KEYS)
