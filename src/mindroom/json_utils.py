"""Small JSON decoding primitives shared by strict input boundaries."""

import math


class DuplicateJSONKeyError(ValueError):
    """Raised when a JSON object contains a repeated key."""


def object_with_unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object while rejecting repeated keys instead of overwriting."""
    result = dict(pairs)
    if len(result) != len(pairs):
        msg = "JSON object must not contain duplicate keys"
        raise DuplicateJSONKeyError(msg)
    return result


def has_only_finite_numbers(value: object) -> bool:
    """Return whether decoded JSON data contains no NaN or infinity, which strict JSON cannot encode."""
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(has_only_finite_numbers(item) for item in value.values())
    if isinstance(value, list):
        return all(has_only_finite_numbers(item) for item in value)
    return True
