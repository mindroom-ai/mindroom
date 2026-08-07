"""Durable ownership of inbound Matrix events, visible content, and delivery.

Three facts live here, each with exactly one owner:

- what MindRoom accepted from Matrix and still owes work for (the journal),
- what a conversation currently looks like (the projection),
- what MindRoom intends to send and whether it landed (the outbox).
"""

from .approvals import RecordedApprovalDecision, StoredApprovalCard
from .history_debt import HistoryDebtOutcome, RoomHistoryDebt
from .identity import decode_thread_id, delivery_transaction_id, encode_thread_id
from .membership import MembershipFence, MembershipView
from .models import (
    TURN_BACKED_KINDS,
    AdmissionResult,
    ConversationCursor,
    ConversationPage,
    DeliveryAcknowledgement,
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
    TerminalTurnWrite,
    VisibleMessage,
)
from .projection import ProjectedEvent, replacement_target, thread_root, visible_content
from .store import EventJournalStore, PrincipalStore, TurnRecordStore
from .views import (
    AdmissionView,
    ApprovalView,
    ConversationReadView,
    DispatchView,
    HistoryDebtRecordView,
    HydrationView,
    OutboxView,
    PendingTurnView,
    PointLookupView,
    ProjectionView,
    RelationView,
    ReplayView,
)

__all__ = [
    "TURN_BACKED_KINDS",
    "AdmissionResult",
    "AdmissionView",
    "ApprovalView",
    "ConversationCursor",
    "ConversationPage",
    "ConversationReadView",
    "DeliveryAcknowledgement",
    "DeliveryStage",
    "DepartureObservation",
    "DepartureOutcome",
    "DepartureSource",
    "DispatchView",
    "EventClass",
    "EventJournalStore",
    "EventKind",
    "HistoryDebtOutcome",
    "HistoryDebtRecordView",
    "HydrationView",
    "InboundEvent",
    "JournalEvent",
    "MembershipFence",
    "MembershipView",
    "OutboxDelivery",
    "OutboxView",
    "PendingTurnView",
    "PointLookupView",
    "PrincipalStore",
    "ProjectedEvent",
    "ProjectionView",
    "RecordedApprovalDecision",
    "RefreshRequest",
    "RelationView",
    "ReplayView",
    "RoomHistoryDebt",
    "SemanticConsumer",
    "SettlementOutcome",
    "StoredApprovalCard",
    "TerminalTurnWrite",
    "TurnRecordStore",
    "VisibleMessage",
    "decode_thread_id",
    "delivery_transaction_id",
    "encode_thread_id",
    "replacement_target",
    "thread_root",
    "visible_content",
]
