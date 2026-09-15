"""Toolkit wrapper for governed external event delivery."""

from __future__ import annotations

import json

from agno.tools import Toolkit

from mindroom.external_events import ExternalEventDeliveryError, deliver_event
from mindroom.tool_system.runtime_context import get_tool_runtime_context


class ExternalEventsTools(Toolkit):
    """Deliver external events to the current requester, agent, and room."""

    def __init__(self) -> None:
        super().__init__(name="external_events", tools=[self.deliver_event])

    async def deliver_event(
        self,
        source: str,
        event_id: str,
        message: str,
        conversation_key: str | None = None,
        actor_id: str | None = None,
        title: str | None = None,
        data: dict[str, object] | None = None,
    ) -> str:
        """Deliver verified external text with stable source/event idempotency.

        Args:
            source: Provider-qualified subscription identifier, at most 128 characters.
            event_id: Stable external event identifier, at most 256 characters.
            message: External message text; the first delivery's content is retained on retries.
            conversation_key: Opaque external conversation identifier to group into one new thread.
            actor_id: External actor provenance, never a Matrix authority override.
            title: Optional visible title.
            data: Optional verified source metadata, rendered as JSON.

        """
        context = get_tool_runtime_context()
        if context is None:
            msg = "External events require live Matrix tool context."
            raise ExternalEventDeliveryError(msg)
        return json.dumps(
            await deliver_event(
                context,
                source=source,
                event_id=event_id,
                message=message,
                conversation_key=conversation_key,
                actor_id=actor_id,
                title=title,
                data=data,
            ),
        )
