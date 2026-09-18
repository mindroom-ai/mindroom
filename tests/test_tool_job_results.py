"""Durable tool result encoding stays within one cumulative memory budget."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from agno.media import File

from mindroom.tool_jobs import results


def test_result_encoding_budget_counts_full_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Individually valid byte values cannot exceed the budget when combined in one envelope."""
    monkeypatch.setattr(results, "_MAX_ENCODED_RESULT_BYTES", 180, raising=False)

    payload = results.encode_tool_result([b"a" * 40])

    assert len(json.dumps(payload).encode("utf-8")) == 140
    with pytest.raises(ValueError, match="encoded JSON limit"):
        results.encode_tool_result([b"a" * 40, b"b" * 40])


def test_result_encoding_budget_matches_default_json_del_escape(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default JSON encoder's escaped DEL character counts as six payload bytes."""
    value = chr(127)
    assert len(json.dumps({"version": 1, "value": value}).encode("utf-8")) == 33
    monkeypatch.setattr(results, "_MAX_ENCODED_RESULT_BYTES", 32)

    with pytest.raises(ValueError, match="encoded JSON limit"):
        results.encode_tool_result(value)


def test_result_file_encoding_uses_remaining_bounded_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A growing artifact is rejected after one bounded read and before base64 expansion."""
    monkeypatch.setattr(results, "_MAX_ENCODED_RESULT_BYTES", 2048, raising=False)
    read_sizes: list[int] = []

    class GrowingFile(io.BytesIO):
        def read(self, size: int | None = -1, /) -> bytes:
            effective_size = -1 if size is None else size
            read_sizes.append(effective_size)
            return b"x" * (4096 if effective_size < 0 else effective_size)

    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: GrowingFile())

    with pytest.raises(ValueError, match="encoded JSON limit"):
        results.encode_tool_result(File(filepath="artifact.bin"))

    assert len(read_sizes) == 1
    assert 0 < read_sizes[0] <= 2049
