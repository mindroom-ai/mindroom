"""The oracle's sequences: named reproductions of past turn-state bugs, and seeded random ones, as plain data.

Each action is ``{"op": <TurnStateAdapter action>, **params}``; the adapter documents each op. Recordings store
the actions they ran, so changing this file changes only what the next recording drives.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

from tests.turn_state_oracle.adapter import REQUESTER

if TYPE_CHECKING:
    from collections.abc import Callable

type Action = dict[str, Any]

SEEDS = 20
SEED_LENGTH = 25
ALICE, BOB = REQUESTER, "@bob:example.org"


def op(name: str, **params: object) -> Action:
    """Return one action."""
    return {"op": name, **params}


def _couple(*sources: str) -> list[str]:
    return [ALICE] * len(sources)


NAMED: dict[str, list[Action]] = {
    # d9b4d63734 (#1646): writers that each held a whole copy lost edited prompts and revisions, so the newest
    # revision must win however late an older regeneration commits, across siblings and a restart.
    "d9b4d63734_late_older_edit_keeps_newest_revision": [
        op("admit", events=["$m1", "$m2"], room="a"),
        op("pending", sources=["$m1", "$m2"], room="a", senders=_couple("$m1", "$m2")),
        op("responded", sources=["$m1", "$m2"], response="$r1", room="a", senders=_couple("$m1", "$m2")),
        op("edit", source="$m1", edit="$e2", ts=20, commit=False),
        op("edit", source="$m1", edit="$e1", ts=10),
        op("record_edit", source="$m1", edit="$e2", ts=20),
        op("record_edit", source="$m1", edit="$e1", ts=10),
        op("edit", source="$m2", edit="$e3", ts=30),
        op("reopen"),
        op("record_edit", source="$m1", edit="$e1", ts=10),
    ],
    # d9b4d63734 (#1646): a relayed message keeps the human's original as a discovery alias through an edit, a
    # second relay, a restart, and the original's redaction.
    "d9b4d63734_routed_alias_survives_relay_edit_and_restart": [
        op("admit", events=["$a1", "$m3", "$m4"], room="a"),
        op("pending", sources=["$m3"], aliases=["$a1"], relays={"$m3": "$a1"}, room="a"),
        op("responded", sources=["$m3"], response="$r3", aliases=["$a1"], relays={"$m3": "$a1"}, room="a"),
        op("pending", sources=["$m4"], aliases=["$a1"], room="a"),
        op("edit", source="$a1", edit="$e4", ts=40),
        op("reopen"),
        op("redact", target="$a1", room="a"),
        op("reopen"),
    ],
    # 50d89d0e53 (#1671): a coalesced turn with an absent, partial, or empty sender map must not read as one
    # requester's, including after a completed sibling forces the redaction projection.
    "50d89d0e53_unattributed_coalesced_turns_fail_closed": [
        op("admit", events=["$m1", "$m2", "$m3", "$m4", "$m5", "$m6"], room="a"),
        op("pending", sources=["$m1", "$m2"], room="a", senders=None),
        op("pending", sources=["$m3", "$m4"], room="a", senders=[ALICE, None]),
        op("pending", sources=["$m5", "$m6"], room="a", senders=[]),
        op("reopen"),
        op("terminal", sources=["$m1"]),
        op("redact", target="$m2", room="a"),
        op("responded", sources=["$m3"], response="$r3", room="a", senders=[BOB]),
        op("redact", target="$m4", room="a"),
        op("pending", sources=["$m7", "$m8"], room="a", relays={"$m7": "$m8"}, senders=[BOB, ALICE]),
        op("terminal", sources=["$m7"]),
        op("redact", target="$m8", room="a"),
        op("reopen"),
    ],
    # f5aaea2b0e (#1639): a late write derived from a copy older than a committed FINAL acknowledgement or reply
    # settlement must neither erase nor rebind the delivered answer.
    "f5aaea2b0e_late_write_keeps_committed_delivery": [
        op("admit", events=["$m1"], room="a"),
        op("pending", sources=["$m1"], room="a", response="$p1"),
        op("final_ack", turn="$m1", response="$r1", publish=False),
        op("pending", sources=["$m1"], room="a"),
        op("edit", source="$m1", edit="$e1", ts=10),
        op("final_ack", turn="$m1", response="$r2"),
        op("reopen"),
        op("admit", events=["$m2", "$m3"], room="a"),
        op("pending", sources=["$m2", "$m3"], room="a", senders=_couple("$m2", "$m3")),
        op("settle", sources=["$m2", "$m3"], publish=False),
        op("record_edit", source="$m3", edit="$e4", ts=40),
        op("pending", sources=["$m2", "$m3"], room="a", senders=_couple("$m2", "$m3")),
        op("reopen"),
        op("final_ack", turn="$m3", response="$r3"),
        op("pending", sources=["$m5", "$m6"], room="b", senders=_couple("$m5", "$m6")),
        op("final_ack", turn="$m6", response="$r6"),
        op("settle", sources=["$m5", "$m6"]),
    ],
    # bb62aea414 (#2121): a voice checkpoint has to survive every merge a pending batch makes, die with completion,
    # and age out on its own only once its source settled.
    "bb62aea414_voice_checkpoint_survives_merges": [
        op("admit", events=["$m1", "$m2", "$m3"], room="a"),
        op("voice", source="$m1", body="first transcript"),
        op("voice", source="$m1", body="second transcript"),
        op("echo", source="$m1", echo="$v1"),
        op("pending", sources=["$m1"], room="a"),
        op("voice", source="$m2", body="other transcript"),
        op("pending", sources=["$m1", "$m2"], room="a", senders=_couple("$m1", "$m2")),
        op("cleanup", days=31, unsettled=["$m1", "$m2"]),
        op("reopen"),
        op("finalize_echo", source="$m1", echo="$v1", fallback=True),
        op("finalize_echo", source="$m1", echo="$v1", fallback=False),
        op("finalize_echo", source="$m1", echo="$v1", fallback=True),
        op("responded", sources=["$m1", "$m2"], response="$r1", room="a", senders=_couple("$m1", "$m2")),
        op("voice", source="$m1", body="late transcript"),
        op("voice", source="$m3", body="orphan transcript"),
        op("echo", source="$m3", echo="$v3"),
        op("finalize_echo", source="$m3", echo="$v3", fallback=True),
        op("cleanup", days=31, unsettled=["$m3"]),
        op("cleanup", days=31),
        op("voice", source="$m4", body="checkpoint only"),
        op("cleanup", days=31),
    ],
    # 49a6d51806 (#2544): a redaction names its target by id alone, so it tombstones only an event recorded or
    # admitted in the redaction's own room.
    "49a6d51806_redaction_needs_its_event_in_its_room": [
        op("admit", events=["$m1"], room="a"),
        op("redact", target="$m1", room="b"),
        op("redact", target="$m9", room="a"),
        op("pending", sources=["$m2"]),
        op("admit", events=["$m2"], room="a"),
        op("redact", target="$m2", room="b"),
        op("pending", sources=["$m1"], room="a"),
        op("redact", target="$m1", room="b"),
        op("prepare", room="a", sources=["$m1"], history=[["$m2", None, 0]]),
        op("redact", target="$m2", room="a"),
        op("redact", target="$m1", room="a"),
        op("prepare", room="a", sources=["$m1"]),
        op("reopen"),
    ],
    # 5197054f5c (#2681): history cleanup derives from tombstones, so a split alias keeps its tombstone, cold
    # redactions are found at the next response, and a conversation's tombstones outlive retention.
    "5197054f5c_conversation_tombstones_outlive_retention": [
        op("admit", events=["$m1", "$m2", "$m3", "$m4", "$m5", "$m6"], room="a"),
        op("pending", sources=["$m1", "$m2"], room="a", senders=_couple("$m1", "$m2")),
        op("redact", target="$m1", room="a"),
        op("responded", sources=["$m2"], response="$r2", room="a"),
        op("pending", sources=["$m3"], room="a"),
        op("redact", target="$m4", room="a", via="journal"),
        op("prepare", room="a", sources=["$m3"], history=[["$m4", None, 0]]),
        op("responded", sources=["$m5"], response="$r5", room="a", revisions={"$m5": ["$e5", 50]}),
        op("redact", target="$e5", room="a"),
        op("pending", sources=["$m6"], room="a"),
        op("prepare", room="a", sources=["$m6"], history=[["$m5", "$e5", 50]]),
        op("redact", target="$m3", room="a", via="journal"),
        op("prepare", room="a", sources=["$m3"]),
        op("cleanup", days=40),
        op("reopen"),
    ],
    # The batch shapes coalescing produces: a batch absorbs single-source echo and voice records, re-forms with a
    # different source set, and completes through a context-consuming response.
    "batch_reformation_absorbs_single_source_records": [
        op("admit", events=["$m1", "$m2", "$m3", "$m4"], room="a"),
        op("echo", source="$m1", echo="$v1"),
        op("voice", source="$m2", body="spoken"),
        op("pending", sources=["$m1", "$m2"], room="a", senders=_couple("$m1", "$m2")),
        op("pending", sources=["$m2", "$m3"], room="a", senders=_couple("$m2", "$m3")),
        op("pending", sources=["$m1", "$m2", "$m3"], room="a", senders=_couple("$m1", "$m2", "$m3")),
        op("edit", source="$m4", edit="$e4", ts=40),
        op("prepare", room="a", sources=["$m3"], history=[["$m4", "$e4", 40]]),
        op("final_ack", turn="$m3", response="$r3"),
        op("terminal", sources=["$m4"], room="a"),
        op("reopen"),
    ],
}

_SOURCES = ("$m1", "$m2", "$m3", "$m4", "$m5")
_EDITS = {"$e1": ("$m1", 10), "$e2": ("$m1", 20), "$e3": ("$m2", 30), "$e4": ("$m3", 40)}
_ALIASES = ("$a1", "$a2")
_WEIGHTS = {
    "admit": 3,
    "pending": 5,
    "terminal": 2,
    "responded": 3,
    "edit": 3,
    "record_edit": 1,
    "prepare": 2,
    "voice": 2,
    "echo": 2,
    "finalize_echo": 2,
    "redact": 3,
    "final_ack": 2,
    "settle": 2,
    "cleanup": 1,
    "reopen": 1,
}


def _batch(rng: random.Random) -> list[str]:
    picked = set(rng.sample(_SOURCES, rng.choice((1, 1, 1, 2, 2, 3))))
    return [source for source in _SOURCES if source in picked]


def _turn(rng: random.Random, *, completed: bool) -> dict[str, Any]:
    sources = _batch(rng)
    turn: dict[str, Any] = {"sources": sources, "room": rng.choice(("a", "a", "b", None))}
    if rng.random() < 0.2:
        turn["aliases"] = [rng.choice(_ALIASES)]
    senders = rng.choice(("absent", "full", "full", "partial", "empty"))
    if senders != "absent":
        turn["senders"] = {"full": [ALICE] * len(sources), "partial": [ALICE, *[None] * (len(sources) - 1)]}.get(
            senders,
            [],
        )
    edited = [(edit, owner, ts) for edit, (owner, ts) in _EDITS.items() if owner in sources]
    if edited and rng.random() < 0.4:
        edit, owner, ts = rng.choice(edited)
        turn["revisions"] = {owner: [edit, ts]}
    if completed:
        turn["response"] = "$r"
    elif rng.random() < 0.2:
        turn["response"] = "$p"
    return turn


def _edit(rng: random.Random) -> dict[str, Any]:
    edit = rng.choice(tuple(_EDITS))
    source, ts = _EDITS[edit]
    return {"source": source, "edit": edit, "ts": ts}


def _history(rng: random.Random) -> list[list[Any]]:
    history: list[list[Any]] = [[source, None, 0] for source in rng.sample(_SOURCES, rng.choice((0, 1, 2)))]
    for entry in history:
        edits = [(edit, ts) for edit, (owner, ts) in _EDITS.items() if owner == entry[0]]
        if edits and rng.random() < 0.5:
            entry[1], entry[2] = rng.choice(edits)
    return history


def _echo(rng: random.Random) -> dict[str, str]:
    """Return a source and one of its own echoes: a first one or a retried one, as one echo event never serves two."""
    source = rng.choice(_SOURCES)
    return {"source": source, "echo": f"$v{source.removeprefix('$m')}{rng.choice(('', 'b'))}"}


# How each kind draws its parameters.
_DRAWS: dict[str, Callable[[random.Random], dict[str, Any]]] = {
    "admit": lambda rng: {"events": rng.sample((*_SOURCES, *_ALIASES), 2), "room": rng.choice(("a", "a", "b"))},
    "pending": lambda rng: _turn(rng, completed=False),
    "terminal": lambda rng: {"sources": _batch(rng), "room": rng.choice(("a", None, None))},
    "responded": lambda rng: _turn(rng, completed=True),
    "edit": lambda rng: {**_edit(rng), "commit": rng.random() < 0.7},
    "record_edit": _edit,
    "prepare": lambda rng: {
        "room": rng.choice(("a", "a", "b")),
        "sources": [rng.choice(_SOURCES)],
        "history": _history(rng),
    },
    "voice": lambda rng: {"source": rng.choice(_SOURCES), "body": f"transcript {rng.randrange(3)}"},
    "echo": _echo,
    "finalize_echo": lambda rng: {**_echo(rng), "fallback": rng.random() < 0.5},
    "redact": lambda rng: {
        "target": rng.choice((*_SOURCES, *_SOURCES, *_EDITS, *_ALIASES)),
        "room": rng.choice(("a", "a", "a", "b")),
        "via": rng.choice(("live", "live", "live", "journal")),
    },
    "final_ack": lambda rng: {
        "turn": rng.choice(_SOURCES),
        "response": "$r",
        "publish": rng.random() < 0.8,
    },
    "settle": lambda rng: {"sources": _batch(rng), "publish": rng.random() < 0.8},
    "cleanup": lambda rng: {"days": rng.choice((0, 31)), "unsettled": rng.sample(_SOURCES, rng.choice((0, 1, 2)))},
    "reopen": lambda _rng: {},
}


def generate(seed: int, length: int = SEED_LENGTH) -> list[Action]:
    """Return ``length`` actions drawn with ``random.Random(seed)``."""
    rng = random.Random(seed)  # noqa: S311 - a reproducible sequence, not a secret
    kinds = [kind for kind, weight in _WEIGHTS.items() for _ in range(weight)]
    actions = [op(kind, **_DRAWS[kind](rng)) for kind in (rng.choice(kinds) for _ in range(length))]
    for index, action in enumerate(actions):
        if "response" in action:
            # A visible response is one Matrix event, so no two turns or acknowledgements share one.
            action["response"] += str(index)
    return actions
