"""A tool job result is one typed payload in one bounded, non-executable envelope."""

from __future__ import annotations

import io
import json
import os
from typing import TYPE_CHECKING

import pytest
from agno.media import File, Image
from agno.models.message import Message
from agno.tools.function import ToolResult

from mindroom.tool_jobs import results
from mindroom.tool_jobs.results import (
    ToolResultPayload,
    _decode_result_payload,
    encode_result_payload,
    read_result_payload,
)
from tests.tool_job_helpers import job_owner, start_job, tool_job_runtime

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_jobs.runtime import BackgroundOutcome


def _saved(payload: ToolResultPayload) -> ToolResultPayload:
    return _decode_result_payload(json.loads(json.dumps(encode_result_payload(payload))))


def test_payload_round_trips_every_field_with_exact_types() -> None:
    """Rich values, state changes, stream layout, and control survive persistence unchanged."""
    payload = ToolResultPayload(
        value=ToolResult(
            content="picture",
            images=[Image(content=b"\x00\xff")],
            metadata={"structured_content": {"a": 1}, "pairs": [(1, "two"), ([], (3,))]},
        ),
        state_delta={"counter": {"before_present": True, "before": 1, "present": True, "value": 2}},
        error="partial failure",
        elapsed=0.25,
        replay=((0, {"event": "ToolCallStarted"}), (7, None)),
        control={"message": "stop", "messages": [Message(role="user", content="why")], "stop_execution": True},
    )

    assert _saved(payload) == payload


def test_payload_captures_a_local_artifact_before_cleanup(tmp_path: Path) -> None:
    """Durable media bytes survive deletion of a toolkit-owned file."""
    path = tmp_path / "image.png"
    path.write_bytes(b"artifact bytes")
    encoded = encode_result_payload(ToolResultPayload(value=ToolResult(content="image", images=[Image(filepath=path)])))
    path.unlink()

    image = _decode_result_payload(encoded).value.images[0]

    assert image.content == b"artifact bytes"
    assert image.filepath is None


def test_payload_limit_counts_the_whole_encoding_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Value and state share one limit: the exact encoded size passes and one byte less fails."""
    payload = ToolResultPayload(value=b"a" * 40, state_delta={"large": {"value": "é" * 40}})
    size = len(json.dumps(encode_result_payload(payload)).encode("utf-8"))
    monkeypatch.setattr(results, "_MAX_ENCODED_RESULT_BYTES", size)
    encode_result_payload(payload)
    monkeypatch.setattr(results, "_MAX_ENCODED_RESULT_BYTES", size - 1)

    with pytest.raises(ValueError, match="encoded JSON limit"):
        encode_result_payload(payload)


def test_payload_rejects_an_oversized_artifact_without_reading_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file whose base64 form cannot fit is rejected from its size before any read."""
    monkeypatch.setattr(results, "_MAX_ENCODED_RESULT_BYTES", 400)
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"x" * 301)
    monkeypatch.setattr(os, "fdopen", lambda *_args, **_kwargs: pytest.fail("oversized artifact was read"))

    with pytest.raises(ValueError, match="encoded JSON limit"):
        encode_result_payload(ToolResultPayload(value=File(filepath=path)))


def test_payload_artifacts_share_one_read_allowance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Files that each fit alone cannot be read together: the second is rejected from its size unread."""
    monkeypatch.setattr(results, "_MAX_ENCODED_RESULT_BYTES", 400)
    first, second = tmp_path / "first.bin", tmp_path / "second.bin"
    first.write_bytes(b"x" * 200)
    second.write_bytes(b"y" * 200)
    read: list[int] = []
    real_fdopen = os.fdopen

    def recording_fdopen(descriptor: int, *args: object, **kwargs: object) -> object:
        read.append(os.fstat(descriptor).st_ino)
        return real_fdopen(descriptor, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", recording_fdopen)

    with pytest.raises(ValueError, match="encoded JSON limit"):
        encode_result_payload(ToolResultPayload(value=[File(filepath=first), File(filepath=second)]))

    assert read == [first.stat().st_ino]


def test_payload_reads_a_growing_artifact_once_within_its_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file that grows after its size check is rejected after one bounded read."""
    monkeypatch.setattr(results, "_MAX_ENCODED_RESULT_BYTES", 2048)
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"x")
    read_sizes: list[int] = []

    class GrowingFile(io.BytesIO):
        def read(self, size: int | None = -1, /) -> bytes:
            effective_size = -1 if size is None else size
            read_sizes.append(effective_size)
            return b"x" * (4096 if effective_size < 0 else effective_size)

    monkeypatch.setattr(os, "fdopen", lambda *_args, **_kwargs: GrowingFile())

    with pytest.raises(ValueError, match="encoded JSON limit"):
        encode_result_payload(ToolResultPayload(value=File(filepath=path)))

    assert read_sizes == [3 * (2048 // 4) + 1]


def test_payload_keeps_returned_bytes_without_reopening_their_path(tmp_path: Path) -> None:
    """Worker code can replace a returned file, so bytes the tool returned stand and its path is never read."""
    secret = tmp_path / "secret"
    secret.write_bytes(b"primary secret")
    path = tmp_path / "generated.txt"
    path.symlink_to(secret)
    encoded = encode_result_payload(ToolResultPayload(value=File(content=b"generated", filepath=path)))

    restored = _decode_result_payload(encoded).value

    assert (restored.content, restored.filepath) == (b"generated", None)


@pytest.mark.parametrize("kind", ["link", "fifo"])
def test_payload_reads_a_path_only_artifact_only_as_a_regular_file(tmp_path: Path, kind: str) -> None:
    """A path-only artifact replaced by a link is refused, and a FIFO cannot stall encoding."""
    path = tmp_path / "artifact.bin"
    if kind == "link":
        secret = tmp_path / "secret"
        secret.write_bytes(b"primary secret")
        path.symlink_to(secret)
    else:
        os.mkfifo(path)

    with pytest.raises(OSError if kind == "link" else ValueError):
        encode_result_payload(ToolResultPayload(value=File(filepath=path)))


@pytest.mark.asyncio
async def test_runtime_written_summary_reports_its_truncation(tmp_path: Path) -> None:
    """A long message the runtime saved only as a summary says it was cut when read as the result."""
    runtime = await tool_job_runtime(tmp_path)
    message = "failure detail " * 100

    async def operation() -> BackgroundOutcome:
        raise RuntimeError(message)

    try:
        await start_job(
            runtime,
            "failed",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
        )
        waited = await runtime.wait("failed", owner=job_owner(), depth=0)
        await runtime.release_wait("failed", waited.claim)
        assert (waited.job.status, waited.job.has_result_payload) == ("failed", False)
        payload = await read_result_payload(runtime, waited.job)
        assert payload.value == f"{message[:500]}\n{results._SUMMARY_TRUNCATED_NOTICE}"
    finally:
        await runtime.shutdown()
