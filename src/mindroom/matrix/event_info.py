"""Comprehensive event relation analysis for Matrix events.

This module provides a unified API for analyzing all Matrix event relations
including threads (MSC3440), edits, replies, reactions, and more.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EventInfo:
    """Comprehensive analysis of Matrix event relations."""

    # Thread information (MSC3440)
    is_thread: bool
    """Whether this event is part of a thread."""

    thread_id: str | None
    """The thread root event ID if this is a thread message."""

    can_be_thread_root: bool
    """Whether this event can be used as a thread root per MSC3440."""

    safe_thread_root: str | None
    """Safe event ID to use as thread root (None means use this event)."""

    # Edit information
    is_edit: bool
    """Whether this event is an edit (m.replace)."""

    original_event_id: str | None
    """The event ID being edited if this is an edit."""

    # Reply information
    is_reply: bool
    """Whether this event is a reply to another event."""

    reply_to_event_id: str | None
    """The event ID being replied to if this is a reply."""

    # Reaction information
    is_reaction: bool
    """Whether this event is a reaction (m.annotation)."""

    reaction_key: str | None
    """The reaction key/emoji if this is a reaction."""

    reaction_target_event_id: str | None
    """The event ID being reacted to if this is a reaction."""

    # General relation information
    has_relations: bool
    """Whether this event has any relations."""

    relation_type: str | None
    """The relation type if any (m.replace, m.annotation, m.thread, etc)."""

    relates_to_event_id: str | None
    """The primary event ID this event relates to (if any)."""

    @staticmethod
    def from_event(event_source: dict | None) -> EventInfo:
        """Create EventInfo from a raw event source dictionary."""
        return _analyze_event_relations(event_source)


def _analyze_event_relations(event_source: dict | None) -> EventInfo:
    """Analyze complete relation information for a Matrix event.

    This unified function provides all relation-related information in one place,
    replacing manual extraction of m.relates_to throughout the codebase.

    Per MSC3440:
    - A thread can only be created from events that don't have any rel_type
    - Thread messages use rel_type: m.thread
    - Edits use rel_type: m.replace
    - Reactions use rel_type: m.annotation
    - Replies can be within threads or standalone

    Args:
        event_source: The event source dictionary (e.g., event.source for nio events)

    Returns:
        EventInfo object with complete relation analysis

    """
    if not event_source:
        return EventInfo(
            is_thread=False,
            thread_id=None,
            can_be_thread_root=True,
            safe_thread_root=None,
            is_edit=False,
            original_event_id=None,
            is_reply=False,
            reply_to_event_id=None,
            is_reaction=False,
            reaction_key=None,
            reaction_target_event_id=None,
            has_relations=False,
            relation_type=None,
            relates_to_event_id=None,
        )

    content = event_source.get("content", {})
    relates_to = content.get("m.relates_to", {})

    # Extract basic relation information
    relation_type = relates_to.get("rel_type")
    has_relations = bool(relates_to)
    relates_to_event_id = relates_to.get("event_id")

    # Thread analysis
    is_thread = relation_type == "m.thread"
    thread_id = relates_to_event_id if is_thread else None

    # Edit analysis
    is_edit = relation_type == "m.replace"
    original_event_id = relates_to_event_id if is_edit else None

    # Reaction analysis
    is_reaction = relation_type == "m.annotation"
    reaction_key = relates_to.get("key") if is_reaction else None
    reaction_target_event_id = relates_to_event_id if is_reaction else None

    # Reply analysis
    # Replies can exist within threads or as standalone
    # They have m.in_reply_to field
    in_reply_to = relates_to.get("m.in_reply_to", {})
    is_reply = bool(in_reply_to and in_reply_to.get("event_id"))
    reply_to_event_id = in_reply_to.get("event_id") if is_reply else None

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
            if relates_to_event_id:
                safe_thread_root = str(relates_to_event_id)
        elif is_reply and reply_to_event_id:
            # For rich replies, use the event being replied to
            safe_thread_root = str(reply_to_event_id)

    return EventInfo(
        # Thread info
        is_thread=is_thread,
        thread_id=thread_id,
        can_be_thread_root=can_be_thread_root,
        safe_thread_root=safe_thread_root,
        # Edit info
        is_edit=is_edit,
        original_event_id=original_event_id,
        # Reply info
        is_reply=is_reply,
        reply_to_event_id=reply_to_event_id,
        # Reaction info
        is_reaction=is_reaction,
        reaction_key=reaction_key,
        reaction_target_event_id=reaction_target_event_id,
        # General info
        has_relations=has_relations,
        relation_type=relation_type,
        relates_to_event_id=relates_to_event_id,
    )
