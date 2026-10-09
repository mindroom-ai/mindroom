"""Parse the typed, length-bounded parameters of desktop commands."""

from __future__ import annotations

from mindroom.desktop.protocol import DesktopProtocolError

_MAX_PARAMETER_IDENTIFIER_LENGTH = 256
_MAX_PARAMETER_LENGTHS = {"text": 2_000, "value": 2_000, "path": 4_096, "cwd": 4_096, "command": 8_192}


def reject_unexpected_parameters(parameters: dict[str, object], *, allowed: frozenset[str]) -> None:
    """Reject every parameter the action does not accept."""
    unexpected = sorted(set(parameters) - allowed)
    if unexpected:
        msg = f"Unexpected desktop parameters: {', '.join(unexpected)}."
        raise DesktopProtocolError(msg)


def required_int_parameter(parameters: dict[str, object], key: str) -> int:
    """Return the integer parameter ``key``; booleans are not integers here."""
    value = parameters.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"Desktop parameter {key} must be an integer."
        raise DesktopProtocolError(msg)
    return value


def optional_int_parameter(parameters: dict[str, object], key: str) -> int | None:
    """Return the integer parameter ``key``, or ``None`` when it is absent."""
    if key not in parameters:
        return None
    return required_int_parameter(parameters, key)


def optional_bool_parameter(parameters: dict[str, object], key: str) -> bool:
    """Return the boolean parameter ``key``, which defaults to ``False``."""
    value = parameters.get(key, False)
    if not isinstance(value, bool):
        msg = f"Desktop parameter {key} must be a boolean."
        raise DesktopProtocolError(msg)
    return value


def required_str_parameter(parameters: dict[str, object], key: str, *, allow_empty: bool = False) -> str:
    """Return the string parameter ``key`` within its length bound."""
    value = parameters.get(key)
    if not isinstance(value, str) or (not value and not allow_empty):
        qualifier = "a string" if allow_empty else "a non-empty string"
        msg = f"Desktop parameter {key} must be {qualifier}."
        raise DesktopProtocolError(msg)
    max_length = _MAX_PARAMETER_LENGTHS.get(key, _MAX_PARAMETER_IDENTIFIER_LENGTH)
    if len(value) > max_length:
        msg = f"Desktop parameter {key} must not exceed {max_length} characters."
        raise DesktopProtocolError(msg)
    return value


def required_object_parameter(parameters: dict[str, object], key: str) -> dict[str, object]:
    """Return the object parameter ``key`` with string keys."""
    value = parameters.get(key)
    if not isinstance(value, dict):
        msg = f"Desktop parameter {key} must be an object with string keys."
        raise DesktopProtocolError(msg)
    result: dict[str, object] = {}
    for item_key, item_value in value.items():
        if not isinstance(item_key, str):
            msg = f"Desktop parameter {key} must be an object with string keys."
            raise DesktopProtocolError(msg)
        result[item_key] = item_value
    return result


def optional_str_parameter(parameters: dict[str, object], key: str, *, default: str) -> str:
    """Return the string parameter ``key``, or ``default`` when it is absent."""
    if key not in parameters:
        return default
    return required_str_parameter(parameters, key)
