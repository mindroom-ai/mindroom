"""Shared finite policy for application-local keyboard and scroll input."""

DESKTOP_SAFE_KEYS = frozenset(
    {
        "backspace",
        "delete",
        "down",
        "end",
        "enter",
        "esc",
        "escape",
        "home",
        "left",
        "pagedown",
        "pageup",
        "return",
        "right",
        "tab",
        "up",
    },
)
DESKTOP_SCROLL_DIRECTIONS = frozenset({"up", "down", "left", "right"})
_SELECTION_KEYS = frozenset({"left", "right", "up", "down", "home", "end", "pageup", "pagedown", "tab"})
_EDIT_KEYS = frozenset({"a", "c", "x", "v", "z", "f"})


def normalize_key_chord(keys: list[str]) -> tuple[str, ...]:
    """Normalize only explicitly allowed navigation and editing combinations."""
    if not keys or any(not isinstance(key, str) for key in keys):
        msg = "Keyboard chord is not allowed."
        raise ValueError(msg)
    normalized = tuple(key.strip().lower() for key in keys)
    if len(normalized) == 1 and normalized[0] in DESKTOP_SAFE_KEYS:
        return normalized
    modifiers, key = normalized[:-1], normalized[-1]
    if len(modifiers) == len(set(modifiers)):
        if modifiers == ("shift",) and key in _SELECTION_KEYS:
            return normalized
        if modifiers in {("command",), ("ctrl",)} and key in _EDIT_KEYS:
            return normalized
        if key == "z" and set(modifiers) in ({"command", "shift"}, {"ctrl", "shift"}):
            return ("command" if "command" in modifiers else "ctrl", "shift", key)
    msg = "Keyboard chord is not allowed; use approved app-local editing or navigation keys."
    raise ValueError(msg)


__all__ = ["DESKTOP_SAFE_KEYS", "DESKTOP_SCROLL_DIRECTIONS", "normalize_key_chord"]
