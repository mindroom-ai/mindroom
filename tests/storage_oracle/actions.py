"""The oracle's action vocabulary: what a conversation does to its store, as plain data.

An action names a kind and two small integers. ``index`` picks among the candidates the driver finds at that point
(which run to resume, which event to redact) and the variant of the kind (how a turn ends); ``count`` sizes it (how
many runs a chunk folds). A backend interprets the same actions the same way, so the recordings of one store are the
expected observables of another.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Literal

type ScenarioKind = Literal["agent", "team"]

# Each kind with its weight in generated sequences; a team session has no CLI turns, an agent session no members.
_SHARED_WEIGHTS = {
    "add": 5,
    "pause": 2,
    "resume": 2,
    "compact": 3,
    "race": 1,
    "fold": 4,
    "end": 3,
    "crash_after_fold": 1,
    "notice": 1,
    "scrub": 1,
    "link": 2,
    "delegation_state": 1,
    "interrupted": 2,
    "discard": 1,
    "stale_save": 2,
    "force": 1,
    "edit": 1,
    "redact": 2,
    "reopen": 1,
}
_KIND_WEIGHTS: dict[ScenarioKind, dict[str, int]] = {
    "agent": {**_SHARED_WEIGHTS, "checkpoint": 2, "cli_delegate": 1},
    "team": {**_SHARED_WEIGHTS, "member_late": 1, "team_pause_advanced": 1},
}
# Kinds that only start a conversation: they need a database without a session row.
OPENING_KINDS = frozenset({"legacy", "fold_first_turn"})
# Kinds after which a recording keeps the full observables, not only their digests.
CUT_KINDS = frozenset({"edit", "redact"})


@dataclass(frozen=True)
class Action:
    """One operation on the conversation."""

    kind: str
    index: int = 0
    count: int = 1

    def to_json(self) -> list[Any]:
        """Return the action as recordings store it."""
        return [self.kind, self.index, self.count]

    @classmethod
    def from_json(cls, value: list[Any]) -> Action:
        """Rebuild an action from its recorded form."""
        kind, index, count = value
        return cls(kind, index, count)


@dataclass(frozen=True)
class Scenario:
    """One conversation's kind and the actions applied to it in order."""

    kind: ScenarioKind
    actions: tuple[Action, ...]

    def to_json(self) -> dict[str, Any]:
        """Return the scenario as recordings store it."""
        return {"kind": self.kind, "actions": [action.to_json() for action in self.actions]}

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> Scenario:
        """Rebuild a scenario from its recorded form."""
        return cls(value["kind"], tuple(Action.from_json(action) for action in value["actions"]))


def generate(seed: int, length: int) -> Scenario:
    """Return ``length`` actions drawn with ``random.Random(seed)``; every third seed is a team conversation."""
    rng = random.Random(seed)  # noqa: S311 - a reproducible sequence, not a secret
    kind: ScenarioKind = "team" if seed % 3 == 2 else "agent"
    weights = _KIND_WEIGHTS[kind]
    kinds = [name for name, weight in weights.items() for _ in range(weight)]
    actions: list[Action] = []
    opening = rng.random()
    if opening < 0.25:
        actions.append(Action("legacy", index=rng.randrange(3)))
    elif opening < 0.4:
        actions.append(Action("fold_first_turn", index=rng.randrange(3)))
    while len(actions) < length:
        actions.append(Action(rng.choice(kinds), index=rng.randrange(31), count=rng.randint(1, 3)))
    return Scenario(kind, tuple(actions))


def _scenario(kind: ScenarioKind, *actions: Action) -> Scenario:
    return Scenario(kind, actions)


# The sequences spec v4 section 4.7 names, each a regression the generated ones may miss.
NAMED: dict[str, Scenario] = {
    # A first turn folds before any run is stored: the summary is accepted, rejected, or cancelled.
    "first_turn_fold_accepted": _scenario(
        "agent",
        Action("fold_first_turn", index=0),
        Action("end", index=0),
        Action("add"),
    ),
    "first_turn_fold_rejected": _scenario("agent", Action("fold_first_turn", index=1), Action("end", index=0)),
    "first_turn_fold_cancelled": _scenario("agent", Action("fold_first_turn", index=2), Action("add")),
    "first_turn_fold_team": _scenario(
        "team",
        Action("fold_first_turn", index=0),
        Action("fold", index=2),
        Action("end", index=0),
    ),
    # Voice playout interrupts an answer the run had already stored complete, before and after it is covered.
    "interrupted_voice_playout": _scenario(
        "agent",
        Action("add"),
        Action("add"),
        Action("interrupted", index=1),
        Action("compact", count=2),
        Action("interrupted", index=4),
        Action("add"),
    ),
    # A turn folds and is then discarded as a skipped attempt; an edit of what it answered follows.
    "discarded_fold_owner_edited": _scenario(
        "agent",
        Action("add"),
        Action("fold", index=0),
        Action("discard", index=1),
        Action("add"),
        Action("edit", index=1),
        Action("add"),
    ),
    # The same, followed by a redaction of the thread context only that turn read.
    "discarded_fold_owner_redacted": _scenario(
        "agent",
        Action("add"),
        Action("fold", index=0),
        Action("discard", index=1),
        Action("add"),
        Action("redact", index=3),
        Action("add"),
    ),
    # A reply is linked to a run a summary already covers, and the reply is then redacted.
    "linked_covered_run_redacted": _scenario(
        "agent",
        Action("add"),
        Action("add"),
        Action("compact", count=1),
        Action("link", index=0),
        Action("redact", index=4),
        Action("add"),
    ),
    # Interrupted records over runs that folded: their folded tool calls stay out of the retained context.
    "interrupted_after_fold": _scenario(
        "agent",
        Action("add"),
        Action("fold", index=0),
        Action("end", index=1),
        Action("interrupted", index=0),
        Action("fold", index=0),
        Action("end", index=0),
        Action("interrupted", index=4),
        Action("add"),
    ),
    # A compaction archive whose legacy summary is in force; redacting what that summary holds cuts every run.
    "archive_legacy_in_force_redacted": _scenario(
        "agent",
        Action("legacy", index=2),
        Action("add"),
        Action("redact", index=0),
        Action("add"),
    ),
    # A paused team run whose member row advanced before the team row was saved again.
    "team_pause_advanced": _scenario(
        "team",
        Action("add"),
        Action("pause"),
        Action("team_pause_advanced"),
        Action("add"),
        Action("resume"),
        Action("add"),
    ),
    # A minimal-mode turn whose Bash call delegates, then completes.
    "cli_delegate_then_complete": _scenario(
        "agent",
        Action("add"),
        Action("checkpoint"),
        Action("cli_delegate"),
        Action("checkpoint"),
        Action("end", index=0),
        Action("add"),
    ),
    # A paused run's delegation state is saved, and saved again after the run continued and a summary covered it.
    "delegation_state_after_continuation": _scenario(
        "agent",
        Action("pause"),
        Action("delegation_state", index=0),
        Action("resume"),
        Action("delegation_state", index=1),
        Action("compact", count=2),
        Action("delegation_state", index=1),
        Action("add"),
    ),
    # A stale paused copy of a run that completed is saved again.
    "stale_paused_copy": _scenario(
        "agent",
        Action("pause"),
        Action("resume"),
        Action("stale_save", index=0),
        Action("add"),
    ),
}
