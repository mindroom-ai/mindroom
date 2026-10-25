"""Comparison 1: the storage v2 store, driven by every recorded sequence, shows what the Agno-layout store recorded.

Every difference is one of the named masks of ``masks.py``. Each sequence's masks, and the step where a mask ended
its comparison, are pinned in ``native_masks.json``, so a change of where the two stores part shows as a failure
rather than as fewer compared steps; ``python -m tests.storage_oracle.test_native`` rewrites the pins after a
deliberate change.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.storage_oracle.actions import Scenario
from tests.storage_oracle.determinism import deterministic
from tests.storage_oracle.masks import MASKS, MaskedBackend
from tests.storage_oracle.record import FIXTURES, decode, digest
from tests.storage_oracle.test_recorded import first_difference

_SEQUENCES = sorted([*(FIXTURES / "generated").glob("*.json"), *(FIXTURES / "named").glob("*.json")])
_PINS = Path(__file__).with_name("native_masks.json")


def _id(path: Path) -> str:
    return f"{path.parent.name}/{path.stem}"


@dataclass
class Comparison:
    """How one recorded sequence compared: the masks it used, and where a mask ended the comparison."""

    used: set[str] = field(default_factory=set)
    diverged: tuple[str, int] | None = None
    compared_steps: int = 0

    def pin(self) -> dict[str, Any]:
        """Return what ``native_masks.json`` keeps of this comparison."""
        return {"used": sorted(self.used), "diverged": list(self.diverged) if self.diverged else None}


def _json[Value](value: Value) -> Value:
    return json.loads(json.dumps(value, sort_keys=True))


def compare(path: Path, root: Path) -> Comparison:
    """Drive ``path``'s recorded sequence on the v2 store under ``root``; fail at its first unmasked difference."""
    recorded = decode(path.read_text(encoding="utf-8"))
    scenario = Scenario.from_json(recorded["scenario"])
    comparison = Comparison()
    with deterministic() as determinism:
        backend = MaskedBackend(root, scenario.kind, determinism)
        for index, action in enumerate(scenario.actions):
            outcome = _json(backend.apply(action))
            if backend.diverged is not None:
                comparison.diverged = (backend.diverged.mask, index)
                break
            step = recorded["steps"][index]
            assert outcome == step["outcome"], f"step {index} {action}: outcome {step['outcome']!r} -> {outcome!r}"
            observed = _json(backend.old_view(backend.observe()))
            full = recorded["full"].get(str(index))
            for key, value in observed.items():
                if digest(value) != step["digests"][key]:
                    where = first_difference(full[key], value, key) if full is not None else "digest differs"
                    pytest.fail(f"step {index} {action}: {key}: {where}")
            comparison.compared_steps += 1
        else:
            final = _json(backend.old_view(backend.observe()))
            assert first_difference(recorded["final"], final) is None, first_difference(recorded["final"], final)
    comparison.used = backend.used
    return comparison


@pytest.mark.parametrize("path", _SEQUENCES, ids=_id)
def test_a_recorded_sequence_shows_the_same_on_the_v2_store_but_for_named_masks(path: Path, tmp_path: Path) -> None:
    """The v2 store gives every recorded outcome and observable once each named mask undoes its change."""
    comparison = compare(path, tmp_path)
    assert comparison.used <= MASKS.keys()
    assert comparison.pin() == json.loads(_PINS.read_text(encoding="utf-8"))[_id(path)]


def test_every_mask_is_used_and_every_sequence_pinned() -> None:
    """Each named mask explains a difference some recorded sequence shows, and no pin is stale."""
    pins = json.loads(_PINS.read_text(encoding="utf-8"))
    assert set(pins) == {_id(path) for path in _SEQUENCES}
    assert {mask for pin in pins.values() for mask in pin["used"]} == MASKS.keys()


def _write_pins() -> None:
    pins: dict[str, object] = {}
    for path in _SEQUENCES:
        with tempfile.TemporaryDirectory() as root:
            comparison = compare(path, Path(root))
        pins[_id(path)] = comparison.pin()
        print(_id(path), comparison.pin(), comparison.compared_steps)
    lines = (f"{json.dumps(name)}: {json.dumps(pin, sort_keys=True)}" for name, pin in sorted(pins.items()))
    _PINS.write_text("{\n" + ",\n".join(lines) + "\n}\n", encoding="utf-8")


if __name__ == "__main__":
    _write_pins()
