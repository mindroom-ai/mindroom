"""Pure authorization policy for thread-export targets.

Removal of previously exported data is a retraction action, so it may only follow a definitive
authorization answer: the target no longer accepts the room's source category, or a successful
membership lookup proves the scoped user is not a member. When authorization cannot be verified
(membership lookup errors, account failures), targets keep their existing exports and simply skip
the room for that pass; a transient homeserver error must never destroy authorized data.
Each target may retract data only from an output directory it exclusively owns, so enabled targets
whose resolved directories overlap by equality or nesting are all skipped before Matrix work.
"""

from mindroom.thread_export.models import ThreadExportRoom, ThreadExportTarget


def target_accepts_room(target: ThreadExportTarget, room: ThreadExportRoom) -> bool:
    """Return whether one target includes the room's category and owning entity."""
    if room.invited and not target.include_invited_rooms:
        return False
    if target.source_entity_names is None:
        return True
    return any(entity_name in target.source_entity_names for entity_name in room.source_entity_names)
