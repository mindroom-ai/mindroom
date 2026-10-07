"""The two step types a built-in automation returns to the runner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.main import Config


@dataclass(frozen=True)
class Ask:
    """Post ``text`` mentioning the agent so it answers with a normal visible run.

    ``new_thread`` starts a thread, and a session, of its own; otherwise the text goes to the previous prompt's thread.
    When the run's response is final, or after the fallback timeout, the runner calls ``then`` off the event loop
    with the current config, the thread the text was posted in, and whether the timeout fired.
    Without ``then``, the chain ends once the run does.
    """

    text: str
    new_thread: bool
    then: Callable[[Config, str, bool], Ask | Done] | None = None


@dataclass(frozen=True)
class Done:
    """End the chain: post ``notice`` in the last prompt's thread without mentioning the agent, and resolve threads."""

    notice: str
    resolve: tuple[str, ...] = ()
    # Called on the event loop, for work that must be scheduled there, such as a background re-index.
    on_loop: Callable[[], None] | None = None
