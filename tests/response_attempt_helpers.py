"""Explicit source values for tests constructing historical approval scenarios."""

from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from mindroom.event_journal import EventClass, EventKind, InboundEvent
from tests.conftest import unwrap_extracted_collaborator

if TYPE_CHECKING:
    from mindroom.bot import AgentBot, TeamBot
    from mindroom.event_journal import MatrixDeliveryView, PrincipalStore
    from mindroom.event_journal.replies import PreparedReplyRow, ReplyRowEnqueue, ReplyRowRequest


class _DirectResponseOutbox:
    """Supply the ingress admission explicitly skipped by direct runner tests."""

    def __init__(self, inner: "MatrixDeliveryView", principal: "PrincipalStore") -> None:
        self.inner = inner
        self.principal = principal

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        return getattr(self.inner, name)

    async def _admit_sources(self, request: "ReplyRowRequest") -> None:
        span = await self.principal.replies.span(request.span_id)
        reply = None if span is None else await self.principal.replies.load(span.reply_id)
        if span is not None and reply is not None:
            for event_id in span.sources.pending:
                if await self.principal.load_event(event_id) is None:
                    await self.principal.admit(
                        InboundEvent(
                            event_id=event_id,
                            room_id=reply.room_id,
                            thread_id=reply.thread_id,
                            kind=EventKind.MESSAGE,
                            event_class=EventClass.ACTIONABLE,
                            sender="@user:localhost",
                            origin_server_ts=1,
                            source={},
                        ),
                    )

    async def enqueue_reply_row(
        self,
        request: "ReplyRowRequest",
        prepared: "PreparedReplyRow",
    ) -> "ReplyRowEnqueue | None":
        await self._admit_sources(request)
        return await self.inner.enqueue_reply_row(request, prepared)


def install_direct_response_admission(bot: "AgentBot | TeamBot") -> None:
    """Opt a direct-runner fixture into the admission normally provided by ingress.

    Existing admissions remain untouched, including conflicting room/epoch facts.
    Real ingress and ownership validation tests must keep their original outbox.
    """
    gateway = unwrap_extracted_collaborator(bot._delivery_gateway)
    if not isinstance(gateway.deps.outbox, _DirectResponseOutbox):
        outbox = _DirectResponseOutbox(gateway.deps.outbox, bot.journal_principal())
        object.__setattr__(gateway, "deps", replace(gateway.deps, outbox=cast("MatrixDeliveryView", outbox)))
