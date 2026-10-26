"""Comparison 3: a converted end state and the native one, driven by the same further actions, stay equal.

Each recorded sequence is driven twice through ``NextBackend``. One backend keeps its database, the native end state.
The other's database is replaced by the recorded Agno-layout end state, which it then opens as the runtime does,
converting it, so both backends hold the same driver state over the two databases. Where both show the same
observables, they take the same further random actions, and both must report the same outcome and show the same
observables after each one. A sequence whose native run stored something else, through a deliberate change that
comparison 1 masks, starts apart: ``_APART`` pins those, and each one's comparison 1 pin names such a change.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from tests.storage_oracle.actions import Scenario, draw
from tests.storage_oracle.determinism import deterministic
from tests.storage_oracle.next_backend import NextBackend
from tests.storage_oracle.record import SEQUENCES, decode, first_difference, sequence_id

if TYPE_CHECKING:
    from tests.storage_oracle.determinism import Determinism

_NATIVE_PINS = Path(__file__).with_name("native_masks.json")
_FURTHER_ACTIONS = 40
# Comparison 1's masks of a change in what the store holds, rather than in how a result is named or shown.
_STORED_DIFFERENTLY = frozenset(
    {
        "visible_run_is_final",
        "interrupted_record_replaces_covered_run",
        "member_final_once_team_visible",
        "paused_team_members_from_requirements",
        "link_reaches_any_stored_run",
        "fold_stores_cancelled_row",
        "first_stored_by_fold_sorts_at_fold",
        "minimal_mode_delegation_patches_checkpoint",
        "delegation_state_is_a_key_patch",
        "folded_calls_go_with_their_run",
    },
)
# The sequences whose native end state differs from their converted recorded one.
_APART = frozenset(
    {
        *(
            f"generated/{seed}"
            for seed in (2, 3, 5, 8, 10, 11, 12, 13, 15, 16, 17, 20, 21, 22, 23, 25, 27, 28, 29, 31, 32, 34, 35, 36)
        ),
        "generated/38",
        "generated/39",
        "named/delegation_state_after_continuation",
        "named/interrupted_after_fold",
        "named/interrupted_voice_playout",
        "named/linked_covered_run_redacted",
        "named/stale_paused_copy",
        "named/team_pause_advanced",
    },
)


def _json[Value](value: Value) -> Value:
    return json.loads(json.dumps(value, sort_keys=True))


def _pair(path: Path, root: Path, determinism: Determinism) -> tuple[Scenario, NextBackend, NextBackend]:
    """Return the recorded sequence, its native end state, and its converted recorded one, under one driver state."""
    scenario = Scenario.from_json(decode(path.read_text(encoding="utf-8"))["scenario"])
    native = NextBackend(root / "native", scenario.kind, determinism)
    converted = NextBackend(root / "converted", scenario.kind, determinism)
    for action in scenario.actions:
        native.apply(action)
        converted.apply(action)
    converted.database.unlink()
    with closing(sqlite3.connect(converted.database)) as connection:
        connection.executescript(path.with_suffix(".sql").read_text(encoding="utf-8"))
    converted.open_store()
    return scenario, native, converted


def _observed(determinism: Determinism, native: NextBackend, converted: NextBackend) -> tuple[Any, Any]:
    """Observe both backends as of one moment: the second sees the ids the first saw minted."""
    with determinism.ids_repeated():
        seen = _json(native.observe())
    return seen, _json(converted.observe())


@pytest.mark.timeout(120)
@pytest.mark.parametrize("path", SEQUENCES, ids=sequence_id)
def test_a_converted_end_state_keeps_showing_what_the_native_one_does(path: Path, tmp_path: Path) -> None:
    """Further actions on a converted end state give the outcomes and observables they give on the native one."""
    with deterministic() as determinism:
        scenario, native, converted = _pair(path, tmp_path, determinism)
        apart = first_difference(*_observed(determinism, native, converted))
        assert (apart is not None) == (sequence_id(path) in _APART), apart
        if apart is not None:
            return
        for index, action in enumerate(draw(scenario.kind, sequence_id(path), _FURTHER_ACTIONS)):
            outcomes = _json(native.apply(action)), _json(converted.apply(action))
            assert outcomes[0] == outcomes[1], f"further action {index} {action}: {outcomes}"
            difference = first_difference(*_observed(determinism, native, converted))
            assert difference is None, f"further action {index} {action}: {difference}"


def test_every_sequence_apart_stored_something_else_natively() -> None:
    """Each sequence pinned apart used a comparison 1 mask of a change in what the store holds."""
    pins = json.loads(_NATIVE_PINS.read_text(encoding="utf-8"))
    assert set(pins) >= _APART
    for name in sorted(_APART):
        assert set(pins[name]["used"]) & _STORED_DIFFERENTLY, name
