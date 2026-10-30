"""The one rule every turn entry point applies to what its response left of its sources."""

from __future__ import annotations

import pytest

from mindroom.response_sources import Owned, Unclaimed, UnclaimedReason
from mindroom.turn_store import SourcePreparation
from mindroom.turns.source_fate import settle_source_fate

pytestmark = pytest.mark.asyncio

_OWN = ("$own-a", "$own-b")


async def _settle(fate: Owned | Unclaimed) -> tuple[list[tuple[str, ...]], list[tuple[str, tuple[str, ...]]]]:
    settled: list[tuple[str, ...]] = []
    retried: list[tuple[str, tuple[str, ...]]] = []

    async def settle(event_ids: tuple[str, ...]) -> None:
        settled.append(event_ids)

    await settle_source_fate(
        fate,
        _OWN,
        room_id="!room",
        settle=settle,
        retry_when_claim_releases=lambda room_id, event_ids: retried.append((room_id, event_ids)),
    )
    return settled, retried


async def test_an_owned_fate_leaves_its_sources_to_the_records() -> None:
    """A reply's records, a refused claim, or an approval hold the sources: the caller does nothing."""
    assert await _settle(Owned()) == ([], [])


@pytest.mark.parametrize("reason", list(UnclaimedReason))
async def test_an_unclaimed_fate_settles_the_callers_own_sources(reason: UnclaimedReason) -> None:
    """Sources no reply took settle as the caller's own, whatever left them unclaimed."""
    assert await _settle(Unclaimed(reason)) == ([_OWN], [])


async def test_terminal_sources_without_survivors_settle_the_callers_own_sources() -> None:
    """A preparation whose sources all ended settles them all, as the caller names them."""
    fate = Unclaimed(UnclaimedReason.TERMINAL_SOURCES, SourcePreparation(invalid=_OWN))

    assert await _settle(fate) == ([_OWN], [])


async def test_survivors_answer_as_their_own_turn_once_the_claim_releases() -> None:
    """Only the ended sources settle; the survivors go back to the journal for their own turn."""
    fate = Unclaimed(
        UnclaimedReason.TERMINAL_SOURCES,
        SourcePreparation(invalid=("$own-a",), surviving=("$own-b",)),
    )

    assert await _settle(fate) == ([("$own-a",)], [("!room", ("$own-b",))])
