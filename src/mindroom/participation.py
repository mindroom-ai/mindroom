"""Framework-independent participation state shared by one response turn."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from mindroom.judgment.state import JudgmentQuestion
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.judgment.state import JudgmentMessage

logger = get_logger(__name__)


PARTICIPATION_QUESTION = JudgmentQuestion(
    id="participation",
    instructions=(
        "You have already participated in this thread. Decide whether to reply again now. Multiple humans are talking "
        "and nobody explicitly addressed the assistant in the latest messages."
    ),
    when_true="Add clear value: answer an open question, provide requested help, or correct a consequential misunderstanding.",
    when_false="Acknowledgements, human-to-human coordination, unfinished thoughts, already answered questions, or repeating yourself.",
)


class ParticipationDecision(BaseModel):
    """Validated, immutable participation outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["respond", "stay_silent", "error"]
    reason: str = Field(min_length=1, max_length=500)


type ParticipationDecider = Callable[[tuple[JudgmentMessage, ...]], Awaitable[ParticipationDecision | None]]


@dataclass
class ParticipationGate:
    """One decision shared by retries and continuations of a response turn."""

    instructions: str = ""
    decider: ParticipationDecider | None = field(default=None, repr=False)
    _decision: ParticipationDecision | None = field(default=None, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    decided: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def decision(self) -> ParticipationDecision | None:
        """The immutable result, assigned once by the owning response turn."""
        return self._decision

    @property
    def approved(self) -> bool:
        """Whether the turn may generate a reply and execute tools."""
        return self.decision is not None and self.decision.action == "respond"

    @property
    def is_silent(self) -> bool:
        """Whether a settled turn must not generate a reply, including failed checks."""
        return self.decision is not None and self.decision.action != "respond"

    @property
    def is_declined(self) -> bool:
        """Whether a valid judgment deliberately chose not to reply."""
        return self.decision is not None and self.decision.action == "stay_silent"

    def decline(self, reason: str) -> bool:
        """Settle a failed turn quietly unless already decided; never authorize a reaction."""
        self._settle(ParticipationDecision(action="error", reason=reason))
        return self.is_silent

    def approve_existing_response(self) -> None:
        """Restore approval for a turn that already owns a visible response."""
        self._settle(ParticipationDecision(action="respond", reason="existing_visible_response"))

    def _settle(self, decision: ParticipationDecision) -> None:
        if self._decision is None:
            self._decision = decision
            self.decided.set()
            logger.info("Participation decided", action=decision.action, reason=decision.reason)

    async def check(self, decide: Callable[[], Awaitable[ParticipationDecision]]) -> bool:
        """Run a lazy decider once; concurrent callers share the settled result."""
        async with self._lock:
            if self.decision is None:
                self._settle(await decide())
            return self.approved
