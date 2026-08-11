# Matrix event-journal security and plaintext lifecycle

MindRoom decrypts Matrix conversations to answer them, and answering them takes more than one process lifetime, so some of that plaintext has to be written down.

This document says exactly which plaintext is durable, which principal owns it, and what removes it.

The storage described here is `src/mindroom/event_journal/`: the journal of admitted events, the visible-message projection built from them, and the delivery outbox that answers them.

## Principal binding

One database holds every bot in a runtime, and every content row carries a `principal_id` column that separates them.

The principal is the agent name joined to the full Matrix user ID, which is narrower than the Matrix account alone.

Two agents can be configured onto one Matrix account, and neither should read the other's conversations, so the account by itself is not a sufficient owner.

Room membership is not the authorization boundary either, because two bots joined to the same room can hold different encryption keys and therefore decrypt different subsets of it.

`EventJournalStore.principal()` hands out a `PrincipalStore` with the principal bound into the object.

No operational method on that view takes a `principal_id` argument, so reading or settling another bot's rows is not something a caller can express rather than something it is trusted not to do.

Turn records are the one deliberate exception, scoped to the agent name alone.

A turn record is the proof that a message was already answered, which stays true across a re-login, and scoping it per principal would make a bot that reauthenticates under a new Matrix ID answer every outstanding message a second time.

That record still holds conversation-derived text in `record_json`, so it is content and not merely bookkeeping.

Both backends run the same schema statements.

PostgreSQL is not partitioned into per-principal namespaces: separation is the same `principal_id` predicate SQLite uses, applied in every statement, and a query that omitted it would cross principals rather than fail.

## Where durable plaintext lives

`journal_events.source_json` holds the full decrypted Matrix event, and only while that event still owes semantic work.

Settlement overwrites it with the empty string in the same statement that marks the work terminal, because the row's remaining job is to prove the event already produced its one turn.

The row is kept and the payload is dropped, which is the smallest thing that survives a restart without retaining every message the bot has ever seen.

A context-only event never carries a payload at all: it is admitted already settled, so the field it would have used is written empty from the start.

`visible_messages.content_json` holds the current visible body of one logical message, and it is the only long-lived plaintext store.

The projection keeps no edit history, so an edit overwrites the body and the previous text is gone.

`unresolved_edits.content_json` holds an edit whose target has not arrived yet, and it is deleted the moment the target lands or is redacted.

`response_outbox.payload_json` holds an answer frozen before it was sent, and it survives until the delivery is acknowledged.

`approval_cards.card_json` and `approval_cards.resolution_json` hold one tool-approval prompt and the decision taken on it.

## Sidecar previews are never stored as bodies

A message too large for a single Matrix event carries a truncated preview in its content and its real text in an attached file.

The projection refuses to store that shape: it writes no body and a refresh token instead, which is the same row shape a redaction leaves behind.

Storing the preview would hand every reader a body that looks complete and is not, and no reader could tell the difference by inspecting it.

There is no plaintext table keyed by media URL, and no runtime-wide process-local plaintext cache shared across bots.

Resolved content carries no sidecar metadata of its own, so storing the resolution is what clears the debt, and nothing has to remember to clear it separately.

## Edits

An edit is applied only when its sender matches the sender already recorded on the visible row, compared through that row's inline `sender` column.

An edit from anyone else changes nothing, so a foreign replacement cannot rewrite another account's message.

Held edits are keyed by target and sender together for the same reason.

Without the sender in the key, anyone in the room could send an edit for a message that has not arrived yet and evict the author's real edit before it could apply.

Revisions are ordered by `(origin_server_ts, event_id)` rather than by timestamp alone, because two edits can share a millisecond and clients disagree about clocks.

The tie-break makes every replica of the projection converge on the same visible revision, whether it was built from live events or reconstructed from the server.

## Redaction

A redaction records its tombstone before it projects anything.

That order is what stops an original or an edit arriving later — a real ordering on a server that backfills — from resurrecting content the sender deleted.

Redacting a logical message deletes its visible row and every edit held against it.

Redacting the revision currently on screen instead clears `content_json` in the same transaction and sets a refresh token, so the body stops being readable before the server-authoritative replacement is known.

A conversation read reports such a message as owing a refetch and omits it from the returned messages, and there is no read that returns it, whether or not the caller is willing to wait.

A point refetch is refused if the revision it chose has since been tombstoned, which the refresh token alone cannot cover: redacting a revision that is not the one on screen moves no token but does record a tombstone.

A refetch is also refused if the content it returns still holds a sidecar preview, because installing it would satisfy the debt with the very text the debt was raised about.

Membership fencing deliberately does not sweep up pending redactions along with unanswerable turns, because a redaction still owes real cleanup in durable turn and session state, and settling it silently would let redacted content survive in later context.

## Membership

Every projected row carries the `membership_epoch` it was written under.

Rejoining a room can expose a different slice of history than the bot saw before, so state built under the old membership is dropped rather than merged with the new view.

A departure advances the epoch and deletes that room's conversation hydration, visible messages, unresolved edits, redaction tombstones, and history-recovery obligation.

It deletes them for the departing principal only, so another bot still joined to the same room keeps everything it holds.

Journal rows survive the fence on purpose, because they are the proof that an event already had its turn, and their payloads were released at settlement.

Turn-backed rows still pending are settled as intentionally ignored, since their answers would be refused by the epoch check forever and leaving them pending would replay the model run on every recovery pass.

Unattempted outbox rows for the room are deleted, because they answer a conversation the bot has left and nothing outside the process has seen them.

An attempted row is kept instead: its outcome is unknown, and only presenting the same frozen transaction again can converge on one visible answer rather than posting a second.

One departure reaches the bot twice, locally and again in the sync response that reports it, and both must fence exactly once.

Fencing twice is not merely wasteful — if the bot rejoined in between, the second fence deletes the conversation it has already hydrated under the new membership, along with any answer queued for it.

The bookkeeping that decides which observation is a repeat is durable and counted rather than a flag, because leave/rejoin/leave owes two reports and it has to survive a restart between a local departure and its report.

## Restart

A Matrix sync token is only meaningful next to the store that consumed the events it already covers.

`journal_identity` holds a single generation, written once when the database is first opened and never rewritten.

A saved sync checkpoint records that generation, and a checkpoint naming a different one is refused, so a bot resuming against a database that no longer exists starts cold instead of skipping every event in between.

Only startup refuses a checkpoint this way.

A room departure deliberately does not discard the global position, because that room is already fenced by its own membership epoch and dropping the checkpoint would resync every other room with it.

## Storage and connections

SQLite stores the journal at `mindroom_data/tracking/event_journal.db`, and PostgreSQL is selected by configuring a database URL instead.

That URL carries a password, so it is excluded from the backend's dataclass representation, which would otherwise reach logs and tracebacks without anyone choosing to print it.

Every SQL statement originates in a module constant, and the only transformations applied to it are the parameter marker and the byte-order pin.

Both rewrites are plain string substitution, so both refuse a statement that places their marker adjacent to a string literal rather than trusting that no statement does.

Values are bound by the driver in every case and are never formatted into SQL.
