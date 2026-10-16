"""Shared tool-name filter policy."""

from collections.abc import Collection


def tool_name_allowed(
    name: str,
    *,
    include: Collection[str] | None,
    exclude: Collection[str] | None,
) -> bool:
    """Apply normalized tool-name filters; None means unrestricted inclusion."""
    return (include is None or name in include) and (exclude is None or name not in exclude)
