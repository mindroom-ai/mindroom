"""Pure authorization policy for thread-export targets.

Removal of previously exported data is a retraction action, so it may only follow a definitive
authorization answer: the target no longer accepts the room's source category, or a successful
membership lookup proves the scoped user is not a member. When authorization cannot be verified
(membership lookup errors, account failures), targets keep their existing exports and simply skip
the room for that pass; a transient homeserver error must never destroy authorized data.
"""

from mindroom.thread_export.models import ThreadExportRoom, ThreadExportTarget


def target_accepts_room(target: ThreadExportTarget, room: ThreadExportRoom) -> bool:
    """Return whether one target includes the room's source category."""
    return target.include_invited_rooms or not room.invited


def target_retains_unverified_room(target: ThreadExportTarget, room: ThreadExportRoom) -> bool:
    """Return whether existing exports stay when source authorization cannot be verified."""
    return target_accepts_room(target, room)
