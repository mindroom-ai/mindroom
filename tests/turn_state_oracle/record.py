"""Record the oracle's goldens: ``python -m tests.turn_state_oracle.record [--inputs]``.

Every named sequence, seeded sequence, and legacy input is driven through the current turn store, and each step's
action, outcome, and changed observables are written to ``tests/fixtures/turn_state_oracle/<group>/<name>.json``.
``--inputs`` first rebuilds the legacy inputs, which only the turn-record schema they capture can produce.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from tests.turn_state_oracle.actions import NAMED, SEEDS, generate
from tests.turn_state_oracle.driver import changes, run_scenario, scenario_ids
from tests.turn_state_oracle.legacy import FIXTURES, build_inputs

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


def record(scenario: dict[str, Any], root: Path) -> dict[str, Any]:
    """Return the golden of one scenario driven under ``root``."""
    steps, previous = [], {}
    for action, step in zip([None, *scenario["actions"]], run_scenario(scenario, root), strict=True):
        steps.append({"action": action, "outcome": step["outcome"], "changes": changes(previous, step["state"])})
        previous = step["state"]
    return {"scenario": scenario, "steps": steps}


def main(argv: list[str] | None = None) -> None:
    """Rebuild the goldens, and the legacy inputs with ``--inputs``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", action="store_true", help="rebuild the legacy input databases and files first")
    if parser.parse_args(argv).inputs:
        build_inputs()
    wanted = scenarios()
    for path in recordings():
        if recording_id(path) not in wanted:
            path.unlink()
    for name, scenario in wanted.items():
        with tempfile.TemporaryDirectory() as root:
            golden = record(scenario, Path(root))
        path = GOLDENS / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(golden, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        print(name, len(golden["steps"]))


if __name__ == "__main__":
    main()
