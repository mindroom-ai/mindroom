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

Unreadable historical ciphertext keeps only a settled envelope identity, without its encrypted payload.
A later decrypted observation may populate conversation context but cannot make that identity actionable.
Unreadable live and recovered ciphertext remains Nio's recovery responsibility and never owns application journal work; runtime diagnostics issue authorized, best-effort key requests and warnings separately.

`visible_messages.content_json` holds the current visible body of one logical message and is the general long-lived conversation-body projection.

The projection keeps no edit history, so an edit overwrites the body and the previous text is gone.

`interactive_questions.question_json` duplicates the active question text and options while its visible-message row survives.
It is deleted when the current question revision is cleared, and its foreign key also cascades when the visible message is deleted.

`turn_records.record_json` retains durable turn identity, outcome, and regeneration content.

`unresolved_edits.content_json` holds an edit whose target has not arrived yet, and it is deleted the moment the target lands or is redacted.

`matrix_delivery_outbox.payload_json` holds each ordinary response or tool-approval event frozen before it is sent.

`approval_cards` retains only the durable delivery reference, exact continuation and tool-call identity, and membership epoch while a card is actionable.

`approval_continuations.context_json` may contain the original `request_body`, `memory_prompt`, and `memory_thread_history[*].body` required to resume an approved call.
It also retains the acknowledged `response_text`, structured team `response_presentation_state`, and `response_tool_trace` needed to preserve transcript order after continuation.
The durable tool trace contains redacted argument and result previews plus internal tool-call and member-scope identities; those internal identities are omitted from Matrix message metadata.
A team continuation without the versioned structured presentation is rejected instead of reconstructed from rendered Markdown, because reconstruction could bind a tool to the wrong member or transcript position; the requester must start a new turn.
`finish()` and `discard_unavailable()` delete the continuation after terminal delivery or cleanup, and foreign-key cascades remove its sources and calls.

The decision remains in the exact-call continuation ledger, the terminal edit is another frozen outbox stage, and `approval_action_tombstones` retains the acknowledged card event ID after retirement so duplicate clicks remain consumed.

The [automatic Nio 1.0 migration](../deployment/nio-upgrade.md) settles old pending events and recreates execution, approval, and membership state atomically while preserving journal identity and message history; it never converts old unfinished work into new requests.

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

Unattempted non-card outbox rows for the room are retired, because they answer a conversation the bot has left and must never be sent after rejoin.

An unattempted approval card is instead deleted with its provably invisible delivery stages, while an attempted visible card follows the approval cleanup policy below.

The retired row remains as the delivery identity tombstone, so a source-less multi-stage turn cannot enqueue `INITIAL` before departure and let `FINAL` adopt the later membership.

An attempted row is kept instead because its outcome is unknown and its immutable payload, transaction, and sending-device facts are required for exact recovery.

Same-device recovery reuses the frozen transaction, while changed-device recovery first reconciles room history and then either replays an ordinary response or retains actionable approval debt.

When a visible approval card deliberately survives a router departure, its card row and both delivery stages atomically transfer to the successor membership so the already-decided terminal edit remains recoverable.

The actionable root card is retained after an inconclusive changed-device scan, while its immutable terminal edit may be replayed because it cannot create another approval action.

Old-membership recovery never sends and retires the row only after exact reconciliation proves its physical event absent.

Every outbox row freezes the membership epoch that authorized it, and acknowledgement projects its Matrix event only while that exact membership remains current.

Nio owns durable recognition of local membership commands and their later sync echoes.
MindRoom applies the producer's ordered membership transitions once per admitted batch and retains application tenure fencing without a second echo protocol.
A departure advances that tenure and invalidates work authorized by the ended membership.
A rejoin retains the advanced tenure, so a late acknowledgement cannot project an older delivery into the new conversation.
Response shutdown can prove intentional termination from the exact retained sources: each must be settled and belong to an older membership epoch than its own room's current epoch.
That proof uses one journal recovery snapshot and needs no final delivery; missing sources, current-epoch settlement, and sources spanning ended and current memberships do not qualify.

## Restart

`matrix_sync_consumers` binds each principal's durable consumer generation to one nio stream and records its next batch sequence.
The owned session reuses that consumer identity on restart and rejects a mismatched stream binding.
Soft-logout renewal requests the existing Matrix device and preserves its keys, stream, producer positions, and delivery identity.
Hard logout, missing device storage, or changed account/device identity stops startup; automatic device replacement is unsupported.
Initial login persists its exact credentials after the local store exists and before journal binding, so interrupted startup can reopen the same device.
A batch committed before a crash is recognized on redelivery, while its pending semantic work remains recoverable from the journal.
Nio owns the receive cursor; MindRoom's continuity file contains only pending join/decrypt fences.
The one-time upgrade resets pre-durable membership tenures and converts v2/v3 continuity files to v4 while preserving pending join/decrypt fences.

## Storage and connections

SQLite stores the journal at `mindroom_data/tracking/event_journal.db`, and PostgreSQL is selected by configuring a database URL instead.

That URL carries a password, so it is excluded from the backend's dataclass representation, which would otherwise reach logs and tracebacks without anyone choosing to print it.

SQL structure is authored only from fixed internal constants and controlled fragments, including fixed column selections, cursor clauses, placeholder counts, and PRAGMA values.

Both rewrites are plain string substitution, so both refuse a statement that places their marker adjacent to a string literal rather than trusting that no statement does.

Caller-provided values are bound by the driver in every case and are never formatted into SQL.

### Auxiliary Matrix operations

Dashboard room reads, schedule state operations, and avatar updates use saved access tokens on HTTP-only clients with encryption disabled and no store path.
They never provision an account, renew credentials, or open the owned crypto store.
Dashboard departures use the running bot's serialized durable membership gateway.
CLI thread exports require the running API and borrow the same clients and principal-bound readers used by workspace exports.
The CLI sends its config and storage paths so the API can reject a request aimed at a different installation before writing files.
Manual exports hold existing runtime replacement admission; automatic workspace exports are cancelled and drained at every replacement boundary.
Both paths drain their hydration tasks before borrowed clients or journals close.
The workspace runner queues a fresh full pass and waits for replacement admission to reopen before borrowing current owners.
