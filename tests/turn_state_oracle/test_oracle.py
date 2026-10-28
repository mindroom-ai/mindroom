"""The turn store still shows, after every recorded step, what the recordings pinned, but for the named masks."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from tests.turn_state_oracle.driver import OBSERVABLES, State, apply_changes, run_scenario
from tests.turn_state_oracle.masks import MASKS, masked
from tests.turn_state_oracle.record import recording_id, recordings, scenarios

if TYPE_CHECKING:
    from pathlib import Path

# Every scenario reopens its database, so the ledger must really load it rather than start pre-warmed and empty.
pytestmark = pytest.mark.ledger_loads_from_disk


def _difference(scenario: str, expected: State, actual: State) -> str | None:
    for observable in OBSERVABLES:
        recorded, current = expected.get(observable, {}), actual[observable]
        for key in sorted({*recorded, *current}):
            before = masked(scenario, observable, key, recorded.get(key))
            after = masked(scenario, observable, key, current.get(key))
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
    for index, (recorded, step) in enumerate(zip(golden["steps"], steps, strict=True)):
        where = f"step {index} {json.dumps(recorded['action'])}"
        outcome = masked(name, "outcome", str(index), step["outcome"])
        assert masked(name, "outcome", str(index), recorded["outcome"]) == outcome, f"{where}: outcome"
        apply_changes(expected, recorded["changes"])
        difference = _difference(name, expected, step["state"])
        assert difference is None, f"{where}: {difference}"


def test_every_scenario_is_recorded() -> None:
    """The recordings are exactly the named, seeded, and legacy scenarios, each as currently defined."""
    wanted = scenarios()
    assert {recording_id(path) for path in recordings()} == set(wanted)
    for path in recordings():
        assert json.loads(path.read_text(encoding="utf-8"))["scenario"] == wanted[recording_id(path)], path.name


def test_masks_name_known_observables() -> None:
    """A mask that names no observable would silently mask nothing."""
    assert {mask.observable for mask in MASKS} <= {*OBSERVABLES, "outcome"}
