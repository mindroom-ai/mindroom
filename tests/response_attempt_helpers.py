"""Explicit source values for tests constructing historical approval scenarios."""

from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from mindroom.event_journal import EventClass, EventKind, InboundEvent
from mindroom.response_sources import ResponseSources
from mindroom.turn_record import TurnRecord


def approval_sources(pending: tuple[str, ...], prepared: TurnRecord | None) -> ResponseSources:
    """Build a current request value from a test's selected turn snapshot."""
    return ResponseSources(
        pending,
        prepared.source_event_ids if prepared is not None else pending,
        prepared.discovery_event_ids if prepared is not None else (),
        prepared.latest_edit_receipt_order if prepared is not None else None,
    )


if TYPE_CHECKING:
    from mindroom.bot import AgentBot, TeamBot
    from mindroom.event_journal import MatrixDeliveryView, PrincipalStore


class _DirectResponseOutbox:
    """Supply the ingress admission explicitly skipped by direct runner tests."""

    def __init__(self, inner: "MatrixDeliveryView", principal: "PrincipalStore") -> None:
        self.inner = inner
        self.principal = principal

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        return getattr(self.inner, name)

    async def enqueue_matrix_delivery(self, **kwargs: Any) -> str | None:  # noqa: ANN401
        attempt = kwargs.get("response_attempt")
        if attempt is not None:
            for event_id in attempt.sources.pending_event_ids:
                if await self.principal.load_event(event_id) is None:
                    await self.principal.admit(
                        InboundEvent(
                            event_id=event_id,
                            room_id=kwargs["room_id"],
                            thread_id=kwargs["thread_id"],
                            kind=EventKind.MESSAGE,
                            event_class=EventClass.ACTIONABLE,
                            sender="@user:localhost",
                            origin_server_ts=1,
                            source={},
                        ),
                    )
        return await self.inner.enqueue_matrix_delivery(**kwargs)


def install_direct_response_admission(bot: "AgentBot | TeamBot") -> None:
    """Opt a direct-runner fixture into the admission normally provided by ingress.

    Existing admissions remain untouched, including conflicting room/epoch facts.
    Real ingress and ownership validation tests must keep their original outbox.
    """
    from tests.conftest import unwrap_extracted_collaborator  # noqa: PLC0415

    gateway = unwrap_extracted_collaborator(bot._delivery_gateway)
    if not isinstance(gateway.deps.outbox, _DirectResponseOutbox):
        outbox = _DirectResponseOutbox(gateway.deps.outbox, bot.journal_principal())
        object.__setattr__(gateway, "deps", replace(gateway.deps, outbox=cast("MatrixDeliveryView", outbox)))
