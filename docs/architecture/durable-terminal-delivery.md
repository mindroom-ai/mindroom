# Durable terminal delivery

MindRoom normally answers by replacing a visible placeholder with the completed response.
Matrix can temporarily reject that final edit during limited-sync recovery or a transport outage.
Durable terminal delivery remembers only that final edit so it can be retried after recovery or restart.

## Checkpoint

The existing `TurnRecord` remains the single durable authority for a response.
Before the first Matrix attempt, the gateway stores a `TerminalEditCheckpoint` on that record.
The checkpoint contains only a deterministic transaction ID, the exact prepared Matrix content, the response correlation ID, and the source-redaction snapshot.
The record's existing `response_event_id` identifies the placeholder to replace.

The prepared content is frozen after formatting and large-message handling.
Every retry therefore sends the same content with the same Matrix transaction ID.
A delivered or durably deferred edit completes the response turn, so model execution is not repeated.

## Recovery

One coordinator retries pending checkpoints sequentially when Matrix becomes ready and on a periodic wakeup.
Successful delivery clears the checkpoint.
A restart reloads pending checkpoints from the existing handled-turn ledger.
There is no separate terminal-delivery database.

Source redaction prevents a pending answer from being published and removes an already visible answer.
Target redaction clears matching delivery debt.
Stale-stream cleanup checks terminal ownership before writing an interruption note over a response.

## Scope

Only completed edits of placeholders use this durable path.
New sends, edit regeneration, cancellation notes, hooks, interactive buttons, memory updates, and thread summaries keep their existing behavior.
Those effects are intentionally best-effort because retrying them would require additional state and idempotency rules.
