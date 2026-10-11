"""Bounded caller ownership for desktop observation references."""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from mindroom.desktop.accessibility import MAX_RETAINED_STATES, STATE_TTL_SECONDS, AccessibilityState
from mindroom.desktop.protocol import DesktopCommand, DesktopProtocolError

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True, slots=True)
class _Observation:
    state: AccessibilityState
    scope: tuple[str, str, str]
    expires_at: float


@dataclass
class DesktopObservations:
    """Bind state and element references to one caller, command session, and app."""

    clock: Callable[[], float] = time.monotonic
    _states: OrderedDict[str, _Observation] = field(default_factory=OrderedDict, init=False)

    def remember(self, state: AccessibilityState, command: DesktopCommand) -> None:
        """Retain an observed state without extending existing reference lifetimes."""
        self._prune()
        scope = _scope(command)
        previous = self._states.get(state.state_id)
        if previous is not None:
            if previous.scope != scope or previous.state != state:
                msg = "Desktop observation reused a state ID with a different scope or content."
                raise DesktopProtocolError(msg)
            return
        self._states[state.state_id] = _Observation(state, scope, self.clock() + STATE_TTL_SECONDS)
        while len(self._states) > MAX_RETAINED_STATES:
            self._states.popitem(last=False)

    def resolve(self, command: DesktopCommand) -> DesktopCommand:
        """Validate caller scope and map an opaque ref to its original element index."""
        self._prune()
        state_id = command.parameters.get("state_id")
        if not isinstance(state_id, str) or state_id not in self._states:
            msg = "Desktop observation is missing or expired; request get_app_state before acting."
            raise DesktopProtocolError(msg)
        observed = self._states[state_id]
        if observed.scope != _scope(command) or observed.state.app_id != command.parameters.get("app"):
            msg = "Desktop observation scope does not match this caller, session, or application."
            raise DesktopProtocolError(msg)
        if "element_ref" not in command.parameters:
            return command
        reference = command.parameters["element_ref"]
        index = next(
            (
                element.index
                for element in observed.state.elements
                if observed.state.element_ref(element.index) == reference
            ),
            None,
        )
        if index is None:
            msg = "Desktop element reference is unknown in this observation."
            raise DesktopProtocolError(msg)
        if "element_index" in command.parameters and (
            type(command.parameters["element_index"]) is not int or command.parameters["element_index"] != index
        ):
            msg = "Desktop element reference and index disagree."
            raise DesktopProtocolError(msg)
        parameters = {key: value for key, value in command.parameters.items() if key != "element_ref"}
        return replace(command, parameters={**parameters, "element_index": index})

    def _prune(self) -> None:
        now = self.clock()
        for state_id, observation in list(self._states.items()):
            if now >= observation.expires_at:
                del self._states[state_id]


def _scope(command: DesktopCommand) -> tuple[str, str, str]:
    return command.requester_id, command.agent_name, command.session_id


__all__ = ["DesktopObservations"]
