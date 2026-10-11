"""Explicit app-local keyboard and scroll policy."""

import pytest

from mindroom.desktop.input import normalize_key_chord


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        (["Enter"], ("enter",)),
        (["shift", "left"], ("shift", "left")),
        (["command", "a"], ("command", "a")),
        (["ctrl", "c"], ("ctrl", "c")),
        (["shift", "command", "z"], ("command", "shift", "z")),
    ],
)
def test_safe_edit_and_navigation_chords(keys: list[str], expected: tuple[str, ...]) -> None:
    """Safe edit chords normalize modifiers without admitting unrelated shortcuts."""
    assert normalize_key_chord(keys) == expected


@pytest.mark.parametrize(
    "keys",
    [
        [],
        ["command", "tab"],
        ["command", "q"],
        ["command", "space"],
        ["command", "l"],
        ["alt", "f4"],
        ["ctrl", "alt", "delete"],
        ["command", "command", "a"],
        ["f12"],
        ["shift", "enter"],
    ],
)
def test_global_or_unapproved_chords_are_rejected(keys: list[str]) -> None:
    """Unapproved chords never become global keyboard input."""
    with pytest.raises(ValueError, match="allowed"):
        normalize_key_chord(keys)
