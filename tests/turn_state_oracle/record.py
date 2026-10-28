"""The oracle's scenarios and their recordings in ``tests/fixtures/turn_state_oracle/<group>/<name>.json``.

Each recording holds every step's action, outcome, and changed observables as the turn store before the row rewrite
produced them, driven through every named sequence, seeded sequence, and legacy input.
That store is gone, so the recordings are frozen evidence and cannot be regenerated: recording from the current store
would compare it with itself.
The scenario set is therefore closed; test new turn-state behavior at its owning seam, such as
``tests/test_turn_rows.py`` or ``tests/test_turn_store.py``, and declare a deliberate change to a recorded behavior as
a mask in ``masks.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.turn_state_oracle.actions import NAMED, SEEDS, generate
from tests.turn_state_oracle.driver import scenario_ids
from tests.turn_state_oracle.legacy import FIXTURES

GOLDENS = Path(__file__).resolve().parents[1] / "fixtures" / "turn_state_oracle"
GROUPS = ("named", "generated", "legacy")


def scenarios() -> dict[str, dict[str, Any]]:
    """Return every scenario the oracle records, by ``<group>/<name>``."""
    found: dict[str, dict[str, Any]] = {}
    for name, actions in NAMED.items():
        found[f"named/{name}"] = {"input": None, "ids": scenario_ids(actions), "actions": actions}
    for seed in range(SEEDS):
        actions = generate(seed)
        found[f"generated/seed_{seed:02d}"] = {"input": None, "ids": scenario_ids(actions), "actions": actions}
    for name, fixture in FIXTURES.items():
        stored = [fixture.rows, fixture.tombstones, fixture.ledger]
        found[f"legacy/{name}"] = {
            "input": name,
            "ids": scenario_ids(fixture.actions, scenario_ids([stored])),
            "actions": list(fixture.actions),
        }
    return json.loads(json.dumps(found))


def recordings() -> list[Path]:
    """Return every golden on disk."""
    return sorted(path for group in GROUPS for path in (GOLDENS / group).glob("*.json"))


def recording_id(path: Path) -> str:
    """Return a golden's ``<group>/<name>``."""
    return f"{path.parent.name}/{path.stem}"
