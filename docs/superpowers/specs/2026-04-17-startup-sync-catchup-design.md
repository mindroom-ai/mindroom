# Startup Sync Catch-Up And Exact-Once Reply Design

## Problem

MindRoom should answer user messages that arrive while a bot is down or still starting.
The current sync token restore helps replay missed messages after restart.
It does not fully solve the startup gap because `next_batch` is persisted before all event callback tasks from that sync response finish.
It also does not fully solve exact-once delivery because a visible reply can be sent before the handled-turn ledger is finalized.

## Goals

- Reply to messages that arrived while the bot was offline or still starting.
- Avoid dropping events when a restart happens after a sync response arrives but before that response's callbacks finish.
- Preserve exact-once reply behavior across restart and replay for normal inbound message handling.

## Non-Goals

- Rebuild Matrix ingress around a new durable inbox.
- Change edit regeneration semantics beyond what exact-once replay requires.
- Change room setup, stale stream cleanup, or startup prewarm behavior except where they interact with sync checkpointing.

## Current Failure Modes

1. The bot restores `client.next_batch` before sync starts, but the sync loop itself starts later in orchestrator startup.
2. When a `SyncResponse` arrives, event callbacks are scheduled as background tasks.
3. The bot persists `next_batch` immediately after caching the timeline instead of after those callback tasks finish.
4. A process death in that window can lose inbound events because restart resumes after the already-persisted token.
5. A second window exists after a visible reply is sent but before the handled-turn ledger is marked terminal.
6. A replay after that crash can send the same reply a second time because the outbound Matrix send is not idempotent across restarts.

## Proposed Design

### 1. Per-Sync Checkpoint Barrier

Each sync response should create one checkpoint barrier for the callback work derived from that response.
Background event callbacks created from the sync response should register with that barrier.
The bot should only persist `next_batch` for that response after the barrier reaches zero unfinished callback tasks.
If the process dies before the barrier completes, the token stays behind and the same sync batch replays on restart.

### 2. Response-Scope Outbound Idempotency

The first visible send for one handled turn should use a deterministic Matrix transaction id derived from persisted turn metadata.
The runtime should persist an in-progress outbound marker before the first visible send is attempted.
If the process crashes after the server accepted the send but before the handled-turn ledger is finalized, replay should reuse the same transaction id and recover the same event id instead of creating a second visible reply.
Terminal handled-turn persistence should still remain the source of truth for duplicate suppression after the response completes.

### 3. Turn Store Extensions

The turn store should gain one durable pending-response record that exists before the visible send becomes final.
That record should include at least the source event ids, stable outbound transaction id, response target identity, and terminal completion status.
When a turn reaches its terminal outcome, the pending record can be folded into the existing handled-turn ledger or removed after the terminal record is safely written.

### 4. Delivery Gateway Support

The Matrix send layer should accept an optional caller-owned transaction id instead of always generating a fresh UUID.
Normal non-replayed sends can keep the current default behavior.
Exact-once response sends should pass the persisted transaction id from the turn store.

## Data Flow

1. Startup restores the last durable sync token.
2. The first sync response arrives and creates a checkpoint barrier.
3. Event callbacks spawned from that response register with the barrier.
4. Each callback runs normal dispatch and either skips, rejects, or generates a response.
5. If a response path needs a first visible send, it first persists the pending outbound record and stable transaction id.
6. The Matrix send uses that transaction id.
7. When the callback finishes, it releases the sync barrier.
8. When all callbacks for the sync response finish, the bot persists `next_batch`.
9. On crash before step 8, the response batch replays.
10. On crash after step 6 but before terminal handled-turn persistence, replay reuses the same outbound transaction id and does not duplicate the visible reply.

## Error Handling

- If pending outbound persistence fails, fail closed and do not attempt the visible send.
- If sync barrier bookkeeping fails, fail closed and do not advance the sync token.
- Shutdown should drain or cancel in-flight sync-barrier work before persisting the final token.
- Existing handled-turn and edit-regeneration behavior should continue to suppress plain redeliveries after a terminal record exists.

## Testing

Add or extend tests for these cases.

1. Startup replay answers a message that arrived while the bot was offline.
2. Crash after `SyncResponse` delivery but before callback completion replays the same inbound event after restart.
3. Crash after visible send but before handled-turn finalization does not create a duplicate visible reply on replay.
4. Regular duplicate redelivery still skips once the terminal handled-turn record exists.
5. Edit redelivery behavior remains correct.

## Recommended Implementation Order

1. Add sync-response checkpoint barrier plumbing and tests.
2. Add optional Matrix transaction id support in the send layer.
3. Add pending outbound turn persistence in the turn store.
4. Thread stable outbound transaction ids through the first visible response send paths.
5. Add crash-replay tests covering both inbound and outbound windows.
