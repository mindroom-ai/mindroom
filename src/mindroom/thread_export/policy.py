"""Pure authorization policy for thread-export targets."""

from mindroom.thread_export.models import ThreadExportRoom, ThreadExportTarget


def target_accepts_room(target: ThreadExportTarget, room: ThreadExportRoom) -> bool:
    """Return whether one target includes the room's source category."""
    return target.include_invited_rooms or not room.invited


def target_retains_unverified_room(target: ThreadExportTarget, room: ThreadExportRoom) -> bool:
    """Return whether stale data may remain when source authorization cannot be verified."""
    return target.required_member_user_id is None and target_accepts_room(target, room)
