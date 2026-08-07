"""Durable ownership of inbound Matrix events, visible content, and delivery.

Three facts live here, each with exactly one owner:

- what MindRoom accepted from Matrix and still owes work for (the journal),
- what a conversation currently looks like (the projection),
- what MindRoom intends to send and whether it landed (the outbox).
"""

from .identity import decode_thread_id, delivery_transaction_id, encode_thread_id
from .models import (
    AdmissionResult,
    ConversationCursor,
    ConversationPage,
    DeliveryStage,
    EventClass,
    EventKind,
    InboundEvent,
    JournalEvent,
    OutboxDelivery,
    RefreshRequest,
    SettlementOutcome,
    VisibleMessage,
)
from .projection import ProjectedEvent, replacement_target, thread_root, visible_content
from .store import EventJournalStore, PrincipalStore

__all__ = [
    "AdmissionResult",
    "ConversationCursor",
    "ConversationPage",
    "DeliveryStage",
    "EventClass",
    "EventJournalStore",
    "EventKind",
    "InboundEvent",
    "JournalEvent",
    "OutboxDelivery",
    "PrincipalStore",
    "ProjectedEvent",
    "RefreshRequest",
    "SettlementOutcome",
    "VisibleMessage",
    "decode_thread_id",
    "delivery_transaction_id",
    "encode_thread_id",
    "replacement_target",
    "thread_root",
    "visible_content",
]
