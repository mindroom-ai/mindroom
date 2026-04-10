# ruff: noqa: INP001
"""Notify threads tagged pending-restart after a bot restart."""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.hooks import EVENT_BOT_READY, hook
from mindroom.thread_tags import (
    THREAD_TAGS_EVENT_TYPE,
    list_tagged_threads_from_state_map,
    normalize_tag_name,
    remove_thread_tag_via_room_state,
)

if TYPE_CHECKING:
    from mindroom.hooks import AgentLifecycleContext


CLAIM_STALE_AFTER_SECONDS = 60


def _claim_file_age_seconds(fd: int) -> float:
    """Return the age of the current claim file contents."""
    return max(time.time() - os.fstat(fd).st_mtime, 0.0)


def _acquire_restart_claim(claim_path: Path) -> int | None:
    """Acquire the startup claim or return ``None`` when another worker holds it."""
    fd = os.open(str(claim_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _write_claim_owner(fd: int) -> None:
    """Refresh the claim file contents and timestamp for the active worker."""
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"{os.getpid()}\n".encode())
    os.fsync(fd)


async def _notify_room_threads(ctx: AgentLifecycleContext, room_id: str, tag: str) -> int:
    """Notify pending-restart threads in one room and return the count."""
    try:
        room_tags = await ctx.query_room_state(room_id, THREAD_TAGS_EVENT_TYPE)
    except Exception:
        ctx.logger.warning("Failed to query room state", room_id=room_id, exc_info=True)
        return 0
    if room_tags is None:
        ctx.logger.warning("Failed to query room state", room_id=room_id)
        return 0

    tagged_threads = list_tagged_threads_from_state_map(
        room_id,
        room_tags,
        tag=tag,
    )
    if not tagged_threads:
        return 0

    notified = 0
    for thread_id, thread_state in tagged_threads.items():
        try:
            event_id = await ctx.send_message(
                room_id=room_id,
                text=f"🔄 Restart completed — this thread's `{tag}` changes are now live.",
                thread_id=thread_id,
                trigger_dispatch=True,
            )
        except Exception:
            ctx.logger.warning(
                "Failed to notify thread",
                room_id=room_id,
                thread_id=thread_id,
                exc_info=True,
            )
            continue
        if not event_id:
            ctx.logger.warning("Failed to notify thread", room_id=room_id, thread_id=thread_id)
            continue

        try:
            verified_state = await remove_thread_tag_via_room_state(
                room_id,
                thread_id,
                tag,
                query_room_state=ctx.query_room_state,
                put_room_state=ctx.put_room_state,
                expected_record=thread_state.tags[tag],
            )
        except Exception:
            ctx.logger.warning(
                "Failed to clear restart tag after notification",
                room_id=room_id,
                thread_id=thread_id,
                exc_info=True,
            )
            continue
        if verified_state.tags.get(tag) is None:
            ctx.logger.info("Notified pending-restart thread", room_id=room_id, thread_id=thread_id)
            notified += 1
        else:
            ctx.logger.warning("Failed to clear restart tag after notification", room_id=room_id, thread_id=thread_id)
    return notified


@hook(EVENT_BOT_READY, name="notify-after-restart", agents=(ROUTER_AGENT_NAME,), priority=100, timeout_ms=30000)
async def notify_after_restart(ctx: AgentLifecycleContext) -> None:
    """Scan rooms for pending-restart tagged threads and notify them."""
    tag = ctx.settings.get("tag", "pending-restart")
    tag = normalize_tag_name(tag)

    if ctx.room_state_querier is None:
        ctx.logger.warning("No room state querier — cannot scan for pending-restart threads")
        return
    if ctx.room_state_putter is None:
        ctx.logger.warning("No room state putter — cannot clear pending-restart tags")
        return
    if ctx.message_sender is None:
        ctx.logger.warning("No message sender — cannot notify pending-restart threads")
        return

    claim_path = Path(ctx.state_root) / ".restart-claim"
    claim_fd = _acquire_restart_claim(claim_path)
    if claim_fd is None:
        return

    try:
        claim_age_seconds = _claim_file_age_seconds(claim_fd)
        if claim_age_seconds > CLAIM_STALE_AFTER_SECONDS:
            ctx.logger.info(
                "Recovered stale restart-notify claim",
                claim_path=str(claim_path),
                claim_age_seconds=claim_age_seconds,
            )
        _write_claim_owner(claim_fd)

        notified = 0
        for room_id in ctx.joined_room_ids:
            notified += await _notify_room_threads(ctx, room_id, tag)
        if notified:
            ctx.logger.info("Restart-notify complete", notified_count=notified)
    finally:
        claim_path.unlink(missing_ok=True)
        os.close(claim_fd)
