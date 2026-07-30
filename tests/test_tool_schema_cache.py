"""Tests for the shared prompt tool-schema cache."""

from __future__ import annotations

from agno.tools.function import Function

from mindroom.tool_schema_cache import cached_processed_schema


def test_cached_processed_schema_returns_private_copies() -> None:
    """Mutating a returned snapshot must not corrupt the shared LRU entry."""

    def sync_event(title: str, include_attendees: bool = False) -> str:
        """Sync one event."""
        return f"{title}:{include_attendees}"

    function = Function(name="sync_event", entrypoint=sync_event)

    first = cached_processed_schema(function, strict=False)
    assert first is not None
    first.parameters["properties"]["injected"] = {"type": "string"}
    first.parameters["required"].append("injected")

    second = cached_processed_schema(function, strict=False)
    assert second is not None
    assert "injected" not in second.parameters["properties"]
    assert second.parameters["required"] == ["title"]
    assert second.parameters is not first.parameters
