"""A streamed reply's live frames and the durable checkpoints they make of its edits."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

import mindroom.live_frames.publisher as publisher_module
import mindroom.streaming as streaming_module
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig, RouterConfig, StreamingConfig
from mindroom.constants import COMPACTIONS_CONTENT_KEY
from mindroom.history.reply_compactions import ReplyCompactions
from mindroom.history.types import CompactionLifecycleStart
from mindroom.live_frames.publisher import LivePublisher
from mindroom.live_frames.transport import LiveSendOutcome
from mindroom.llm.events import ReasoningDelta, ReportedCall, TextDelta, ToolCompleted, ToolProgress, ToolStarted
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.message_target import MessageTarget
from mindroom.streaming import (
    PROGRESS_PLACEHOLDER,
    StreamingResponse,
    UnfinishedStreamedReply,
    send_streaming_response,
    stream_progress_edits,
)
from mindroom.tool_system.events import StructuredStreamChunk, ToolTraceEntry
from mindroom.tool_system.runtime_context import WorkerProgressEvent, get_worker_progress_pump
from mindroom.workers.models import WorkerReadyProgress
from tests.conftest import bind_runtime_paths, make_matrix_client_mock, runtime_paths_for, test_runtime_paths
from tests.streaming_helpers import FakeReplyRecords

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
    from pathlib import Path

    import nio

_ROOM_ID = "!room:localhost"


def _config(tmp_path: Path, **streaming: Any) -> Config:  # noqa: ANN401
    config = Config(
        agents={"code": AgentConfig(display_name="Code", rooms=[_ROOM_ID])},
        models={"default": ModelConfig(provider="ollama", id="test-model")},
        router=RouterConfig(model="default"),
        defaults=DefaultsConfig(streaming=StreamingConfig(**streaming)),
    )
    return bind_runtime_paths(config, test_runtime_paths(tmp_path))


class _Frames:
    """The frames a reply sent, answered by a scripted outcome per frame."""

    def __init__(self, outcomes: list[LiveSendOutcome] | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.outcomes = outcomes or []

    async def send(
        self,
        _client: nio.AsyncClient,
        room_id: str,
        frame: dict[str, Any],
        *,
        transaction_id: str,
    ) -> LiveSendOutcome:
        assert room_id == _ROOM_ID
        assert transaction_id == f"{frame['sid']}.{frame['seq']}"
        self.sent.append(frame)
        return self.outcomes.pop(0) if self.outcomes else LiveSendOutcome.SENT


@pytest.fixture
def frames(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Frames]:
    """Relay live frames through a recording sender, ticking every 10 ms."""
    recorder = _Frames()

    async def relays(_homeserver_url: str) -> bool:
        return True

    monkeypatch.setattr(streaming_module, "homeserver_relays_live_frames", relays)
    monkeypatch.setattr(publisher_module, "send_live_frame", recorder.send)
    monkeypatch.setattr(publisher_module, "_FRAME_INTERVAL_SECONDS", 0.01)
    return recorder


class _Edits:
    """Progress edits the stream sent to Matrix."""

    def __init__(self) -> None:
        self.contents: list[dict[str, Any]] = []

    async def edit(
        self,
        _client: object,
        _room_id: str,
        _event_id: str,
        content: dict[str, Any],
        _text: str,
    ) -> DeliveredMatrixEvent:
        self.contents.append(content)
        return DeliveredMatrixEvent(event_id=f"$edit{len(self.contents)}", content_sent=dict(content))


async def _deliver(content: dict[str, Any]) -> DeliveredMatrixEvent:
    return DeliveredMatrixEvent(event_id="$answer", content_sent=dict(content))


def _records() -> FakeReplyRecords:
    async def send(_client: object, _room_id: str, content: dict[str, Any]) -> DeliveredMatrixEvent:
        return await _deliver(content)

    async def edit(
        _client: object,
        _room_id: str,
        _event_id: str,
        content: dict[str, Any],
        _text: str,
    ) -> DeliveredMatrixEvent:
        return await _deliver(content)

    return FakeReplyRecords(send=send, edit=edit)


async def _stream(
    *chunks: str | float | ReasoningDelta | ToolStarted | ToolProgress | ToolCompleted | Callable[[], object],
) -> AsyncIterator[Any]:
    """Yield text, reasoning, and tool events, pausing for each float and calling each callable as the turn would."""
    for chunk in chunks:
        if isinstance(chunk, float):
            await asyncio.sleep(chunk)
        elif isinstance(chunk, str):
            yield TextDelta(chunk)
        elif isinstance(chunk, ReasoningDelta | ToolStarted | ToolProgress | ToolCompleted):
            yield chunk
        else:
            chunk()


async def _run(
    config: Config,
    stream: AsyncIterator[Any],
    records: FakeReplyRecords,
    edits: _Edits,
    *,
    placeholder_event_id: str | None = None,
    existing_answer_event_id: str | None = None,
    show_tool_calls: bool = True,
    compactions: ReplyCompactions | None = None,
    resumed: UnfinishedStreamedReply | None = None,
    placeholder_ready: Callable[[], Awaitable[bool]] | None = None,
    allow_new_terminal_message: Callable[[], bool] | None = None,
) -> None:
    with patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=edits.edit)):
        await send_streaming_response(
            make_matrix_client_mock(user_id="@mindroom_code:localhost"),
            MessageTarget.resolve(_ROOM_ID, None, "$question"),
            config,
            runtime_paths_for(config),
            stream,
            existing_event_id=placeholder_event_id or existing_answer_event_id,
            adopt_existing_placeholder=placeholder_event_id is not None,
            show_tool_calls=show_tool_calls,
            compactions=compactions,
            resumed=resumed,
            placeholder_ready=placeholder_ready,
            allow_new_terminal_message=allow_new_terminal_message,
            **records.hooks(),
        )


def _terminal(records: FakeReplyRecords) -> dict[str, Any]:
    terminal = [write for write in records.writes if write.kind == "terminal"]
    assert len(terminal) == 1
    assert terminal[0].content is not None
    return terminal[0].content


@pytest.mark.asyncio
async def test_frames_carry_progress_and_durable_edits_wait_for_the_checkpoint(tmp_path: Path, frames: _Frames) -> None:
    """Text after the first message goes out as frames, and the terminal edit names the last frame it covers."""
    records, edits = _records(), _Edits()
    # Without frames, this ramp would edit the message after every chunk.
    config = _config(tmp_path, min_update_interval=0.01, interval_ramp_seconds=60)
    await _run(config, _stream("Hello", 0.05, " world", 0.05, "!", 0.05), records, edits)

    assert edits.contents == []
    assert frames.sent[0]["seq"] == 0
    assert frames.sent[0]["reply"] == "$answer"
    assert frames.sent[0]["snapshot"]["status"] == "streaming"
    assert [frame["seq"] for frame in frames.sent] == list(range(len(frames.sent)))
    assert all("delta" in frame for frame in frames.sent[1:])
    final_md = frames.sent[0]["snapshot"]["md"]
    for frame in frames.sent[1:]:
        splice = frame["delta"]["md"]
        final_md = final_md[: splice["at"]] + splice["ins"] + final_md[splice["at"] + splice["del"] :]
    assert final_md == "Hello world!"
    terminal = _terminal(records)
    assert terminal["body"] == "Hello world!"
    assert terminal["io.mindroom.live_checkpoint"] == {"sid": frames.sent[-1]["sid"], "seq": frames.sent[-1]["seq"]}


@pytest.mark.asyncio
async def test_reasoning_streams_live_and_the_final_event_keeps_it(tmp_path: Path, frames: _Frames) -> None:
    """Reasoning never enters the answer text; frames show it, and the terminal update keeps it with its duration."""
    records, edits = _records(), _Edits()
    stream = _stream("Answer", 0.03, ReasoningDelta("Checking the cache"), 0.05, " is ready.", 0.05)
    await _run(_config(tmp_path), stream, records, edits)

    terminal = _terminal(records)
    assert terminal["body"] == "Answer is ready."
    assert terminal["io.mindroom.reasoning"]["text"] == "Checking the cache"
    assert terminal["io.mindroom.reasoning"]["duration_ms"] >= 40
    shown = [
        frame.get("snapshot", {}).get("reasoning") or frame.get("delta", {}).get("reasoning") for frame in frames.sent
    ]
    assert any(shown)
    assert all("io.mindroom.reasoning" not in content for content in edits.contents)


def _reasoning_segment_stream() -> AsyncIterator[Any]:
    started = ReportedCall(tool_call_id="call-1", tool_name="web_search", tool_args={"query": "matrix"})
    completed = ReportedCall(tool_call_id="call-1", tool_name="web_search", tool_args={"query": "matrix"}, result="ok")
    return _stream(
        ReasoningDelta("🙂 first"),
        0.05,
        ToolStarted(started),
        0.05,
        ToolCompleted(completed),
        0.05,
        ReasoningDelta("second"),
        0.05,
        "Done.",
        0.05,
    )


def _live_segments(sent: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The segments the frames left, taking each snapshot's and each delta that carries them."""
    segments: list[dict[str, Any]] = []
    for frame in sent:
        segments = frame.get("snapshot", frame.get("delta", {})).get("segments", segments)
    return segments


@pytest.mark.asyncio
async def test_reasoning_segments_record_where_each_began(tmp_path: Path, frames: _Frames) -> None:
    """Each reasoning segment keeps its UTF-16 start, the tool calls before it, and its own duration."""
    records, edits = _records(), _Edits()
    await _run(_config(tmp_path), _reasoning_segment_stream(), records, edits, placeholder_event_id="$placeholder")

    reasoning = _terminal(records)["io.mindroom.reasoning"]
    assert reasoning["text"] == "🙂 first\n\nsecond"
    segments = reasoning["segments"]
    assert [(segment["start"], segment["after_tool"]) for segment in segments] == [
        (0, 0),
        (len("🙂 first\n\n".encode("utf-16-le")) // 2, 1),
    ]
    assert segments[1]["start"] == 10
    assert all(isinstance(segment["duration_ms"], int) and segment["duration_ms"] >= 0 for segment in segments)
    assert abs(sum(segment["duration_ms"] for segment in segments) - reasoning["duration_ms"]) <= len(segments)
    assert _live_segments(frames.sent) == [
        {"start": segment["start"], "after_tool": segment["after_tool"]} for segment in segments
    ]


@pytest.mark.asyncio
async def test_reasoning_segments_anchor_to_the_top_when_tool_calls_are_hidden(tmp_path: Path, frames: _Frames) -> None:
    """Without tool-call markers in the body, every segment sits at the top of the reply."""
    records, edits = _records(), _Edits()
    await _run(
        _config(tmp_path),
        _reasoning_segment_stream(),
        records,
        edits,
        placeholder_event_id="$placeholder",
        show_tool_calls=False,
    )

    segments = _terminal(records)["io.mindroom.reasoning"]["segments"]
    assert len(segments) == 2
    assert all(segment["after_tool"] == 0 for segment in segments)
    assert all(segment["after_tool"] == 0 for segment in _live_segments(frames.sent))


@pytest.mark.asyncio
async def test_empty_reasoning_deltas_open_no_segment(tmp_path: Path, frames: _Frames) -> None:
    """A provider's empty reasoning delta adds no segment, so segment starts stay strictly increasing."""
    records, edits = _records(), _Edits()
    started = ReportedCall(tool_call_id="call-1", tool_name="web_search", tool_args={"query": "matrix"})
    completed = ReportedCall(tool_call_id="call-1", tool_name="web_search", tool_args={"query": "matrix"}, result="ok")
    stream = _stream(
        ReasoningDelta(""),
        0.03,
        ToolStarted(started),
        0.03,
        ToolCompleted(completed),
        0.03,
        ReasoningDelta("a"),
        0.03,
        "Answer",
        0.03,
        ReasoningDelta(""),
        0.03,
        " is",
        0.03,
        ReasoningDelta("b"),
        0.03,
        " ready.",
        0.03,
    )
    await _run(_config(tmp_path), stream, records, edits, placeholder_event_id="$placeholder")

    reasoning = _terminal(records)["io.mindroom.reasoning"]
    assert reasoning["text"] == "a\n\nb"
    assert [(segment["start"], segment["after_tool"]) for segment in reasoning["segments"]] == [(0, 1), (3, 1)]
    assert _live_segments(frames.sent) == [{"start": 0, "after_tool": 1}, {"start": 3, "after_tool": 1}]


@pytest.mark.asyncio
async def test_reasoning_after_two_tool_calls_anchors_after_the_second(tmp_path: Path, frames: _Frames) -> None:
    """A segment that begins after two completed tool calls counts both of them."""
    records, edits = _records(), _Edits()
    first = ReportedCall(tool_call_id="call-1", tool_name="web_search", tool_args={"query": "matrix"})
    second = ReportedCall(tool_call_id="call-2", tool_name="web_search", tool_args={"query": "nio"})
    stream = _stream(
        ToolStarted(first),
        0.03,
        ToolCompleted(
            ReportedCall(tool_call_id="call-1", tool_name="web_search", tool_args={"query": "matrix"}, result="ok"),
        ),
        0.03,
        ToolStarted(second),
        0.03,
        ToolCompleted(
            ReportedCall(tool_call_id="call-2", tool_name="web_search", tool_args={"query": "nio"}, result="ok"),
        ),
        0.03,
        ReasoningDelta("Both searches agree."),
        0.03,
        "Answer.",
        0.03,
    )
    await _run(_config(tmp_path), stream, records, edits, placeholder_event_id="$placeholder")

    (segment,) = _terminal(records)["io.mindroom.reasoning"]["segments"]
    assert (segment["start"], segment["after_tool"]) == (0, 2)
    assert _live_segments(frames.sent) == [{"start": 0, "after_tool": 2}]


@pytest.mark.asyncio
async def test_a_reasoning_segment_open_at_the_end_gets_its_duration(tmp_path: Path, frames: _Frames) -> None:
    """A stream that ends while reasoning closes that segment before the terminal update records it."""
    records, edits = _records(), _Edits()
    await _run(_config(tmp_path), _stream("Answer", 0.03, ReasoningDelta("Still thinking"), 0.05), records, edits)

    assert frames.sent
    (segment,) = _terminal(records)["io.mindroom.reasoning"]["segments"]
    assert isinstance(segment["duration_ms"], int)
    assert segment["duration_ms"] >= 40


@pytest.mark.asyncio
async def test_reasoning_time_starts_when_the_provider_says_reasoning_began(tmp_path: Path, frames: _Frames) -> None:
    """A provider that sends reasoning only after the model finished it says when it began, and the duration counts it."""
    records, edits = _records(), _Edits()
    began = time.monotonic() - 3.0
    await _run(
        _config(tmp_path),
        _stream(ReasoningDelta("Planned", started_at=began), 0.02, "Done.", 0.02),
        records,
        edits,
    )

    assert frames.sent
    assert _terminal(records)["io.mindroom.reasoning"]["duration_ms"] >= 3000


class _Compaction:
    """One automatic compaction the turn runs while its reply streams, recorded through the reply's recorder."""

    def __init__(self, recorder: ReplyCompactions) -> None:
        self.recorder = recorder
        self.entry_id: str | None = None

    def start(self) -> None:
        self.entry_id = self.recorder.start(
            CompactionLifecycleStart(
                mode="auto",
                session_id="session-1",
                scope="agent:code",
                summary_model="haiku",
                before_tokens=30_000,
                history_budget_tokens=100_000,
                runs_before=20,
                phase="mid_turn",
            ),
        )

    def progress(self) -> None:
        assert self.entry_id is not None
        self.recorder.update(self.entry_id, status="running", after_tokens=9_000, runs_remaining=12, duration_ms=50)

    def succeed(self) -> None:
        assert self.entry_id is not None
        self.recorder.update(self.entry_id, status="success", after_tokens=4_000, compacted_run_count=20)

    def fail(self) -> None:
        assert self.entry_id is not None
        self.recorder.update(self.entry_id, status="failed", failure_reason="Summary model failed.", duration_ms=70)


def _two_tool_calls_then(*after: str | float | Callable[[], object]) -> AsyncIterator[Any]:
    first = ReportedCall(tool_call_id="call-1", tool_name="web_search", tool_args={"query": "matrix"})
    second = ReportedCall(tool_call_id="call-2", tool_name="web_search", tool_args={"query": "nio"})
    return _stream(
        ToolStarted(first),
        0.03,
        ToolCompleted(ReportedCall(tool_call_id="call-1", tool_name="web_search", tool_args={}, result="ok")),
        0.03,
        ToolStarted(second),
        0.03,
        ToolCompleted(ReportedCall(tool_call_id="call-2", tool_name="web_search", tool_args={}, result="ok")),
        0.03,
        *after,
    )


def _live_compactions(sent: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The compaction entries the frames left, taking each snapshot's and each delta that carries them."""
    compactions: list[dict[str, Any]] = []
    for frame in sent:
        compactions = frame.get("snapshot", frame.get("delta", {})).get("compactions", compactions)
    return compactions


async def _anchored_compaction(tmp_path: Path, frames: _Frames, *, show_tool_calls: bool) -> tuple[int, int]:
    """Run a compaction after two tool calls; return where the final event and the frames anchor it."""
    records, edits, recorder = _records(), _Edits(), ReplyCompactions()
    compaction = _Compaction(recorder)
    stream = _two_tool_calls_then(compaction.start, 0.05, compaction.succeed, 0.05, "Answer.", 0.03)
    await _run(
        _config(tmp_path),
        stream,
        records,
        edits,
        placeholder_event_id="$placeholder",
        show_tool_calls=show_tool_calls,
        compactions=recorder,
    )

    [entry] = _terminal(records)[COMPACTIONS_CONTENT_KEY]
    [live_entry] = _live_compactions(frames.sent)
    assert live_entry["status"] == "success"
    return entry["after_tool"], live_entry["after_tool"]


@pytest.mark.asyncio
async def test_compaction_after_two_visible_tools_anchors_after_tool_2(tmp_path: Path, frames: _Frames) -> None:
    """A compaction that began after two shown tool calls sits after the second, in frames and the final event."""
    assert await _anchored_compaction(tmp_path, frames, show_tool_calls=True) == (2, 2)


@pytest.mark.asyncio
async def test_compaction_with_hidden_tool_calls_anchors_at_zero(tmp_path: Path, frames: _Frames) -> None:
    """Without tool-call markers in the body, a compaction sits at the top of the reply."""
    assert await _anchored_compaction(tmp_path, frames, show_tool_calls=False) == (0, 0)


@pytest.mark.asyncio
async def test_compaction_in_a_resumed_reply_anchors_after_the_resumed_tools(tmp_path: Path, frames: _Frames) -> None:
    """A resumed reply's earlier tool calls count: a compaction before any new tool sits after them."""
    records, edits, recorder = _records(), _Edits(), ReplyCompactions()
    compaction = _Compaction(recorder)
    resumed = UnfinishedStreamedReply(
        visible_text="🔧 `counter` [1]\n\nHalf of the report",
        tool_trace=(
            ToolTraceEntry(type="tool_call_completed", tool_name="counter", args_preview="{}", result_preview="1"),
        ),
    )
    stream = _stream(compaction.start, 0.05, compaction.succeed, 0.05, "The rest.", 0.03)
    await _run(
        _config(tmp_path),
        stream,
        records,
        edits,
        placeholder_event_id="$placeholder",
        compactions=recorder,
        resumed=resumed,
    )

    [entry] = _terminal(records)[COMPACTIONS_CONTENT_KEY]
    assert entry["after_tool"] == 1
    assert [live["after_tool"] for live in _live_compactions(frames.sent)] == [1]


@pytest.mark.asyncio
@pytest.mark.usefixtures("frames")
@pytest.mark.parametrize("approved", [False, True], ids=["declined", "approved"])
async def test_compaction_before_the_participation_decision_creates_no_reply(tmp_path: Path, approved: bool) -> None:
    """A compaction before the turn may become visible sends nothing: a declined turn leaves no reply.

    An approved one's placeholder, sent once the decision allows it, carries the entry.
    """
    records, edits, recorder = _records(), _Edits(), ReplyCompactions()
    compaction = _Compaction(recorder)
    decided = asyncio.Event()
    creates_before_decision: list[int] = []

    async def placeholder_ready() -> bool:
        await decided.wait()
        return approved

    def decide() -> None:
        creates_before_decision.append(sum(write.kind == "create" for write in records.writes))
        decided.set()

    stream = _stream(compaction.start, 0.1, compaction.succeed, 0.1, decide, 0.05)
    await _run(
        _config(tmp_path, update_interval=0.05),
        stream,
        records,
        edits,
        compactions=recorder,
        placeholder_ready=placeholder_ready,
        allow_new_terminal_message=lambda: approved,
    )

    assert creates_before_decision == [0]
    creates = [write for write in records.writes if write.kind == "create"]
    if not approved:
        assert creates == []
        assert [write for write in records.writes if write.kind == "terminal"] == []
        assert edits.contents == []
        return
    [create] = creates
    assert create.content is not None
    assert create.content["body"] == PROGRESS_PLACEHOLDER
    assert [entry["status"] for entry in create.content[COMPACTIONS_CONTENT_KEY]] == ["success"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("frames")
@pytest.mark.parametrize("live_frames", [False, True], ids=["edits", "frames"])
async def test_a_regenerations_compaction_never_blanks_the_answer_it_replaces(
    tmp_path: Path,
    live_frames: bool,
) -> None:
    """A compaction before a regeneration's first text leaves the prior answer; its first real write carries it."""
    records, edits, recorder = _records(), _Edits(), ReplyCompactions()
    compaction = _Compaction(recorder)
    stream = _stream(compaction.start, 0.15, compaction.succeed, 0.15, "New answer.", 0.03)
    await _run(
        _config(tmp_path, update_interval=0.05, live_frames=live_frames),
        stream,
        records,
        edits,
        existing_answer_event_id="$prior_answer",
        compactions=recorder,
    )

    assert PROGRESS_PLACEHOLDER not in [content["body"] for content in edits.contents]
    shown = [content for content in edits.contents if COMPACTIONS_CONTENT_KEY in content]
    assert all(content["body"] == "New answer." for content in shown)
    [entry] = _terminal(records)[COMPACTIONS_CONTENT_KEY]
    assert entry["status"] == "success"


@pytest.mark.asyncio
async def test_compaction_start_on_placeholder_sends_progress_edit(tmp_path: Path) -> None:
    """A compaction that starts while only the placeholder shows edits the placeholder at once to show it."""
    records, edits, recorder = _records(), _Edits(), ReplyCompactions()
    compaction = _Compaction(recorder)
    config = _config(tmp_path, live_frames=False)
    stream = _stream(compaction.start, 0.1, compaction.progress, 0.1, compaction.succeed, 0.1, "Done.", 0.03)
    await _run(config, stream, records, edits, placeholder_event_id="$placeholder", compactions=recorder)

    statuses = [
        [entry["status"] for entry in content[COMPACTIONS_CONTENT_KEY]]
        for content in edits.contents
        if COMPACTIONS_CONTENT_KEY in content
    ]
    assert statuses[0] == ["running"]
    assert ["success"] in statuses
    placeholder_edit = next(content for content in edits.contents if COMPACTIONS_CONTENT_KEY in content)
    assert placeholder_edit["body"] == PROGRESS_PLACEHOLDER
    assert "runs_remaining" not in _terminal(records)[COMPACTIONS_CONTENT_KEY][0]


@pytest.mark.asyncio
async def test_compaction_change_schedules_checkpoint_with_live_frames(tmp_path: Path, frames: _Frames) -> None:
    """While frames carry progress, a compaction change alone still earns the next durable checkpoint."""
    records, edits, recorder = _records(), _Edits(), ReplyCompactions()
    compaction = _Compaction(recorder)
    # "Hello" is the reply's first event, so after it only the compaction is left to checkpoint.
    stream = _stream("Hello", 0.05, compaction.start, 0.4)
    await _run(_config(tmp_path, update_interval=0.1), stream, records, edits, compactions=recorder)

    checkpoints = [content for content in edits.contents if COMPACTIONS_CONTENT_KEY in content]
    assert checkpoints
    assert checkpoints[0]["body"] == "Hello"
    assert "io.mindroom.live_checkpoint" in checkpoints[0]
    assert [entry["status"] for entry in _live_compactions(frames.sent)] == ["running"]


@pytest.mark.asyncio
async def test_compaction_on_placeholder_earns_a_checkpoint_with_live_frames(tmp_path: Path, frames: _Frames) -> None:
    """With frames on and no text yet, a compaction that starts over the placeholder still earns a durable checkpoint."""
    records, edits, recorder = _records(), _Edits(), ReplyCompactions()
    compaction = _Compaction(recorder)
    # The pause lets frames go live first, so the compaction reaches the reply only through a checkpoint.
    stream = _stream(0.15, compaction.start, 0.4, "Done.", 0.03)
    await _run(
        _config(tmp_path, update_interval=0.1),
        stream,
        records,
        edits,
        placeholder_event_id="$placeholder",
        compactions=recorder,
    )

    checkpoints = [
        content
        for content in edits.contents
        if COMPACTIONS_CONTENT_KEY in content and "io.mindroom.live_checkpoint" in content
    ]
    assert checkpoints
    assert checkpoints[0]["body"] == PROGRESS_PLACEHOLDER
    assert [entry["status"] for entry in checkpoints[0][COMPACTIONS_CONTENT_KEY]] == ["running"]
    assert edits.contents.index(checkpoints[0]) < next(
        index for index, content in enumerate(edits.contents) if content["body"] == "Done."
    )
    assert [entry["status"] for entry in _live_compactions(frames.sent)] == ["running"]


def test_compaction_change_is_uncommitted_once_an_event_shows_the_stream(tmp_path: Path) -> None:
    """A change the stream's event does not show yet is uncommitted; before one shows it, its first write does.

    An earlier answer a regeneration writes over does not show the stream.
    """
    recorder = ReplyCompactions()
    config = _config(tmp_path)
    streaming = StreamingResponse(
        target=MessageTarget.resolve(_ROOM_ID, None, "$question"),
        config=config,
        runtime_paths=runtime_paths_for(config),
        compactions=recorder,
        **_records().hooks(),
    )
    _Compaction(recorder).start()
    assert not streaming.has_uncommitted_presentation()

    streaming.event_id = "$prior_answer"
    assert not streaming.has_uncommitted_presentation()

    # As an adopted placeholder, or the stream's own first write, makes it.
    streaming._shows_stream = True
    assert streaming.has_uncommitted_presentation()


@pytest.mark.asyncio
@pytest.mark.usefixtures("frames")
async def test_final_event_keeps_success_entry(tmp_path: Path) -> None:
    """The final event keeps the finished compaction, without the running-only remaining-runs count."""
    records, edits, recorder = _records(), _Edits(), ReplyCompactions()
    compaction = _Compaction(recorder)
    stream = _stream(compaction.start, 0.03, compaction.progress, 0.03, compaction.succeed, "Answer.", 0.03)
    await _run(_config(tmp_path), stream, records, edits, placeholder_event_id="$placeholder", compactions=recorder)

    [entry] = _terminal(records)[COMPACTIONS_CONTENT_KEY]
    assert entry["status"] == "success"
    assert entry["after_tokens"] == 4_000
    assert entry["compacted_run_count"] == 20
    assert "runs_remaining" not in entry
    assert _terminal(records)["body"] == "Answer."


@pytest.mark.asyncio
async def test_failed_compaction_is_terminal_on_final_event(tmp_path: Path, frames: _Frames) -> None:
    """A compaction that failed mid-reply ends failed on the final event, with its reason."""
    records, edits, recorder = _records(), _Edits(), ReplyCompactions()
    compaction = _Compaction(recorder)
    stream = _stream("Partial", 0.03, compaction.start, 0.03, compaction.progress, compaction.fail, " answer.", 0.03)
    await _run(_config(tmp_path), stream, records, edits, compactions=recorder)

    [entry] = _terminal(records)[COMPACTIONS_CONTENT_KEY]
    assert entry["status"] == "failed"
    assert entry["failure_reason"] == "Summary model failed."
    assert "runs_remaining" not in entry
    assert [live["status"] for live in _live_compactions(frames.sent)] == ["failed"]


@pytest.mark.asyncio
async def test_reply_without_compactions_carries_no_key(tmp_path: Path, frames: _Frames) -> None:
    """A reply that ran no compaction writes no compactions key, durably or live."""
    records, edits, recorder = _records(), _Edits(), ReplyCompactions()
    await _run(_config(tmp_path), _stream("Hello", 0.05), records, edits, compactions=recorder)

    assert COMPACTIONS_CONTENT_KEY not in _terminal(records)
    assert _live_compactions(frames.sent) == []


@pytest.mark.asyncio
async def test_without_relaying_homeserver_edits_keep_their_ordinary_cadence(tmp_path: Path) -> None:
    """A homeserver that does not relay frames gets the usual fast progress edits and no watermark."""
    records, edits = _records(), _Edits()
    config = _config(tmp_path, update_interval=0.01, min_update_interval=0.01, interval_ramp_seconds=0)
    await _run(config, _stream("Hello", 0.05, " world", 0.05), records, edits)

    assert edits.contents
    assert "io.mindroom.live_checkpoint" not in _terminal(records)


@pytest.mark.asyncio
async def test_switched_off_frames_are_never_sent(tmp_path: Path, frames: _Frames) -> None:
    """``defaults.streaming.live_frames: false`` keeps every reply on durable edits."""
    records, edits = _records(), _Edits()
    await _run(_config(tmp_path, live_frames=False), _stream("Hello", 0.05, " world", 0.05), records, edits)

    assert frames.sent == []
    assert "io.mindroom.live_checkpoint" not in _terminal(records)


@pytest.mark.asyncio
async def test_a_quiet_stream_still_gets_a_durable_checkpoint(tmp_path: Path, frames: _Frames) -> None:
    """While nothing arrives, as during a long tool call, the durable reply still catches up every update interval."""
    records, edits = _records(), _Edits()
    await _run(_config(tmp_path, update_interval=0.1), _stream("Hello", 0.05, " world", 0.4), records, edits)

    assert any(content["body"] == "Hello world" for content in edits.contents)
    checkpointed = [content["io.mindroom.live_checkpoint"] for content in edits.contents]
    assert all(checkpoint["sid"] == frames.sent[0]["sid"] for checkpoint in checkpointed)


@pytest.mark.asyncio
async def test_tool_calls_ride_in_frames_until_the_checkpoint(tmp_path: Path, frames: _Frames) -> None:
    """A tool start, which edits at once without frames, shows in frames and waits for the checkpoint."""
    records, edits = _records(), _Edits()
    started = ReportedCall(tool_call_id="call-1", tool_name="web_search", tool_args={"query": "matrix"})
    completed = ReportedCall(tool_call_id="call-1", tool_name="web_search", tool_args={"query": "matrix"}, result="ok")
    stream = _stream("Looking", 0.05, ToolStarted(started), 0.05, ToolCompleted(completed), 0.05, " done", 0.05)
    await _run(_config(tmp_path), stream, records, edits)

    assert edits.contents == []
    tools = [frame["delta"]["tools"] for frame in frames.sent if "tools" in frame.get("delta", {})]
    assert [change["set"][0][1]["type"] for change in tools] == ["tool_call_started", "tool_call_completed"]
    assert _terminal(records)["io.mindroom.tool_trace"]["events"][0]["type"] == "tool_call_completed"


def _tool_states(sent: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """The tools document each frame left, applying snapshots and tools deltas in order."""
    tools: list[dict[str, Any]] = []
    states = []
    for frame in sent:
        if "snapshot" in frame:
            tools = list(frame["snapshot"]["tools"])
        elif "tools" in frame["delta"]:
            change = frame["delta"]["tools"]
            tools = (tools + [{}] * change["len"])[: change["len"]]
            for index, entry in change["set"]:
                tools[index] = entry
        states.append(list(tools))
    return states


def _running_shell(*between: float | ToolProgress) -> AsyncIterator[Any]:
    started = ReportedCall(tool_call_id="call-1", tool_name="run_shell_command", tool_args={"command": "make"})
    completed = ReportedCall(
        tool_call_id="call-1",
        tool_name="run_shell_command",
        tool_args={"command": "make"},
        result="built",
    )
    return _stream(
        "Building",
        0.05,
        ToolStarted(started),
        0.05,
        *between,
        ToolCompleted(completed),
        0.05,
        " done",
        0.05,
    )


@pytest.mark.asyncio
async def test_a_running_tool_shows_its_latest_output_in_frames_only(tmp_path: Path, frames: _Frames) -> None:
    """A running call's reported output rides in its frame entry until it completes; no write keeps it."""
    records, edits = _records(), _Edits()
    stream = _running_shell(ToolProgress("call-1", "step 1"), 0.05, ToolProgress("call-1", "x" * 1000), 0.05)
    await _run(_config(tmp_path), stream, records, edits)

    entries = [state[0] for state in _tool_states(frames.sent) if state]
    shown = [entry.get("progress") for entry in entries]
    assert "progress" not in entries[0]
    assert "step 1" in shown
    assert "x" * 1000 in shown
    assert entries[-1]["type"] == "tool_call_completed"
    assert "progress" not in entries[-1]
    written = [write.content for write in records.writes if write.content is not None] + edits.contents
    assert all('"progress"' not in json.dumps(content) for content in written)


@pytest.mark.asyncio
async def test_a_continued_reply_shows_a_running_calls_output_in_frames(tmp_path: Path, frames: _Frames) -> None:
    """Work resumed after an approval shows a running call's output in the reply it continues."""
    config = _config(tmp_path)
    started = [ToolTraceEntry(type="tool_call_started", tool_name="run_shell_command", tool_call_id="call-1")]
    chunk = StructuredStreamChunk(
        content="Approved.\n\n🔧 `run_shell_command` [1] ⏳",
        tool_trace=started,
        tool_progress={"call-1": "step 1"},
    )
    with patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=_Edits().edit)):
        async with stream_progress_edits(
            make_matrix_client_mock(user_id="@mindroom_code:localhost"),
            MessageTarget.resolve(_ROOM_ID, None, "$question"),
            config,
            runtime_paths_for(config),
            event_id="$answer",
            show_tool_calls=True,
            **FakeReplyRecords().progress_hooks(),
        ) as publish:
            await publish(chunk)
            await asyncio.sleep(0.1)

    entries = [state[0] for state in _tool_states(frames.sent) if state]
    assert any(entry.get("progress") == "step 1" for entry in entries)


@pytest.mark.asyncio
async def test_hidden_tool_calls_show_no_output(tmp_path: Path, frames: _Frames) -> None:
    """A reply that hides its tool calls shows none of their output either."""
    records, edits = _records(), _Edits()
    await _run(
        _config(tmp_path),
        _running_shell(ToolProgress("call-1", "step 1"), 0.05),
        records,
        edits,
        show_tool_calls=False,
    )

    assert all(state == [] for state in _tool_states(frames.sent))


@pytest.mark.asyncio
async def test_reasoning_before_the_answer_streams_over_the_placeholder(tmp_path: Path, frames: _Frames) -> None:
    """A model that thinks before it answers shows its reasoning live over the reply's Thinking placeholder."""
    records, edits = _records(), _Edits()
    stream = _stream(ReasoningDelta("Comparing the two caches"), 0.1, "Answer", 0.05)
    await _run(_config(tmp_path), stream, records, edits, placeholder_event_id="$placeholder")

    first = frames.sent[0]
    assert first["reply"] == "$placeholder"
    assert first["snapshot"]["md"] == ""
    assert first["snapshot"]["reasoning"] == "Comparing the two caches"
    assert _terminal(records)["io.mindroom.reasoning"]["text"] == "Comparing the two caches"


def _warmup(phase: str) -> WorkerProgressEvent:
    return WorkerProgressEvent(
        tool_name="shell",
        function_name="run",
        progress=WorkerReadyProgress(
            phase=phase,
            worker_key="worker-a",
            backend_name="kubernetes",
            elapsed_seconds=2.0,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("frames")
async def test_a_checkpoint_clears_a_finished_worker_warmup(tmp_path: Path) -> None:
    """Once the worker is ready, the next checkpoint drops its warm-up line, even while the tool keeps running."""
    records, edits = _records(), _Edits()

    async def stream() -> AsyncIterator[Any]:
        pump = get_worker_progress_pump()
        assert pump is not None
        pump.queue.put_nowait(_warmup("cold_start"))
        await asyncio.sleep(0.3)
        pump.queue.put_nowait(_warmup("ready"))
        await asyncio.sleep(0.4)
        yield TextDelta("done")

    await _run(_config(tmp_path, update_interval=0.1), stream(), records, edits, placeholder_event_id="$placeholder")

    warm = [index for index, content in enumerate(edits.contents) if "io.mindroom.warmup_suffix" in content]
    assert warm
    cleared = [content for content in edits.contents[warm[-1] + 1 :] if "io.mindroom.warmup_suffix" not in content]
    assert cleared
    assert cleared[0]["body"] == "Thinking..."


@pytest.mark.asyncio
@pytest.mark.usefixtures("frames")
async def test_a_frame_loop_error_ends_frames() -> None:
    """A bug while building a frame ends frames for the reply, so its edits fall back to the ordinary cadence."""
    publisher = LivePublisher(client=make_matrix_client_mock(), room_id=_ROOM_ID)

    def broken() -> None:
        raise RuntimeError

    stopped: list[bool] = []
    await publisher.run(current=broken, on_tick=lambda: None, on_stopped=lambda: stopped.append(True))

    assert publisher.running is False
    assert stopped == [True]


@pytest.mark.asyncio
async def test_a_failing_send_is_not_repeated_every_tick(tmp_path: Path, frames: _Frames) -> None:
    """While sends fail at once, as through a proxy during a homeserver restart, frames back off instead of resending."""
    frames.outcomes = [LiveSendOutcome.RETRY] * 100
    records, edits = _records(), _Edits()
    await _run(_config(tmp_path), _stream("Hello", 0.3), records, edits, placeholder_event_id="$placeholder")

    assert 1 <= len(frames.sent) <= 2


@pytest.mark.asyncio
async def test_stopped_frames_catch_up_at_once(tmp_path: Path, frames: _Frames) -> None:
    """When frames stop, refused or too large to send, the durable reply catches up at once instead of at the checkpoint."""
    frames.outcomes = [LiveSendOutcome.SENT, LiveSendOutcome.STOP]
    records, edits = _records(), _Edits()
    await _run(_config(tmp_path), _stream("Hello", 0.05, " world", 0.1, " again", 0.05), records, edits)

    assert len(frames.sent) == 2
    assert edits.contents[0]["body"].startswith("Hello world")
    assert edits.contents[0]["io.mindroom.live_checkpoint"] == {"sid": frames.sent[0]["sid"], "seq": 1}


@pytest.mark.asyncio
async def test_a_terminal_update_cancelled_before_it_is_recorded_keeps_what_frames_showed(
    tmp_path: Path,
    frames: _Frames,
) -> None:
    """A Stop during the final transform leaves the reply's records holding the text frames showed, not the create."""
    records, edits = _records(), _Edits()
    transforming = asyncio.Event()

    async def transform(text: str) -> str:
        transforming.set()
        await asyncio.Event().wait()
        return text

    with patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=edits.edit)):
        config = _config(tmp_path, update_interval=60)
        response = asyncio.create_task(
            send_streaming_response(
                make_matrix_client_mock(user_id="@mindroom_code:localhost"),
                MessageTarget.resolve(_ROOM_ID, None, "$question"),
                config,
                runtime_paths_for(config),
                _stream("Hello", 0.05, " world", 0.05),
                final_text_transform=transform,
                **records.hooks(),
            ),
        )
        await asyncio.wait_for(transforming.wait(), timeout=5)
        response.cancel()
        with pytest.raises(asyncio.CancelledError):
            await response

    assert any("world" in json.dumps(frame) for frame in frames.sent)
    assert [write.kind for write in records.writes] == ["create", "shown_live"]
    assert records.writes[-1].progress.text == "Hello world"


async def _until(condition: Callable[[], bool]) -> None:
    while not condition():  # noqa: ASYNC110
        await asyncio.sleep(0.01)


def _shown_by(frames: _Frames, text: str) -> Callable[[], bool]:
    return lambda: any(text in json.dumps(frame) for frame in frames.sent)


async def _until_stopped(*chunks: str, ready: Callable[[], bool] | None = None) -> AsyncIterator[Any]:
    """Yield ``chunks``, then wait forever, as a model the user stops; ``ready`` holds the last chunk back."""
    for chunk in chunks[:-1]:
        yield TextDelta(chunk)
        await asyncio.sleep(0.05)
    if ready is not None:
        await asyncio.wait_for(_until(ready), timeout=5)
    yield TextDelta(chunks[-1])
    await asyncio.Event().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("frames_state", ["switched off", "none sent yet"])
async def test_a_reply_ending_early_without_a_frame_records_nothing_more(
    tmp_path: Path,
    frames: _Frames,
    monkeypatch: pytest.MonkeyPatch,
    frames_state: str,
) -> None:
    """Without a frame shown, the records keep what the edits wrote, so the note goes below the last edit."""
    if frames_state == "none sent yet":
        monkeypatch.setattr(publisher_module, "_FRAME_INTERVAL_SECONDS", 30.0)
    config = _config(
        tmp_path,
        update_interval=60,
        interval_ramp_seconds=0,
        max_idle=30,
        live_frames=frames_state == "none sent yet",
    )
    records, edits = _records(), _Edits()
    stream = _until_stopped("Hello", " world", ready=lambda: bool(records.writes))
    response = asyncio.create_task(_run(config, stream, records, edits))
    await asyncio.sleep(0.2)
    response.cancel()
    with pytest.raises(asyncio.CancelledError):
        await response

    assert frames.sent == []
    assert edits.contents == []
    assert [write.kind for write in records.writes] == ["create"]


@pytest.mark.asyncio
async def test_frames_that_stopped_on_their_own_record_no_text_nothing_showed(tmp_path: Path, frames: _Frames) -> None:
    """Once frames stop and an edit caught up with them, an early end records nothing that neither showed."""
    frames.outcomes = [LiveSendOutcome.SENT, LiveSendOutcome.STOP]
    config = _config(tmp_path, update_interval=60, interval_ramp_seconds=0.001, max_idle=30)
    records, edits = _records(), _Edits()
    caught_up = lambda: any(content["body"] == "Hello world" for content in edits.contents)  # noqa: E731
    stream = _until_stopped("Hello", " world", " unseen", ready=caught_up)
    response = asyncio.create_task(_run(config, stream, records, edits))
    await asyncio.wait_for(_until(caught_up), timeout=5)
    await asyncio.sleep(0.2)
    response.cancel()
    with pytest.raises(asyncio.CancelledError):
        await response

    assert len(frames.sent) == 2
    assert not any("unseen" in json.dumps(frame) for frame in frames.sent)
    assert not any("unseen" in content["body"] for content in edits.contents)
    assert not any("unseen" in write.progress.text for write in records.writes)


@pytest.mark.asyncio
@pytest.mark.parametrize("ended_early", [True, False])
async def test_a_continued_reply_left_early_keeps_what_frames_showed(
    tmp_path: Path,
    frames: _Frames,
    ended_early: bool,
) -> None:
    """Work resumed after an approval and then stopped leaves the records holding the text its frames showed.

    Work that ends normally records nothing more: its caller's terminal update follows.
    """
    config = _config(tmp_path, update_interval=60)
    records = FakeReplyRecords()

    async def building() -> None:
        async with stream_progress_edits(
            make_matrix_client_mock(user_id="@mindroom_code:localhost"),
            MessageTarget.resolve(_ROOM_ID, None, "$question"),
            config,
            runtime_paths_for(config),
            event_id="$answer",
            show_tool_calls=True,
            **records.progress_hooks(),
        ) as publish:
            await publish(StructuredStreamChunk(content="Approved."))
            await asyncio.wait_for(_until(_shown_by(frames, "Approved.")), timeout=5)
            await publish(StructuredStreamChunk(content="Approved. Building now"))
            await asyncio.wait_for(_until(_shown_by(frames, "Building now")), timeout=5)
            if ended_early:
                raise asyncio.CancelledError

    with patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=_Edits().edit)):
        if ended_early:
            with pytest.raises(asyncio.CancelledError):
                await building()
        else:
            await building()

    if not ended_early:
        assert [write.kind for write in records.writes] == ["progress", "progress_delivered"]
        return
    assert [write.kind for write in records.writes] == ["progress", "progress_delivered", "shown_live"]
    assert records.writes[-1].progress.text == "Approved. Building now"


@pytest.mark.asyncio
async def test_frames_are_recorded_only_after_the_edit_in_flight_landed(tmp_path: Path, frames: _Frames) -> None:
    """An early end waits for the progress edit in flight, then records what frames showed past it."""
    config = _config(tmp_path, update_interval=0.2)
    records = FakeReplyRecords()
    edits = _Edits()
    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def edit(*args: Any) -> DeliveredMatrixEvent:  # noqa: ANN401
        if edits.contents:
            in_flight.set()
            await release.wait()
        return await edits.edit(*args)

    async def stopped_while_a_checkpoint_is_in_flight() -> None:
        async with stream_progress_edits(
            make_matrix_client_mock(user_id="@mindroom_code:localhost"),
            MessageTarget.resolve(_ROOM_ID, None, "$question"),
            config,
            runtime_paths_for(config),
            event_id="$answer",
            show_tool_calls=True,
            **records.progress_hooks(),
        ) as publish:
            await publish(StructuredStreamChunk(content="Approved."))
            await asyncio.wait_for(_until(_shown_by(frames, "Approved.")), timeout=5)
            await publish(StructuredStreamChunk(content="Approved. Building"))
            # The checkpoint edit stays in flight while frames show more.
            await asyncio.wait_for(in_flight.wait(), timeout=5)
            await publish(StructuredStreamChunk(content="Approved. Building now"))
            # The delta frame carries only the new words.
            await asyncio.wait_for(_until(_shown_by(frames, " now")), timeout=5)
            asyncio.get_running_loop().call_later(0.05, release.set)
            raise asyncio.CancelledError

    with (
        patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=edit)),
        pytest.raises(asyncio.CancelledError),
    ):
        await stopped_while_a_checkpoint_is_in_flight()

    kinds = [write.kind for write in records.writes]
    assert kinds == ["progress", "progress_delivered", "progress", "progress_delivered", "shown_live"]
    assert records.writes[-1].progress.text == "Approved. Building now"
