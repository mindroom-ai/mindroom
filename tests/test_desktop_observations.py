"""Caller ownership and expiry of observed desktop references."""

from __future__ import annotations

from dataclasses import replace

import pytest

from mindroom.desktop.accessibility import AccessibilityElement, AccessibilityState, DesktopRect
from mindroom.desktop.observations import DesktopObservations
from mindroom.desktop.protocol import DesktopCommand, DesktopProtocolError

ELEMENT = AccessibilityElement(0, 0, None, "AXButton", None, "Save", None, True, False, None, ("AXPress",))
STATE = AccessibilityState("snapshot-1", "com.example.Editor", "Editor", DesktopRect(0, 0, 800, 600), (ELEMENT,), False)
COMMAND = DesktopCommand(
    "request",
    "session",
    1,
    1000,
    31000,
    "click_element",
    "@alice:example.org",
    "computer",
    {"app": "com.example.Editor", "state_id": "snapshot-1", "element_index": 0},
)


@pytest.mark.parametrize(
    "changes",
    [
        {"requester_id": "@bob:example.org"},
        {"agent_name": "other"},
        {"session_id": "another"},
        {"parameters": {"app": "com.example.Other", "state_id": "snapshot-1", "element_index": 0}},
    ],
)
def test_reference_cannot_cross_caller_session_or_application(changes: dict[str, object]) -> None:
    """A valid opaque state is not authority for another caller or app."""
    cache = DesktopObservations()
    cache.remember(STATE, COMMAND)
    with pytest.raises(DesktopProtocolError, match="scope"):
        cache.resolve(replace(COMMAND, **changes))


def test_opaque_element_ref_resolves_only_within_original_snapshot() -> None:
    """A wire ref resolves to its observed index without trusting a supplied index."""
    cache = DesktopObservations()
    cache.remember(STATE, COMMAND)
    element_ref = STATE.to_result()["elements"][0]["ref"]
    command = replace(
        COMMAND,
        parameters={
            "app": "com.example.Editor",
            "state_id": "snapshot-1",
            "element_ref": element_ref,
        },
    )
    assert cache.resolve(command).parameters == {
        "app": "com.example.Editor",
        "state_id": "snapshot-1",
        "element_index": 0,
    }
    with pytest.raises(DesktopProtocolError, match="reference"):
        cache.resolve(replace(command, parameters={**command.parameters, "element_ref": "made-up"}))
    with pytest.raises(DesktopProtocolError, match="index"):
        cache.resolve(replace(command, parameters={**command.parameters, "element_index": 1}))


def test_observation_expiry_uses_monotonic_time() -> None:
    """Expired state IDs require a new observation even if their app looks unchanged."""
    now = 0.0
    cache = DesktopObservations(clock=lambda: now)
    cache.remember(STATE, COMMAND)
    now = 119.0
    assert cache.resolve(COMMAND) == COMMAND
    now = 120.0
    with pytest.raises(DesktopProtocolError, match="expired"):
        cache.resolve(COMMAND)


def test_interleaved_observations_remain_usable_until_bounded_eviction() -> None:
    """New observations must not instantly invalidate another admitted caller's state."""
    cache = DesktopObservations()
    cache.remember(STATE, COMMAND)
    for index in range(127):
        cache.remember(replace(STATE, state_id=f"other-{index}"), COMMAND)
    assert cache.resolve(COMMAND) == COMMAND
    cache.remember(replace(STATE, state_id="last"), COMMAND)
    with pytest.raises(DesktopProtocolError, match="expired"):
        cache.resolve(COMMAND)
