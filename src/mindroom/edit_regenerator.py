"""Regenerate the reply to an edited message when it is the latest in its conversation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from mindroom.coalescing.batch import coalesced_prompt, tagged_coalesced_prompt
from mindroom.entity_resolution import persisted_bot_user_ids
from mindroom.hooks import hook_ingress_policy
from mindroom.logging_config import get_logger
from mindroom.matrix.client_visible_messages import extract_visible_edit_body
from mindroom.matrix.member_display_names import room_member_display_names
from mindroom.reply_lifecycle import ReplyState
from mindroom.reply_turn.request import ResponseRequest
from mindroom.response_admission import ResponseAdmissionRefusedError
from mindroom.response_sources import ResponseSources, Unclaimed
from mindroom.runtime_protocols import SupportsClientConfig  # noqa: TC001
from mindroom.timestamp_formatting import normalize_timestamp_ms
from mindroom.turn_policy import ResponseAction
from mindroom.turn_record import canonicalize_turn_record
from mindroom.turn_store import SourcePreparation
from mindroom.turns.dispatch import run_response_action

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    import nio

    from mindroom.coalescing.batch import TimestampFormatter
    from mindroom.constants import RuntimePaths
    from mindroom.conversation_resolver import ConversationResolver, MessageContext
    from mindroom.event_journal import PrincipalStore
    from mindroom.hooks import MessageEnvelope
    from mindroom.hooks.ingress import IngressHookRunner
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.event_info import EventInfo
    from mindroom.reply_lifecycle import Reply
    from mindroom.response_runner import ResponseRunner
    from mindroom.response_sources import SourceFate
    from mindroom.turn_policy import TurnPolicy
    from mindroom.turn_record import SourceEventRevision, TurnRecord
    from mindroom.turn_store import TurnStore
    from mindroom.turns.facts import IngressFacts


logger = get_logger(__name__)


@dataclass(frozen=True)
class EditRegeneratorDeps:
    """Collaborators needed for edit-triggered regeneration."""

    runtime: SupportsClientConfig
    runtime_paths: RuntimePaths
    agent_name: str
    resolver: ConversationResolver
    turn_store: TurnStore
    ingress_hook_runner: IngressHookRunner
    # Runs each regeneration as the entity answers now, on a task of its own off the room's lane.
    response_runner: ResponseRunner
    turn_policy: TurnPolicy
    # Settles sources nothing will answer, as the room's lane does for an event it ignores.
    settle_sources: Callable[[tuple[str, ...]], Awaitable[None]]
    # Records a Stop on a reply that still runs, as a Stop reaction would.
    stop_reply: Callable[[Reply, int], Awaitable[object]]
    receipt_order: Callable[[], Awaitable[int]]
    timestamp_formatter: TimestampFormatter
    # The reply records and the stored conversation an edit is checked against.
    store: PrincipalStore


@dataclass(frozen=True)
class _EditedSource:
    """The message an edit changed, the edit's revision and text, the answer it regenerates, and its live sources."""

    source_event_id: str
    revision: SourceEventRevision
    reply_event_id: str | None
    content: str
    # The turn's sources nobody deleted when the regeneration was built.
    live_source_event_ids: tuple[str, ...]


def _not_regenerable(reply: Reply | None) -> str | None:
    """Return why an edit regenerates no reply, or ``None`` for one that may show something and is not gone.

    A create Matrix has not acknowledged yet may show. A reply that recorded
    nothing yet, as a participation turn that has not sent anything yet, is
    not stopped: it would leave a cancel note where nothing showed, and its
    regeneration would answer without the participation decision.
    """
    if reply is None:
        return "no_reply"
    if reply.state is ReplyState.GONE:
        return "gone"
    if reply.event_id is None and reply.possibly_shown is None:
        return "reply_not_shown"
    return None


@dataclass
class EditRegenerator:
    """Re-run the reply to the latest message of a conversation when its author edits it."""

    deps: EditRegeneratorDeps

    def _client(self) -> nio.AsyncClient:
        client = self.deps.runtime.client
        if client is None:
            msg = "Matrix client is not ready for edit regeneration"
            raise RuntimeError(msg)
        return client

    async def _later_human_message(self, record: TurnRecord) -> bool:
        """Return whether a human wrote in the turn's conversation after it, so an edit of it regenerates nothing."""
        target = record.conversation_target
        assert target is not None, "an edit regenerates only a turn with a conversation"
        return await self.deps.store.later_message_exists(
            room_id=target.room_id,
            thread_id=target.resolved_thread_id,
            source_event_ids=record.source_event_ids,
            excluded_senders=persisted_bot_user_ids(self.deps.runtime_paths),
            # In room mode the room is one conversation, threads included.
            whole_room=self.deps.resolver.room_is_one_conversation(target.room_id),
        )

    async def handle_message_edit(  # noqa: C901, PLR0911, PLR0912
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageFormatted,
        event_info: EventInfo,
        facts: IngressFacts,
    ) -> bool | None:
        """Regenerate the reply an edit's message got; True when its regeneration owns the edit.

        Only the latest message of its conversation regenerates; a reply that
        still runs, or one an approval holds, is stopped first, which cancels
        that approval. Any other edit changes nothing the agent did.
        """
        if not event_info.original_event_id:
            return None
        original_event_id = event_info.original_event_id
        requester_user_id = facts.lane_requester_id
        # Another sender's edit that runs for the requester, as anyone's in a contact room does, is not the
        # requester revising their own message, even when it names one.
        if facts.acts_for_requester:
            logger.info(
                "edit_not_regenerated",
                room_id=room.room_id,
                source_event_id=original_event_id,
                reason="edited_by_another_sender",
            )
            return None

        # Every decision below is taken from the record's own conversation target, never from the thread the
        # edit names.
        turn_record = await self.deps.turn_store.get_turn_record_after_writes(original_event_id)
        if turn_record is None:
            # Usually this agent never took the message; it may also still wait for media or voice before its turn.
            logger.info(
                "edit_not_regenerated",
                room_id=room.room_id,
                source_event_id=original_event_id,
                reason="no_turn_record",
            )
            return None
        if (
            turn_record.conversation_target is None
            or turn_record.history_scope is None
            or turn_record.response_owner != self.deps.agent_name
        ):
            return None
        if turn_record.requester_id_for_source(original_event_id) != requester_user_id:
            return None
        deleted = await self.deps.turn_store.deleted_sources(turn_record)
        # Regeneration replays every source under the editor's identity, so a
        # record mixing senders would run their messages as the editor.
        if not turn_record.sources_all_from_requester(requester_user_id, deleted=deleted):
            return None
        # A requester owns replies an entity wrote for them without having written them.
        if any(metadata.speaker is not None for metadata in (turn_record.source_event_metadata or {}).values()):
            return None
        if turn_record.prompt_source_event_id(original_event_id) in deleted:
            return None
        reply = await self.deps.store.replies.for_sources(turn_record.source_event_ids)
        reason = _not_regenerable(reply) or (
            "later_human_message" if await self._later_human_message(turn_record) else None
        )
        if reason is not None:
            logger.info("edit_not_regenerated", room_id=room.room_id, source_event_id=original_event_id, reason=reason)
            return None
        assert reply is not None

        revision = (event.server_timestamp, event.event_id)
        # The edited message can be the human message a relay answers for; its revisions live on the relay's source.
        prompt_source = turn_record.prompt_source_event_id(original_event_id)
        committed = (turn_record.source_event_revisions or {}).get(prompt_source)
        watermark = turn_record.revision_watermark(original_event_id)
        if watermark is not None and revision < watermark:
            return None
        registered = await self.deps.turn_store.register_edit_revision(turn_record, original_event_id, revision)
        if registered is None or await self.deps.turn_store.redacted_event_ids(
            turn_record.conversation_target.room_id,
            (revision[1],),
        ):
            return None
        edited_content, _ = await extract_visible_edit_body(
            event.source,
            self._client(),
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
        )
        if edited_content is None:
            return None
        context = await self.deps.resolver.extract_message_context(
            room,
            event,
            thread_id=turn_record.conversation_target.resolved_thread_id,
        )
        envelope = self.deps.resolver.build_envelope(
            event=event,
            facts=facts,
            target=turn_record.conversation_target,
            mentioned_agents=context.mentioned_agents,
            body=edited_content,
        )
        if revision != committed and await self.deps.ingress_hook_runner.emit_message_received_hooks(
            envelope=envelope,
            correlation_id=event.event_id,
            policy=hook_ingress_policy(envelope),
        ):
            return None

        record = canonicalize_turn_record(
            registered,
            source_event_prompts={**(registered.source_event_prompts or {}), prompt_source: edited_content},
            source_event_revisions={**(registered.source_event_revisions or {}), prompt_source: revision},
        )
        live_sources = tuple(source for source in record.source_event_ids if source not in deleted)
        prompt, structured = self._prompt(room, record, live_sources, edited_content)
        if prompt is None:
            # A sibling's text is no longer known: nothing regenerates from a partial turn.
            return None
        if reply.current_span_id is not None or reply.approval_id is not None:
            # The answer still runs or waits for an approval: the edit stops it, which cancels that approval,
            # and the regeneration takes its place.
            await self.deps.stop_reply(reply, await self.deps.receipt_order())
        request = self._request(
            room,
            record,
            context,
            envelope,
            _EditedSource(original_event_id, revision, reply.event_id, edited_content, live_sources),
            prompt,
            structured=structured,
        )
        self._regenerate(request)
        return True

    def _prompt(
        self,
        room: nio.MatrixRoom,
        record: TurnRecord,
        live_sources: tuple[str, ...],
        edited_content: str,
    ) -> tuple[str | None, bool]:
        """Return the prompt the edited turn runs with from its sources nobody deleted, and whether it is structured."""
        if not record.is_coalesced:
            return edited_content, False
        prompt_map = dict(record.source_event_prompts or {})
        parts = [prompt_map.get(source_event_id) for source_event_id in live_sources]
        if any(part is None for part in parts):
            return None, False
        if record.source_event_metadata is not None:
            tagged = tagged_coalesced_prompt(
                list(live_sources),
                prompt_map,
                dict(record.source_event_metadata),
                timestamp_formatter=self.deps.timestamp_formatter,
                member_display_names=room_member_display_names(room),
            )
            if tagged is not None:
                return tagged, True
        return coalesced_prompt([part for part in parts if part is not None]), False

    def _request(
        self,
        room: nio.MatrixRoom,
        record: TurnRecord,
        context: MessageContext,
        envelope: MessageEnvelope,
        edited: _EditedSource,
        prompt: str,
        *,
        structured: bool,
    ) -> ResponseRequest:
        requester_id = envelope.requester_id
        revision = edited.revision

        async def prepare_snapshot(history: Sequence[ResolvedVisibleMessage]) -> SourcePreparation:
            stale = await self.deps.turn_store.prepare_edit_snapshot(
                record=record,
                live_source_event_ids=edited.live_source_event_ids,
                driving_revision_id=revision[1],
                thread_history=history,
            )
            return SourcePreparation(invalid=record.source_event_ids) if stale else SourcePreparation()

        async def commit_edit() -> None:
            # The claimed reply answers the edited text from now on, so the turn holds it and the history run
            # this edit replaces may go.
            await self.deps.turn_store.record_edit(
                record,
                edited_event_id=edited.source_event_id,
                prompt=edited.content,
                revision=revision,
            )
            await self.deps.turn_store.remove_stale_runs_for_edit(turn_record=record, requester_user_id=requester_id)

        return ResponseRequest(
            thread_history=context.thread_history,
            member_display_names=room_member_display_names(room),
            prompt=prompt,
            response_envelope=envelope,
            sources=ResponseSources(
                pending_event_ids=(revision[1],),
                logical_source_event_ids=record.source_event_ids,
                discovery_event_ids=record.discovery_event_ids,
            ),
            existing_event_id=edited.reply_event_id,
            user_id=requester_id,
            correlation_id=revision[1],
            matrix_run_metadata=self.deps.turn_store.build_run_metadata(record),
            current_timestamp_ms=normalize_timestamp_ms(revision[0]),
            current_prompt_is_structured=structured,
            prepare_source_turn=prepare_snapshot,
            on_reply_claimed=commit_edit,
            edit_regeneration=True,
        )

    def _regenerate(self, request: ResponseRequest) -> None:
        """Start the regeneration off the room's lane, which hands it the edit.

        Its claim waits for the conversation, which another reply of this agent
        holds for as long as that reply runs, so the lane does not wait for it.
        A regeneration whose response left the edit unclaimed settles it; one
        that fails settles it only before a claim, since a claimed reply's
        records own the edit from then on.
        """
        claimed = False
        commit_edit = request.on_reply_claimed
        assert commit_edit is not None

        async def on_claimed() -> None:
            nonlocal claimed
            claimed = True
            await commit_edit()

        edit_sources = request.sources.pending_event_ids
        room_id = request.response_envelope.target.room_id

        async def regenerate() -> None:
            try:
                fate = await self._run_regeneration(replace(request, on_reply_claimed=on_claimed))
            except ResponseAdmissionRefusedError:
                # Refused by a runtime being replaced, the edit stays pending, as it does when cancelled at shutdown,
                # so the replacement or a restart regenerates again.
                raise
            except Exception:
                if not claimed:
                    await self.deps.settle_sources(edit_sources)
                raise
            if isinstance(fate, Unclaimed):
                # An edit's source check reports its own sources stale or none, so no other source survives it.
                await self.deps.settle_sources(edit_sources)

        self.deps.response_runner.inbox.track_inbox_response(
            regenerate(),
            name=f"edit_regeneration:{request.correlation_id}",
            room_id=room_id,
            # The edit stays pending until a span settles it, so a restart regenerates again.
            recovery_proof_ready=lambda: True,
            source_event_ids=edit_sources,
        )

    async def _run_regeneration(self, request: ResponseRequest) -> SourceFate:
        """Answer the edited turn as this entity answers now, or end its reply without an answer.

        An edit that leaves no prompt ends the reply empty before any team is
        resolved. A configured team that cannot answer now shows why in the
        reply it regenerates.
        """
        runner = self.deps.response_runner
        if not request.prompt.strip():
            response_kind = "team" if self.deps.agent_name in self.deps.runtime.config.teams else "ai"
            return await runner.generate_response_for_empty_prompt(request, response_kind=response_kind)
        action = self.deps.turn_policy.effective_response_action(ResponseAction(kind="individual"))
        if action.kind == "reject":
            assert action.form_team is not None
            assert action.rejection_message is not None
            return await runner.deliver_team_rejection(
                request,
                member_names=[member.name for member in action.form_team.member_statuses],
                team_mode=self.deps.runtime.config.teams[self.deps.agent_name].mode,
                rejection_message=action.rejection_message,
            )
        return await run_response_action(runner, request, action)
