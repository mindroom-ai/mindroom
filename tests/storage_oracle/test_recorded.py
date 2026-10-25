"""The Agno-layout store still produces every recording of the oracle, and recording twice gives the same bytes."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.storage_oracle.actions import NAMED, Scenario
from tests.storage_oracle.legacy import EXTRA_RUN_IDS
from tests.storage_oracle.record import (
    FIXTURES,
    GENERATED_SEEDS,
    GOLDEN_DUMPS,
    GROUPS,
    decode,
    keeps_every_step,
    observe_database,
    record,
    record_scenario,
)

if TYPE_CHECKING:
    from pathlib import Path

_SEQUENCES = sorted([*(FIXTURES / "generated").glob("*.json"), *(FIXTURES / "named").glob("*.json")])
_GOLDENS = {dump.stem: dump for dump in GOLDEN_DUMPS}
_LEGACY = sorted((FIXTURES / "legacy").glob("*.json"))
# The fixtures are committed, so they stay small.
_FIXTURE_BUDGET_BYTES = 5_000_000


def _id(path: Path) -> str:
    return f"{path.parent.name}/{path.stem}"


def first_difference(expected: object, actual: object, path: str = "") -> str | None:
    """Return where ``actual`` first differs from ``expected``, or None when they are equal."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted({*expected, *actual}):
            if key not in expected or key not in actual:
                return f"{path}/{key}: present only in {'actual' if key in actual else 'expected'}"
            if (found := first_difference(expected[key], actual[key], f"{path}/{key}")) is not None:
                return found
        return None
    if isinstance(expected, list) and isinstance(actual, list):
        for index, (left, right) in enumerate(zip(expected, actual)):
            if (found := first_difference(left, right, f"{path}[{index}]")) is not None:
                return found
        return None if len(expected) == len(actual) else f"{path}: {len(expected)} items, now {len(actual)}"
    return None if expected == actual else f"{path}: {expected!r} != {actual!r}"


def test_every_sequence_and_input_is_recorded() -> None:
    """Every generated seed, named sequence, golden, and legacy input has its recording."""
    assert {path.stem for path in (FIXTURES / "generated").glob("*.json")} == {str(seed) for seed in GENERATED_SEEDS}
    assert {path.stem for path in (FIXTURES / "named").glob("*.json")} == set(NAMED)
    assert {path.stem for path in (FIXTURES / "goldens").glob("*.json")} == set(_GOLDENS)
    assert {path.stem for path in _LEGACY} == {path.stem for path in (FIXTURES / "legacy").glob("*.sql")}
    assert sum(path.stat().st_size for path in FIXTURES.rglob("*") if path.is_file()) < _FIXTURE_BUDGET_BYTES


@pytest.mark.parametrize("path", _SEQUENCES, ids=_id)
def test_a_recorded_sequence_replays_on_the_agno_layout_store(path: Path, tmp_path: Path) -> None:
    """Driving the recorded actions again gives every recorded outcome, digest, cut, and final observable."""
    recorded = decode(path.read_text(encoding="utf-8"))
    replayed, _ = record_scenario(
        Scenario.from_json(recorded["scenario"]),
        tmp_path,
        every_step=keeps_every_step(path.parent.name),
    )
    assert first_difference(recorded, replayed) is None, first_difference(recorded, replayed)


@pytest.mark.parametrize("path", sorted((FIXTURES / "goldens").glob("*.json")), ids=_id)
def test_a_recorded_golden_observes_the_same(path: Path, tmp_path: Path) -> None:
    """Opening a storage golden again gives every recorded observable of its sessions."""
    recorded = decode(path.read_text(encoding="utf-8"))
    observed = observe_database(_GOLDENS[path.stem].read_text(encoding="utf-8"), tmp_path)
    assert first_difference(recorded, observed) is None, first_difference(recorded, observed)


@pytest.mark.parametrize("path", _LEGACY, ids=_id)
def test_a_recorded_legacy_input_observes_the_same(path: Path, tmp_path: Path) -> None:
    """Opening a legacy input again, migrations included, gives every recorded observable of its sessions."""
    recorded = decode(path.read_text(encoding="utf-8"))
    dump = path.with_suffix(".sql").read_text(encoding="utf-8")
    observed = observe_database(dump, tmp_path, EXTRA_RUN_IDS.get(path.stem))
    assert first_difference(recorded, observed) is None, first_difference(recorded, observed)


# The generated group is recorded in shards, so no one test records all of it twice.
_SHARDS = 8
_DETERMINISM_CASES = [
    *(("generated", frozenset(str(seed) for seed in GENERATED_SEEDS[shard::_SHARDS])) for shard in range(_SHARDS)),
    *((group, None) for group in GROUPS if group != "generated"),
]


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    ("group", "names"),
    _DETERMINISM_CASES,
    ids=[f"{group}-{index}" for index, (group, _) in enumerate(_DETERMINISM_CASES)],
)
def test_recording_is_deterministic(group: str, names: frozenset[str] | None, tmp_path: Path) -> None:
    """Recording a group twice writes byte-identical files."""
    first, second = tmp_path / "first", tmp_path / "second"
    record(first, group, names)
    record(second, group, names)
    files = sorted(path.relative_to(first) for path in first.rglob("*") if path.is_file())
    assert files
    assert files == sorted(path.relative_to(second) for path in second.rglob("*") if path.is_file())
    for relative in files:
        assert (first / relative).read_bytes() == (second / relative).read_bytes(), relative
