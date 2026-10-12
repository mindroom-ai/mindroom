# Matrix event-journal security and plaintext lifecycle

MindRoom decrypts Matrix conversations to answer them, and answering them takes more than one process lifetime, so some of that plaintext has to be written down.
This page records which plaintext the event journal (`src/mindroom/event_journal/`) keeps durably, which principal owns it, and what removes it.
Where the journal is stored is documented in [Event Journal configuration](../deployment/storage.md#event-journal).

## Principal binding

One database holds every bot in a runtime, and its content rows are owned by a principal: the agent name joined to the full Matrix user ID.
The Matrix account alone is not a sufficient owner, because two agents can be configured onto one account and neither should read the other's conversations.
Room membership is not the boundary either, because two bots joined to the same room can hold different encryption keys and therefore decrypt different subsets of it.

Approval coordination is the one place that deliberately crosses principals, because the router owns approval cards while the responding entity owns the paused continuation.
It does so only by validating persisted card-to-continuation relationships inside the runtime, and it is not a way for ordinary conversation readers to select another principal.
Turn records are scoped to the agent name alone, so a re-login under a new Matrix ID does not answer every outstanding message again, and they still hold conversation-derived text.

## Where durable plaintext lives

- `journal_events.source_json` holds the full decrypted event only while that event still owes semantic work.
  Settlement blanks it and keeps the row as proof the event already had its one turn, and context-only events are admitted settled without a payload.
  Unreadable history keeps only a settled envelope identity without its ciphertext, so a later decryption can add conversation context but never make the event actionable.
- `visible_messages.content_json` holds the current visible body of each logical message and keeps no edit history.
  Redacting a message deletes its row and its held edits, and redacting the revision on screen clears the body in the same transaction, after which no read returns that message until a refetch installs the server's replacement.
  The truncated preview carried by a message too large for one Matrix event is never stored as its body.
  A departure deletes the departing principal's projection for that room, including its held edits and redaction tombstones, while other bots in the room keep theirs.
- `unresolved_edits.content_json` holds an edit whose target has not arrived yet, and it is deleted the moment the target lands or is redacted.
  Redacting the held edit itself clears its payload but keeps its identity and ordering until the target arrives.
- `interactive_questions.question_json` holds question text and options for every revision, including superseded revisions needed for source replay and consumption proof.
  Redacting a revision deletes its row, and deleting the logical message deletes all of its revisions.
- `turn_records.record_json` holds turn identity, outcome, and regeneration content.
  Its prepared voice-source checkpoint (prepared body, Matrix content, batching scope, and echo thread) lets a retry skip transcription, is never copied into agent run metadata, and is removed on turn completion or source redaction, including redaction through a source's discovery alias.
  Records holding a conversation's redaction tombstone are kept indefinitely, because history cleanup derives from them; other records follow age and count retention once no unsettled source or unfinished turn fact protects them.
- `matrix_delivery_outbox.payload_json` holds each response or tool-approval event frozen before it is sent, `result_json` holds local completion facts, which can include final response text and interactive metadata, and `reply_row_json` can hold the reply text that delivery writes.
  Rows an earlier release wrote can also hold source prompts inside a serialized prepared edit turn record.
- `approval_continuations.context_json` can hold the original `request_body`, `memory_prompt`, and `memory_thread_history[*].body` needed to resume an approved call, and `approval_continuation_calls.reason` can hold the text of a human's denial reply; both are deleted with the continuation after terminal delivery or cleanup.
- `reply_messages.presentation_json` and `possibly_shown_json` hold what each AI reply shows and what its latest progress edit may have shown: visible text, tool trace, and team presentation state.
  `reply_messages.owed_write_json` can hold reply text a Matrix write still owes, and `reply_spans.rollback_json` holds the presentation a claimed span restores if it rolls back.
  `reply_tool_calls.entry_json` records every tool call before it runs, with its redacted argument preview and, once it returns, its redacted result preview, whether or not the reply shows tool calls.
  These traces also carry internal tool-call and member-scope identities that Matrix message metadata omits, and a finished reply, its spans, and its tool calls are deleted with handled-turn retention.
- Approval terminal edits are frozen outbox stages stripped of full-argument transport fields and pending duration choices, keeping the compact argument preview and grant acknowledgement.
  `approval_cards`, `approval_action_tombstones`, `approval_grant_cards`, and `approval_grant_locks` hold identities, scopes, and references rather than conversation text.
- `approval_grants.resolution_json` holds the terminal card body and redacted argument preview needed to publish a later revocation edit.
  Grant maintenance clears it when an unrevoked grant expires, when either owning principal leaves the room, or when the revocation edit is acknowledged, and acknowledged revocation outbox rows are removed once it is cleared.
  A revocation accepted before expiry keeps its material until acknowledged, and compact grant identity remains so a duplicate action cannot recreate a window.
- `scheduled_call_approvals.arguments_json` holds each scheduled call's unredacted arguments beside its task, room, thread, requester, and approver, while Matrix task state and the trigger carry only the task ID.
  Grant maintenance deletes the row 30 days after its send time or withdrawal, once its card has retired and any receipt is settled.
- `background_approval_calls.reason` can hold the text of a human's denial reply to a scheduled-call or script approval card, and it is deleted with its call once the shared card retires.

There is no plaintext table keyed by media URL, and no runtime-wide process-local plaintext cache shared across bots.

## Known gaps

- Principals are separated by principal-bound queries only: neither SQLite nor PostgreSQL gives a principal its own database, schema, or connection.
- Outbox copies outlive the conversation: acknowledgement does not clear `payload_json` or `result_json`, projection redaction and membership cleanup do not remove them, and ordinary acknowledged or retired rows have no TTL or payload pruning.
  Only approval cleanup and withdrawal of a superseded, unattempted `INITIAL` delivery delete rows.
- A conversation read stops at a stored-content and an estimated decoded-size budget, because anyone who can post can make every message in a thread name one large file.
  A thread past that budget loses what needs its complete history, as one past the row limit does: untagged continuation in rooms with several responders, thread summaries, and mid-turn judgment.
- For messages still waiting on their file, that budget bounds what a strict read stores, not what it downloads: each waiting message that names an unreadable file still costs one download.
