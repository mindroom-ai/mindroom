"""Save, take over, release, resume, and stop the reply messages that hold background work between turns."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from mindroom.streaming import StreamingPresentation, UnfinishedStreamedReply
from mindroom.tool_jobs.completion import HeldContinuation, completion_prompt
from mindroom.tool_jobs.held_replies import (
    HeldReply,
    conversation_work,
    decode_held_reply,
    encode_held_reply,
    ended_edit,
    held_edit,
    holds_job,
    released_edit,
    waiting_notice,
)
from mindroom.tool_jobs.runtime import get_background_runtime

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    import nio
    import structlog

    from mindroom.constants import RuntimePaths
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.event_journal import HeldReplyStore, PrincipalStore, SavedHeldReply
    from mindroom.final_delivery import FinalDeliveryOutcome
    from mindroom.response_runner import ResponseRequest
    from mindroom.stop import StopManager
    from mindroom.tool_jobs.completion import ReplyBoundary
    from mindroom.tool_jobs.runtime import BackgroundJob


@dataclass(frozen=True)
class HeldReplyLifecycle:
    """One entity's held messages: which message holds its work, and what a turn, wake, or Stop does to that."""

    store: HeldReplyStore
    journal: PrincipalStore
    delivery_gateway: DeliveryGateway
    stop_manager: StopManager
    client: Callable[[], nio.AsyncClient]
    runtime_paths: RuntimePaths
    agent_name: str
    logger: structlog.stdlib.BoundLogger
    # Messages a turn runs on, from before that turn can be stopped until it settles them.
    _turns: set[str] = field(default_factory=set, init=False, repr=False)

    def read(self, saved: SavedHeldReply) -> HeldReply | None:
        """Read one saved hold; an unreadable one holds nothing this runtime can continue."""
        try:
            return decode_held_reply(saved)
        except ValueError:
            self.logger.exception("held_reply_unreadable", hold_id=saved.hold_id)
            return None

    async def _redact_stop_button(self, hold: HeldReply) -> None:
        """Remove the Stop button a released message kept while it held work."""
        if hold.stop_button_event_id is None:
            return
        try:
            await self.client().room_redact(hold.key.room_id, hold.stop_button_event_id, reason="Response completed")
        except Exception as error:
            # The message no longer holds anything; a leftover button only finds nothing to stop.
            self.logger.warning("held_reply_stop_button_cleanup_failed", error=str(error))

    async def release(self, hold: HeldReply, *, stopped: bool = False) -> None:
        """Show a message as its reply finished, or as stopped, once it holds nothing any more."""
        if hold.message_event_id is not None:
            await self.delivery_gateway.edit_text(
                ended_edit(hold, cancel_source="user_stop") if stopped else released_edit(hold),
            )
        await self._redact_stop_button(hold)

    async def save(self, hold: HeldReply) -> None:
        """Make a message the holder of its work, releasing the message that held it before."""
        replaced, _saved = await self.store.save(
            hold_id=hold.key.hold_id,
            recipient=hold.key.recipient,
            message_event_id=hold.message_event_id,
            hold_json=encode_held_reply(hold),
        )
        previous = self.read(replaced) if replaced is not None else None
        if previous is not None and previous.message_event_id != hold.message_event_id:
            await self.release(previous)
        await self._show_held(hold)

    async def _show_held(self, hold: HeldReply) -> None:
        """Show a message holding its work with its notice, and wake the work if it is ready already."""
        if hold.message_event_id is not None:
            await self.delivery_gateway.edit_text(held_edit(hold))
        runtime = get_background_runtime(self.runtime_paths)
        if runtime is not None:
            # Work may have become ready before this hold existed to be woken for it.
            runtime.changed.set()

    async def settle(
        self,
        request: ResponseRequest,
        final_outcome: FinalDeliveryOutcome,
        boundary: ReplyBoundary | None,
        *,
        continued: HeldReply | None,
        stop_button_event_id: str | None,
    ) -> None:
        """Let a finished reply's message hold its outstanding work, and release whatever nothing holds any more.

        ``continued`` is the hold of the message this turn ran on, such as one it continued or an edit regenerated.
        """
        message_id = final_outcome.final_visible_event_id
        if boundary is not None and continued is not None and continued.key.hold_id != boundary.key.hold_id:
            # The turn holds under another key, such as after its team's roster changed, so the hold it ran on ends.
            await self.store.delete(continued.key.hold_id, generation=continued.generation)
        notice = boundary.notice if boundary is not None else None
        if boundary is not None and notice is None:
            # Nothing is outstanding, so the message that held the conversation's work holds nothing any more.
            released = await self.store.delete(boundary.key.hold_id)
            previous = self.read(released) if released is not None else None
            if previous is not None and previous.message_event_id != message_id:
                await self.release(previous)
            return
        if (
            boundary is not None
            and notice is not None
            and final_outcome.terminal_status == "completed"
            and (message_id is not None or boundary.key.silent)
        ):
            await self.save(
                HeldReply(
                    key=boundary.key,
                    target=request.response_envelope.target,
                    source_kind=request.response_envelope.source_kind,
                    message_event_id=message_id,
                    presentation=StreamingPresentation(
                        response_text=(final_outcome.final_visible_body or "").strip(),
                        tool_trace=final_outcome.tool_trace,
                    ),
                    extra_content=dict(final_outcome.extra_content or {}),
                    notice=notice,
                    stop_button_event_id=stop_button_event_id,
                    joins=boundary.joins,
                    offered=boundary.offered,
                ),
            )
            return
        # The turn could not show the work it leaves outstanding: it failed, was stopped or interrupted, or paused for
        # approval. A message that held that work before goes on holding it, but the one this turn ran on holds nothing
        # any more; an approval pause reaches a boundary of its own once it resumes.
        if continued is None:
            return
        # A Stop on the message may have released the hold already.
        await self.store.delete(continued.key.hold_id, generation=continued.generation)
        if (
            continued.message_event_id is not None
            and final_outcome.delivery_kind is None
            and final_outcome.terminal_status != "suspended"
        ):
            # Nothing replaced the message, which still shows the waiting notice.
            await self.delivery_gateway.edit_text(
                released_edit(continued)
                if final_outcome.terminal_status == "completed"
                else ended_edit(continued, cancel_source=final_outcome.resolved_cancel_source or "interrupted"),
            )

    @contextmanager
    def running_on(self, hold: HeldReply | None) -> Iterator[None]:
        """Leave a held message to the turn running on it, so a Stop does not edit it under that turn."""
        message_id = hold.message_event_id if hold is not None else None
        if message_id is None:
            yield
            return
        self._turns.add(message_id)
        try:
            yield
        finally:
            self._turns.discard(message_id)

    async def released(self, hold: HeldReply) -> bool:
        """Whether a hold is gone, such as after a Stop released its message before a turn on it could be stopped."""
        saved = await self.store.load(hold.key.hold_id)
        return saved is None or saved.generation != hold.generation

    async def on_message(self, message_id: str | None) -> HeldReply | None:
        """Return the hold one of this entity's messages carries; only an instance running background jobs has any."""
        if message_id is None or get_background_runtime(self.runtime_paths) is None:
            return None
        saved = await self.store.load_for_message(self.agent_name, message_id)
        return self.read(saved) if saved is not None else None

    async def holds_work(self, message_id: str, room_id: str) -> bool:
        """Whether one of this entity's messages in a room holds outstanding work, so a Stop on it ends that work."""
        hold = await self.on_message(message_id)
        return hold is not None and hold.key.room_id == room_id

    async def stop(self, message_id: str, stop_receipt_order: int) -> bool:
        """End the work a message holds while no turn runs on it, and show the message as stopped.

        False when the message holds nothing, or when a turn began continuing it meanwhile; that turn stops like any
        reply and shows how it ended.
        """
        hold = await self.on_message(message_id)
        runtime = get_background_runtime(self.runtime_paths)
        if hold is None or runtime is None:
            return False
        # Released first, so a wake queued for the message no longer continues it.
        released = await self.store.delete(hold.key.hold_id, generation=hold.generation)
        journal = self.journal
        # Like any Stop, it ends work through the message's latest turn, not the work of a newer reply still running.
        cutoff = await journal.response_receipt_order_before_stop(
            room_id=hold.key.room_id,
            response_event_id=message_id,
            stop_receipt_order=stop_receipt_order,
        )

        async def held_by_message(job: BackgroundJob) -> bool:
            # Outcomes a turn already read belong to that turn, not to the message's outstanding work.
            if not holds_job(hold.key, job) or job.consumed:
                return False
            source = await journal.load_event(job.source_event_id) if job.source_event_id is not None else None
            return cutoff is None or source is None or source.receipt_order <= cutoff

        await runtime.stop_jobs(receipt_order=stop_receipt_order, matches=held_by_message)
        if self.stop_manager.can_handle_stop_reaction(message_id, hold.key.room_id):
            return False
        # A turn running on the message settles it instead: one that cannot be stopped yet stops itself once it can,
        # and one already past its reply keeps that reply.
        if released is not None and message_id not in self._turns:
            # No later turn edits the message: a takeover or wake finds the hold gone.
            await self.release(hold, stopped=True)
        return True

    async def release_unstarted(self, hold: HeldReply) -> None:
        """Release a hold whose continuation never began, such as one its requester may no longer run."""
        # A turn that began on the hold saved or released it; only one that never began leaves it to the wake.
        if await self.store.delete(hold.key.hold_id, generation=hold.generation) is not None:
            await self.release(hold)

    async def resume(self, request: ResponseRequest) -> ResponseRequest | None:
        """Under the conversation lock, continue a held message with ready work, or leave it as its work stands."""
        hold = request.held_reply
        if hold is None:
            return request
        runtime = get_background_runtime(self.runtime_paths)
        if runtime is None:
            # The wake stays pending until a runtime can tell what the held work is.
            msg = "Tool job runtime is not ready for a held reply continuation"
            raise RuntimeError(msg)
        saved = await self.store.load(hold.key.hold_id)
        if saved is None or saved.generation != hold.generation:
            # A newer turn saved or released the hold since the wake.
            return None
        work = await conversation_work(runtime, hold.key, attempted=hold.offered)
        envelope = request.response_envelope
        # A run of this wake that a crash cut short may already have read outcomes; its re-run reads them again.
        reread = [
            job
            for job in await runtime.source_jobs(
                envelope.source_event_id,
                transport_agent_name=self.agent_name,
                room_id=request.room_id,
                thread_id=request.thread_id,
                session_id=envelope.target.session_id,
                requester_id=envelope.requester_id,
            )
            if job.consumed_by_source == envelope.source_event_id
        ]
        ready = (*work.ready, *reread)
        if ready:
            prompt = completion_prompt(ready)
            return replace(
                request,
                prompt=prompt,
                response_envelope=replace(envelope, body=prompt),
                held_continuation=HeldContinuation(
                    attempted_job_ids=hold.offered | {job.job_id for job in ready},
                    joins=hold.joins,
                ),
                # The message keeps its text and trace above the continuation, as a reply a restart cut short does.
                existing_event_is_placeholder=hold.message_event_id is not None,
                resumed_reply=None
                if hold.message_event_id is None
                else UnfinishedStreamedReply(
                    visible_text=hold.presentation.response_text,
                    tool_trace=hold.presentation.tool_trace,
                    interrupted=False,
                ),
            )
        if work.jobs:
            # The work changed without becoming ready, so the message shows what it waits for now, unless a Stop
            # released the hold meanwhile.
            waiting = replace(hold, notice=waiting_notice(work.jobs))
            if (
                await self.store.resave(
                    hold.key.hold_id,
                    generation=hold.generation,
                    hold_json=encode_held_reply(waiting),
                )
                is not None
            ):
                await self._show_held(waiting)
        elif await self.store.delete(hold.key.hold_id, generation=hold.generation) is not None:
            await self.release(hold)
        return None
