"""Unified thread information utilities for Matrix events.

This module provides a single, consistent API for working with Matrix threads
according to the MSC3440 specification.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ThreadInfo:
    """Encapsulates thread information for a Matrix event."""

    is_thread: bool
    """Whether this event is part of a thread."""

    thread_id: str | None
    """The thread root event ID if this is a thread message."""

    can_be_thread_root: bool
    """Whether this event can be used as a thread root per MSC3440."""

    safe_thread_root: str | None
    """Safe event ID to use as thread root (None means use this event)."""

    has_relations: bool
    """Whether this event has any relations (edits, reactions, replies)."""

    relation_type: str | None
    """The relation type if any (m.replace, m.annotation, m.thread, etc)."""


def analyze_thread_info(event_source: dict | None) -> ThreadInfo:
    """Analyze complete thread information for a Matrix event.

    This unified function replaces both extract_thread_info and get_safe_thread_root,
    providing all thread-related information in one place.

    Per MSC3440:
    - A thread can only be created from events that don't have any rel_type
    - Thread messages use rel_type: m.thread
    - Edits use rel_type: m.replace
    - Reactions use rel_type: m.annotation

    Args:
        event_source: The event source dictionary (e.g., event.source for nio events)

    Returns:
        ThreadInfo object with complete thread analysis

    """
    if not event_source:
        return ThreadInfo(
            is_thread=False,
            thread_id=None,
            can_be_thread_root=True,
            safe_thread_root=None,
            has_relations=False,
            relation_type=None,
        )

    content = event_source.get("content", {})
    relates_to = content.get("m.relates_to", {})

    # Check for any relation type
    relation_type = relates_to.get("rel_type")
    has_relations = bool(relates_to)

    # Check if this is a thread message
    is_thread = relation_type == "m.thread"
    thread_id = relates_to.get("event_id") if is_thread else None

    # Determine if this event can be a thread root (per MSC3440)
    # An event can only be a thread root if it has NO relations
    can_be_thread_root = not has_relations

    # Determine safe thread root for creating new threads
    safe_thread_root = None
    if not can_be_thread_root:
        # This event has relations, so it cannot be a thread root
        # Try to use the target of the relation as the thread root

        if relation_type in ("m.replace", "m.annotation", "m.reference"):
            # For edits, reactions, and references, use the target event
            target_event_id = relates_to.get("event_id")
            if target_event_id:
                safe_thread_root = str(target_event_id)
        elif "m.in_reply_to" in relates_to:
            # For rich replies (even without rel_type)
            in_reply_to = relates_to.get("m.in_reply_to", {})
            if in_reply_to and "event_id" in in_reply_to:
                safe_thread_root = str(in_reply_to["event_id"])

    return ThreadInfo(
        is_thread=is_thread,
        thread_id=thread_id,
        can_be_thread_root=can_be_thread_root,
        safe_thread_root=safe_thread_root,
        has_relations=has_relations,
        relation_type=relation_type,
    )
