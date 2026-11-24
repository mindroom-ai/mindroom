"""The turn store still shows, after every recorded step, what the recordings pinned, but for the named masks."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from tests.turn_state_oracle.adapter import ROOMS
from tests.turn_state_oracle.driver import OBSERVABLES, State, apply_changes, run_scenario
from tests.turn_state_oracle.masks import DIVERGENCES, MASKS, Context, masked
from tests.turn_state_oracle.record import recording_id, recordings, scenarios

if TYPE_CHECKING:
    from pathlib import Path


def _context(
    action: dict[str, Any] | None,
    actual: State,
    redactions: frozenset[tuple[str, str]] = frozenset(),
    event_rooms: dict[str, str] | None = None,
    echoes: dict[str, str] | None = None,
    published_echoes: frozenset[str] = frozenset(),
) -> Context:
    """Return what a mask may consult at one step: its action and what the current store reports redacted per room."""
    redacted_in = {
        ROOMS[room]: frozenset(event_ids)
        for room, event_ids in actual["redacted_event_ids"].items()
        if isinstance(event_ids, list)
    }
    return Context(
        action=action,
        redacted_in=redacted_in,
        redactions=redactions,
        state=actual,
        event_rooms=event_rooms,
        echoes=echoes,
        published_echoes=published_echoes,
    )


def _track_rooms(action: dict[str, Any] | None, event_rooms: dict[str, str]) -> None:
    """Record the room an action first names each event in, as the adapter does."""
    if action is None or action.get("room") is None:
        return
    for event_id in (*action.get("events", ()), *action.get("sources", ()), *action.get("aliases", ())):
        event_rooms.setdefault(event_id, ROOMS[action["room"]])


def _difference(scenario: str, expected: State, actual: State, context: Context) -> str | None:
    for observable in OBSERVABLES:
        recorded, current = expected.get(observable, {}), actual[observable]
        for key in sorted({*recorded, *current}):
            before = masked(scenario, observable, key, recorded.get(key), context)
            after = masked(scenario, observable, key, current.get(key), context, current=True)
            if before != after:
                return f"{observable}[{key}]: recorded {json.dumps(before)}, now {json.dumps(after)}"
    return None


@pytest.mark.parametrize("path", recordings(), ids=recording_id)
def test_a_recording_replays_unchanged(path: Path, tmp_path: Path) -> None:
    """Every step's outcome and every observable after it match the recording."""
    name = recording_id(path)
    golden: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    steps = run_scenario(golden["scenario"], tmp_path)
    assert len(steps) == len(golden["steps"])
    expected: State = {}
    divergence = DIVERGENCES.get(name)
    redactions: set[tuple[str, str]] = set()
    event_rooms: dict[str, str] = {}
    echoes: dict[str, str] = {}
    published: set[str] = set()
    for index, (recorded, step) in enumerate(zip(golden["steps"], steps, strict=True)):
        if divergence is not None and index == divergence.step:
            divergence.affirm(step["outcome"], step["state"])
            return
        where = f"step {index} {json.dumps(recorded['action'])}"
        action = recorded["action"]
        if action is not None and action["op"] == "redact":
            redactions.add((action["room"], action["target"]))
        _track_rooms(action, event_rooms)
        if action is not None and action["op"] == "echo":
            echoes[action["source"]] = action["echo"]
            published.add(action["echo"])
        context = _context(
            action,
            step["state"],
            frozenset(redactions),
            dict(event_rooms),
            dict(echoes),
            frozenset(published),
        )
        outcome = masked(name, "outcome", str(index), step["outcome"], context, current=True)
        assert masked(name, "outcome", str(index), recorded["outcome"], context) == outcome, f"{where}: outcome"
        apply_changes(expected, recorded["changes"])
        difference = _difference(name, expected, step["state"], context)
        assert difference is None, f"{where}: {difference}"
    assert divergence is None, f"{name} never reached its divergence at step {divergence.step}"


def test_every_scenario_is_recorded() -> None:
    """The recordings are exactly the named, seeded, and legacy scenarios, each as currently defined."""
    wanted = scenarios()
    assert {recording_id(path) for path in recordings()} == set(wanted)
    for path in recordings():
        assert json.loads(path.read_text(encoding="utf-8"))["scenario"] == wanted[recording_id(path)], path.name


def test_masks_name_known_observables() -> None:
    """A mask that names no observable or scenario, or a divergence that names no scenario, would mask nothing."""
    assert {mask.observable for mask in MASKS} <= {*OBSERVABLES, "outcome"}
    assert {scenario for mask in MASKS for scenario in mask.scenarios} <= set(scenarios())
    assert set(DIVERGENCES) <= set(scenarios())
