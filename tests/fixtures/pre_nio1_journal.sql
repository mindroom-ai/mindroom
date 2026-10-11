-- Released journal schema immediately before the Nio 1.0 cutover.
-- Frozen upgrade fixture; do not regenerate from current production DDL.

CREATE TABLE IF NOT EXISTS journal_events (
        receipt_order INTEGER PRIMARY KEY AUTOINCREMENT,
        principal_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        room_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        sender TEXT NOT NULL,
        origin_server_ts BIGINT NOT NULL,
        source_json TEXT NOT NULL,
        semantic_consumer TEXT,
        membership_epoch BIGINT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending', 'settled')),
        UNIQUE (principal_id, event_id)
    );

CREATE TABLE IF NOT EXISTS visible_messages (
        principal_id TEXT NOT NULL,
        room_id TEXT NOT NULL,
        logical_event_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        sender TEXT NOT NULL,
        created_ts BIGINT NOT NULL,
        revision_event_id TEXT NOT NULL,
        revision_ts BIGINT NOT NULL,
        content_json TEXT,
        refresh_token BIGINT,
        membership_epoch BIGINT NOT NULL,
        PRIMARY KEY (principal_id, room_id, logical_event_id)
    );

CREATE TABLE IF NOT EXISTS unresolved_edits (
        principal_id TEXT NOT NULL,
        room_id TEXT NOT NULL,
        target_event_id TEXT NOT NULL,
        sender TEXT NOT NULL,
        edit_event_id TEXT NOT NULL,
        edit_ts BIGINT NOT NULL,
        content_json TEXT NOT NULL,
        PRIMARY KEY (principal_id, room_id, target_event_id, sender)
    );

CREATE TABLE IF NOT EXISTS redaction_tombstones (
        principal_id TEXT NOT NULL,
        room_id TEXT NOT NULL,
        redacted_event_id TEXT NOT NULL,
        PRIMARY KEY (principal_id, room_id, redacted_event_id)
    );

CREATE TABLE IF NOT EXISTS room_membership (
        principal_id TEXT NOT NULL,
        room_id TEXT NOT NULL,
        membership_epoch BIGINT NOT NULL,
        departure_fenced INTEGER NOT NULL DEFAULT 0,
        owed_departure_reports BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (principal_id, room_id)
    );

CREATE TABLE IF NOT EXISTS reported_departures (
        report_order INTEGER PRIMARY KEY AUTOINCREMENT,
        principal_id TEXT NOT NULL,
        observation_id TEXT NOT NULL,
        room_id TEXT NOT NULL,
        journal_order BIGINT NOT NULL,
        run_epoch BIGINT NOT NULL,
        run_closed INTEGER NOT NULL DEFAULT 0,
        UNIQUE (principal_id, observation_id)
    );

CREATE TABLE IF NOT EXISTS interactive_questions (
        principal_id TEXT NOT NULL,
        question_event_id TEXT NOT NULL,
        revision_event_id TEXT NOT NULL,
        room_id TEXT NOT NULL,
        question_json TEXT NOT NULL,
        consumed_by_source_event_id TEXT,
        PRIMARY KEY (principal_id, question_event_id, revision_event_id),
        UNIQUE (principal_id, consumed_by_source_event_id),
        FOREIGN KEY (principal_id, consumed_by_source_event_id)
            REFERENCES journal_events (principal_id, event_id),
        FOREIGN KEY (principal_id, room_id, question_event_id)
            REFERENCES visible_messages (principal_id, room_id, logical_event_id)
            ON DELETE CASCADE
    );

CREATE TABLE IF NOT EXISTS interactive_selections (
        principal_id TEXT NOT NULL,
        source_event_id TEXT NOT NULL,
        question_event_id TEXT NOT NULL,
        revision_event_id TEXT NOT NULL,
        selection_key TEXT NOT NULL,
        PRIMARY KEY (principal_id, source_event_id),
        FOREIGN KEY (principal_id, source_event_id)
            REFERENCES journal_events (principal_id, event_id),
        FOREIGN KEY (principal_id, question_event_id, revision_event_id)
            REFERENCES interactive_questions (principal_id, question_event_id, revision_event_id)
            ON DELETE CASCADE
    );

CREATE TABLE IF NOT EXISTS conversation_hydration (
        principal_id TEXT NOT NULL,
        room_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        membership_epoch BIGINT NOT NULL,
        complete INTEGER NOT NULL DEFAULT 0,
        attempted_policy_rank BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (principal_id, room_id, thread_id)
    );

CREATE TABLE IF NOT EXISTS room_history_recovery (
        principal_id TEXT NOT NULL,
        room_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('repairable', 'truncated', 'repaired')),
        revision BIGINT NOT NULL DEFAULT 0,
        attempted_policy_rank BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (principal_id, room_id)
    );

CREATE TABLE IF NOT EXISTS matrix_delivery_outbox (
        principal_id TEXT NOT NULL,
        delivery_id TEXT NOT NULL,
        stage TEXT NOT NULL CHECK (stage IN ('initial', 'final')),
        event_type TEXT NOT NULL,
        room_id TEXT NOT NULL,
        membership_epoch BIGINT NOT NULL,
        thread_id TEXT NOT NULL,
        transaction_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        result_json TEXT,
        edits_event_id TEXT,
        edit_target_pending INTEGER NOT NULL DEFAULT 0,
        attempted INTEGER NOT NULL DEFAULT 0,
        retired INTEGER NOT NULL DEFAULT 0,
        permanent_failure_reason TEXT,
        sending_device_id TEXT,
        acknowledged_event_id TEXT,
        created_at_ns BIGINT NOT NULL,
        PRIMARY KEY (principal_id, delivery_id, stage)
    );

CREATE TABLE IF NOT EXISTS approval_cards (
        principal_id TEXT NOT NULL,
        delivery_id TEXT NOT NULL,
        continuation_id TEXT NOT NULL,
        continuation_generation BIGINT NOT NULL,
        tool_call_id TEXT NOT NULL,
        membership_epoch BIGINT NOT NULL,
        PRIMARY KEY (principal_id, delivery_id)
    );

CREATE TABLE IF NOT EXISTS background_approval_calls (
        principal_id TEXT NOT NULL,
        delivery_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        call_id TEXT NOT NULL,
        expires_at_ns BIGINT NOT NULL,
        decision TEXT CHECK (decision IS NULL OR decision IN ('approved', 'denied', 'expired')),
        reason TEXT,
        PRIMARY KEY (principal_id, delivery_id),
        UNIQUE (principal_id, run_id, call_id)
    );

CREATE TABLE IF NOT EXISTS approval_action_tombstones (
        principal_id TEXT NOT NULL,
        room_id TEXT NOT NULL,
        card_event_id TEXT NOT NULL,
        PRIMARY KEY (principal_id, card_event_id)
    );

CREATE TABLE IF NOT EXISTS approval_continuations (
        principal_id TEXT NOT NULL,
        approval_id TEXT NOT NULL UNIQUE,
        entity_name TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('waiting', 'ready', 'claimed', 'failing')),
        generation BIGINT NOT NULL DEFAULT 0,
        runtime_generation TEXT,
        failure_reason TEXT,
        context_json TEXT NOT NULL,
        created_at_ns BIGINT NOT NULL,
        PRIMARY KEY (principal_id, approval_id)
    );

CREATE TABLE IF NOT EXISTS approval_continuation_sources (
        principal_id TEXT NOT NULL,
        approval_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        source_ordinal BIGINT NOT NULL,
        PRIMARY KEY (principal_id, approval_id, event_id),
        UNIQUE (principal_id, event_id),
        UNIQUE (principal_id, approval_id, source_ordinal),
        FOREIGN KEY (principal_id, approval_id)
            REFERENCES approval_continuations (principal_id, approval_id) ON DELETE CASCADE,
        FOREIGN KEY (principal_id, event_id)
            REFERENCES journal_events (principal_id, event_id)
    );

CREATE TABLE IF NOT EXISTS approval_continuation_calls (
        principal_id TEXT NOT NULL,
        approval_id TEXT NOT NULL,
        generation BIGINT NOT NULL,
        tool_call_id TEXT NOT NULL,
        call_ordinal BIGINT NOT NULL,
        tool_name TEXT NOT NULL,
        invoking_agent TEXT NOT NULL,
        expires_at_ns BIGINT NOT NULL,
        decision TEXT CHECK (decision IS NULL OR decision IN ('approved', 'denied', 'expired')),
        reason TEXT,
        human_approval_required BOOLEAN,
        PRIMARY KEY (principal_id, approval_id, generation, tool_call_id),
        UNIQUE (principal_id, approval_id, generation, call_ordinal),
        FOREIGN KEY (principal_id, approval_id)
            REFERENCES approval_continuations (principal_id, approval_id) ON DELETE CASCADE
    );

CREATE TABLE IF NOT EXISTS turn_records (
        agent_name TEXT NOT NULL,
        index_event_id TEXT NOT NULL,
        anchor_event_id TEXT NOT NULL,
        record_json TEXT NOT NULL,
        PRIMARY KEY (agent_name, index_event_id)
    );

CREATE TABLE IF NOT EXISTS journal_identity (
        singleton BOOLEAN NOT NULL PRIMARY KEY,
        generation TEXT NOT NULL
    );

CREATE INDEX IF NOT EXISTS reported_departures_open
    ON reported_departures (principal_id, room_id, report_order)
    WHERE run_closed = 0;

CREATE INDEX IF NOT EXISTS interactive_selections_revision
    ON interactive_selections (principal_id, question_event_id, revision_event_id);

CREATE INDEX IF NOT EXISTS journal_events_pending
    ON journal_events (principal_id, receipt_order)
    WHERE state = 'pending';

    CREATE INDEX IF NOT EXISTS journal_events_pending_thread
    ON journal_events (principal_id, room_id, thread_id, origin_server_ts)
    WHERE state = 'pending';

CREATE INDEX IF NOT EXISTS visible_messages_page
    ON visible_messages (principal_id, room_id, thread_id, created_ts, logical_event_id);

CREATE INDEX IF NOT EXISTS visible_messages_revision
    ON visible_messages (principal_id, room_id, revision_event_id);

CREATE INDEX IF NOT EXISTS visible_messages_refresh
    ON visible_messages (principal_id, room_id, thread_id)
    WHERE refresh_token IS NOT NULL;

    CREATE INDEX IF NOT EXISTS matrix_delivery_outbox_unacknowledged_scan
    ON matrix_delivery_outbox (
        principal_id, event_type, created_at_ns, delivery_id, stage
    )
    WHERE acknowledged_event_id IS NULL AND retired = 0;

CREATE INDEX IF NOT EXISTS matrix_delivery_outbox_room_scan
    ON matrix_delivery_outbox (
        principal_id, room_id, stage, created_at_ns, delivery_id
    );

CREATE INDEX IF NOT EXISTS approval_cards_continuation
    ON approval_cards (continuation_id, continuation_generation, tool_call_id);

CREATE INDEX IF NOT EXISTS approval_continuations_owner_scan
    ON approval_continuations (entity_name, approval_id);
