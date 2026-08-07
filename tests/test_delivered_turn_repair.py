"""A delivered answer whose ledger write was lost is repaired at startup."""

from __future__ import annotations

import dataclasses

import pytest

from mindroom.delivered_turn_repair import repair_delivered_turns
from mindroom.turn_record import TurnRecord

pytestmark = pytest.mark.asyncio


@dataclasses.dataclass
class _Deliveries:
    rows: tuple[tuple[int, str, str], ...]

    async def acknowledged_final_deliveries(
        self,
        *,
        limit: int = 200,
        after: tuple[int, str] | None = None,
    ) -> tuple[tuple[int, str, str], ...]:
        rows = [r for r in self.rows if after is None or (r[0], r[1]) > after]
        return tuple(sorted(rows)[:limit])


@dataclasses.dataclass
class _Turns:
    records: dict[str, TurnRecord]
    writes: int = 0

    def get_turn_record(self, source_event_id: str) -> TurnRecord | None:
        return self.records.get(source_event_id)

    async def record_turn(self, turn_record: TurnRecord) -> None:
        self.writes += 1
        for source in turn_record.source_event_ids:
            self.records[source] = turn_record


def _record(source: str, *, response: str | None, completed: bool) -> TurnRecord:
    return TurnRecord(
        source_event_ids=(source,),
        anchor_event_id=source,
        response_event_id=response,
        completed=completed,
    )


async def test_a_delivered_answer_whose_ledger_write_was_lost_is_repaired() -> None:
    """The crash boundary between the outbox acknowledgement and the ledger write."""
    turns = _Turns(records={"$src": _record("$src", response=None, completed=False)})
    deliveries = _Deliveries(rows=((10, "$src", "$answer"),))

    assert await repair_delivered_turns(deliveries, turns) == 1

    repaired = turns.records["$src"]
    assert repaired.response_event_id == "$answer"
    assert repaired.completed
    assert turns.writes == 1


async def test_a_ledger_that_already_knows_its_answer_is_left_alone() -> None:
    """The ledger is authoritative when it has the fact; this only supplies a missing one."""
    turns = _Turns(records={"$src": _record("$src", response="$better", completed=True)})
    deliveries = _Deliveries(rows=((10, "$src", "$first-ever-sent"),))

    assert await repair_delivered_turns(deliveries, turns) == 0
    assert turns.records["$src"].response_event_id == "$better"
    assert turns.writes == 0


async def test_every_page_is_walked_not_just_the_first() -> None:
    """Stopping at one page would report success while leaving the rest broken."""
    count = 450
    turns = _Turns(records={f"$s{i}": _record(f"$s{i}", response=None, completed=False) for i in range(count)})
    deliveries = _Deliveries(rows=tuple((i, f"$s{i}", f"$a{i}") for i in range(count)))

    assert await repair_delivered_turns(deliveries, turns) == count
    assert all(turns.records[f"$s{i}"].response_event_id == f"$a{i}" for i in range(count))


def test_the_repair_runs_before_any_sync_callback_is_registered() -> None:
    """Placement is the whole fix, and the tests above cannot see it.

    Repairing the ledger is only useful before anything reads terminal turn
    state. Recovered events are exempt from the turn-replay gate by design --
    admission keeps them in ``_live_events`` and ``_run_event`` lets those
    through -- so an edit arriving on the first sync after a crash reaches
    ``EditRegenerator`` as soon as callbacks are live. If the repair has not
    run, that edit is dropped for having no response event and settled as
    ignored, and nothing re-delivers a consumed edit.

    The tests above drive ``repair_delivered_turns`` directly, so they pass no
    matter where it is called from. This reads the startup sequence instead:
    the repair must appear before the first ``add_event_callback``.
    """
    import inspect  # noqa: PLC0415 - reading the startup sequence is this test's whole point

    from mindroom.bot import AgentBot  # noqa: PLC0415 - a heavy import, deferred to this one test

    source = inspect.getsource(AgentBot.start)

    repair_at = source.find("repair_delivered_turns(")
    first_callback_at = source.find("add_event_callback(")

    assert repair_at != -1, "AgentBot.start must repair delivered turns"
    assert first_callback_at != -1, "expected AgentBot.start to register sync callbacks"
    assert repair_at < first_callback_at
