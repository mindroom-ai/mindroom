"""Dict codecs of ``mindroom.llm`` reproduce the shapes Agno 3.0.9 wrote.

Every dict here comes from a real Agno object built in the test or from the stored-run fixtures under
``tests/fixtures/agno_parity/runs``, so the owned readers and writers are proven against Agno's own
``to_dict``; the stored data they must keep decoding was written that way.
Keys the owned records deliberately drop are listed once per type and removed before comparing.
"""
# ruff: noqa: D103

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from agno.media import Audio as AgnoAudio
from agno.media import File as AgnoFile
from agno.media import Image as AgnoImage
from agno.media import Video as AgnoVideo
from agno.metrics import MessageMetrics as AgnoMessageMetrics
from agno.metrics import ModelMetrics as AgnoModelMetrics
from agno.metrics import RunMetrics as AgnoRunMetrics
from agno.metrics import SessionMetrics as AgnoSessionMetrics
from agno.metrics import ToolCallMetrics as AgnoToolCallMetrics
from agno.metrics import accumulate_model_metrics
from agno.models.message import Message as AgnoMessage
from agno.models.response import ModelResponse
from agno.models.response import ToolExecution as AgnoToolExecution
from agno.run.agent import RunInput
from agno.run.agent import RunOutput as AgnoRunOutput
from agno.run.base import RunStatus as AgnoRunStatus
from agno.run.requirement import RunRequirement as AgnoRunRequirement
from agno.run.team import TeamRunOutput as AgnoTeamRunOutput

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

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(32))
WAV_BYTES = b"RIFF\x00\x00\x00\x00WAVEfmt " + bytes(range(16))
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + bytes(range(24))
PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"

# --- media -----------------------------------------------------------------------------------------------------


type AgnoMedia = AgnoImage | AgnoAudio | AgnoVideo | AgnoFile


def agno_media_cases() -> list[tuple[str, AgnoMedia, type[Image | Audio | Video | File]]]:
    return [
        (
            "image-content",
            AgnoImage(
                id="img-1",
                content=PNG_BYTES,
                format="png",
                mime_type="image/png",
                detail="high",
                original_prompt="a cat",
                revised_prompt="a tabby cat",
                alt_text="cat",
                metadata={"source": "test"},
            ),
            Image,
        ),
        ("image-url", AgnoImage(id="img-2", url="https://example.invalid/cat.png", mime_type="image/png"), Image),
        ("image-filepath", AgnoImage(id="img-3", filepath=Path("/data/cat.png"), format="png"), Image),
        (
            "audio-content",
            AgnoAudio(
                id="aud-1",
                content=WAV_BYTES,
                format="wav",
                mime_type="audio/wav",
                duration=1.5,
                sample_rate=16000,
                channels=2,
                transcript="hello",
                expires_at=1_900_000_000,
                metadata={"source": "voice"},
            ),
            Audio,
        ),
        ("audio-filepath", AgnoAudio(id="aud-2", filepath="/data/clip.ogg", mime_type="audio/ogg"), Audio),
        (
            "video-content",
            AgnoVideo(
                id="vid-1",
                content=MP4_BYTES,
                format="mp4",
                mime_type="video/mp4",
                duration=2.25,
                width=640,
                height=480,
                fps=29.97,
                eta="soon",
                original_prompt="a dog",
                revised_prompt="a running dog",
                metadata={"source": "tool"},
            ),
            Video,
        ),
        ("video-url", AgnoVideo(id="vid-2", url="https://example.invalid/dog.mp4"), Video),
        (
            "file-binary",
            AgnoFile(
                id="file-1",
                content=PDF_BYTES,
                mime_type="application/pdf",
                file_type="document",
                filename="report.pdf",
                size=len(PDF_BYTES),
                format="pdf",
                name="report",
                metadata={"pages": 1},
            ),
            File,
        ),
        ("file-text", AgnoFile(id="file-2", content=b"hello\nworld", mime_type="text/plain", filename="a.txt"), File),
        ("file-string", AgnoFile(id="file-3", content="raw text", mime_type="text/markdown"), File),
        (
            "file-filepath",
            AgnoFile(id="file-4", filepath="/data/sheet.csv", mime_type="text/csv", filename="sheet.csv"),
            File,
        ),
    ]


@pytest.mark.parametrize(
    ("name", "agno_media", "kind"),
    agno_media_cases(),
    ids=lambda case: case if isinstance(case, str) else "",
)
def test_media_round_trips_agno_dict(
    name: str,
    agno_media: AgnoMedia,
    kind: type[Image | Audio | Video | File],
) -> None:
    expected = agno_media.to_dict()

    assert kind.from_dict(expected).to_dict() == expected, name


@pytest.mark.parametrize(
    ("name", "agno_media", "kind"),
    agno_media_cases(),
    ids=lambda case: case if isinstance(case, str) else "",
)
def test_media_to_dict_without_content_matches_agno(
    name: str,
    agno_media: AgnoMedia,
    kind: type[Image | Audio | Video | File],
) -> None:
    expected = agno_media.to_dict(include_base64_content=False)

    assert kind.from_dict(agno_media.to_dict()).to_dict(include_content=False) == expected, name


def test_media_from_dict_decodes_content_bytes_as_agno_did() -> None:
    image = Image.from_dict(AgnoImage(content=PNG_BYTES).to_dict())
    text_file = File.from_dict(AgnoFile(content=b"plain", mime_type="text/plain").to_dict())
    binary_file = File.from_dict(AgnoFile(content=PDF_BYTES, mime_type="application/pdf").to_dict())

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


def agno_message_metrics() -> AgnoMessageMetrics:
    return AgnoMessageMetrics(
        input_tokens=120,
        output_tokens=30,
        total_tokens=150,
        audio_input_tokens=4,
        audio_output_tokens=2,
        audio_total_tokens=6,
        cache_read_tokens=100,
        cache_write_tokens=20,
        reasoning_tokens=10,
        cost=0.0123,
        duration=1.25,
        time_to_first_token=0.4,
        provider_metrics={"context_usage": {"input_tokens": 40}, "service_tier": "default", "timings": 3},
    )


def test_message_metrics_round_trip_agno_dict() -> None:
    expected = agno_message_metrics().to_dict()

    assert MessageMetrics.from_dict(expected).to_dict() == expected


def test_message_metrics_to_dict_omits_zero_and_none_as_agno() -> None:
    agno = AgnoMessageMetrics(input_tokens=1, cost=0.0, duration=0.0, provider_metrics={})
    owned = MessageMetrics(input_tokens=1, cost=0.0, duration=0.0, provider_metrics={})

    assert owned.to_dict() == agno.to_dict() == {"input_tokens": 1, "cost": 0.0, "duration": 0.0}
    assert MessageMetrics().to_dict() == AgnoMessageMetrics().to_dict() == {}


def test_message_metrics_from_dict_ignores_timer_and_unknown_keys() -> None:
    assert MessageMetrics.from_dict({"input_tokens": 3, "timer": {"start": 1}, "mystery": 1}) == MessageMetrics(
        input_tokens=3,
    )


def test_message_metrics_addition_matches_agno() -> None:
    first = agno_message_metrics()
    second = AgnoMessageMetrics(input_tokens=5, output_tokens=7, cost=0.001, duration=0.5, time_to_first_token=9.0)
    second.provider_metrics = {"timings": 2, "service_tier": "flex", "new": 1}
    expected = (first + second).to_dict()

    owned = MessageMetrics.from_dict(first.to_dict()) + MessageMetrics.from_dict(second.to_dict())

    assert owned.to_dict() == expected
    assert (MessageMetrics(input_tokens=1) + MessageMetrics()).to_dict() == {"input_tokens": 1}


def test_tool_call_metrics_round_trip_agno_dict() -> None:
    expected = AgnoToolCallMetrics(start_time=1.0, end_time=2.5, duration=1.5).to_dict()

    assert ToolCallMetrics.from_dict(expected).to_dict() == expected
    assert ToolCallMetrics.from_dict({**expected, "timer": None}).to_dict() == expected


def agno_run_metrics() -> AgnoRunMetrics:
    return AgnoRunMetrics(
        input_tokens=300,
        output_tokens=60,
        total_tokens=360,
        cache_read_tokens=200,
        reasoning_tokens=12,
        cost=0.05,
        time_to_first_token=0.3,
        duration=4.5,
        details={
            "model": [
                AgnoModelMetrics(
                    id="claude-opus-5-5",
                    provider="Anthropic",
                    input_tokens=200,
                    output_tokens=40,
                    total_tokens=240,
                    cache_read_tokens=200,
                    cost=0.04,
                    provider_metrics={"context_usage": {"input_tokens": 50}},
                ),
                AgnoModelMetrics(
                    id="gpt-6-luna",
                    provider="OpenAI Chat",
                    input_tokens=100,
                    output_tokens=20,
                    total_tokens=120,
                    reasoning_tokens=12,
                    cost=0.01,
                ),
            ],
            "learning_model": [
                AgnoModelMetrics(id="gpt-6-luna", provider="OpenAI Chat", input_tokens=9, provider_metrics={}),
            ],
        },
        additional_metrics={"eval_duration": 1.5, "label": "x"},
    )


def test_run_metrics_round_trip_agno_dict() -> None:
    expected = agno_run_metrics().to_dict()

    assert RunMetrics.from_dict(expected).to_dict() == expected
    assert RunMetrics().to_dict() == AgnoRunMetrics().to_dict() == {}


def test_run_metrics_add_matches_agno_accumulate_model_metrics() -> None:
    class _Claude:
        id = "claude-opus-5-5"

        @staticmethod
        def get_provider() -> str:
            return "Anthropic"

    class _Luna:
        id = "gpt-6-luna"

        @staticmethod
        def get_provider() -> str:
            return "OpenAI Chat"

    reports = [
        (
            _Claude(),
            {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12, "cost": 0.1, "provider_metrics": {"n": 1}},
        ),
        (
            _Claude(),
            {
                "input_tokens": 5,
                "output_tokens": 1,
                "total_tokens": 6,
                "cache_read_tokens": 4,
                "provider_metrics": {"n": 2, "tier": "a"},
            },
        ),
        (_Luna(), {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}),
    ]
    # Agno's accumulate aliases and then mutates the report's provider_metrics dict, so the owned side reads copies.
    usages = [MessageMetrics.from_dict(copy.deepcopy(report)) for _, report in reports]
    agno = AgnoRunMetrics()
    for model, report in reports:
        accumulate_model_metrics(
            ModelResponse(response_usage=AgnoMessageMetrics.from_dict(report)),
            model,  # type: ignore[arg-type]
            "model",
            agno,
        )
    owned = RunMetrics()
    for (model, _), usage in zip(reports, usages, strict=True):
        owned.add(usage, model_id=model.id, provider=model.get_provider())

    assert owned.to_dict() == agno.to_dict()
    assert [entry.id for entry in owned.details["model"]] == ["claude-opus-5-5", "gpt-6-luna"]  # type: ignore[index]
    # Agno aliased the request's provider_metrics into the details entry and rewrote it on the next request.
    assert usages[0].provider_metrics == {"n": 1}


def test_run_metrics_addition_matches_agno() -> None:
    first = agno_run_metrics()
    second = AgnoRunMetrics(
        input_tokens=10,
        output_tokens=1,
        cost=0.5,
        duration=1.0,
        time_to_first_token=0.1,
        details={"model": [AgnoModelMetrics(id="claude-opus-5-5", provider="Anthropic", input_tokens=10, cost=0.5)]},
        additional_metrics={"eval_duration": 0.5, "label": "y"},
    )
    expected = (first + second).to_dict()

    owned = RunMetrics.from_dict(first.to_dict()) + RunMetrics.from_dict(second.to_dict())

    assert owned.to_dict() == expected
    assert (RunMetrics(input_tokens=1) + RunMetrics()).to_dict() == {"input_tokens": 1}


def test_model_metrics_round_trip_agno_dict() -> None:
    expected = AgnoModelMetrics(id="m", provider="P", input_tokens=1, cost=0.2, provider_metrics={"k": 1}).to_dict()

    assert ModelMetrics.from_dict(expected).to_dict() == expected


# --- messages --------------------------------------------------------------------------------------------------


def agno_messages() -> list[AgnoMessage]:
    tool_call = {"id": "call_1", "type": "function", "function": {"name": "shell", "arguments": '{"cmd": "ls"}'}}
    return [
        AgnoMessage(role="system", content="You are MindRoom.", id="m-sys", created_at=1_700_000_000),
        AgnoMessage(
            role="user",
            content="Look at these",
            id="m-user",
            name="alice",
            created_at=1_700_000_001,
            images=[AgnoImage(id="img", content=PNG_BYTES, mime_type="image/png", detail="low")],
            audio=[AgnoAudio(id="aud", content=WAV_BYTES, format="wav")],
            videos=[AgnoVideo(id="vid", content=MP4_BYTES, mime_type="video/mp4")],
            files=[AgnoFile(id="doc", content=PDF_BYTES, mime_type="application/pdf", filename="r.pdf")],
            provider_data={"mindroom_queued_message_notice": True},
        ),
        AgnoMessage(
            role="assistant",
            content="Running it.",
            id="m-assistant",
            created_at=1_700_000_002,
            tool_calls=[tool_call],
            reasoning_content="think",
            redacted_reasoning_content="redacted",
            compressed_content="short",
            provider_data={"mindroom_model": {"id": "claude-opus-5-5", "provider": "Anthropic"}, "signature": "sig"},
            metrics=agno_message_metrics(),
            stop_after_tool_call=True,
        ),
        AgnoMessage(
            role="tool",
            content="file.txt",
            id="m-tool",
            created_at=1_700_000_003,
            tool_call_id="call_1",
            tool_name="shell",
            tool_args={"cmd": "ls"},
            tool_call_error=False,
            from_history=True,
        ),
        AgnoMessage(
            role="user",
            content=[{"type": "text", "text": "block"}, {"type": "image_url", "image_url": {"url": "data:,"}}],
            id="m-blocks",
            created_at=1_700_000_004,
        ),
        AgnoMessage(
            role="assistant",
            content="",
            id="m-empty",
            created_at=1_700_000_005,
            tool_args={},
            tool_calls=[],
            provider_data={},
        ),
        AgnoMessage(role="user", content=None, id="m-none", created_at=1_700_000_006, tool_call_error=True),
    ]


@pytest.mark.parametrize("agno_message", agno_messages(), ids=lambda message: message.id)
def test_message_round_trips_agno_dict(agno_message: AgnoMessage) -> None:
    expected = agno_message.to_dict()

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
    agno = AgnoMessage(
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
        metrics=AgnoMessageMetrics(input_tokens=1),
        from_history=True,
        images=[AgnoImage(id="i", url="https://example.invalid/i")],
        audio=[AgnoAudio(id="a", url="https://example.invalid/a")],
        videos=[AgnoVideo(id="v", url="https://example.invalid/v")],
        files=[AgnoFile(id="f", url="https://example.invalid/f")],
    )

    assert owned.to_dict() == agno.to_dict()


def test_message_from_dict_ignores_agno_only_keys() -> None:
    stored = AgnoMessage(
        role="assistant",
        content="hi",
        id="m",
        created_at=1,
        audio_output=AgnoAudio(id="out", content=WAV_BYTES),
        checkpoint_status="COMPLETED",
        checkpoint_created_at=2,
    ).to_dict()
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
    stored = AgnoMessage(
        role="user",
        content="look",
        images=[AgnoImage(id="img", content=PNG_BYTES, detail="high", alt_text="cat", metadata={"k": "v"})],
    ).to_dict()
    [agno_image] = AgnoMessage.from_dict(dict(stored)).images or []
    [owned_image] = Message.from_dict(stored).images or []

    assert agno_image.detail is None  # Agno's own reader loses these; the owned one must not.
    assert (owned_image.detail, owned_image.alt_text, owned_image.metadata) == ("high", "cat", {"k": "v"})


def test_message_from_dict_reads_non_persisted_flags_from_model_dump_payloads() -> None:
    payload = AgnoMessage(role="user", content="x", add_to_agent_memory=False, temporary=True).model_dump(mode="python")

    message = Message.from_dict(payload)

    assert (message.add_to_agent_memory, message.temporary) == (False, True)


# --- tool executions and requirements --------------------------------------------------------------------------


def agno_tool_execution() -> AgnoToolExecution:
    return AgnoToolExecution(
        tool_call_id="call_1",
        tool_name="shell",
        tool_args={"cmd": "rm -rf /tmp/x"},
        tool_call_error=False,
        result="done",
        metrics=AgnoToolCallMetrics(start_time=1.0, end_time=2.5, duration=1.5),
        child_run_id="child-1",
        stop_after_tool_call=True,
        created_at=1_700_000_010,
        requires_confirmation=True,
        confirmed=True,
        confirmation_note="ok",
        external_execution_required=False,
        external_execution_silent=True,
        approval_type="mindroom_policy",
    )


def test_tool_execution_round_trips_agno_dict_minus_dropped_keys() -> None:
    agno_dict = agno_tool_execution().to_dict()
    expected = without(agno_dict, DROPPED_TOOL_EXECUTION_KEYS)

    owned = ToolExecution.from_dict(agno_dict).to_dict()

    assert owned == expected
    assert set(agno_dict) - set(owned) == DROPPED_TOOL_EXECUTION_KEYS


def test_tool_execution_default_dict_matches_agno_minus_dropped_keys() -> None:
    agno = AgnoToolExecution(tool_call_id="c", tool_name="t", tool_args={}, created_at=7)
    owned = ToolExecution(tool_call_id="c", tool_name="t", tool_args={}, created_at=7)

    assert owned.to_dict() == without(agno.to_dict(), DROPPED_TOOL_EXECUTION_KEYS)
    assert ToolExecution.from_dict({"tool_name": "t"}).created_at > 0


def agno_requirement() -> AgnoRunRequirement:
    requirement = AgnoRunRequirement(
        AgnoToolExecution(
            tool_call_id="call_2",
            tool_name="delegate",
            tool_args={"task": "x"},
            requires_confirmation=True,
            created_at=3,
        ),
        id="req-1",
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )
    requirement.member_agent_id = "writer"
    requirement.member_agent_name = "Writer"
    requirement.member_run_id = "member-run-1"
    return requirement


def test_run_requirement_round_trips_agno_dict() -> None:
    expected = expected_requirement_dict(agno_requirement().to_dict())

    assert RunRequirement.from_dict(expected).to_dict() == expected


@pytest.mark.parametrize("decision", ["confirm", "reject", "reject_with_note"])
def test_run_requirement_confirmation_updates_match_agno(decision: str) -> None:
    agno = agno_requirement()
    owned = RunRequirement.from_dict(agno.to_dict())
    for requirement in (agno, owned):
        if decision == "confirm":
            requirement.confirm()
        elif decision == "reject":
            requirement.reject()
        else:
            requirement.reject("Not approved by requester")

    assert owned.to_dict() == expected_requirement_dict(agno.to_dict())
    assert owned.is_resolved() is agno.is_resolved() is True
    assert owned.needs_confirmation is agno.needs_confirmation is False


def test_run_requirement_external_result_matches_agno() -> None:
    execution = AgnoToolExecution(
        tool_call_id="call_3",
        tool_name="run_subagent",
        tool_args={},
        external_execution_required=True,
        created_at=4,
    )
    agno = AgnoRunRequirement(execution, id="req-2", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    owned = RunRequirement.from_dict(agno.to_dict())
    assert owned.needs_external_execution is agno.needs_external_execution is True

    agno.set_external_execution_result("subagent said hi")
    owned.set_external_execution_result("subagent said hi")

    assert owned.to_dict() == expected_requirement_dict(agno.to_dict())
    assert owned.tool_execution.result == "subagent said hi"
    assert owned.is_resolved() is agno.is_resolved() is True


def test_run_requirement_from_dict_ignores_user_input_schemas() -> None:
    stored = agno_requirement().to_dict()
    stored["user_input_schema"] = [{"name": "x", "field_type": "str", "description": None, "value": None}]
    stored["user_feedback_schema"] = [
        {"question": "q", "header": None, "multi_select": False, "selected_options": None},
    ]

    assert RunRequirement.from_dict(stored).to_dict() == expected_requirement_dict(agno_requirement().to_dict())


# --- run records -----------------------------------------------------------------------------------------------


def agno_paused_agent_run() -> AgnoRunOutput:
    requirement = agno_requirement()
    return AgnoRunOutput(
        run_id="run-1",
        agent_id="assistant",
        agent_name="Assistant",
        session_id="session-1",
        parent_run_id=None,
        user_id="@alice:example.org",
        input=RunInput(input_content=agno_messages()[:2]),
        content="Waiting for approval.",
        model="claude-opus-5-5",
        model_provider="Anthropic",
        messages=agno_messages(),
        metrics=agno_run_metrics(),
        tools=[agno_tool_execution(), requirement.tool_execution],
        requirements=[requirement],
        metadata={
            "room_id": "!room:example.org",
            "requester_id": "@alice:example.org",
            "mindroom_compacted_requests": 1,
        },
        session_state={"mindroom_pending_compaction_scope_keys": ["k"]},
        status=AgnoRunStatus.paused,
        created_at=1_700_000_100,
    )


def agno_member_run() -> AgnoRunOutput:
    return AgnoRunOutput(
        run_id="member-run-1",
        agent_id="writer",
        agent_name="Writer",
        session_id="session-2",
        parent_run_id="team-run-1",
        user_id="@alice:example.org",
        content="Draft written.",
        model="gpt-6-luna",
        model_provider="OpenAI",
        messages=[
            AgnoMessage(role="user", content="write", id="w1", created_at=1),
            AgnoMessage(role="assistant", content="Draft written.", id="w2", created_at=2),
        ],
        metrics=AgnoRunMetrics(input_tokens=3, output_tokens=4, total_tokens=7),
        metadata={"requester_id": "@alice:example.org"},
        status=AgnoRunStatus.completed,
        created_at=1_700_000_200,
    )


def agno_team_run() -> AgnoTeamRunOutput:
    member = agno_member_run()
    return AgnoTeamRunOutput(
        run_id="team-run-1",
        team_id="writers",
        team_name="Writers",
        session_id="session-2",
        user_id="@alice:example.org",
        content="Here is the draft.",
        model="claude-opus-5-5",
        model_provider="Anthropic",
        messages=[AgnoMessage(role="user", content="write", id="t1", created_at=1)],
        metrics=AgnoRunMetrics(input_tokens=30, output_tokens=5, total_tokens=35),
        member_responses=[member],
        tools=[
            AgnoToolExecution(
                tool_call_id="d1",
                tool_name="delegate_task_to_member",
                tool_args={"member_id": "writer"},
                result="Draft written.",
                child_run_id=member.run_id,
                created_at=9,
            ),
        ],
        metadata={"room_id": "!room:example.org", "mindroom_delegation": {"version": 1}},
        status=AgnoRunStatus.completed,
        created_at=1_700_000_300,
    )


def test_run_record_round_trips_agno_dict_minus_dropped_keys() -> None:
    agno_dict = agno_paused_agent_run().to_dict()
    expected = expected_run_dict(agno_dict)

    owned = RunRecord.from_dict(agno_dict).to_dict()

    assert owned == expected
    assert set(agno_dict) - set(owned) <= DROPPED_RUN_KEYS
    assert {"input", "session_state", "content_type"} <= set(agno_dict)


def test_run_record_minimal_dict_matches_agno_minus_dropped_keys() -> None:
    agno = AgnoRunOutput(
        run_id="r",
        agent_id="a",
        session_id="s",
        status=AgnoRunStatus.completed,
        messages=[],
        metrics=AgnoRunMetrics(),
        created_at=1,
    )

    owned = RunRecord(
        run_id="r",
        agent_id="a",
        session_id="s",
        status=RunStatus.completed,
        metrics=RunMetrics(),
        created_at=1,
    )

    assert owned.to_dict() == expected_run_dict(agno.to_dict())
    assert owned.to_dict()["metrics"] == {}
    assert "tools" not in owned.to_dict()
    assert "requirements" not in owned.to_dict()


def test_run_record_writes_tools_requirements_and_metrics_exactly_when_set() -> None:
    # Agno's loop left ``tools = []`` on runs without tool calls and ``requirements = []`` after resolving a
    # pause; MindRoom's interrupted-turn records never set ``metrics``. Each key must survive as stored.
    agno_dict = AgnoRunOutput(run_id="r", agent_id="a", messages=[], tools=[], requirements=[], created_at=1).to_dict()
    assert (agno_dict["tools"], agno_dict["requirements"]) == ([], [])
    assert "metrics" not in agno_dict

    owned = RunRecord(run_id="r", agent_id="a", tools=[], requirements=[], created_at=1)

    assert owned.to_dict() == expected_run_dict(agno_dict)
    assert RunRecord.from_dict(agno_dict).to_dict() == expected_run_dict(agno_dict)
    assert RunRecord.from_dict(agno_dict).metrics is None
    absent = RunRecord(run_id="r", agent_id="a", created_at=1).to_dict()
    assert {"tools", "requirements", "metrics"}.isdisjoint(absent)


def test_team_run_record_round_trips_agno_dict_minus_dropped_keys() -> None:
    agno_dict = agno_team_run().to_dict()
    expected = expected_run_dict(agno_dict)

    record = TeamRunRecord.from_dict(agno_dict)

    assert record.to_dict() == expected
    assert isinstance(record.member_responses[0], RunRecord)
    assert record.member_responses[0].parent_run_id == "team-run-1"
    assert AgnoTeamRunOutput(run_id="t", team_id="x", created_at=1).to_dict()["member_responses"] == []
    assert TeamRunRecord(run_id="t", team_id="x", created_at=1).to_dict()["member_responses"] == []


def test_run_record_from_dict_discriminates_on_team_id() -> None:
    nested_team = AgnoTeamRunOutput(
        run_id="inner",
        team_id="inner-team",
        session_id="s",
        messages=[AgnoMessage(role="user", content="inner", id="i1", created_at=1)],
        metrics=AgnoRunMetrics(input_tokens=1),
        status=AgnoRunStatus.completed,
        created_at=1,
    )
    outer = agno_team_run()
    outer.member_responses.append(nested_team)
    outer_dict = outer.to_dict()

    record = run_record_from_dict(outer_dict)

    assert isinstance(record, TeamRunRecord)
    assert [type(member) for member in record.member_responses] == [RunRecord, TeamRunRecord]
    assert isinstance(run_record_from_dict(agno_member_run().to_dict()), RunRecord)
    assert record.to_dict() == expected_run_dict(outer_dict)
    usage_only = AgnoRunOutput(
        run_id="u",
        model="m",
        model_provider="P",
        metrics=AgnoRunMetrics(),
        messages=[],
    ).to_dict()
    assert isinstance(run_record_from_dict(usage_only), RunRecord)


def test_run_status_values_and_names_match_agno() -> None:
    assert [(status.name, status.value) for status in RunStatus] == [
        (status.name, status.value) for status in AgnoRunStatus
    ]
    for status in RunStatus:
        # ``.name`` is persisted as ``mindroom_original_status``, so the member names must match too.
        assert AgnoRunStatus(status.value).name == status.name == status.value.lower()
        assert status == str(status)


@pytest.mark.parametrize("status", ["PENDING", "REGENERATED"])
def test_run_record_reads_every_status_agno_could_store(status: str) -> None:
    agno_dict = AgnoRunOutput(
        run_id="r",
        agent_id="a",
        status=AgnoRunStatus(status),
        messages=[],
        created_at=1,
    ).to_dict()

    assert RunRecord.from_dict(agno_dict).to_dict() == expected_run_dict(agno_dict)


def test_run_requirement_reads_rows_agno_read_leniently() -> None:
    """A requirement without a tool execution or with an unreadable timestamp still reads, as with Agno."""
    stored = {"id": "req-9", "created_at": "not a timestamp", "confirmation": True}
    agno = AgnoRunRequirement.from_dict(copy.deepcopy(stored))

    owned = RunRequirement.from_dict(stored)

    assert (owned.tool_execution.tool_name, owned.tool_execution.tool_args) == ("unknown", {})
    assert owned.created_at.tzinfo is not None
    agno_dict, owned_dict = expected_requirement_dict(agno.to_dict()), owned.to_dict()
    for result in (agno_dict, owned_dict):
        del result["created_at"], result["tool_execution"]["created_at"]
    assert owned_dict == agno_dict


@pytest.mark.parametrize(
    "stored",
    [
        {"confirmation": False, "confirmation_note": "No."},
        {"confirmation": True},
        {"external_execution_result": "done"},
        {"confirmation": False, "tool_execution": {"tool_name": "t", "requires_confirmation": True, "confirmed": True}},
    ],
    ids=["rejection", "approval", "external-result", "explicit-tool-decision-wins"],
)
def test_run_requirement_moves_a_decision_on_the_requirement_onto_its_tool_execution(stored: dict[str, Any]) -> None:
    data = {
        "id": "req-1",
        "created_at": "2026-01-02T03:04:05+00:00",
        "tool_execution": {"tool_name": "t", "requires_confirmation": True, "created_at": 3},
        **stored,
    }

    owned = RunRequirement.from_dict(copy.deepcopy(data)).to_dict()

    assert owned == expected_requirement_dict(AgnoRunRequirement.from_dict(copy.deepcopy(data)).to_dict())


@pytest.mark.parametrize(
    "stored",
    [{"id": "provider-file"}, {"id": "gemini-file", "external": {"uri": "gemini://f"}, "mime_type": "application/pdf"}],
    ids=["id-only", "external-only"],
)
def test_message_reads_files_agno_accepted_without_a_source(stored: dict[str, Any]) -> None:
    message = {"role": "user", "content": "see file", "files": [stored], "created_at": 1}
    [agno_file] = AgnoMessage.from_dict(copy.deepcopy(message)).files or []

    [owned_file] = Message.from_dict(message).files or []

    assert owned_file.id == agno_file.id == stored["id"]
    assert owned_file.to_dict() == {key: value for key, value in stored.items() if key != "external"}


# --- stored fixtures -------------------------------------------------------------------------------------------

FIXTURE_RUNS = Path(__file__).resolve().parents[1] / "fixtures" / "agno_parity" / "runs"

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
        agno_run_metrics().to_dict(),
        AgnoRunMetrics(
            input_tokens=10,
            output_tokens=1,
            cost=0.5,
            details={
                "model": [
                    AgnoModelMetrics(id="claude-opus-5-5", provider="Anthropic", input_tokens=10, cost=0.5),
                    AgnoModelMetrics(id="new-model", provider="P", input_tokens=1, provider_metrics={}),
                ],
                "memory_model": [],
            },
            additional_metrics={"eval_duration": 0.5, "label": "y", "flag": True},
        ).to_dict(),
        {},
        *json.loads((FIXTURE_RUNS / "run_metrics.json").read_text()).values(),
    ]


def test_session_metrics_count_runs_as_agno_accumulated_them() -> None:
    agno = AgnoSessionMetrics()
    owned = SessionMetrics()
    for case in _run_metrics_cases():
        agno.accumulate_from_run(AgnoRunMetrics.from_dict(copy.deepcopy(case)))
        owned.count_run(RunMetrics.from_dict(copy.deepcopy(case)))

        assert owned.to_dict() == agno.to_dict()
    assert SessionMetrics.from_dict(agno.to_dict()).to_dict() == agno.to_dict()


def test_session_metrics_count_a_saved_again_run_once() -> None:
    """A run saved when it paused and again when it finished adds only its final usage."""
    paused = RunMetrics.from_dict(agno_run_metrics().to_dict())
    finished = RunMetrics.from_dict(agno_run_metrics().to_dict())
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
