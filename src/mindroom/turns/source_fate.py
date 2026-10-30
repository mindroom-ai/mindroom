"""Settle what a response left of a coalesced turn's sources, as turn dispatch and edit regeneration both need."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.response_sources import Unclaimed

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.response_sources import SourceFate


async def settle_source_fate(
    fate: SourceFate,
    own_sources: tuple[str, ...],
    *,
    room_id: str,
    settle: Callable[[tuple[str, ...]], Awaitable[None]],
    retry_when_claim_releases: Callable[[str, tuple[str, ...]], None],
) -> None:
    """Settle the sources no reply took; an owned fate leaves them to the records that hold them.

    Terminal sources found before any reply existed settle alone when others survive, and the survivors answer as
    their own turn once the caller's claim releases; every other unclaimed fate settles the caller's own sources.
    """
    if not isinstance(fate, Unclaimed):
        return
    preparation = fate.preparation
    if preparation is not None and preparation.surviving:
        await settle(preparation.invalid)
        retry_when_claim_releases(room_id, preparation.surviving)
        return
    await settle(own_sources)
