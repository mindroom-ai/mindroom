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
from mindroom.live_frames.publisher import LivePublisher
from mindroom.live_frames.transport import LiveSendOutcome
from mindroom.llm.events import ReasoningDelta, ReportedCall, TextDelta, ToolCompleted, ToolProgress, ToolStarted
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.message_target import MessageTarget
from mindroom.streaming import send_streaming_response, stream_progress_edits
from mindroom.tool_system.events import StructuredStreamChunk, ToolTraceEntry
from mindroom.tool_system.runtime_context import WorkerProgressEvent, get_worker_progress_pump
from mindroom.workers.models import WorkerReadyProgress
from tests.conftest import bind_runtime_paths, make_matrix_client_mock, runtime_paths_for, test_runtime_paths
from tests.streaming_helpers import FakeReplyRecords

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
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
    *chunks: str | float | ReasoningDelta | ToolStarted | ToolProgress | ToolCompleted,
) -> AsyncIterator[Any]:
    """Yield text, reasoning, and tool events, pausing for each float as a model or a tool call would."""
    for chunk in chunks:
        if isinstance(chunk, float):
            await asyncio.sleep(chunk)
        elif isinstance(chunk, str):
            yield TextDelta(chunk)
        else:
            yield chunk


async def _run(
    config: Config,
    stream: AsyncIterator[Any],
    records: FakeReplyRecords,
    edits: _Edits,
    *,
    placeholder_event_id: str | None = None,
    show_tool_calls: bool = True,
) -> None:
    with patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=edits.edit)):
        await send_streaming_response(
            make_matrix_client_mock(user_id="@mindroom_code:localhost"),
            MessageTarget.resolve(_ROOM_ID, None, "$question"),
            config,
            runtime_paths_for(config),
            stream,
            existing_event_id=placeholder_event_id,
            adopt_existing_placeholder=placeholder_event_id is not None,
            show_tool_calls=show_tool_calls,
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
