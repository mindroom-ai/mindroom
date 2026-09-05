"""Durable ownership of inbound Matrix events, visible content, and delivery.

Three facts live here, each with exactly one owner:

- what MindRoom accepted from Matrix and still owes work for (the journal),
- what a conversation currently looks like (the projection),
- what MindRoom intends to send and whether it landed (the outbox).
"""

from mindroom.history_recovery import (
    HistoryRecoveryOutcome,
    HistoryRecoveryState,
    RoomHistoryRecovery,
)
from mindroom.interactive_models import InteractiveSelection

from .approval_card_state import ApprovalCardReservation, RecordedApprovalDecision
from .approval_continuations import (
    ApprovalCall,
    ApprovalContinuation,
    ApprovalDecision,
    ApprovalMemoryTurn,
)
from .approvals import (
    StoredApprovalCard,
    UnreadableApprovalCard,
)
from .background_approvals import BackgroundApprovalDecision
from .identity import decode_thread_id, delivery_transaction_id, encode_thread_id
from .journal import validate_ingestion_batch_admission
from .models import (
    TURN_BACKED_KINDS,
    AdmissionFacts,
    AdmissionResult,
    ConversationCursor,
    ConversationPage,
    DeliveryAcknowledgement,
    DeliveryProjectionPendingError,
    DeliveryStage,
    DepartureObservation,
    DepartureOutcome,
    DepartureSource,
    EventClass,
    EventKind,
    HydrationCoverage,
    HydrationPolicy,
    InboundEvent,
    IngestionBatchAdmission,
    IngestionBatchIntegrityError,
    IngestionBatchSequenceError,
    IngestionBatchValidationError,
    IngestionConsumer,
    IngestionConsumerBindingError,
    IngestionRecordDisposition,
    JournalEvent,
    MatrixDelivery,
    PendingPage,
    RefreshRequest,
    RoomMembershipPosition,
    SemanticConsumer,
    TerminalTurnWrite,
    UnreadableMatrixDelivery,
    VisibleMessage,
)
from .outbox import matrix_delivery_payload
from .projection import ProjectedEvent, replacement_target, thread_root, visible_content
from .store import EventJournalStore, PrincipalStore, TurnRecordStore
from .views import (
    AdmissionView,
    ApprovalDeliveryView,
    ConversationReadView,
    DispatchView,
    HistoryRecoveryRecordView,
    HydrationView,
    MatrixDeliveryView,
    PendingTurnView,
    RelationView,
    ReplayView,
)

__all__ = [
    "TURN_BACKED_KINDS",
    "AdmissionFacts",
    "AdmissionResult",
    "AdmissionView",
    "ApprovalCall",
    "ApprovalCardReservation",
    "ApprovalContinuation",
    "ApprovalDecision",
    "ApprovalDeliveryView",
    "ApprovalMemoryTurn",
    "BackgroundApprovalDecision",
    "ConversationCursor",
    "ConversationPage",
    "ConversationReadView",
    "DeliveryAcknowledgement",
    "DeliveryProjectionPendingError",
    "DeliveryStage",
    "DepartureObservation",
    "DepartureOutcome",
    "DepartureSource",
    "DispatchView",
    "EventClass",
    "EventJournalStore",
    "EventKind",
    "HistoryRecoveryOutcome",
    "HistoryRecoveryRecordView",
    "HistoryRecoveryState",
    "HydrationCoverage",
    "HydrationPolicy",
    "HydrationView",
    "InboundEvent",
    "IngestionBatchAdmission",
    "IngestionBatchIntegrityError",
    "IngestionBatchSequenceError",
    "IngestionBatchValidationError",
    "IngestionConsumer",
    "IngestionConsumerBindingError",
    "IngestionRecordDisposition",
    "InteractiveSelection",
    "JournalEvent",
    "MatrixDelivery",
    "MatrixDeliveryView",
    "PendingPage",
    "PendingTurnView",
    "PrincipalStore",
    "ProjectedEvent",
    "RecordedApprovalDecision",
    "RefreshRequest",
    "RelationView",
    "ReplayView",
    "RoomHistoryRecovery",
    "RoomMembershipPosition",
    "SemanticConsumer",
    "StoredApprovalCard",
    "TerminalTurnWrite",
    "TurnRecordStore",
    "UnreadableApprovalCard",
    "UnreadableMatrixDelivery",
    "VisibleMessage",
    "decode_thread_id",
    "delivery_transaction_id",
    "encode_thread_id",
    "matrix_delivery_payload",
    "replacement_target",
    "thread_root",
    "validate_ingestion_batch_admission",
    "visible_content",
]
