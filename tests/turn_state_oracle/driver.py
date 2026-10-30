"""Drive one scenario through the adapter and observe the turn state after every step.

A scenario is plain JSON: ``actions``, each ``{"op": <adapter action>, **params}``; ``ids``, the event ids every
observation reads; and ``input``, the legacy database or ledger file it opens instead of an empty database. A step's
state maps each observable to ``{key: value}``, leaving out keys whose value is the reader's default (``None`` or
``False``), so a recording stores only what changed.
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tests.turn_state_oracle.adapter import (
    ACTIONS,
    PER_EVENT_READERS,
    ROOMS,
    TARGETS,
    TurnStateAdapter,
    controlled_clock,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Iterable

type State = dict[str, dict[str, Any]]

INPUTS = Path(__file__).resolve().parents[1] / "fixtures" / "turn_state_oracle" / "inputs"
OBSERVABLES = (*PER_EVENT_READERS, "redacted_event_ids", "redacted_history_events")


def event_ids(value: object) -> set[str]:
    """Return every Matrix event id an action or a list of them names."""
    if isinstance(value, str):
        return {value} if value.startswith("$") else set()
    if isinstance(value, dict):
        return {event_id for key, item in value.items() for event_id in event_ids(key) | event_ids(item)}
    if isinstance(value, list | tuple):
        return {event_id for item in value for event_id in event_ids(item)}
    return set()


async def _attempt(call: Awaitable[object]) -> object:
    try:
        return await call
    except AssertionError:
        # A failed check, such as the adapter's on what a pending write returns, fails the test instead of becoming
        # an outcome that a recording's divergence step could stop comparing.
        raise
    except Exception as error:
        return {"error": type(error).__name__, "message": str(error)}


async def observe(adapter: TurnStateAdapter, ids: list[str]) -> State:
    """Return every observable over ``ids``, without default values."""
    state: State = {}
    for reader in PER_EVENT_READERS:
        values = {event_id: await _attempt(getattr(adapter, reader)(event_id)) for event_id in ids}
        state[reader] = {key: value for key, value in values.items() if value is not None and value is not False}
    rooms = {room: await _attempt(adapter.redacted_event_ids(room, ids)) for room in ROOMS}
    state["redacted_event_ids"] = {key: value for key, value in rooms.items() if value}
    conversations = {key: await _attempt(adapter.redacted_history_events(key, ids)) for key in TARGETS}
    state["redacted_history_events"] = {key: value for key, value in conversations.items() if value}
    return state


def _restore_input(name: str, root: Path) -> Path | None:
    """Install a legacy input under ``root``; return the pre-journal ledger file to import, if it has one."""
    dump = INPUTS / f"{name}.sql"
    if dump.exists():
        with closing(sqlite3.connect(root / "journal.db")) as connection:
            connection.executescript(dump.read_text(encoding="utf-8"))
    ledger = INPUTS / f"{name}.ledger.json"
    if not ledger.exists():
        return None
    legacy_ledger = root / "tracking" / "agent_responded.json"
    legacy_ledger.parent.mkdir(parents=True)
    shutil.copyfile(ledger, legacy_ledger)
    return legacy_ledger


async def _run(scenario: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    legacy_ledger = _restore_input(scenario["input"], root) if scenario["input"] else None
    adapter = TurnStateAdapter(root, legacy_ledger=legacy_ledger)
    ids = scenario["ids"]
    with controlled_clock(adapter.clock):
        steps = [{"outcome": await _attempt(adapter.open()), "state": await observe(adapter, ids)}]
        for action in scenario["actions"]:
            op = action["op"]
            assert op in ACTIONS, f"unknown action {op!r}"
            params = {key: value for key, value in action.items() if key != "op"}
            outcome = await _attempt(getattr(adapter, op)(**params))
            steps.append({"outcome": outcome, "state": await observe(adapter, ids)})
        await adapter.close()
    return steps


def run_scenario(scenario: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    """Return each step's outcome and state: opening the store, then every action."""
    return asyncio.run(_run(scenario, root))


def apply_changes(state: State, delta: State) -> None:
    """Advance ``state`` by one recorded step."""
    for observable in OBSERVABLES:
        values = state.setdefault(observable, {})
        for key, value in delta.get(observable, {}).items():
            if value is None:
                values.pop(key, None)
            else:
                values[key] = value


def scenario_ids(actions: Iterable[dict[str, Any]], extra: Iterable[str] = ()) -> list[str]:
    """Return the event ids a scenario observes: every one its actions name, plus ``extra``."""
    return sorted(event_ids(list(actions)) | set(extra))
