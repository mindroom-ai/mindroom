"""Durable ownership of inbound Matrix events, visible content, and delivery.

Three facts live here, each with exactly one owner:

- what MindRoom accepted from Matrix and still owes work for (the journal),
- what a conversation currently looks like (the projection),
- what MindRoom intends to send and whether it landed (the outbox).
"""

from .approvals import StoredApprovalCard
from .identity import decode_thread_id, delivery_transaction_id, encode_thread_id
from .membership import MembershipFence, MembershipView
from .models import (
    AdmissionResult,
    ConversationCursor,
    ConversationPage,
    DeliveryStage,
    DepartureObservation,
    DepartureOutcome,
    DepartureSource,
    EventClass,
    EventKind,
    InboundEvent,
    JournalEvent,
    OutboxDelivery,
    RefreshRequest,
    SemanticConsumer,
    SettlementOutcome,
    VisibleMessage,
)
from .projection import ProjectedEvent, replacement_target, thread_root, visible_content
from .store import EventJournalStore, PrincipalStore
from .views import (
    AdmissionView,
    ApprovalView,
    ConversationReadView,
    DispatchView,
    HydrationView,
    OutboxView,
    ProjectionView,
    RelationView,
    ReplayView,
)

__all__ = [
    "AdmissionResult",
    "AdmissionView",
    "ApprovalView",
    "ConversationCursor",
    "ConversationPage",
    "ConversationReadView",
    "DeliveryStage",
    "DepartureObservation",
    "DepartureOutcome",
    "DepartureSource",
    "DispatchView",
    "EventClass",
    "EventJournalStore",
    "EventKind",
    "HydrationView",
    "InboundEvent",
    "JournalEvent",
    "MembershipFence",
    "MembershipView",
    "OutboxDelivery",
    "OutboxView",
    "PrincipalStore",
    "ProjectedEvent",
    "ProjectionView",
    "RefreshRequest",
    "RelationView",
    "ReplayView",
    "SemanticConsumer",
    "SettlementOutcome",
    "StoredApprovalCard",
    "VisibleMessage",
    "decode_thread_id",
    "delivery_transaction_id",
    "encode_thread_id",
    "replacement_target",
    "thread_root",
    "visible_content",
]
