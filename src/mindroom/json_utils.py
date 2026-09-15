"""Small JSON decoding primitives shared by strict input boundaries."""


class DuplicateJSONKeyError(ValueError):
    """Raised when a JSON object contains a repeated key."""


def object_with_unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object while rejecting repeated keys instead of overwriting."""
    result = dict(pairs)
    if len(result) != len(pairs):
        msg = "JSON object must not contain duplicate keys"
        raise DuplicateJSONKeyError(msg)
    return result
