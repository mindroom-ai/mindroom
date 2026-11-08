"""The namespaces MindRoom's runtime reserves for its own metadata in Matrix content keys and event types."""

from __future__ import annotations

MINDROOM_RESERVED_PREFIXES = ("com.mindroom.", "io.mindroom.")


def reserved_content_keys(value: object, prefixes: tuple[str, ...]) -> list[str]:
    """Return every key starting with one of *prefixes* placed anywhere in *value*.

    A managed Matrix account speaks for the runtime and its plugins, so
    content composed for it must never carry the namespaces they read
    as their own metadata. Nested payloads count: edits and sidecars
    promote inner objects into the content receivers read.

    The walk keeps its own stack: such content can nest deeply enough to
    exhaust the interpreter's, and a rejection is only useful if it
    reaches the caller.
    """
    found: set[str] = set()
    pending: list[object] = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            found.update(key for key in current if isinstance(key, str) and key.startswith(prefixes))
            pending.extend(current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend(current)
    return sorted(found)
