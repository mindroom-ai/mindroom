"""Intended differences from the recordings, each named with its reason. None yet.

A mask names one observable (or ``"outcome"``), why it differs, the scenarios it covers (every one when empty),
and ``normalize(key, value)``, which both the recorded and the current value pass through before they compare; it
returns ``None`` to treat a key as absent. A later change declares a deliberate behavior change here instead of
re-recording, so the recordings keep pinning everything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class Mask:
    """One deliberate difference."""

    observable: str
    reason: str
    normalize: Callable[[str, object], object]
    scenarios: frozenset[str] = frozenset()


MASKS: tuple[Mask, ...] = ()


def masked(scenario: str, observable: str, key: str, value: object) -> object:
    """Return ``value`` as every mask covering this scenario and observable compares it."""
    for mask in MASKS:
        if mask.observable == observable and (not mask.scenarios or scenario in mask.scenarios):
            value = mask.normalize(key, value)
    return value
