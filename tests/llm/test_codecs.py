"""Dict codecs of ``mindroom.llm`` read the dicts Agno 3.0.9 wrote, keeping every value the owned records model.

These are legacy inputs and never change: ``tests/fixtures/legacy_agno3/codecs.json`` holds what real Agno objects
produced, and the stored-run fixtures under ``tests/fixtures/legacy_agno3/runs`` hold what MindRoom stored through Agno.
Each one read and written back is the dict it was, minus the keys the owned records deliberately drop and those held
at their defaults, which ``tests.llm.agno_dict_helpers`` lists once per type. The schema MindRoom itself writes is
``tests/llm/test_stored_dicts.py``'s.
"""
# ruff: noqa: D103

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any

import pytest
from structlog.testing import capture_logs

from mindroom.history.message_content import render_message_content
from mindroom.llm.media import Audio, File, Image, Video
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord, ToolExecution
from mindroom.llm.usage import MessageMetrics, ModelMetrics, RunMetrics, TokenCounts
from tests.llm.agno_dict_helpers import (
    DROPPED_RUN_KEYS,
    DROPPED_TOOL_EXECUTION_KEYS,
    expected_message_dict,
    expected_metrics_dict,
    expected_request_metrics_dict,
    expected_run_dict,
    expected_tool_dict,
    stored_run_record,
    without,
)
from tests.session_storage.goldens import LEGACY_AGNO3

if TYPE_CHECKING:
    from collections.abc import Callable

# What Agno 3.0.9 objects wrote, keyed by the case that built them; see "Codec goldens" in
# ``tests/fixtures/agno_parity/README.md``.
AGNO: dict[str, Any] = json.loads((LEGACY_AGNO3 / "codecs.json").read_text(encoding="utf-8"))

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


def test_message_metrics_round_trip_agno_dict_minus_dropped_keys() -> None:
    expected = expected_request_metrics_dict(agno("message_metrics"))

    assert MessageMetrics.from_dict(agno("message_metrics")).to_dict() == expected


def test_claude_context_usage_stored_under_provider_metrics_reads_as_context() -> None:
    """A Claude response stored before ``context`` existed reads its final iteration's counters as its context."""
    stored = {
        "input_tokens": 12,
        "output_tokens": 5,
        "total_tokens": 17,
        "cache_read_tokens": 3000,
        "cache_write_tokens": 40,
        "duration": 1.5,
        "provider_metrics": {
            "server_tool_use": {"web_search_requests": 1},
            "service_tier": "standard",
            "context_usage": {"input_tokens": 8, "cache_read_tokens": 2000, "cache_write_tokens": 0},
        },
    }

    metrics = MessageMetrics.from_dict(stored)

    assert metrics.context == TokenCounts(input_tokens=8, cache_read_tokens=2000)
    assert metrics.to_dict() == {
        **{key: value for key, value in stored.items() if key != "provider_metrics"},
        "context": {"input_tokens": 8, "cache_read_tokens": 2000},
    }
    assert MessageMetrics.from_dict(metrics.to_dict()) == metrics
    # A stored ``context`` is the one a later release wrote, and it wins over any copy under ``provider_metrics``.
    assert MessageMetrics.from_dict({**stored, "context": {"input_tokens": 9}}).context == TokenCounts(input_tokens=9)


def test_usage_stored_with_provider_extras_and_a_cost_reads_without_them() -> None:
    """Request, run, and per-model usage an earlier release stored with provider extras or a cost read without them.

    A message and a run holding them load, and write back only what the owned metrics model.
    """
    counters = {"input_tokens": 42, "output_tokens": 12, "total_tokens": 54}
    request = {**counters, "cost": 0.00021, "duration": 0.5, "provider_metrics": {"total_duration": 812000000}}
    entry = {**counters, "cost": 0.00021, "id": "m", "provider": "OpenRouter", "provider_metrics": {"traffic": "x"}}
    run = {**counters, "cost": 0.00021, "duration": 0.5, "details": {"model": [entry]}}

    assert MessageMetrics.from_dict(request).to_dict() == {**counters, "duration": 0.5}
    assert RunMetrics.from_dict(run).to_dict() == {
        **counters,
        "duration": 0.5,
        "details": {"model": [{**counters, "id": "m", "provider": "OpenRouter"}]},
    }
    message = Message.from_dict({"role": "assistant", "content": "hi", "id": "a", "metrics": request, "created_at": 1})
    assert message.to_dict()["metrics"] == {**counters, "duration": 0.5}
    record = RunRecord.from_dict({"run_id": "r", "agent_id": "code", "metrics": run, "messages": [message.to_dict()]})
    assert record.metrics == RunMetrics.from_dict(run)
    assert record.to_dict()["metrics"] == RunMetrics.from_dict(run).to_dict()


def test_message_metrics_from_dict_ignores_timer_and_unknown_keys() -> None:
    assert MessageMetrics.from_dict({"input_tokens": 3, "timer": {"start": 1}, "mystery": 1}) == MessageMetrics(
        tokens=TokenCounts(input_tokens=3),
    )


def test_run_metrics_round_trip_agno_dict_minus_dropped_keys() -> None:
    expected = expected_metrics_dict(agno("run_metrics"))

    assert RunMetrics.from_dict(agno("run_metrics")).to_dict() == expected
    assert RunMetrics().to_dict() == {}


def test_model_metrics_round_trip_agno_dict_minus_dropped_keys() -> None:
    expected = expected_metrics_dict(agno("model_metrics"))

    assert ModelMetrics.from_dict(agno("model_metrics")).to_dict() == expected


# --- messages --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("message_id", list(AGNO["messages"]))
def test_message_round_trips_agno_dict(message_id: str) -> None:
    stored = agno("messages")[message_id]

    assert Message.from_dict(stored).to_dict() == expected_message_dict(stored)


def test_a_message_agno_stored_with_a_compressed_copy_keeps_it() -> None:
    """Agno's retired ``compress_tool_results`` stored a shortened copy beside the content, which still reads.

    Estimates and summary input read the copy, as they did before the upgrade, and a rewritten row keeps it.
    """
    stored = agno("messages")["m-assistant"]
    assert (stored["content"], stored["compressed_content"]) == ("Running it.", "short")

    message = Message.from_dict(stored)

    assert render_message_content(message) == "short"
    assert message.to_dict()["compressed_content"] == "short"


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


@pytest.mark.parametrize("malformed", [7, "", ["call-1"]], ids=["number", "empty", "list"])
def test_a_message_stored_with_a_malformed_call_id_loads_without_it(malformed: object) -> None:
    """A call id that is not non-empty text is dropped once, when the row decodes, with a log line; the load goes on.

    Every wire then reads a call id as text or None, and well-formed ids and calls stay as stored.
    """
    calls = [
        {"id": malformed, "type": "function", "function": {"name": "invalid", "arguments": "{}"}},
        {"id": "call-2", "type": "function", "function": {"name": "valid", "arguments": "{}"}},
        {"type": "function", "function": {"name": "unnamed", "arguments": "{}"}},
    ]
    stored = [
        {"id": "m.call", "role": "assistant", "tool_calls": calls, "created_at": 1},
        {"id": "m.bad", "role": "tool", "tool_call_id": malformed, "tool_name": "invalid", "created_at": 1},
        {"id": "m.good", "role": "tool", "tool_call_id": "call-2", "tool_name": "valid", "created_at": 1},
    ]

    with capture_logs() as logs:
        assistant, bad, good = (Message.from_dict(data) for data in stored)

    assert assistant.tool_calls == [{**calls[0], "id": None}, calls[1], calls[2]]
    assert (bad.tool_call_id, good.tool_call_id) == (None, "call-2")
    assert [(log["event"], log["log_level"], log["message_id"]) for log in logs] == [
        ("stored_tool_call_id_dropped", "debug", "m.call"),
        ("stored_tool_call_id_dropped", "debug", "m.bad"),
    ]


def test_a_message_whose_result_and_call_ids_are_both_malformed_logs_one_line() -> None:
    """Each decode of a row logs its dropped ids once, at debug level, since the row is read again on every load."""
    stored = {
        "id": "m.odd",
        "role": "assistant",
        "tool_call_id": "",
        "tool_calls": [{"id": 7, "type": "function", "function": {"name": "f", "arguments": "{}"}}],
        "created_at": 1,
    }

    with capture_logs() as logs:
        Message.from_dict(stored)

    assert [(log["event"], log["log_level"], log["message_id"]) for log in logs] == [
        ("stored_tool_call_id_dropped", "debug", "m.odd"),
    ]


# --- tool executions --------------------------------------------------------------------------------------------


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
    # pause; MindRoom's interrupted-turn records never set ``metrics``. Each key the record models survives as stored.
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

FIXTURE_RUNS = LEGACY_AGNO3 / "runs"

# Fixture file -> (reader, expected dict for one stored case); every file under ``runs/`` has an entry.
FIXTURE_CODECS: dict[str, tuple[Callable[[dict[str, Any]], Any], Callable[[dict[str, Any]], dict[str, Any]]]] = {
    "run_output": (stored_run_record, expected_run_dict),
    "team_run_output": (stored_run_record, expected_run_dict),
    "message": (Message.from_dict, expected_message_dict),
    "image": (Image.from_dict, dict),
    "audio": (Audio.from_dict, dict),
    "video": (Video.from_dict, dict),
    "file": (File.from_dict, dict),
    "tool_execution": (ToolExecution.from_dict, expected_tool_dict),
    # A stored requirement copies its call, which reads as the call's own tool execution does.
    "run_requirement": (
        lambda stored: ToolExecution.from_dict(stored["tool_execution"]),
        lambda stored: expected_tool_dict(stored["tool_execution"]),
    ),
    "message_metrics": (MessageMetrics.from_dict, expected_request_metrics_dict),
    "run_metrics": (RunMetrics.from_dict, expected_metrics_dict),
    "model_metrics": (ModelMetrics.from_dict, expected_metrics_dict),
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


@pytest.mark.parametrize("fixture", ["message_metrics", "model_metrics", "run_metrics"])
def test_stored_metrics_keep_agno_key_order_at_every_level(fixture: str) -> None:
    """The counters sit first in Agno's order, in run totals and in each per-model entry alike."""
    stored = list(json.loads((FIXTURE_RUNS / f"{fixture}.json").read_text()).values())
    for key, value in AGNO.items():
        if key.startswith(fixture):
            stored.extend(value if isinstance(value, list) else [value])
    read, expected_for = FIXTURE_CODECS[fixture]

    for value in stored:
        assert json.dumps(read(value).to_dict()) == json.dumps(expected_for(value))
