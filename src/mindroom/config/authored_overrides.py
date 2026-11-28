"""Layer authored tool overrides and handle the inherit sentinel."""

AUTHORED_OVERRIDE_INHERIT = "__MINDROOM_INHERIT__"


def apply_authored_overrides(
    base: dict[str, object],
    overrides: dict[str, object] | None,
) -> dict[str, object]:
    """Apply one authored override layer onto an existing authored-override mapping."""
    resolved = dict(base)
    if not overrides:
        return resolved

    for field_name, value in overrides.items():
        if value == AUTHORED_OVERRIDE_INHERIT:
            resolved.pop(field_name, None)
        else:
            resolved[field_name] = value
    return resolved
