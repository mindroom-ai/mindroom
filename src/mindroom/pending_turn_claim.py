"""Single-release ownership for one pending turn claim."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.handled_turns import TurnRecord


@dataclass
class PendingTurnClaim:
    """Close one claimed turn at most once across ownership handoffs."""

    turn_record: TurnRecord
    release: Callable[[TurnRecord], None] = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self) -> None:
        """Release the underlying store claim once."""
        if self._closed:
            return
        self._closed = True
        self.release(self.turn_record)
