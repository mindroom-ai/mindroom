"""Explicit app-local keyboard, scroll, and MindRoom app exclusion policy."""

import pytest

from mindroom.desktop.accessibility import app_targetable
from mindroom.desktop.input import MINDROOM_APP_IDS, normalize_key_chord
from mindroom.desktop.native_config import NativeConfigError, NativeDesktopConfig

_ELECTRON_MINDROOM_APP_IDS = (
    "chat.mindroom.app",
    "chat.mindroom.app.helper",
    "chat.mindroom.app.helper.Renderer",
    "chat.mindroom.app.helper.GPU",
    "chat.mindroom.app.helper.Plugin",
)


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


def test_mindroom_app_ids_exclude_electron_chat_and_helpers() -> None:
    """MindRoom Chat.app and all Electron helper bundle IDs are excluded from allowlists and computer scope."""
    expected_ids = frozenset(
        {
            "chat.mindroom.menubar",
            "chat.mindroom.desktophelper",
            *_ELECTRON_MINDROOM_APP_IDS,
        },
    )
    assert expected_ids == MINDROOM_APP_IDS
    assert app_targetable("com.apple.TextEdit", frozenset(), all_apps=True) is True
    for app_id in _ELECTRON_MINDROOM_APP_IDS:
        assert app_targetable(app_id, frozenset(), all_apps=True) is False
        with pytest.raises(NativeConfigError, match="control MindRoom itself"):
            NativeDesktopConfig.from_payload(
                {
                    "v": 1,
                    "revision": 0,
                    "enabled": True,
                    "controller": {"user_id": "@controller:example.org", "device_id": "DEVICE", "ed25519": "key"},
                    "allowed_requester_ids": ["@person:example.org"],
                    "allowed_agent_names": ["assistant"],
                    "allowed_app_ids": [app_id],
                    "capture": {"max_screenshot_width": 1568, "jpeg_quality": 80},
                    "browser": {
                        "enabled": False,
                        "executable_path": None,
                        "user_data_dir": None,
                        "timeout_seconds": 90,
                    },
                },
            )
