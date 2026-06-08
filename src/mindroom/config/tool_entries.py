"""Helpers for raw authored tool entries before Pydantic coercion."""

from __future__ import annotations

from typing import cast

from mindroom.config.models import ToolConfigEntry


def raw_tool_entry_name_and_lazy_flag_fields(entry: object) -> tuple[str | None, bool, bool]:
    """Extract a raw tool entry name and lazy flags from authored config data."""
    name: str | None = None
    defer = False
    initial = False

    if isinstance(entry, ToolConfigEntry):
        name = entry.name
        defer = "defer" in entry.model_fields_set
        initial = "initial" in entry.model_fields_set
    elif isinstance(entry, str):
        name = entry
    elif isinstance(entry, dict):
        raw_entry = cast("dict[object, object]", entry)
        if "name" in raw_entry or "overrides" in raw_entry:
            raw_name = raw_entry.get("name")
            name = raw_name if isinstance(raw_name, str) else None
            defer = "defer" in raw_entry
            initial = "initial" in raw_entry
        elif len(raw_entry) == 1:
            raw_name, overrides = next(iter(raw_entry.items()))
            name = raw_name if isinstance(raw_name, str) else None
            if isinstance(overrides, dict):
                override_map = cast("dict[object, object]", overrides)
                defer = "defer" in override_map
                initial = "initial" in override_map

    return name, defer, initial


def raw_tool_entry_name(entry: object) -> str | None:
    """Extract a raw tool entry name from authored config data."""
    return raw_tool_entry_name_and_lazy_flag_fields(entry)[0]


def raw_tools_entries(data: dict[object, object], section: str) -> list[object]:
    """Return raw tool entries for one top-level config section."""
    raw_section = data.get(section)
    if not isinstance(raw_section, dict):
        return []
    section_data = cast("dict[object, object]", raw_section)
    tools = section_data.get("tools")
    return list(tools) if isinstance(tools, list) else []
