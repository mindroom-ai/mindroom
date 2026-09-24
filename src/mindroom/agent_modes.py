"""Bounded conversation mode choices in canonical agent state storage."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast

from mindroom.durable_write import load_cached_override_records, write_bounded_override_records
from mindroom.file_locks import advisory_file_lock

if TYPE_CHECKING:
    from pathlib import Path

AgentMode = Literal["standard", "minimal"]


def _key(agent_name: str, session_id: str) -> str:
    return json.dumps([agent_name, session_id], separators=(",", ":"))


def _valid(key: str, record: dict[object, object]) -> bool:
    return (
        all(isinstance(record.get(field), str) for field in ("agent", "session", "mode", "set_by", "set_at"))
        and record["mode"] in {"standard", "minimal"}
        and key == _key(cast("str", record["agent"]), cast("str", record["session"]))
    )


def resolve_agent_mode(state_root: Path, agent_name: str, session_id: str) -> AgentMode:
    """Resolve only this agent/conversation, defaulting safely to standard."""
    path = state_root / "agent_modes.json"
    record = load_cached_override_records(path, _valid).get(_key(agent_name, session_id))
    return cast("AgentMode", record["mode"]) if record is not None else "standard"


def set_agent_mode(state_root: Path, agent_name: str, session_id: str, mode: AgentMode, set_by: str) -> None:
    """Audit and atomically replace a choice without losing concurrent updates."""
    path = state_root / "agent_modes.json"
    with advisory_file_lock(path.with_suffix(".lock")):
        records = load_cached_override_records(path, _valid)
        records[_key(agent_name, session_id)] = {
            "agent": agent_name,
            "session": session_id,
            "mode": mode,
            "set_by": set_by,
            "set_at": datetime.now(UTC).isoformat(),
        }
        write_bounded_override_records(path, records, max_records=1000)


def clear_agent_mode(state_root: Path, agent_name: str, session_id: str) -> bool:
    """Reset one choice, retaining every other agent and conversation."""
    path = state_root / "agent_modes.json"
    with advisory_file_lock(path.with_suffix(".lock")):
        records = load_cached_override_records(path, _valid)
        if records.pop(_key(agent_name, session_id), None) is None:
            return False
        write_bounded_override_records(path, records, max_records=1000)
    return True
