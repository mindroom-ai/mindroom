# Matrix Event Pipeline Simplification Plan

> **Archived. Historical record, not current guidance.**
>
> This is the working plan and review log for the event-journal cutover, kept
> for the decisions and the refuted theories in it. It is a chronological
> document: later sections correct earlier ones, and several claims here were
> true only of the prototype.
>
> **For what the journal actually guarantees today, read
> [`docs/dev/matrix-event-journal-contracts.md`](../matrix-event-journal-contracts.md).**
> Nothing below should be quoted as current behaviour without checking it
> against the code first.

## Status

This is a feasibility-first architecture and cutover plan, not a prediction of every file or commit that implementation will require.

The implementation proceeds only if a focused prototype proves that the replacement is correct, fast enough, and materially smaller than the current system.

All MindRoom production changes then land in one implementation PR.

The required mindroom-nio change necessarily remains a separate prerequisite PR because it is a different repository.

### Implementation state

The prototype proved out and the cutover is landing on `wip/matrix-journal-ingress-cutover` (draft PR #1800).

**Done:** the nio prerequisite (merged, released as **0.37.0**, and now resolved from PyPI rather than a branch pin); the journal, principal-bound store, admission and pending worker; the visible-message projection with bounded reads and hydration; the deterministic outbox; the crash matrix and the live Tuwunel harness; ingress cutover; delivery cutover; and **all eleven boundary contracts**, each verified against the code rather than assumed — see the status table below.

**Final state of this line, written after the reviews stopped finding things.**

Review rounds against the running code kept reproducing production defects directly, in two kinds: ownership and ordering errors in paths this plan already called done, and tests that passed without ever exercising the behaviour they are named for.
Neither kind is detectable from the status table below, because a boundary contract can be verified against the code and still be broken by the code that calls it, and a green suite proves only that the assertions that ran were satisfied.

The last review round found two items, both dead code rather than behaviour, and both were fixed.
The last defect found by running the suite was a module-global approval manager leaking between tests, which surfaced as an unrelated test hanging to its timeout roughly one run in five; it took sixteen full-suite runs and captured instrumentation to identify, and is fixed.
What remains is the four deferrals recorded in the pull request, which are decisions rather than defects.

Treat even this version of the line as a claim to check.

Terminal truth now has one owner. The terminal turn record commits inside the acknowledgement's transaction, and `delivered_turn_repair.py` -- which existed only because the two stores could not share one -- is deleted rather than kept. That deletion is the check that this was a collapse and not another reconciler.

The two external items need a person rather than a change:

- **Three plugin PRs and PR #1800 itself.** An agent is blocked from `gh pr merge` by policy, deliberately.
- **A `mindroom-nio` release.** Per-room continued recovery is implemented and green on branch `per-room-recovery`, steps 1 through 4 including the sticky abandonment flag. Once it ships and the PyPI pin moves, MindRoom deletes `history_debt.py` (187) and `sync_recovery_escape.py` (98) with the machinery around them. That deletion is already proven on branch `history-debt-deletion`: net **-662**, full suite exit 0. Do not merge its temporary local-path pin commit.

Deleting the history-debt side *before* the release would be a regression, not progress. It would convert a context-only fallback into a silent one for the cases nio abandons outright -- the `backfill_max_events` cap, a hard non-retryable `/messages` rejection, an unverifiable page. Step 4's sticky flag is what makes those visible instead.

Five earlier versions of this line were wrong in the same direction, and the pattern is worth more than the line. The first said "Remaining: nothing" while the same document described two gaps below it. The second named an approval card sent before its recovery row was written -- real, and since fixed (`f5e22efbb`). The third said terminal truth still had two owners after the collapse had landed, contradicting the gate-check table further down. The latest said nothing implementable remained while review was still reproducing production defects, and while the fixes for them were being committed to this very branch. Each time the summary was more finished, or less finished, than the work. Treat this line as a claim to check rather than as evidence.

The projection cutover landed with the deletion of `src/mindroom/matrix/cache/`, and with it `conversation_cache.py`, `client_thread_history.py`, `runtime_support.py`, `membership_fence.py`, `thread_bookkeeping.py` and `postgres_cursor` — 27 source modules. `ThreadReadMode` moved to `matrix/conversation_reads.py`, where the read API it describes already lives; it is a caller contract, not cache policy. `vulture_whitelist.py` came through byte-identical, which was read at the time as evidence that nothing had been suppressed to make the deletion pass. It was the opposite. Eight entries naming `matrix/cache/` modules survived the modules themselves, and vulture cannot fail on a whitelist line whose symbol no longer exists — so the deletion passed while the suppressions it should have retired stayed in the tree. They are removed now, and `uv run vulture` still passes without them.

These are whole-tree figures including deleted tests and tooling. The
production-source number is the one that answers "did this simplify anything",
and it is smaller than the headline.

Measured at `fa4b3268c`, the merged tip. Every earlier version of this table
was captured mid-branch and overstated the reduction by roughly 3,400 lines in
production source and 19,600 across the branch, because deletions landed before
the additions that replaced them:

| | added | deleted | net |
| --- | --- | --- | --- |
| `src/mindroom` (production) | 16,728 | 21,825 | **−5,097** |
| tests, docs and tooling | 42,222 | 46,949 | −4,727 |
| whole branch vs `main` | 58,950 | 68,774 | **−9,824** |

Two structural figures survive restatement better than the line counts, because
they do not move as the branch does: `matrix/cache/` was 12,441 lines in 29
files and `event_journal/` is 6,295 in 19, while doing strictly more; and the
tree holds 328 fewer functions than `main`.

**Closed along the way**, each with a mutation-tested pin:

- An attempted outbox row whose sending device was never recorded was resent blind. A Matrix transaction ID deduplicates within one device, so that is only safe when the device happens not to have changed; a row written before the column existed, followed by a re-login, put the same answer in the room twice. Reproduced on both backends. Unknown now reconciles by reading the room -- which is what the comment beside the column already said recovery would do, so the code and its own schema documentation had been contradicting each other. The cost is bounded by exempting rows nobody has attempted, where an absent device means only that nothing has sent it yet. The test that used to cover this pinned the defect: it blanked the device without changing it, so no duplicate was reachable and its "no scan happened" assertion was recording the bug.
- A failed FINAL edit left two owners, and the first fix for it was wrong in an instructive way. The failure notice was sent as a plain message rather than through the frozen `m.replace` row -- deliberately, because a second delivery under the same turn and stage resends *those* bytes -- but the row stayed attempted and unacknowledged, so the next recovery pass resent the frozen envelope and the same error reached the room twice. The first fix adopted the fallback as the row's outcome after the send. That loses a race with no crash in it: `recover_deliveries` can resend and acknowledge in the gap, and acknowledgement is first-writer-wins, so adoption can never bind. It also could not tell a membership-fence refusal from a Matrix failure, since both surface as the same false return -- so a refused enqueue direct-sent an old turn's error into a room the bot had left.

  The second fix is a deletion, and `adopt_final_delivery` went with it. Once a FINAL edit carrying a turn fails, exactly one of two things is true and neither wants another message: either the fence refused, and the conversation is not the bot's any more, or the row exists and the outbox still owes this turn an answer -- in which case recovery resends the frozen envelope and the placeholder becomes the error notice, which is the outcome the path wanted all along. The direct send survives only where there is no durable owner at all: no placeholder to edit, or an edit that never carried a turn.
- 🔒 The move onto `turn_records` initially ignored every existing ledger, which would have been the worst regression in this branch. An installation that has been answering messages keeps all of its terminal truth in `tracking/<agent>_responded.json`; a runtime reading only the new table sees an empty ledger, concludes nothing has ever been answered, and re-answers the entire backlog on its first replay. `load` now imports that file once, through the same codec that writes the rows, so the round trip is lossless by construction and a retired field is dropped exactly as it would be on any other load. The import runs only when the table is empty and renames the file on the way out, which is what makes it idempotent: a compaction that legitimately empties the table cannot resurrect deleted history. The field carrying the path is deliberately **required** rather than defaulted -- a missing constructor argument fails loudly, while a silently skipped migration re-answers a user's backlog.
- Shared ledger state was keyed by agent name alone, so two ledgers over *different* databases aliased: the second bound to state the first had already marked loaded, skipped its own read, and answered "handled" from rows its database had never held. Now keyed by `(backend identity, agent)`. One process normally owns one database, but tests routinely open several, which is exactly where that aliasing turns into a green run that proves nothing.
- 🔒 Turn records stopped carrying attachment key material. Every media turn recorded a `TurnInputSnapshot` keeping each media source's whole Matrix event, which for an encrypted attachment is the `file` block: the AES key JWK, the IV, and the content hashes. The ledger is a plain JSON file that outlives the turn, so a copy of every attachment key the bot had ever been sent accumulated in it. Nothing ever read it back -- the only decoder was private, uncalled, and waiting on a handoff that was ultimately designed the other way round, transferring ownership at durable outbox enqueue instead. The journal already stores each pending event's source and rebuilds the media event on replay, so this was a second durable copy of the same secrets serving a design that was never built. The ledger schema version is deliberately unchanged: records parse field by field, so a file already on disk drops the retired key on its next write, where bumping the version would quarantine every existing ledger and discard the live turn identity around it -- and a turn whose identity is gone is a turn the bot answers twice.

- The stalled-recovery escape is no longer lossy. A skipped room records a timestamp-anchored `room_history_debt`, and the repayment walk is debt-aware: it walks past the prompt window until it actually reaches the anchor, so a bounded stop is no longer misfiled as permanent loss. The debt is *ordered* before the checkpoint under the same lock, not written in the same transaction as it — an earlier version of this line claimed the latter, and the two are not interchangeable. The debt lives in the journal database and the checkpoint in the continuity files, so no transaction spans them; what makes the pair safe is the ordering plus failing closed. Crash between the two and the debt describes a gap the un-advanced cursor re-syncs anyway, costing one redundant walk. Reverse the order and the watermark moves past history nothing is left to ask for. This pair is `reconciled`, not `transactional`.
- The `PendingEventWorker` lane-halting race is closed, along with a second instance of the same class found afterwards: `admit_and_run` ran its callback outside the lane after admission had already woken the pump, so one `ROOM_LIFECYCLE` event could get two concurrent handlers. Both are now single-handler by construction.
- Hydration is single-flight again. `ensure_hydrated` checked the hydration marker before awaiting but awaited twice more before starting the walk, and `_shared` cannot join a task that has already finished and been dropped — so two readers could each walk the same conversation. That is a contract violation rather than a wasted request, because a conversation is meant to hydrate at most once per conversation and membership epoch. The marker is now rechecked in `_hydrate`, the last point before any server request.

### The durable fact that had two owners, and no longer does

"Is this turn finished?" used to be answered by two records in two substrates. The journal settled a source transactionally when its answer became durable; the handled-turn ledger was a per-agent JSON file with a debounced, retrying write-behind persist. Being different substrates, they could not share a transaction, so the pair was unguarded by construction rather than by oversight, and `dispatch_replay_guard.py` had to consult both.

**Both halves are now done.** The ledger's records live in `turn_records`, in the journal's own database, and the terminal record commits inside the acknowledgement's transaction. The proof that this collapsed ownership rather than adding a third writer is that `delivered_turn_repair.py` is *deleted*: it existed only to rejoin an acknowledged delivery with a record that had not caught up, and with the two in one transaction there is no window left to rejoin.

Everything that existed to make two substrates approximately agree went with it -- the write-behind queue, durability barriers, the retry timer and its backoff, the bounded persist thread pool, the advisory file lock, corruption quarantine, `wait_for_persist`, `record_turn_durably`, `has_durably_responded`, `is_durably_handled`. Awaiting the write is the durability wait.

Reads stayed synchronous off an in-memory map, which is why the conversion touched 28 write call sites rather than the hundred-odd read sites. That choice has one consequence, paid deliberately: a synchronous read can no longer lazily load, so an unloaded read raises instead of answering. Returning "no record" would have been the worst available answer, because it reads as "never handled" and that is how a bot answers a message twice.

The migration's real cost was not the conversion. It was four defects the estimate could not have predicted, each found by review and each fixed with a probe: reading only the new table would have made every existing installation re-answer its whole backlog; shared state keyed by agent name aliased across two databases; a cancelled write rolled memory back after its transaction had already committed; and recovery committed the record while leaving the map stale. The last two are the same disagreement seen from opposite sides.

**Fixed: thread export no longer refuses threads longer than the prompt window.** Export read the projection through a hydrator built with the prompt's bounds, so past `HYDRATED_PROMPT_WINDOW_MESSAGES` hydration recorded `complete=False` and `projected_history.py` declined to write a suffix as though it were the whole thread. Refusing was right given that input; the input was wrong for this caller. `origin/main` defaulted `prefer_cache=False` and paginated Matrix directly (`thread_export/execution.py:281`, `:138`), so a thread of any length exported in full, and two thousand logical messages is reachable in a busy room.

Export now walks with its own far larger bounds. There is still one walk and one reducer — a second Matrix interpreter is what that module exists to prevent — and the bounds are still finite, because a runaway guard that never fires is not a guard: a thread past even these is reported as too large to write honestly rather than silently truncated. Pinned by `test_export_does_not_inherit_the_prompt_window`, which fails `assert 2000 > 2000` if the bounds are inherited again.

### Known open, with evidence

Every item here was validated against the code, not accepted from a review. None removes a capability the branch had before; the export regression, which did, was fixed. They are listed with their fix direction so the next person does not re-derive them.

**Fixed: a cancelled approval no longer orphans its card.** The card reaches the room before its durable row is written, and a cancellation in that gap left a clickable card with no row — no restart could expire it, and a click found neither a live waiter nor a stored card. `origin/main` recovered these by scanning the event cache for original approval events, and this branch deleted that discovery path.

The cancelled-*send* path already handled exactly this shape: record the card, then expire it, detached so the caller's cancellation cannot interrupt the recovery. The same shape arriving one step later — after the send returned — had no handler, and now uses the same sequence.

Detached rather than shielded, and that distinction is the fix rather than an implementation detail. Shielding the write also makes the row land, and a probe confirms it does, but it delivers the cancellation first, so the expiry runs before the row exists and a cancelled approval is recorded as an *approved* one. Five existing tests catch that, which is how the right shape was found. Pinned by `test_a_cancellation_after_the_send_still_records_and_expires_the_card`, which fails "the orphaned card was never taken back" without the handler.

A hard process crash in the same gap is narrower and still open; closing it needs the card send routed through the response outbox, which already owns every other visible send.

**Two payload divergences on the delivery path**, traced to their seams so the next session starts at implementation.

`_finalize_visible_replacement_edit` (`delivery_gateway.py:1401`) builds an `EditTextRequest` with no `delivery_turn_id`, so the final streamed transform edits outside the outbox *after* the `FINAL` row is acknowledged: the durable row holds raw text while the room shows transformed, and a crash between them leaves the room raw permanently. Routing that second edit through the outbox is not the fix — `response_outbox.stage` is `CHECK (stage IN ('initial', 'final'))` and `final` is taken, so it would need a third stage for an edit that should not exist at all.

The fix is to apply the transform *before* the terminal enqueue, which removes the second edit entirely.

**The seam is not where a first reading puts it.** The obvious candidates are the `terminal_send` closure (`delivery_gateway.py:1317-1347`) and `_durable_terminal_edit`, but both receive `content` already built and would have to reconstruct it from transformed text — reproducing mention formatting, tool traces, the `m.notice` downgrade, the canonical visible body and the warmup suffix. That is the expensive, bug-prone version.

All of that is derived, in one place, from a single string: `_prepare_delivery_from_snapshot` (`streaming.py:366`) takes `snapshot.accumulated_text`, formats it once at `:370`, and everything downstream — `display_text`, `format_message_with_mentions`, `content["body"]`, the warmup suffix — follows from it. Transform that text and every derived field is consistent for free, with no content rebuilding anywhere.

`_prepare_delivery_from_snapshot` is synchronous while the transform hook is async, so the hook cannot run inside it — but its async caller is the right place, and it already exists.

Implementation, traced end to end:

1. `_prepare_delivery` (`streaming.py:1010-1026`) is async: it builds the snapshot, then hands formatting to `asyncio.to_thread`. Apply the transform between those two steps, for `is_final=True` only, with `dataclasses.replace(snapshot, accumulated_text=...)`. Every derived field is then computed from the transformed text by the existing code.
2. The streaming object needs the hook. Add an optional `final_text_transform: Callable[[str], Awaitable[str]] | None` beside `terminal_edit` and `terminal_send` (`streaming.py:468-469`), which are wired the same way.
3. Bind it where those two are constructed (`delivery_gateway.py:1281-1282`), closing over `identity` so it can call `_apply_final_response_transform`. **`StreamingDeliveryRequest` does not carry `identity`** — it lives on `FinalDeliveryRequest`, which only the post-hoc finalizer sees — so the field has to be added and threaded through that request's construction sites first. This is the step that makes the change bigger than the other three combined, and it is the one a first reading misses: steps 1 and 2 apply cleanly on their own and then have nothing to bind to.
4. Delete the post-hoc block at `delivery_gateway.py:1734-1750`, keeping its guard that an empty or unchanged transform result is ignored.

**Steps 1-5 were built once and reverted, and the reason matters.** The production wiring applies and imports cleanly, and `tests/test_streaming_behavior.py` then fails with `assert None == '$stream_1'` and `assert None == '$response'` — the streamed delivery produces no event ID. Those tests were green immediately before the change, so it introduces the failure.

**The change has now been built to completion once and reverted at the last step. It works.** What follows is measured, not projected.

The rejecting constructor was neither `StreamingResponse` nor a test double: it is the module-level entry point in `streaming.py` (~`:1830`) that the gateway calls, which lists `terminal_edit`, `terminal_send` and `transport_is_current` explicitly and forwards them to `streaming_cls(...)`. It needs `final_text_transform` in its signature and in that call. With that added, **`tests/test_streaming_behavior.py` passes.**

Removing the post-hoc edit then cascades in a way worth knowing in advance: deleting the transform block orphans the `try:` that wrapped it (drop the wrapper and dedent, the guard it protected needs no protection), which orphans `_finalize_visible_replacement_edit` — vulture catches it, and deleting it is correct, since removing the second edit is the entire point. `FinalTextTransform` also has to join `streaming.py`'s `__all__`.

**Exactly three tests then fail, and they are not a mechanical fix.** An earlier note here said they only needed an `identity=` argument. That is wrong: all three call `gateway.finalize_streamed_response(...)` **directly**, exercising the post-hoc finalizer this change deletes. They never enter the streaming path, so nothing can be added to them — they have to be rewritten to drive a stream and assert the transformed text arrives in the *terminal payload* rather than in a following edit, or replaced by one test that asserts that contract. Deciding which is a review question, not a typing exercise.

The three: `test_streamed_success_allows_one_final_response_transform`, `test_streamed_success_noop_final_transform_keeps_visible_stream_text`, and `test_streamed_success_noop_final_transform_uses_matching_visible_interactive_metadata`. Their symptom is `assert 'chunk' == 'updated text'` and `Awaited 0 times` — the hook is never reached because the finalizer no longer calls it. That is the whole remaining task, and it is the only thing between this change and landing: the production side is written, imports, passes `tests/test_streaming_behavior.py` up to these three, and satisfies every hook including vulture and privata.

**The error that led here, read rather than inferred**, is `TypeError: ... unexpected keyword argument 'final_text_transform'`, surfacing as `FinalDeliveryOutcome(terminal_status='error', event_id=None)`. So the failure is a constructor rejecting the new field, not anything about the transform's behaviour — and the delivery reports `error` rather than raising, which is why it reads as a bare assertion on a missing event ID.

Note what this rules out: the field *is* declared on `StreamingResponse` (beside `terminal_edit` and `terminal_send`), and the test subclasses take `**kwargs` and pass them through, so neither is the rejecting constructor. Identify which one is before changing anything else; there is a second construction path in play that the five-step reading did not account for.

An earlier version of this note blamed missing `response_hooks` on the test doubles. That was inference and it was wrong. Do not trust the first explanation for it. The initial reading was that the transform factory reaches `deps.response_hooks` and the streaming test doubles lack it, so the closure raises. That is probably wrong: `test_streaming_first_send_uses_resolved_thread_root` drives a real bot through `_process_and_respond_streaming`, not a double, and an `AssertionError` on a missing event ID is not what an unhandled `AttributeError` inside terminal delivery would look like. The real cause is unidentified, and the next attempt should start by reading the traceback rather than by re-deriving the wiring, which is known to be correct as far as import time.

Whatever the cause, `assert None ==` on a stream's event ID means the terminal delivery did not happen — which is the failure mode this change exists to prevent, so it must be understood rather than worked around.

Verify with the full suite, then `tests/manual/streamed_edit_live_proof.py`, which reads the room's *raw* final state rather than `m.new_content` and is the probe that can actually see a wrong fallback body. The mutation to check is that the `FINAL` outbox row's payload and the visible room body carry the same text.

**The first instance is fixed** (`e96dcaf18`): the transform now runs against the answer text before the terminal payload is built, so the outbox row and the room carry the same body and there is no second edit. `_finalize_visible_replacement_edit` is deleted. Two things that fix taught, worth reusing on the second instance: a failing transform must degrade to the streamed text rather than fail the delivery, and the tests that drove the old post-hoc path could not be adapted — they had to be replaced by tests of the terminal payload.

**Both instances are fixed** (`e96dcaf18`, `fd7c50190`): the send and edit paths prepare the payload before the outbox row is written, so the frozen row is the finished wire event. `prepare_large_message` returns content unchanged below the size limit and a prepared payload is by construction below it, so the existing call inside `send_message_result` became a no-op, recovery included.

**That pin is now written and verified to bite.** `test_an_oversized_send_freezes_the_payload_matrix_receives` (`tests/test_streaming_finalize.py`) drives `_send_content` with a 100k body and a `delivery_turn_id`, then reads the frozen row out of `FakeOutbox`. Stubbing `_prepared_for_the_wire` back to returning its input — the exact regression, preparation moved back inside the send — fails it with "the row kept the oversized original". It asserts the row's body is no longer the oversized original rather than asserting a specific `m.file` shape, because whether the sidecar upload succeeds or degrades to an inline preview is the homeserver's business; what must hold either way is that the row holds the *prepared* payload, since that is what goes on the wire and what a resend replays.

The harness is already there, which an earlier note in this file denied: `FakeOutbox` (`tests/conftest.py:895`) keeps every frozen row in `self.rows`, so the payload is directly inspectable, and `make_outbox_mock()` returns one. Write the test in `tests/test_streaming_finalize.py`, which already constructs a `DeliveryGateway` with `DeliveryGatewayDeps` — `test_streaming_behavior.py` does not import the gateway at all, and putting it there costs an import chase for no reason. Call `_send_content` with a `delivery_turn_id` and a body over the limit, then assert the row's payload is the `m.file` form with a `url` or `file` key and a truncated body.

**For reference, the defect that was there.** `send_message_result` calls `prepare_large_message` (`matrix/client_delivery.py:345`), which uploads the sidecar and rewrites the payload — but `_send_claimed` (`delivery_gateway.py:563`) has already frozen the row by then, so the durable payload lacks the MXC the wire carries, and a recovery resend re-uploads and can mint a new MXC and new encrypted-file keys under the old transaction ID. Same remedy: prepare the wire payload before the enqueue, in both the send and edit paths, and send an already-prepared payload on live and recovery alike. Check whether `prepare_large_message` is idempotent before calling it earlier.

**And the original statement of both, also for reference — neither is open.** `_finalize_visible_replacement_edit` built an `EditTextRequest` with no `delivery_turn_id`, so the final streamed transform edited outside the outbox after the `FINAL` row was acknowledged: the durable row held raw text while the room showed transformed, and a crash between them left the room raw. Separately, an oversized terminal delivery uploaded its sidecar after the claim, so the frozen payload lacked the MXC the wire carried. Both were the same class — the durable record and the wire event disagree — and both wanted the wire payload prepared before the enqueue, which is what `e96dcaf18` and `fd7c50190` did.

**Outbox idempotency rested on an unmeasured assumption. Done** (`dcf2b819a`). Recovery retried under the same transaction ID while persisting nothing about the device that used it, so a re-login turned one row into two visible answers.

The decision the earlier version of this note deferred turned out to be a false choice. It framed recovery as picking between refusing to resend — silent loss — and resending under a *fresh* transaction ID, which duplicates if the original landed and also breaks the deterministic-ID invariant the outbox rests on. There is a third option, and it needs no new ID: keep the deterministic one, and when the device has changed, read the room. A transaction ID is per device, so reusing the same string on a new device is still deterministic and still deduplicates that device's own later retries.

So `response_outbox.sending_device_id` records which device's transaction-ID namespace a row was attempted in. `ResponseDelivery.flush` compares it against the device this process logged in as, and on a mismatch asks `find_response_event_ids_via_room_messages` — the same scan `VisibleResponseReconciler` uses — for an answer already in the room, adopting it if there is one and sending only if there is not.

*When* that column is written is the part this note originally got wrong, and the error was not cosmetic. It first said the claim writes it, in the same statement that marks the row attempted. Implemented that way (`efd3c5b71`), a room lookup that raised left the row unacknowledged but already relabelled with the current device, so the next pass compared the marker against itself, concluded nothing had changed, skipped the lookup and posted the answer a second time — reproduced, not argued. Claiming freezes the payload; it does not mean this device is going to send. `claim()` therefore reads the row *before* marking it and returns that pre-claim view, and `record_sending_device` is a separate write called only once a send is actually about to happen (`response_delivery.py:197-207`, `outbox.py:131-138`).

Three properties keep it cheap and honest. Writing the device before the send rather than after a *successful* one bounds the scan to once per device change: a crash mid-send still leaves behind the fact that this device may hold the ID, so a row that keeps failing to send looks like an ordinary resend from the second pass onward — which matters because recovery runs after every sync response. The failure that does *not* advance the marker is the scan itself, so a lookup that cannot run stays owed. An unknown device on either side — a pre-column row, or a process that has not logged in — resends exactly as before rather than paying a scan to rule out a change nobody has evidence of. And an edit is exempt, because a repeated `m.replace` resolves to the same visible message either way.

Two limits, stated rather than papered over. The scan finds genuine replies, so a delivery with no source to reply to — a scheduled message, a hook notice — reads as "not delivered" and is sent, which is the pre-existing blind resend and no worse. And there is still no retry horizon: a permanently failing send is retried on every sync response forever. That is deliberate. Every terminal state available is "drop the answer", the membership fence already withdraws rows for rooms the bot has left, and the once-per-device-change bound removes the sharp edge this fix could otherwise have added.

The live proof carried the same weakness the code did: it demonstrated device-scoped deduplication using a second *registered account*, which would produce a second event whether or not the server keyed on the device. It now re-logs in the same account and refuses to run if the server returns the device it already had (`f12ba305a`).

**The dual-authority collapse, mapped. Step 1 done** (`d5497d7dd`).

`turn_records` now exists in the journal's own database: agent-scoped rather than principal-scoped, because a turn record is proof that a message was answered and that stays true across a re-login, while every other table here is only meaningful beside the sync that produced it. Transactionality needs one database, not one scope key. One row per event that indexes a record, because a coalesced batch is reachable from any of its sources. Nothing reads it yet.

Step 2 is swapping `HandledTurnLedger`'s persistence onto it, and the whole surface has been read rather than estimated:

- `_LedgerState` is keyed by responses-file path; it becomes agent-name.
- `_ensure_loaded_locked` -> `load_all()` plus `TurnRecordCodec` decode.
- `_persist_records` -> `upsert()` per record. The advisory file lock goes: the database owns concurrency, and the whole-file read-modify-write it protected disappears with it.
- `_cleanup_old_events` -> compute the cleaned set in memory, then `forget()` the dropped ids.
- `_write_responses_file_locked` and the entire quarantine path (`malformed`, `structurally invalid`, `unsupported-schema`, non-UTF8) delete. Those exist because a JSON file can be corrupt; a database has its own integrity.
- A one-time import of an existing responses file, provable lossless by codec round-trip because it is the same codec.

Test cost, measured: 69 `HandledTurnLedger(...)` constructions, of which most route through one helper at `tests/test_handled_turns.py:45`; 14 tests touch the file directly, and 6 of those are quarantine tests that delete rather than move. An earlier note in this file said "69 rewrites" -- that was wrong by a factor of five.

**The bridge this plan specified deadlocks, and the design that needs it cannot reach step 3 anyway.** Both corrections came from reading the code, and the first from running it.

The specified bridge was "about ten lines": the persist drain has no running loop, so it calls `run_coroutine_threadsafe` and blocks. The sentence justifying it -- "the loop only schedules, and SQLite still offloads" -- is exactly where the defect hides. Both journal backends offload with `asyncio.to_thread` (`sqlite_backend.py:148,191`, `postgres_backend.py:145,168`), which is the loop's *default* executor. So: a turn calls `update_handled_turn(..., wait_for_persist=True)` from a `to_thread` worker and blocks it on the persist future; the drain bridges back to the loop; the loop tries to run the store write; the write needs a default-executor thread; every one of them is held by a blocked turn. The cycle closes and the runtime wedges. Reproduced with a standalone probe that mirrors all four shapes: one concurrent turn succeeds, and at `max_workers` concurrent turns it never returns. The real bound is `min(32, cpu_count + 4)` concurrent durable turns, which a multi-room deployment reaches.

Giving the journal backends a dedicated executor breaks the cycle -- verified with the same probe -- but it rescues the wrong design. A write-behind queue cannot enlist in the caller's transaction, by definition, so keeping the synchronous API keeps settlement and the terminal record in two commits. Step 3 is unreachable from it.

So the honest shape of step 2 is an async conversion, not a bridge. `update_handled_turn` becomes awaitable and the queue, the barrier futures, the retry timer, the dedicated executor and `wait_for_persist` all delete with it -- awaiting *is* the durability wait, which is roughly 300 lines removed rather than ten added. The cost the earlier note omitted: 32 call sites across 9 public writers, of which only 5 are already inside `asyncio.to_thread`, plus sync callbacks (`turn_controller.py:1796-1804`, `edit_regenerator.py:333`) that are handed to the response machinery and would have to become async callbacks, changing the signatures that invoke them on the hottest path in the runtime.

That is a phase, not a step, and it is the one place where being wrong answers a user twice. It is gated on the live fuzz's `_assert_no_wrong_replies` -- see the note below on which number actually catches a duplicate.

**The conversion is written, and the measured cost is nothing like the estimate above.** Production is **+332 −581, net −249**, with `handled_turns.py` going 1053 → 804 and exactly one file growing. The estimate of "32 call sites" was right in kind and the reason it stayed that small is worth stating: only *writes* became awaitable. The hundred-odd synchronous reads were left alone by keeping the in-memory map and populating it once, so the conversion never reached them.

That choice has one consequence that had to be paid for rather than assumed. A synchronous read can no longer lazily load, so an unloaded read raises instead of answering. Returning "no record" would have been the worst possible wrong answer: it reads as "this turn was never handled", which is precisely how a bot answers a message twice.

Three defects surfaced in the conversion, all of which the estimate had no way to predict:

- 🔒 The first version read only the new table, which would have made every existing installation re-answer its entire backlog on upgrade -- all of its terminal truth is in a JSON file the new code never opened. Now imported once, through the same codec that writes the rows.
- Shared in-memory state was keyed by agent name alone, so two ledgers over different databases aliased and the second answered "handled" from rows it had never held.
- Memory was published before the row committed with no way back, so a write that failed left the map claiming a record the database did not have -- correct until a restart, and a duplicate answer after one. The publication is now rolled back on failure, and cleanup inverts the order for the same reason: deleting before forgetting can only suppress a duplicate, while forgetting first can cause one.

Why it must land atomically: a half-migrated dedupe substrate answers users twice.

**Which fuzz number catches that is not the obvious one, and an earlier version of this note named the wrong one.** `canonical_agent_replies` reads like a count of replies the agents produced; it is `len(oracle.expected_sources)` (`fuzz_live_matrix.py:2853`), the number of reply-*expecting* prompts the scenario issued. It moves with the seed, the profile and how many operations landed between restarts, so comparing it across two runs measures the scenario rather than the runtime. A branch run reporting 17 against `main`'s 16 was read here as a possible duplicate answer; it is not one, and nothing about it is a defect.

The check that actually catches a duplicate is `_assert_no_wrong_replies` (`:2160`), which runs after every batch and fails on any source with more than one reply, on any stray reply to a source nothing expected, and -- at the end of a saturation run -- on any expected source with no reply at all. Exactly-one-reply-per-source is the invariant; the count is telemetry.

**One backend, and now actually shared.** Each bot used to open its own `EventJournalStore`, so N bots meant N*5 PostgreSQL connections and no cross-principal writer serialization.

All three factory branches now borrow the orchestrator's store, and the borrower does not close it: `bot.py` skips the close when the store was injected, and the orchestrator closes it last, after every bot has stopped. That ordering is not incidental -- closing earlier would pull the store out from under a bot still draining its outbox.

Worth recording because the code drifted from the contract silently: the router and team branches forwarded the shared store from the start while the ordinary-agent branch did not, so every regular bot quietly opened its own backend against the same database. One writer per agent instead of one per process, and -- since ledger state is keyed by backend identity -- a replacement instance with its own in-memory view of the same durable facts. The plan had already warned not to let "open my own" survive into production; a review caught that it had.

## Goal

Replace the overlapping Matrix callback-obligation, history-repair, conversation-cache, handled-source, and visible-delivery state machines with explicit non-overlapping owners.

| Fact | Sole owner |
| --- | --- |
| Matrix recovery provenance and callback redelivery | mindroom-nio |
| Accepted inbound event and pending semantic work | Principal-bound Matrix event journal |
| Latest visible conversation content | Conversation projection |
| Completed model execution | `TurnStore` |
| Initial and terminal Matrix delivery intent | Response outbox |
| Last accepted application sync checkpoint | `SyncContinuityStore` |

The desired production flow is:

```text
nio recovery and provenance
  -> durable journal admission and projection update
  -> pending event worker
  -> claimed deterministic outbox delivery
  -> Matrix
```

A model result becomes durable *by* being enqueued; there is no separate durable
record of it beforehand. A crash between the model returning and the enqueue
leaves the sources pending and reruns the model, which is deliberate: persisting
a result that was never claimed would let a restart send content the outbox
never froze, and re-running is the cheaper of the two wrong answers. Only after
the enqueue is the answer owed to a room, and from that point the transaction ID
makes redelivery collapse onto one event.

Intermediate AI edits remain transient transport updates and are not durable product data.

## Why This Is Worth Attempting

At baseline commit `b639b6ef3`, the overlapping subsystem contains 20,248 production lines.

The 29-file `matrix/cache/` package overlaps with conversation history, dispatch obligations, sync trust, source deduplication, and response reconciliation.

`conversation_cache.py` alone exposes five overlapping history-read paths plus advisory outbound notifications.

This duplication makes correctness depend on several stores agreeing about the same event after restarts, limited timelines, edits, redactions, and delivery failures.

The problem is not Sliding Sync alone.

Sliding Sync exposed recovery gaps, but most current complexity comes from MindRoom independently repairing, caching, certifying, and replaying state that nio or one durable application store should own.

## Non-Negotiable Invariants

- No accepted actionable event may be lost after readiness.
- The no-loss guarantee begins only after the first successful baseline response has committed and readiness has been published.
- No event may create more than one semantic turn.
- `LIVE` and `RECOVERED` events are actionable, while `HISTORY` events are context-only.
- MindRoom must consume nio provenance and must never reconstruct it from cursors, timestamps, membership repetition, `limited`, `prev_batch`, or server-specific pagination shapes.
- Nio must reject sync completion when MindRoom cannot durably admit an actionable callback.
- A failed admission must not advance the application checkpoint.
- An unrecovered room is retried rather than silently becoming history. *(Held, by a different mechanism than the original wording — "keeps the bot unready" — implied. Readiness is not gated on it: `_apply_transport_recovery_outcome` (`bot.py:1308`) only emits operator telemetry. The enforcement is certification refusal, `certified = ... and not unrecovered_room_ids`, which withholds the checkpoint and sets `reset_client_token`, so nio rewinds and asks again. That is the better mechanism: a readiness flag would take the whole bot down for one room, and the point is that every other room keeps working. After the bounded escape the room does get passed over, but loudly — an `error` log and a durable history debt — so it is still not silent.)*
- Conversation reads are bounded and indexed after at most one successful hydration per conversation and membership epoch.
- Intermediate edit bodies and edit chains are not retained.
- Redacting a current edit restores the server-authoritative previous visible revision without retaining a local edit chain.
- No read path may serve a revision after that revision's redaction has been durably admitted, so a pending refetch omits the message rather than returning deleted content.
- Initial and terminal response deliveries are idempotent across crashes.
- The final implementation PR head must not leave both the old and replacement production paths active.
- The single implementation PR must be green and must remove every active owner it replaces before merge.
- Existing authorization, E2EE metadata, media transcript, large-message sidecar, reaction, command, source-redaction, and room-membership behavior remains in scope.
- The existing continuity-checkpoint format remains readable during cutover so downtime events can still be recovered.
- `bot.py` remains lifecycle and dependency wiring rather than becoming an implementation owner.
- If a third review round still finds a new correctness class, implementation stops for redesign.

## State Decisions That Must Not Be Rediscovered During Implementation

### Principal ownership

One shared database backend may hold several principals, but runtime code receives only a principal-bound store view.

Operational methods such as `admit`, `pending`, `settle`, `load_conversation`, membership changes, and delivery methods therefore do not accept `principal_id`.

Inbound envelopes and conversation keys also omit `principal_id` because the bound store supplies it.

This prevents callers from accidentally reading or settling another bot's rows.

### Conversation identity storage

Typed APIs represent an unthreaded conversation as `thread_id=None`.

Durable SQLite and PostgreSQL tables represent it with `thread_id TEXT NOT NULL` and the empty string as the single canonical storage value.

One shared boundary helper encodes `None` to the empty string and decodes it back, so primary keys and uniqueness constraints never depend on nullable equality.

### Durable admission

Admission performs the journal insert or deduplication, membership-epoch validation, and projection update in one transaction.

The admission callback returns to nio only after that transaction commits.

Context-only payloads may be compacted after projection, while actionable payloads retain the exact replay input until terminal settlement.

A pending worker processes committed events in durable receipt order and leaves an event pending on cancellation or failure.

No durable `running` state is needed because a process crash must make the event eligible for retry.

### Visible-message projection

The projection stores one row per logical message with its latest visible body.

A valid same-sender edit replaces that row only when `(origin_server_ts, event_id)` is newer than the current replacement identity.

An edit received before its original is stored as one latest unresolved edit per target and sender.

Including sender in the unresolved-edit key prevents an attacker from evicting the legitimate author's edit before the original arrives.

When the original arrives, only its sender's unresolved edit may apply, and all unresolved rows for that target are deleted.

Admission records every redaction target as a compact durable tombstone before projection so an original or edit arriving later cannot resurrect redacted content.

Redacting the logical original tombstones the logical message.

Redacting the currently visible replacement clears the row's visible body and marks the logical row with a durable refresh token derived from the redaction's journal receipt order.

Clearing the body in the same admission transaction is required because a redacted revision must never be readable, and a stale-but-visible row would otherwise let any non-strict caller serve content the sender deleted.

A strict conversation read waits for one shared point refetch of the logical original and its current relations instead of serving content known to be stale.

A non-strict read never waits and never serves a body-cleared row, so it omits that logical message until a refetch installs the server-authoritative revision.

The point refetch uses the same relation traversal and reducer as initial hydration, retains no edit chain, and installs the reconstructed visible row only when both the membership epoch and exact refresh token still match.

A newer edit or redaction changes the projection revision and prevents an older in-flight refetch from overwriting it.

Successful conditional installation clears the refresh token, while failure or cancellation leaves it durable and makes strict reads fail closed until retry succeeds.

The next strict read of that conversation drives the retry, so no background refresh worker exists and a permanently unreachable homeserver degrades reads rather than accumulating retry state.

Redacting an already superseded replacement does not change visible content.

### Bounded conversation reads

Every conversation read requires a positive limit and an optional stable cursor composed of `(created_ts, logical_event_id)`.

The store queries the newest bounded page through a principal, room, thread, timestamp, and event-ID index and returns the page in chronological order.

Prompt assembly requests pages only until its context budget is satisfied.

Full exports iterate explicit pages.

No runtime API may materialize an unbounded room-scoped conversation.

### Hydration

A thread is hydrated by fetching its root and traversing recursive event relations without a relation-type filter.

Mindroom-nio must expose `recurse`, parse the returned `recursion_depth`, and support a required minimum depth.

MindRoom requires only that a non-empty recursive page reports `recursion_depth` at all, because the number itself is not comparable between servers.

The Matrix version advertised by `/versions` is not proof of recursion depth.

A room-scoped conversation may perform one serialized initial `/messages` traversal.

Concurrent first readers share one hydration task, and hydration becomes complete only after successful pagination and an atomic membership-epoch-checked installation.

Failure remains a visible readiness or request failure rather than reviving room-wide repair scans.

Current-edit redaction reuses this hydrator for a logical-message point refetch rather than introducing a second history-repair implementation.

### Deterministic delivery

Initial and final delivery stages use deterministic transaction IDs derived from principal, turn, and stage.

The completed model result is durable in `TurnStore` before final outbox enqueue so recovery does not rerun a completed model call merely to rebuild delivery content.

Enqueue may create a row or update an unattempted row.

The worker then atomically claims the row by committing `attempted=true` before network I/O.

Claiming makes the payload and target immutable and returns the exact stored delivery to send.

An attempted but unacknowledged row is retried with the same payload and transaction ID.

This ordering closes the case where Matrix accepted an older deterministic transaction while a restarted model run produced different content that could never become visible.

### Storage concurrency

SQLite uses one writer task and a bounded command queue.

Writer and reader connections use WAL-compatible settings and an explicit `busy_timeout`.

PostgreSQL implements the same behavioral contract without a second application protocol.

The two backends run the same admission, projection, membership, pagination, and outbox contract tests.

### Homeserver behavior that is not observable from this repository

These facts come from the fork repositories and the deployment configuration rather than from MindRoom source, so implementation must not rediscover them by debugging.

Tuwunel purges superseded `m.replace` events on a background job, which is why edit-redaction recovery must ask the server instead of trusting any local history.

The purge is disabled by default in the fork but enabled in the MindRoom production deployment, with a 24-hour minimum age, an hourly interval, and a 10,000-event batch size.

That 24-hour floor means a current-edit refetch normally returns the true previous edit and returns the original body only once superseded edits have aged out, and both outcomes are correct because every Matrix client sees the same server state.

The purge exists to reclaim storage from MindRoom's own streaming edit churn, which this plan already treats as transient, so it is not a reason to retain edit history locally.

Tuwunel and the MindRoom Synapse fork both cap recursive relation traversal at depth three in source, and neither advertises that cap.

The two servers do not report `recursion_depth` with the same meaning, which a live run against Tuwunel established and which invalidates any numeric floor.

Synapse returns the constant three, describing the depth it is willing to traverse, while Tuwunel returns the depth of the deepest event it actually returned, so a root with one threaded reply and one edit of that reply reports one.

A required depth above zero would therefore reject ordinary complete pages on Tuwunel while proving nothing on Synapse, so the portable requirement is only that a non-empty page reports the field at all.

That requirement still catches the failure worth catching, which is a server that ignores `recurse` and silently returns direct children only, because such a server omits the field.

An empty relation page reports no depth on Tuwunel and must not be treated as a failure, since it has nothing that could have been truncated.

Both homeservers deduplicate a repeated transaction ID per sending device rather than per access token, and MindRoom persists its device across restarts, so deterministic outbox retries survive a crash but would not survive re-login with a new device.

Synapse expires stored transaction mappings on a periodic cleanup, so the real-server proof must record how long a deterministic retry stays idempotent rather than assuming it is unbounded.

## Feasibility Proof Before Production Cutover

The first implementation work occurs on an isolated prototype branch that is not separately merged into `main`.

It proves the risky primitives without wiring a second production path into MindRoom.

If it passes, its implementation and proof harness become the starting point of the single MindRoom implementation PR rather than being rebuilt.

If it fails, the branch is discarded without adding unused architecture to `main`.

### Store and projection proof

The prototype must demonstrate:

- Principal isolation through bound store views.
- Exact journal deduplication and pending replay on SQLite and PostgreSQL.
- Correct ordered and shuffled edit reduction, including pre-original cross-sender edits.
- Current-edit redaction restoring the latest remaining server revision without retaining previous bodies.
- A durable refresh token surviving restart, strict reads waiting for it, and refetch failure serving no stale content.
- A newer edit racing point refetch and winning through conditional installation.
- Non-strict reads omitting a body-cleared message instead of returning the redacted revision, on every read path, including across a restart with the refetch still pending.
- Bounded cursor reads over a 100,000-message room conversation.
- One indexed projection update per edit and no retained intermediate edit chain.
- Zero SQLite lock failures under 50 concurrent Matrix conversations with an explicit `busy_timeout`.

### Crash proof

The harness must cover these boundaries:

1. Before journal commit.
2. After journal commit but before nio records callback acceptance.
3. After callback acceptance but before the pending worker starts.
4. After durable turn creation but before model execution.
5. After the model returns but before its result is durable.
6. After outbox enqueue but before claim commits.
7. After claim commits but before network I/O.
8. After Matrix accepts the transaction but before acknowledgement is stored.
9. After acknowledgement but before journal settlement.

Every case must produce one terminal turn and at most one visible response.

Cases five through nine must execute the model exactly once.

### Real-server proof

The manual harness must run against both Tuwunel and the MindRoom Synapse fork.

It must prove:

- The realistic restart case where bounded `/messages` exhaustion returns an empty chunk and omits `end` still recovers and replies once.
- Cold history populates context and never starts a turn.
- A root, reply, edit, and redaction relation tree reports `recursion_depth` and supplies every indirectly related event.
- Redacting the latest edit reveals the prior unredacted edit when the server still retains it and reveals the original body after superseded edits have been purged.
- Edit-heavy streaming leaves one latest logical message without durable intermediate bodies.
- Deterministic retry after server acceptance creates one Matrix event.

**Run against two live homeservers — Tuwunel and Synapse — with every check passing on both.** `tests/manual/event_journal_live_proof.py` creates a disposable instance, registers throwaway accounts, and removes the stack afterwards, so it can be re-run at any commit without setup.

Running it on a second server was worth more than the redundancy suggests: it immediately caught that the harness was asserting a Tuwunel-specific answer (`recursion_depth < 3`) rather than the property MindRoom actually depends on. That assertion could never have failed on the server it was written against. It is now a measurement, reported rather than asserted, so the harness records what each server does instead of encoding one server's behaviour as a requirement.

| Assumption | What the live server actually did |
| --- | --- |
| Recursive relations report a depth | A non-empty recursive page reports `recursion_depth`, a two-level tree reports the tree's depth rather than a fixed capability, and an impossible requirement is refused rather than silently met |
| Redaction reveals the prior revision | The newest edit is visible before redaction, the redacted revision is unreadable, the message is reported as owing a refetch, and the point refetch installs a revision the server still holds |
| Streaming leaves one row | 25 edits produced exactly 1 logical row, carrying the newest body |
| Sidecars stay unresolved until fetched | 0 rows served before resolution; resolving installed the whole 8704-character body and the preview never reached a reader |
| Retry after acceptance is idempotent | A repeated transaction ID returned the *same* event ID, and deduplication proved to be scoped to the sending device -- the same user, re-logged in as a second device, reusing the ID creates a new event |
| Cold history is context only | An exhausted walk completed hydration, populated the conversation, and started **0** pending events |

Two of these are load-bearing for contracts elsewhere in this file. The transaction-ID result is the assumption contract 8's "resend rather than re-derive" decision rests on, and it is now measured rather than argued. The zero-pending cold-history result is contract 11's premise: nio's `HISTORY` classification really does arrive as context-only work.

**Do not read that premise more broadly than it is stated.** `CONTEXT_ONLY` is assigned in exactly two cases (`matrix/journal_ingress.py:118-121`): `TimelineEventProvenance.HISTORY`, and `m.notice` at any provenance. Old messages are not context-only because they are old. A message that predates a gap but arrives on an ordinary live timeline — as it does when a replacement bot joins a room and syncs normally — is admitted `ACTIONABLE` and suppressed further down by policy, settling `intentionally_ignored`. Both routes end in no turn, so the observable is the same, and a test that asserts the *mechanism* rather than the *outcome* will read zero on the live-timeline path and look like a regression. The live restart-regression profile reports its `context_only` count rather than asserting it for exactly this reason.

**Also run against Synapse: 22 required checks, all pass.** Two servers was the right requirement — it caught something one server could not.

The harness had asserted `recursion_depth < 3`, which is true on Tuwunel and false on Synapse, so it failed there. The field means different things in the two implementations: Tuwunel reports the depth of the deepest event it actually returned, so a two-level tree reports 1, while Synapse reports the constant 3, the depth it is willing to traverse, whatever the tree looks like. Both readings of the field name are defensible.

MindRoom was already right about this. `_REQUIRED_RECURSION_DEPTH` is 0, and the comment at `matrix/conversation_hydration.py:54-66` states the divergence with exactly the numbers the live runs measured. No numeric floor above zero is correct on both servers, so the requirement MindRoom enforces is that a depth was reported *at all* — meaning the server honoured `recurse` rather than silently returning only direct children, which is the failure that would quietly lose every edit hanging off a threaded reply.

So the defect was in the harness, not the runtime: it asserted one server's answer as a contract, which made it fail on the other and therefore never be run there — the opposite of what a two-server proof is for. That check is now a measurement (`Findings.note`), reported without a verdict.

Run first against the upstream `matrixdotorg/synapse:latest` image the local dev stack uses, and then against a build of the MindRoom Synapse fork itself: `ghcr.io/mindroom-ai/synapse:latest`, digest `sha256:fc4d4b8e50f1172d973b53179064dfe93d90070435d9a4642f8824b11f9471ff`, revision `69c5aa228f6b5130529863d43a43a4409dbfab82`, published by the fork's own Docker workflow.
All 22 required checks pass on the fork, with `mindroom_compact_edits_enabled` both on and off, and the fork reports the same `recursion_depth=3` as upstream.

The fork's delta is compact-edit collapsing, and the earlier claim that the edit-churn check exercises it was wrong. That check admits each edit with `room_get_event`, which the fork does not touch; collapsing applies to `/sync`, Sliding Sync, `/messages` pagination and `/context`. The path where the delta can actually reach the journal is cold hydration, which walks `/messages`. Measured on the fork: 25 edits to one message return 25 `m.replace` events from `/messages` with collapsing off and 1 with it on, and hydration lands on one logical row carrying the newest body either way. The projection is invariant to the delta because it already reduces an edit chain to one row, so collapsing removes work it was going to discard rather than information it needed.

Manual integration scripts follow the repository's existing `tests/manual/` convention.

### Recorded feasibility decision

The prototype is built, and `tests/manual/event_journal_measurements.py` reproduces every number below on demand.

Measured on `macOS-26.5.2-arm64` with Python 3.13.10:

Read measurements are taken against a conversation of 100,000 messages, walked to its end over 2,001 cursor pages, because an index that only helps the newest page would look fine over a small table.

| Measurement | Result | Target |
| --- | --- | --- |
| Durable admission, p95 | 0.14 ms | under 50 ms |
| Bounded conversation read, p95 | 0.16 ms | under 50 ms |
| Deepest cursor page, at 100,000 messages | 4.44 ms | no degradation with depth |
| Cursor pages walked | 2,001 | reaches the start of the conversation |
| Writer-queue wait, p95 | 7.4 ms | under 100 ms |
| SQLite lock failures, 50 concurrent conversations | 0 | zero |
| Concurrent admission throughput | ~9,600 per second | not set |
| Conversation read query plan | covering index `visible_messages_page` | indexed |
| Pending replay query plan | index `journal_events_pending` | indexed |
| Database size per message | 636 bytes | not set |
| Replacement source | 4,589 lines (the earlier 3,654 omitted `journal_dispatch.py` and `matrix/outbound_projection.py`) | smaller than replaced |
| Replaced owners | 15,870 lines | — |
| Projected net change | −12,216 lines *projected*; the branch today is **+2,624** | materially net negative, once the old owners are deleted |

The nine crash boundaries all produce one terminal turn and at most one visible response.
Enqueueing is what makes an answer durable, so boundary five costs a model run and every boundary after it re-uses the stored payload instead of asking the model again.

The live-server proof passes against a disposable Tuwunel, covering relation traversal, redaction of the currently visible edit, edit churn, deterministic transaction reuse, device-scoped deduplication, and bounded history exhaustion.

No compatibility facade, second writer, retained edit chain, or second recovery classifier was needed, so none of the stop conditions were triggered.

The remaining risk is not in these primitives; it is in the ingress restructuring the cutover requires, because coalescing, deferred turn settlement, and streaming currently run as background work that the pending worker would need to own.

### Resolved: live messages classified as room history

**Resolved.** With the nio fix pinned, the live Tuwunel fuzz at 45 concurrent conversations passes on seed 42: 200 operations, 45 roots, 27 batches, 123 canonical agent replies, one restart, zero lost replies, zero event-loop stalls.
That is the configuration that previously failed on this branch *and* on `main`, which is what identified the defect as pre-existing rather than introduced here.

The investigation that got there is kept below, because the first hypothesis was wrong in a way worth remembering.

The live Tuwunel fuzz at 45 concurrent conversations still loses replies, and the cause is upstream of everything this plan owns.

Durable evidence, read from the journal at the moment of the stall: the unanswered source events are present in both principals' journals with `event_class = context_only`, meaning nio reported `TimelineEventProvenance.HISTORY` for messages a user had just sent.
MindRoom then correctly declines to answer them, because this plan requires it to consume nio's provenance and never re-derive it.
Nothing is pending, no admission failed, and the event loop is healthy — the bot simply owes no work.

Two distinct causes were found.
The first is fixed: the harness sent its scenario before the agent's first sync completed, and everything in an initial sync timeline is history by design.
The scenario now waits for a warm-up reply, and the seed phase passes.

The second is now fixed in the fork, and the hypothesis above was close but named the wrong mechanism.

Mid-session a sync timeline event is always `LIVE`, so `HISTORY` can only arrive through the gap backfill nio runs when a timeline is `limited`.
Continuity for such a gap is proven by `target_reached or bounded_exhausted`, and the exact-token comparison is only the first of those.
The second never fired: `bounded_exhausted` requires `gap.membership_bound`, and `plan_sync_response` passed a `cursor_token` while leaving that flag at its default.
The sliding path sets it, from the very same cursor.
So on the classic path — the default `MatrixSyncConfig.mode` — a backfill that ran to the start of the visible history answered with no end token, proved nothing, and left every recovered event classified as history.

The classic gap is bounded at both ends: the target token above, and the `since` of the sync that opened it below, since everything at or before that arrived in an earlier response.
`plan_sync_response` now derives `membership_bound` from the same cursor it already passes, which is what the sliding path does.

One nio test pinned the old behavior and was changed deliberately, because the two errors are not symmetric.
Classifying already-seen history as `RECOVERED` costs nothing — the journal recognises the event ID and admits it as a duplicate, producing no turn.
Classifying a missed message as `HISTORY` is unrecoverable: it is admitted context-only and the reply never happens.

This was a message-loss bug in the current system rather than a regression introduced here; it reproduces on `main`.
The fix is `mindroom-nio` commit `0f2c318`, which shipped in **0.37.0** and now resolves from PyPI; the `[tool.uv.sources]` branch pin that carried it in the meantime is gone.

Removing that pin also unbroke the Docker images, which is worth recording because the failure looked unrelated to this work.
The builder stage runs `uv sync --locked --no-install-project --no-dev` in an image that has no `git` binary — git is installed in the *runtime* stage, for knowledge-base cloning — so a git source dependency failed with "Git executable not found" on all four build targets while every Python job stayed green.

### Resolved by deletion: a rejected revision is never reconsidered

**Closed — the mechanism that could produce it no longer exists.** Seeding is gone: there is no `matrix/outbound_projection.py`, no seed writer, and no "own echo" branch in `event_journal/projection.py`.
The sync echo is now the only route into conversation content, so every revision the projection holds was already ordered by a server before it was written.

`_project_edit` is consequently a monotone reduction over server-stamped keys: an edit installs only if `_is_newer((event.origin_server_ts, event.event_id), (current.revision_ts, current.revision_event_id))`, and `_held_edit_yields_to` applies the identical rule to an edit waiting for its original.
`(timestamp, event_id)` is a total order, so the reduction is order-independent — a lower-stamped edit arriving late simply loses, which is the correct outcome rather than a discarded winner.
Nothing can canonicalize a revision *backwards*, so no loser ever needs restoring, and the two expensive options this section was weighing — retaining rejected revisions per logical message, or re-deriving a message from the journal on a backwards move — are both unnecessary.

The federation correction below remains true as an observation about `origin_server_ts`, and it is worth keeping for that reason; it just no longer describes a defect, because it took a locally-stamped seed to turn a late low-stamped edit into a lost one.

The original analysis is kept below for the reasoning, not as open work.

---

Largely fixed, with one case left and a correction to how it was scoped.

The wall-clock part is done: a seeded edit is assigned an ordering key one past the revision it
replaces rather than this machine's clock, so it can no longer claim the future. Every ordering in
which the seed precedes the later edit now reduces correctly.

What remains is the reverse: a genuinely later edit `L` arrives *before* the seed, the seed
replaces it (correctly, as the newest thing this bot knows), and the seed's own echo then
canonicalizes down to a timestamp below `L`. `L` was discarded when it lost, so nothing restores
it. The projection is a reduction and keeps no losers, which is what makes this hard to fix
locally — the honest options are to retain rejected revisions per logical message, or to re-derive
the message from the journal when a canonicalization moves a revision backwards.

**Correction to an earlier claim in this file.** These orderings were previously dismissed as
physically unreachable, on the reasoning that a homeserver stamps our send after everything it has
already delivered to us. That holds for a single server and fails under federation:
`origin_server_ts` is set by the originating server, so a remote server can stamp `L` at 3000
while ours stamps our edit at 2000, and `L` can arrive first. The dismissal was wrong and the
orderings are reachable in any federated room.

### Superseded note: a provisional revision timestamp outranks real ones

Found by enumerating all twenty-four orderings of {seed edit, edit echo, original, later edit}
rather than the orderings a review happened to report. Sixteen of the twenty-four end with the
answer frozen at the earlier edit. The probe is kept at `edit-ordering-matrix.py` in this
session's scratch directory and runs in about a second.

The four fixes made so far each closed one ordering and none closed the class. The class is this:
a seeded revision carries this machine's clock, and that value competes on equal terms with server
timestamps in `_is_newer`. When the local clock is ahead — which is the whole reason the
provisional bit exists — a seeded edit outranks a *genuine later* edit that has already arrived.
The echo then canonicalizes the seed down to its real timestamp, but the later edit was rejected
when it arrived and nothing revisits that decision, so the projection keeps the older body
forever.

Concretely, `later_edit -> seed_edit -> echo_edit -> original` ends on `$e1 "half"` when `$e2
"complete"` is the newest thing the server ever saw.

The same-ID and direction rules do not help here, because these are two *different* edits. What is
wrong is using a provisional timestamp as an ordering key at all.

Two candidate fixes, neither implemented:

- Seed with an ordering key that cannot claim the future — derive it from the revision currently
  installed rather than from the wall clock, so a seed is newer than what it replaces and nothing
  more.
- Keep a seeded revision out of the ordering comparison entirely: install it as the visible body
  but leave the authoritative ordering key untouched, so a later real edit still wins.

The first is smaller. Either needs the full twenty-four-ordering probe as its acceptance test,
because this defect has now survived four fixes that each looked complete against the ordering
that prompted them.

### A failure mode this work kept hitting

Three separate fixes in this design over-corrected past the defect they were aimed at, each time
producing something worse than the original:

| Reported defect | The fix | What it actually did |
| --- | --- | --- |
| A rejoin could resend a stale answer | Bind the transaction ID to the membership epoch | Turned a suppressed duplicate into a visible one — deduplication was the mechanism, not the failure |
| An echo could freeze a streamed answer | Treat a matching revision ID as a canonicalizing echo | Identity has no direction, so a late seed overwrote authoritative data with a local clock |
| An empty page with a continuation was read as exhaustion | Raise when the token stops advancing | A homeserver at the start of its history returns exactly that shape, so a normal room became permanently unhydratable |

Each was reasoned from the one ordering that had been demonstrated.
The lesson is cheap to apply: before committing, state the mirror-image input — the same events reversed, or the benign server response with the same shape — and say which way each choice fails.
Prefer the direction that degrades over the one that errors, because hydration and delivery both run once per something and a hard failure there is not self-healing.
Then write the test for the opposite input, not the reported one.
A twenty-line probe against a temporary SQLite store settled all three in under a minute each, and in every case the probe disagreed with the reasoning.

### Why a failed release re-raises, and what that costs

`AgentBot.stop()` runs all three releases, collects their failures, and then raises the first.
Both halves are deliberate and were arrived at by getting each one wrong first.

Running all three matters because the resources are independent: a faulted lane must not prevent
the store and the client from being released. Re-raising matters because of what the caller does
with success. `stop_entities` awaits `asyncio.gather(*stop_tasks)` and only then pops the entities
from `agent_bots`, so a clean return is taken as proof the bot is gone and a replacement is
started on the same database under the same principal. Swallowing turned a safe halt into a silent
double-open.

The cost is worth stating: because the pop loop runs after the gather, one bot's cleanup failure
blocks removal of every bot in that batch, including ones that stopped cleanly. That is the
pre-existing shape of `gather`-then-pop rather than anything the journal introduced, and the
conservative outcome — a reload that aborts rather than one that proceeds on a half-closed store —
is the right one to prefer while the journal is the thing being torn down.

Two known imperfections, neither fixed: only the first failure is raised, so later ones survive
only in the log, where an `ExceptionGroup` would carry all of them; and `gather` propagates on the
first exception while the remaining stop tasks are still running, so their releases are not
awaited. Both are worth revisiting when shutdown is next restructured.

### The 45-thread live harness is not a pass/fail gate on one run

Observed across this session, all on the same code and all at 45 threads: seed 42 passed, seed 7
passed, seed 42 failed with one lost reply then passed on re-run, seed 42 passed, seed 7 failed
with four lost replies then passed on re-run with identical counts. That is two spurious failures
in seven runs, roughly thirty percent.

Both failures showed the same signature -- replies missing at live batch 0, with
`coordinator_queue_wait_ms` and `thread_read_total_ms` in the seconds -- and neither log contained
any error from the code under test. Twelve-thread runs have not flaked at all.

So a single 45-thread failure is not evidence of a regression, and a single 45-thread pass is not
evidence of its absence. Treat the harness as a signal that needs repetition: re-run the same seed
before concluding anything, and grep the failing run for errors from the paths you changed before
assuming they are implicated. The cheap discriminator is that a real regression reproduces on the
same seed; a flake does not.

### Feasibility decision method

Before any production cutover, record the prototype's source size, database size, admission latency, writer-queue latency, bounded-read latency, query plans, lock failures, and crash results.

Proceed only if all correctness tests pass, no room scan is required after hydration, and the replacement has a credible path to removing substantially more production code than it adds.

Use the measured prototype size to set a written source-growth budget before opening the MindRoom implementation PR.

If the prototype needs compatibility facades, multiple writers, retained edit chains, or a second recovery classifier, stop and reject this design.

## Boundary Contracts

These tighten the ownership boundaries before any further cutover work.
Each one was checked against the working prototype rather than assumed; where the prototype already violates a contract, the violation is named with its evidence.

### 1. The projection is a prompt view, not a Matrix replica

It is a bounded, recent, latest-visible view whose purpose is prompt construction.
It gets no gap repair, no certification, no periodic scan, and no unbounded export API.
A genuine full export paginates Matrix directly instead.

### 2. Ownership transfers at durable handoff

The journal owns an actionable source only until its normalized, coalesced turn is durably adopted by `TurnStore`.
At that point the source is settled and compacted; `TurnStore` owns execution and result, and the outbox owns delivery.
Keeping the source pending through model execution and delivery is what forces cross-store coordination to exist at all.

**Prototype violation.** The source stays pending until the turn is terminal, which is why `release_terminal_turn_sources`, `_terminal_sources`, and `turn_is_terminal` exist, and why settlement has to be reachable from a non-loop thread.
Moving the handoff earlier should delete all three.
Media coalescing is the case that must be proven, because a batch's turn is adopted after a debounce that spans several sources; if that cannot be expressed as one durable adoption, the missing handoff gets written down rather than patched with another repair state.

### 3. The journal retains no raw history

A `CONTEXT_ONLY` event is projected transactionally and then keeps only enough identity to deduplicate.

**Prototype violation, measured.** Such an event is inserted with `state = settled` and its full `source_json`, and `settle` only clears rows `WHERE state = 'pending'`, so the payload is never compacted.
A probe stored a single context-only event and read back `state=settled outcome=None source_json_bytes=558`.
Left alone, the journal becomes exactly the raw-event cache this plan exists to delete.

### 4. Streaming progress is transport-only

Persist the initial logical event identity and the terminal visible body.
Do not write every self-authored intermediate edit into the projection, and do not let the sync echoes of those edits reduce into it either.
User-authored edits still reduce normally.
Without this, one streamed answer costs one projection write per progress edit, twice.

### 5. Acknowledged sends are provisional until the echo canonicalizes them

A Matrix send acknowledgement carries `room_id` and `event_id` only, so it cannot supply the authoritative `origin_server_ts` the projection orders by.
A durable acknowledged send is therefore seeded as a provisional row, and the sync echo replaces its ordering metadata with the server's.
`_project_original` currently inserts `ON CONFLICT DO NOTHING`, so it cannot perform that reconciliation and must change.
Intermediate streaming edits stay out of this path entirely.

### 6. Hydration is defined by the prompt window

Hydration fetches enough recent logical messages to fill the largest prompt the runtime will build — not an arbitrary page count, and not the whole room.

The hydration marker means the one-time walk ran to completion, which is a weaker statement than "the window is full", and the difference is deliberate.
The walk ends when the window fills, when the room is exhausted, or at a raw-event ceiling, and only the first two mean the window was filled.
The ceiling case is logged and still marks the conversation hydrated, because withholding the marker would re-run a twenty-thousand-event walk on every read of that room — a worse outcome than a prompt with less history than its maximum.

**Prototype violation.** `hydrated_from_ts` records the floor a bounded walk reached, but nothing reads it: no caller extends a read past it.
It therefore describes partial completeness without supporting it, which is the same overstatement it was added to fix.
Either incremental hydration is implemented deliberately, or the column and the promise go.

**Second prototype violation, found after the first fix.** Deleting the floor left the window measured in pages of raw Matrix events.
That is the wrong unit by an order of magnitude in this product specifically: a streamed answer is one original followed by a long run of `m.replace` edits, and all of them reduce to a single line in a prompt.
A fixed page budget therefore called a few dozen messages a two-thousand-message window in exactly the rooms MindRoom creates.
The walk now counts logical messages and keeps a separate raw-event ceiling, whose only job is to stop one pathological room from being walked end to end; reaching it is logged, because it is the one exit that does not mean the window was met.

### 7. Exactly one exceptional history repair

Point refetch after the currently visible edit is redacted, and nothing else.
It uses the shared relation reducer, is conditional on both membership epoch and refresh token, and has no background worker.

### 8. Membership epochs fence every derived and pending room fact

Visible projection, resolved sidecar plaintext, approval-card projection, hydration, and pending deliveries are all fenced.
An outbox entry that was never attempted must never deliver into the membership that follows a rejoin.

That invariant used to be stated without the qualifier, and the qualifier is load-bearing.
An *attempted* row has an outcome only the homeserver knows, and the two branches want opposite things.
If the send was accepted, the answer is already in the room and the only convergent move is to resend the identical transaction so it collapses onto the same event.
If it never arrived, resending delivers an old membership's answer into the new one.
Nothing in the row distinguishes them, so no rule satisfies both.

The choice is to resend, and the reason is that the failure modes are not equally bad.
Resending when the send never arrived answers a question that really was asked, slightly late, in a room the bot has rejoined.
The alternative — deriving a fresh transaction, or refreshing the payload under the existing one — either posts the answer twice or leaves the durable record and the room permanently disagreeing about what was said, which is the exact failure the outbox exists to prevent.

Phase 3 must pin both branches explicitly: accepted-but-unacknowledged before a rejoin, and never-received before a rejoin.
The second test will assert the stale delivery, because that is the deliberate cost, and an unpinned deliberate cost is indistinguishable from a bug.

Dropping the pending rows is only half the fence, and the half that is easy to mistake for the whole of it.
The delivery that matters is the one that was claimed and sent, whose network outcome is unknown: the turn behind it is still pending, so it runs again in the new membership.

The first attempt at this fence deleted that row too, and bound the Matrix transaction ID to the membership epoch so the turn's next attempt could not be deduplicated away.
That was wrong, and the reasoning behind it was wrong in an instructive way.
It treated deduplication as the failure, when deduplication is the mechanism: if the homeserver accepted the first send, the answer is *already in the room*, and collapsing the retry onto the same event is what leaves exactly one of it.
Giving the retry a fresh identity turns a suppressed duplicate into a visible one.

The fence is therefore drawn by `attempted`, not by acknowledgement.
An unattempted row is deleted: nothing outside this process has seen it, and sending it would answer the previous membership inside the new one.
An attempted row is kept, with its frozen payload and its transaction, so the only thing a retry can do is present the same transaction again.
That converges on one visible answer whether or not the first attempt landed, and the end-to-end test asserts the room's message count rather than the identity of a transaction.

### 9. One backend, several narrow views

`PrincipalStore` currently exposes 28 methods covering journal, membership, conversation, hydration, refresh, and outbox operations.
That is the shape of the next universal cache dependency.
The shared transaction and backend stay; runtime code receives narrow principal-bound journal, projection, and outbox views instead of the whole surface.

### 10. Special facts stay specialized

Resolved long-message plaintext belongs to the visible revision and dies with it.
Tool approvals get their own small projection.
The generic conversation projection is not widened into an arbitrary event lookup.

### 11. Recovery classification stays in nio

MindRoom never infers `LIVE`, `RECOVERED`, or `HISTORY` from tokens, timestamps, membership repetition, or server behavior.

### Contract status after the correction pass

| Contract | State |
| --- | --- |
| 1 projection is a prompt view | Held; no certification, scan, or export API exists. "No repair" is too broad as written: contract 7's point refetch is one, deliberately |
| 2 ownership transfers at durable handoff | **Corrected, then done** — see below. The handoff is durable outbox enqueue, and enqueue-and-settle is now one backend transaction (`store.py:626` calls `journal.settle_many` inside `_enqueue_delivery`), so there is no window in which a turn is delivered but its source is still pending, or settled with nothing owing it. `turn_is_terminal`, `_terminal_sources`, and `release_terminal_turn_sources` no longer exist anywhere in `src/` or `tests/` |
| 3 no raw history in the journal | **Done.** Context-only events now store identity only; probe previously read `source_json_bytes=558` on a settled row |
| 4 streaming progress is transport-only | **Done, and the gap was narrower than recorded.** One pure predicate in `matrix/transport_progress.py` refuses a self-authored `m.replace` whose `visible_content` carries `pending` or `streaming`, applied from both `projected_event` and hydration's `_projected_from_event`. Originals, terminal revisions of every kind, and other senders' edits all still reduce. **Correction to the previous entry:** live admission was never the expensive half. MindRoom sends in-progress updates as `m.notice` so they raise no push notification, and `_event_kind` owns `m.text` only, so a progress echo was already refused a kind before the projection saw it. Hydration has no such filter, fetches the whole relation tree, and did reinstall every progress edit on the first cold read of a room — which is why the rule had to run in both places rather than only at ingress |
| 5 acknowledged sends are provisional | **Superseded and deleted.** Seeding landed and was then removed: the sync echo is the only route into conversation content, so nothing is provisional because nothing is written before the server has ordered it. The ordering hazards this contract existed to manage are gone with the mechanism, and the tests that pinned them went with it. The cost -- a turn that reads the conversation immediately after speaking does not see its own message -- is recorded under the seeding audit |
| 6 hydration is the prompt window | **Held, for both walks.** Room hydration: the unused floor is gone, the window counts logical messages rather than pages of events, and the ceiling case is logged as a completion rather than a full window. Thread hydration now carries the same bounds, adapted rather than copied — `_fetch_relations` counts a logical message only when `replaces_event_id is None`, so the window's unit matches the room walk's, and `max_fetched_events` bounds the raw relation tree that streaming makes an order of magnitude larger than the message count. The root is kept over and above the window, because a thread starting at its first reply is missing the message it is about. The request ceiling has no thread counterpart: `room_get_event_relations` is one call that nio paginates internally, yielding events rather than pages.<br><br>The edit-tail invariant is what makes early truncation safe, and it rests on ordering. The walk asks for `direction=back` explicitly rather than inheriting nio's default: under MSC3981 the server returns relations in the topological order `/messages` would give, and an edit is sent after the message it revises, so every edit arrives *before* its original. The window may therefore only stop at the moment a logical message was just admitted — its whole edit tail is already collected. The event ceiling can stop mid-message, and under this order that is the harmless direction: it drops an original and keeps edits nothing will claim, rather than keeping a message at a stale revision |
| 7 one exceptional repair | Held; point refetch is the only one |
| 8 membership epochs fence pending facts | **Wired, and being hardened.** `MembershipFence` (`event_journal/membership.py`) advances the epoch at both transitions: immediately on a local leave, and for sync-reported departures. The exactly-once rule is the substance -- one departure arrives twice and the obvious guard, `bot._local_departures_awaiting_sync`, is cleared by `_on_room_joined`, so a rejoin between a leave and its echo would let the echo fence a second time and delete the conversation just hydrated under the new membership. The fence keeps its own record that a join does not clear.<br><br>Review then found the in-process record is not enough: an advance that raises leaves the marker set and the echo is swallowed, giving the departure **zero** fences; a restart between the fence and its echo loses it; two leaves before one echo need two markers and have one; and a marker whose echo never arrives swallows a later genuine departure.<br><br>**Both halves have since landed.** The exactly-once record is durable and atomic with the advance: `fence_departure(room_id, source=LOCAL\|REPORTED)` returns a `DepartureOutcome`, and `rooms_owing_departure_reports` / `retire_owed_departure_reports` carry the owed-report set across a restart instead of an in-process `set`. `bot._local_departures_awaiting_sync` still exists but no longer gates anything: `fence_reported_departures` is called unconditionally (`bot.py:1778`), and the set survives only to subtract locally-left rooms from the joined set handed to the call manager — a transport view, not the fence.<br><br>The in-flight turn is fenced at enqueue. `_enqueue_delivery` consults `_turn_membership_is_current`, comparing the epoch that admitted the turn against the room's current one, and refuses to write the row when they differ. Both are single write transactions against a serialized writer, so the only two orderings are "enqueued, then deleted by the fence" and "fenced, then refused" — neither leaves an answer addressed to a membership the bot has left |
| 9 narrow views, one backend | **Held, and now with no whole-store consumer left.** Eleven structural protocols in `event_journal/views.py` — `AdmissionView`, `ReplayView`, `DispatchView`, `PendingTurnView`, `RelationView`, `PointLookupView`, `ProjectionView`, `ConversationReadView`, `HydrationView`, `OutboxView`, `ApprovalView` — plus `MembershipView` in `event_journal/membership.py`, for twelve. Each collaborator takes the slice it calls. Enforcement is the type checker: a hydrator reaching for `enqueue_delivery` fails `ty` before any test runs.<br><br>The last exception was thread export, whose three collaborators each took a narrow view while the factory that built them took the whole `PrincipalStore` — for want of a name for the union, not for want of a boundary. `ExportProjectionView` composes the three slices export actually reads, so `grep -rn ": PrincipalStore" src/` now returns nothing outside `event_journal/` itself. The composition is the same shape `DispatchView` already used, and it cost one `depends_on` entry and one visibility entry in `tach.toml`, both declared because the Protocol base classes are a runtime import rather than a `TYPE_CHECKING` one |
| 10 special facts stay specialized | **Done.** Resolved sidecar plaintext belongs to the visible revision: the projection refuses to store an unresolved preview and records the refresh debt instead, and hydration resolves the one current revision. Approvals have their own `approval_cards` table behind `ApprovalView`, holding only the cards this bot authored and owes a decision on, fenced by membership epoch. The generic projection was not widened for either |
| 11 classification stays in nio | **Held for the timeline; not held for room-lifecycle events.** Ordinary ingress maps nio provenance directly, with no local inference. Room-member handling does not: `room_member_sync_state_plan` builds `limited_room_ids` from `join_info.timeline.limited` and consults `prev_membership` to decide which state events dispatch, `room_member_sync_timeline_events` calls restored-token timeline events "eligible live joins" without consuming provenance, and `bot.py` then hard-codes both groups as `EventClass.ACTIONABLE`. That is literally the inference this contract forbids.<br><br>The reviewer's remedy — feed nio provenance into room-lifecycle admission — was checked against the fork, and it **splits in two**. Provenance is attached only to timeline events: `record_completed_timeline_event` is called from the timeline walk, and `EventAdmissionCallback` is typed `[MatrixRoom, Event, TimelineEventProvenance]`. `RoomInfo.state` carries none.<br><br>So: **state-block member events cannot consume provenance**, because none exists for them. For those, the honest move is to scope this contract to the timeline and give room-lifecycle actionability its own stated rule, rather than pretend a nio-owned classification is available. **Timeline member events can and should**: `room_member_sync_timeline_events` re-derives "eligible live joins" from the raw response even though nio already hands provenance to the admission callback for exactly those events.<br><br>**The mechanism, traced.** Two paths admit the same member events. Journal admission classes them from provenance and runs first, because `add_event_admission_callback` installs ahead of every other callback. `_emit_room_member_joined_sync_timeline_hooks` then walks the same response and calls `admit_and_run(..., ACTIONABLE)`. Admission is `ON CONFLICT DO NOTHING`, so where both run, the provenance-derived class wins and the hard-coded one is inert.<br><br>The bug is that **the two paths are gated differently**. Journal admission needs `agent_name == ROUTER and _first_sync_done and _room_member_join_hooks_armed`; the hook walk needs only router plus `has_hooks`. In the window where the hooks are unarmed, the hard-coded `ACTIONABLE` is the *only* admission, so it wins — and that window is exactly the restored-token catch-up this function exists for. A cold-`HISTORY` member event there is admitted actionable and fires join hooks for joins that happened long ago, which is the membership form of "answering a conversation that ended". Provenance would separate `RECOVERED` (bot was a member, has not reacted yet — actionable, and the behaviour this function wants) from `HISTORY` (cold, context only). The re-derivation cannot.<br><br>Fix by consuming provenance rather than by aligning the two gates: aligning them only narrows the window.<br><br>**Settled, as a split rather than as one rule.** The timeline half consumes provenance: `bot.py:2391` asks `timeline_member_event_class(event)` for each event `room_member_sync_timeline_events` yields, and admits with the class nio gave. When that returns `None` the event is skipped rather than guessed at — nio accepted it on an earlier pass and so said nothing this time, meaning it is already journaled with its true class, and a guess here would settle a recovered join against it permanently. The hard-coded `ACTIONABLE` is gone.<br><br>The state-block half cannot consume provenance, because none exists: `RoomInfo.state` carries no `TimelineEventProvenance`, and `record_completed_timeline_event` is called only from the timeline walk. So this contract is **scoped to the timeline**, and room-lifecycle state events get their own stated rule rather than a pretence that a nio-owned classification is available for them: `room_member_sync_state_plan` may consult `join_info.timeline.limited` and `prev_membership`, and does so to decide *dispatch versus baseline record*, never to label an event `LIVE`, `RECOVERED`, or `HISTORY`. That is a local policy about which state snapshot deserves a hook, which is a different question from where an event came from. Journal admission still runs first and still classes from provenance wherever provenance exists, so the two never disagree about the same event |

### Correction to contract 2: the handoff is the outbox, not TurnStore adoption

Settling the journal source once `TurnStore` durably adopts the turn would lose answers.

Replay is driven entirely by the journal: startup calls `drain_once` over pending journal events, and `TurnStore.cleanup` only retains records for sources the journal still holds.
Nothing replays an adopted-but-undelivered turn.
So at crash boundary four — after durable turn creation, before the model runs — the journal source would already be settled, no outbox row would exist yet, and no owner would owe the work.
That is silent answer loss, and it breaks the first invariant in this plan.

The defensible handoff is **durable outbox enqueue**:

- Boundaries four and five stay journal-owned, so an interrupted turn replays and the model re-runs, which is the cost already documented.
- Boundaries six through nine become outbox-owned, recovered by resending the identical claimed payload under the same transaction ID.
- `turn_is_terminal`, `_terminal_sources`, and the terminal-source callback still disappear, because settlement is triggered by enqueue rather than by asking another store whether a turn finished.
- Turns that never enqueue — commands, router decisions, intentionally ignored inputs — keep settling through the existing intentionally-ignored path.

This was sequenced behind the delivery cutover because `enqueue_delivery` and `ResponseDelivery` had no production call sites, so the settlement point did not exist outside tests.
**That is no longer true** — the outbox is on the production send and edit paths in `delivery_gateway.py`, and `ResponseDelivery.recover` is reached from every sync response.

**Now landed, and atomically.** `enqueue_delivery` takes `settle_source_event_ids`, and `_enqueue_delivery` writes the outbox row and calls `journal.settle_many(..., SettlementOutcome.SUCCEEDED)` in the same transaction.
Atomicity is the substance rather than a detail: two transactions would leave a crash window in which the row exists and the source is still pending — a delivered answer that replays — or the mirror, a settled source with no row, which is the silent loss this correction exists to prevent.
The same transaction also carries the membership fence, so a turn admitted under a departed membership is refused rather than enqueued.
All three symbols the correction predicted would disappear are gone from `src/` and `tests/` alike.

**What `TurnStore` would have to hold before it could own the decision at all.**
Even at the corrected handoff point, the journal payload is the only durable copy of some of a turn's input.
`TurnRecord` persists the anchor, source event IDs, per-source prompts and revisions, `SourceEventMetadata` (sender, timestamp, discovery event), owner, requester, correlation, command result, history scope, and conversation target.
It persists nothing about attachments or media.
A coalesced batch of one caption and three images therefore replays from `TurnStore` as text alone, so any design in which `TurnStore` decides whether an adopted turn runs needs a normalized durable input snapshot first.

Red tests that must exist before contract 2 is implemented, in the order they should be written:

- Crash after the durable turn record commits and before the response task is created.
- Restart when the journal sources for that turn are no longer pending.
- A coalesced batch of text plus several media sources replays with its exact media, and the turn executes once.
  **Done for the media half** (`TestReplayFidelity`): a plain image keeps its MXC reference, an encrypted image keeps the key material that makes the reference openable, and a batch of three images plus a caption replays whole and in receipt order.
  The remaining half is that the replayed batch executes exactly one turn, which needs the handoff to exist.
- A durable-turn persistence failure leaves every journal source pending.
- A crash between adoption and journal settlement deduplicates the handoff instead of running twice.

### Invariants the delivery cutover must carry

These are behavioral contracts the current pipeline satisfies, restated against the owners that replace it.
They are gates on phase 3, not on the phases before it.

| Invariant | Owner after the cutover | State |
| --- | --- | --- |
| A continuation turn produces no final delivery between attempts, and exactly one after the last | Outbox | **Held.** No longer deferred: `enqueue_delivery` has production callers since the delivery cutover, so the assertion is written against the real path rather than a spy. `tests/test_continuation_delivery_invariant.py` asserts `enqueue:final == 1` across a continuation, an exhausted continuation budget, and the team and router variants, plus `enqueue:initial == 1` for the placeholder |
| Once the model result is durable, restart recovery never runs the model again | Outbox | **Held.** Crash boundaries six through nine assert `model_runs == 1` after recovery; boundary five, where nothing is durable yet, correctly re-runs |
| One stop converges to one durable terminal outcome and one visible cancellation | `UserStopReconciler` | **Held.** Pinned with a real `TurnStore` for a single stop, a redelivered stop, and two racing stops |

### What these contracts delete

| Mechanism | Why it goes |
| --- | --- |
| `release_terminal_turn_sources`, `_terminal_sources`, `turn_is_terminal` | Contract 2 moves the handoff to durable **outbox enqueue** — not to durable adoption, which is the design the correction above rejected — so no store needs to ask another whether a turn finished |
| The off-loop settlement wake in `bot.py` | Only needed because settlement happens after model execution |
| Retained `source_json` on context-only rows | Contract 3 compacts at projection time |
| Projection writes for self-authored streaming edits | Contract 4 makes them transport-only |
| `hydrated_from_ts`, unless incremental hydration is built | Contract 6 forbids recording a promise nothing honors |
| The full-surface `PrincipalStore` dependency | Contract 9 replaces it with narrow views |

### Gate check before more cutover work

- One pending-event worker and one outbox recovery path: **holds today.**
- No cache repair, certification, or gap machinery in the replacement: **holds today.**
- No duplicate "should this event run?" authority: **now holds.** Contract 2 landed -- the handoff is durable outbox enqueue and enqueue-and-settle is one transaction -- and the second half followed when turn records moved into the journal database.
  Say what that bought precisely, because the obvious phrasing is false: source settlement and the terminal turn record do *not* share one transaction, and an earlier revision of this row claimed they did.
  The real shape is a chain of ownership across two backend writes with a network send between them: `journal sources -> FINAL outbox intent -> acknowledged outbox plus terminal turn record`.
  Enqueue commits the outbox row and settles every source it answers; acknowledgement binds `acknowledged_event_id` and upserts `turn_records` together, once Matrix has supplied an event ID.
  A crash between the two leaves settled sources with no terminal record, and that state is intended and tested rather than a hole: the unacknowledged row owns the answer, so recovery resends the frozen payload instead of replaying the model.
  The distinction matters for review, not pedantry -- describing it as one transaction sends a reader hunting for a settlement-to-turn-record reconciler that should not exist.
  The startup pass that used to rejoin them (`delivered_turn_repair.py`) is deleted rather than kept, which is the check that this is a collapse and not another reconciliation.
- Bounded prompt reads: **now holds end-to-end.** Every projection read takes a limit, and the thread walk that used to be unbounded behind a strict read now carries the same bounds as the room walk: `_fetch_relations` counts a logical message only when `replaces_event_id is None`, and `max_fetched_events` caps the raw relation tree that streaming makes an order of magnitude larger than the message count. See contract 6.
- Materially fewer production lines: **now holds.**
  Measured at `fa4b3268c`, the merged tip: `git diff --numstat origin/main...HEAD -- src/mindroom` reports +16,728 / -21,825, a net of **-5,097 production lines**. The whole branch is -9,824.
  Re-measure rather than quoting this figure: it moves with every commit, and five earlier revisions of this row went stale without anyone noticing -- including the one this replaces, which claimed -8,258 and was wrong by 3,161 lines by the time the branch merged.
  The recorded history of this row was +2,624, then +2,754, then +3,940, and every step was in the wrong direction -- because the replacements landed as additions while the thing they replace was still standing. It turned when `matrix/cache/` was deleted, which is exactly where the earlier notes said the turn would come from.
  Read the direction of travel rather than the level, though: production has grown **+2,048** since the cache deletion itself, and that growth is real. Some of it is the replacement finishing (the outbox, the projection, hydration), and some is correctness machinery the reviews demanded. Two known deletions are still queued behind other work: `history_debt.py` (187) and `sync_recovery_escape.py` (98) go when `mindroom-nio` ships per-room continued recovery.
  This row was wrong in the optimistic direction twice before. It is stated as holding now only because the deletion is in the diff and the number is reproducible from the command above.
## One-PR Implementation Sequence

> **Executed.** Every checkpoint below has landed. These are records of how each cutover was done
> and why, not instructions to follow, and the code has since moved past several of them. Read them
> for the reasoning; read the source for the current shape.
>
> The divergence a reader will hit first is `ThreadReadMode`. This section describes four values
> (`ADVISORY_FULL`, `DISPATCH_SNAPSHOT`, `DISPATCH_FULL`, `STRICT_FULL`) mapped onto two reader
> methods, and a `mode.dispatch_safe` predicate. Those four collapsed to the two contracts they
> always encoded -- `NONBLOCKING` and `STRICT` -- and `dispatch_safe` is gone with them, because a
> caller either may block or may not and there was never a third answer. The dispatch tables and
> `strict = mode in (DISPATCH_FULL, STRICT_FULL)` swaps described below are how that collapse was
> reached, not how the read path looks now.

### 0. mindroom-nio prerequisite

Land a focused mindroom-nio PR that adds recursive relation query support, parses `recursion_depth`, and enforces an optional minimum recursion depth before yielding page events.

The current MindRoom baseline already pins mindroom-nio 0.36.0, so MindRoom must bump to the first release containing this additional contract.

That release is a hard prerequisite for every downstream hydration change, and MindRoom must not add a fallback when depth is absent or too shallow.

Do not add batch admission or MindRoom storage policy to nio during this work.

All remaining phases occur on one MindRoom branch and in one implementation PR.

They are internal cutover checkpoints, not separately mergeable PRs, and only the final state may be merged.

### 1. Ingress ownership cutover

Introduce only the journal, membership epoch, principal-bound store, admission adapter, and pending worker needed to replace inbound callback durability.

Before the implementation PR can merge, remove dispatch obligations, dispatch admission, the cold-history fence, and settlement retry ownership.

This checkpoint must pass realistic nio restart recovery, provenance mapping, crash replay, authorization, command, media, reaction, redaction, and decryption-failure behavior.

The journal replacement must be net simpler than the ingress owners it deletes.

**Sequencing correction found while starting phase 2.**
Phase 2 cannot precede phase 3, and the reason is the subject of section 3a below.
The cache does not only answer reads; it also writes MindRoom's own outbound messages directly, through `ThreadOutboundWritePolicy`, so a prompt assembled immediately after a send already contains it.
The journal learns of that message only when its sync echo arrives.
Moving reads onto the projection before outbound seeding exists would therefore build prompts that omit the assistant's own last turn — a silent context regression, and exactly the class of thing a passing test suite would not notice.
Phase 2's read work is unchanged, but it runs after phase 3.

**Where phase 2 stands, and the one decision it is waiting on.**

The seam is a single method, `ConversationResolver._read_thread_messages`, whose dispatch table maps four `ThreadReadMode` values onto four cache methods.
Those four collapse onto the reader's two: `ADVISORY_FULL` and `DISPATCH_SNAPSHOT` onto `read`, `DISPATCH_FULL` and `STRICT_FULL` onto `read_strict`.
Only four places construct `ConversationResolverDeps` (`bot.py` and three test modules), so injecting the reader is small.

The adapter that renders a projected page as a `ThreadHistoryResult` is written and tested (kept at `adapter.patch` alongside `test_projected_thread_history.py` in this session's scratch directory, not committed, because the repository rejects a function with no production caller and landing it separately would be exactly that).
It maps `logical_event_id` to `event_id`, `revision_event_id` to `latest_event_id`, `revision_ts` to `edited_timestamp` when the two differ, and derives `stream_status` through the existing `ResolvedVisibleMessage` constructor so a streaming answer cannot read as a finished one.
`is_full_history` is passed in by the caller rather than inferred, because a page that omitted a message and a conversation that never had one are indistinguishable from inside the adapter.

**Decision, now settled: the read limit is the hydration window.**
The worry was that this turns an unbounded read into a bounded one for every caller at once.
Measurement dissolves it: the consumers already truncate far harder than the window would.
Team prompts render `max_messages=30` (`_MATRIX_TEAM_THREAD_HISTORY_RENDER_LIMITS`), and the agent path is capped by `num_history_messages`, both applied in `_context_messages_from_visible_messages` as `messages[-max_messages:]`.
The hydration window is more than sixty times the larger of those, so bounding the read cannot change prompt content in any realistic conversation — it only stops materialising rows that are discarded a moment later.
A caller wanting more than the window was never served anyway, because that is exactly what hydration guarantees and no more.

**The production swap is written and measured. It is held at `read-cutover.patch` in this session's scratch directory, unapplied, because it leaves 44 tests failing and the branch must stay green.**

What the patch contains, all of it verified to import and to work on the resolver's own path:

- `ConversationHydrator` takes the runtime view instead of a client, with a `_client()` accessor identical to the delivery gateway's, which is what makes it constructible before login.
- `_ConversationReader` and `_StaleConversationError` become public; `HYDRATED_PROMPT_WINDOW_MESSAGES` loses its underscore.
- `projected_thread_history` lands in `conversation_reads.py` with four tests.
- `ConversationResolverDeps` gains `conversation_reader`, wired in `bot.py` from the journal store and a hydrator over the runtime view.
- `_read_thread_messages` loses its four-entry dispatch table: `strict = mode in (DISPATCH_FULL, STRICT_FULL)` selects `read_strict` or `read`, bounded by the hydration window.

The 44 failures are one shape, not forty-four: a test seeds thread history through `make_conversation_cache_mock`, the resolver now reads the projection, and the projection is empty.
They fall in nine files — `test_thread_context_resolution.py` (20), `test_turn_controller_focused.py` (6), `test_conversation_resolver.py` (5), `test_multi_agent_bot.py` (4), `test_turn_dispatch_pipeline.py` (3), `test_thread_mode.py` (2), `test_tach_split_matrix_client_boundaries.py` (2), and one each in `test_multi_agent_e2e.py` and `test_live_message_coalescing.py`.
The full list is in `cutover-failures.txt` beside the patch.

Migrating them by stubbing the reader would be the fast route and the wrong one: it would pin the projection's *absence*.
Seed the journal projection instead, through `store.admit(...)`, so these tests exercise the path that will actually run.
That is the next unit of work, and it should be done in one pass rather than interleaved with production changes.

**There are two migration shapes, not one.**
Tests built on a real `AgentBot` have a journal store, so they seed it with `seed_thread_history`
and let the real read path serve them. Tests built on a store-less unit harness -- `_Harness` in
`test_turn_controller_focused.py` constructs a `TurnController` directly with a mocked
`conversation_cache` and no bot -- have nothing to seed. Those get a fake reader returning a real
`ConversationPage`.

That is not the "stub the reader and pin its absence" mistake warned about below: the objection
there is to stubbing a reader that a real store sits behind, so the test passes whether or not
anything reached the projection. A harness with no store at all has no projection to reach, and a
fake reader is the honest analogue of the `conversation_cache` mock it already carries. The v2
patch currently gives these harnesses a bare `MagicMock()`, which will not survive contact with
the adapter -- it needs to return a constructed page.

**One thing that migration has to decide, and it is not mechanical.**
The current expectations are built with `_message(...)`, which calls `ResolvedVisibleMessage.synthetic` and therefore produces `thread_id=None` — even for messages the test fetched *by thread id*.
That was invisible while a mock returned the same objects the test constructed: the assertion compared a value against itself.
A projected row carries the real thread id, so `context.thread_history == expected_history` cannot hold by dataclass equality any more, and no amount of seeding will make it.

The expectations therefore have to change, which means deciding what they should have said.

**Checked against production, because the whole migration turns on it.**
`client_thread_history.py` and `client_visible_messages.py` both build their results with `thread_id=EventInfo.from_event(event.source).thread_id`.
The real read path has always populated the thread; only the test helper omitted it.
So these assertions were comparing the mock's own objects against themselves and said nothing about thread membership at all — the cutover is not breaking them so much as revealing them.

That also makes the migration mechanical rather than a redesign, which the earlier reading got wrong.
Give `_message` a `thread_id` parameter, seed through `store.admit(...)` with `origin_server_ts=0` and `content={"body": body}`, and every remaining field lines up: `synthetic` produces exactly what the adapter reconstructs.
Both pieces are written and included in `read-cutover-v2.patch` (`_message(..., thread_id=...)` and an async `seed_thread_history(bot, room_id, thread_id, messages)` in `threading_helpers.py`).

**`DISPATCH_SNAPSHOT` does change behaviour — but "make it strict" is the wrong fix.**

An independent triage classified two failures as real behaviour bugs rather than harness noise and
proposed making the dispatch snapshot a strict read. The first half is right and the remedy is not.

Reading the test settles it: `test_plain_reply_with_unproven_root_is_not_admitted_under_guessed_key`
injects `TimeoutError` into `get_dispatch_thread_snapshot` and requires that failure to stop a
reply being admitted under a guessed coalescing key. The old snapshot read the *homeserver* and
could time out. The projection read is a local query and essentially cannot, so the injection point
is gone. That is not a lost safety property; it is the property the projection exists to make
unnecessary.

What survives is the real risk, and it has a different shape. A non-blocking read of a conversation
that was never hydrated returns an incomplete page, and treating "no evidence" as "evidence of
absence" is how a root gets judged unproven and a key gets guessed. The failure mode moved from
*the read failed* to *the read was incomplete*.

Making the snapshot strict does address that, by hydrating before deciding — and reintroduces
exactly the homeserver dependency on the dispatch path that this design removes. The snapshot is
deliberately the non-blocking mode.

**Phase 2's production side is complete and verified. `read-cutover-v13.patch` is the state to start from.**

v13 = v8 plus the winning trigger, applied and measured in the real worktree rather than a scratch
copy: **17 full-suite failures, of which one is the known `test_attachments_tool` timing flake**,
so 16 real — matching the independent count. `test_live_message_coalescing` disappears from the
failure list entirely; both behaviour targets are fixed.

The six in `test_thread_context_resolution.py` are simply **not migrated yet** — confirmed, not
inferred. `test_extract_context_plain_reply_to_thread_reply_inherits_existing_thread` builds its
`expected_history` with bare `_message(event_id=..., body=...)` calls and never calls
`seed_thread_history`, so the expectation carries `thread_id=None` and `timestamp=None` while the
projection returns neither.

They were missed because the earlier migration pass was regex-driven and matched only the
`with patch.object(... "get_thread_history" ...)` block shape. These six use other shapes. The six:

- `test_extract_context_plain_reply_to_thread_reply_inherits_existing_thread`
- `test_extract_context_plain_reply_chain_stays_threaded_transitively`
- `test_extract_context_plain_reply_to_promoted_plain_reply_stays_threaded`
- `test_dispatch_room_demotion_clears_source_and_resolved_thread_ids`
- `test_degraded_dispatch_history_uses_strict_history_before_policy`
- `test_full_history_thread_resolution_uses_full_history_to_prove_root`

The fix is the pattern already applied nine times in that file: give each `_message()` its
`thread_id`, then `await seed_thread_history(bot, room_id=..., thread_id=..., messages=expected_history)`
before the call under test. The seeder stamps the ordinals, so expectation and projection agree.

All 16 remaining are v8 fixture migrations with per-test categories in `CODEX_REVIEW10.md`:
6 `test_thread_context_resolution`, 3 `test_turn_dispatch_pipeline`, 2 `test_multi_agent_bot`,
2 `test_thread_mode`, and one each in `test_multi_agent_e2e`, `test_turn_controller_focused`,
`test_tach_split_matrix_client_boundaries`.

The trigger itself:

```python
mode.dispatch_safe
and source_event_id is not None
and await reader.may_have_unread_history(room_id=..., thread_id=..., source_event_id=...)
```

backed by `has_other_admitted_room_event(..., excluding=source_event_id)` on the journal. The
excluding clause is what makes it work: production admits the source event before the callback
runs, so at proof time a real room has another admitted event and a fresh room does not.

**Superseded: resolved, and not by me — the trigger is in `CODEX_REVIEW12.md`, 18 to 16.**

```python
mode.dispatch_safe
and source_event_id is not None
and not conversation_is_hydrated(room_id, candidate_thread_id)
and has_other_admitted_room_event(room_id, excluding=source_event_id)
```

Both target failures gone, no regressions, two new backend contract cases passing. The exact diff
is in that review.

The load-bearing clause is `excluding=source_event_id`. "The room has other admitted events" was a
lead I raised and then **refuted incorrectly**: I checked whether the two tests *seed* anything,
saw neither did, and concluded the axis was empty on both sides. But production admits the source
event before the callback runs, so at proof time the target room does have another admitted event
and a genuinely fresh room does not. I compared test setup where I should have compared runtime
state.

`read-cutover-v11.patch`'s `page.refresh_pending` trigger is still correct and still costs nothing
(18, no regressions) — it is just not sufficient on its own, because a redacted revision is a
narrower condition than an unread room. Whether to keep both is a judgement: `refresh_pending` is
the honest signal for *known-missing* content, and the clause above is the signal for *unread*
content. They answer different questions and the proof path arguably wants both.

**Superseded: the trigger is `page.refresh_pending`.** That was safe but insufficient.

| Attempt | Trigger | Full suite |
| --- | --- | --- |
| baseline (v8) | — | 18 |
| v9 | `complete is False` | 19 |
| v10 | conversation not hydrated | 24 |
| **v11** | **`bool(page.refresh_pending)`** | **18** |

v11 costs nothing and is the only one that is *semantically* right. The two rejected triggers both
tried to infer incompleteness from ambient state; `refresh_pending` is the projection stating it
outright — a message is present but its visible revision was redacted and not yet refetched, so
absence of a child is not proof of absence. A fresh room has nothing pending and stays undegraded,
which is exactly what v10 got wrong.

It does **not** make the two target tests pass, and that is the finding, not a shortfall. They
inject `TimeoutError` into a cache method that no longer exists. A local read cannot time out, so
no trigger can satisfy them as written.

The rewrite is reachable through the ordinary admission API with no mocking, and the recipe is
verified: admit a message, admit an edit of it, then admit a redaction of the *edit*. Probe output
against the real store:

```
messages: []
refresh_pending: 1
```

**The rewrite is started in `read-cutover-v12-test-rewrite-wip.patch` and is not finished.**
It carries v11 plus a `_leave_root_awaiting_refetch()` helper in `test_live_message_coalescing.py`
that admits a message, an edit, and a redaction of that edit through the real store, replacing the
`TimeoutError` injection. The test now reaches its own assertion and fails on
`DID NOT RAISE RuntimeError` rather than on a missing name, so the plumbing is right and the
condition is not yet reproduced in that flow.

Two things to check first, both unverified: whether the event IDs the helper seeds are the ones the
reply actually targets (`root_response()` derives the root from the reply's `in_reply_to`, and the
helper is currently called with a hard-coded `$root:localhost`), and whether the degrade signal
reaches `_thread_messages_root_proof()` on the coalescing path at all, which is worth a print
before more edits.

Two mechanical notes that cost time: `ruff --fix` moves `InboundEvent`/`ProjectedEvent` into a
type-checking block even though the helper constructs them at runtime, so that import needs
`# noqa: TC001`; and the helper must be inserted above the `@pytest.mark.asyncio` decorator, not
above the `async def`.

So the page comes back with the message withheld rather than shown stale, and `refresh_pending`
non-empty — precisely the state v11 degrades on. Build that, drop the timeout injection, and keep
the existing assertion that the reply is not admitted under a guessed key. That assertion is the
behaviour under test and survives the rewrite intact.

So phase 2's production side is now complete in v11. What remains is 18 fixture failures, of which
these two need rewriting rather than repair, and the other 16 are categorised per-test in
`CODEX_REVIEW10.md`.

**Superseded: both candidate conditions are too broad; the marker is right and the trigger unknown.**

| Attempt | Condition | Marker | Full suite |
| --- | --- | --- | --- |
| baseline | — | — | 18 |
| v9 | `complete=False` | flag only | 19 |
| v10 | conversation not hydrated | source + flag | 24 |

v10 does fix the two target tests and `test_history_summary_call`, so the *marker* is settled: the
proof path reads `THREAD_HISTORY_SOURCE_DIAGNOSTIC`, not the boolean. What it gets wrong is the
trigger. Six new tests fail with `ThreadMembershipLookupError: Could not resolve canonical
coalescing thread`, and the reason is straightforward once seen: a brand-new room is legitimately
un-hydrated and legitimately not a thread. Reporting "proof unavailable" there breaks the ordinary
first-message path.

That lead is refuted: both the target test and the one v10 broke use `_make_bot(tmp_path)` and
seed nothing, so "the room has admitted events" is empty on both sides and cannot discriminate.

Which reframes the problem usefully. The old signal was an *explicit* one — the read failed. The
projection has no automatic equivalent, and both attempts so far tried to infer one from ambient
state (strictness, hydration). But the page already carries an explicit statement of known-missing
content: `ConversationPage.refresh_pending`, non-empty exactly when a message is present but its
visible revision was redacted and not yet refetched.

So the next candidate — untested, but different in kind from the two that failed — is
`degraded = bool(page.refresh_pending)`. `projected_thread_history` already computes
`complete and not page.refresh_pending`, so the value is in hand. It is narrow by construction: a
fresh room has nothing pending and stays undegraded, which is what broke v10.

Note this may also mean the two target tests cannot be made to pass by a trigger alone. They
inject a *timeout*, and if the projection's only honest incompleteness signal is a pending refresh,
the tests have to be rewritten to create that state rather than to fail a read. Establish what the
rewritten test should assert before hunting further for a trigger that satisfies the current one.

So the condition is narrower than either "not strict" or "not hydrated". It is something like
"this conversation may have history we have not read" — an un-hydrated room that the bot has been
a member of, as distinct from one it has just joined or created. Whatever expresses that is the
trigger; the store knows membership epochs and the projection knows whether anything was ever
admitted for the room, so the information is probably there.

Both attempts are kept — `read-cutover-v9-degraded-toobroad.patch` and
`read-cutover-v10-hydration-gate.patch` — so the next person can see two dead ends rather than
rediscover them. `read-cutover-v8.patch` remains the best state at 18 failures.

**Superseded: the fix is diagnostic-only after all — but with the source marker, and gated on hydration.**

Two independent corrections landed on this, and together they specify it:

A runtime probe of `_thread_messages_root_proof()` shows it consults
`is_thread_history_source_degraded()`, which recognises `thread_read_source == degraded`. It does
*not* consult `THREAD_HISTORY_DEGRADED_DIAGNOSTIC`:

```
diagnostics={thread_read_degraded: true}                              => NOT_A_THREAD_ROOT
diagnostics={thread_read_degraded: true, thread_read_source: degraded} => PROOF_UNAVAILABLE
```

`NOT_A_THREAD_ROOT` is what lets coalescing resolve room-level and admit under a guessed key, so
the marker has to be `THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_DEGRADED`. Setting
only the boolean flag changes nothing on that path.

Note also that the resolver guard cited earlier (post-v8 lines 678-702 and 723-730) escalates to
`STRICT_FULL`, which hydrates through Matrix — so it is not the non-blocking demotion it was
described as. The non-blocking fix is to make an incomplete projected root proof return
`PROOF_UNAVAILABLE` *before* room-level classification, which the source marker achieves.

And the condition is hydration, not strictness — see the measurement below.

**Superseded in part: the one-liner as stated is wrong, and the measurement is the proof.** Marking every
non-complete page degraded takes the full suite from 18 failures to 19: it breaks
`test_history_summary_call.py` and does not fix
`test_plain_reply_with_unproven_root_is_not_admitted_under_guessed_key`.

The reason is a conflation. `complete=False` means "this was an advisory read", which
`ADVISORY_FULL` does deliberately and constantly — an advisory read is *supposed* to be
incomplete and is not degraded. The condition that actually matters is "this conversation was
never hydrated", which is a different fact and one the reader can ask the store for
(`conversation_is_hydrated`). Degrade on that, not on non-strictness.

The attempt is kept at `read-cutover-v9-degraded-toobroad.patch` so the next person can see the
shape without repeating it.

The guard the fix needs already exists.
`conversation_resolver.py:707` reads `if mode.dispatch_safe and is_thread_history_degraded(...)`,
which is exactly the "do not decide on incomplete evidence" rule the timeout used to trigger.
`is_thread_history_degraded` consults the diagnostics dict, and `projected_thread_history` never
sets it — so an incomplete projected page looks perfectly healthy and the guard never fires.

Have `projected_thread_history` mark a page degraded when `complete` is false (the same
`THREAD_HISTORY_DEGRADED_DIAGNOSTIC` the cache set), and the existing demotion path does the rest.
No mapping change, no blocking read on the dispatch path, and the safety property comes back in the
form the projection can actually express.

Rewrite the two tests to inject incompleteness rather than a timeout: a timeout is no longer
something a local read can do, and a test that keeps injecting one is asserting against a
mechanism that no longer exists.

`read-cutover-v8.patch` (854 lines) is the current state at 18 failures.
`CODEX_REVIEW10.md` has a per-test category and minimal fix for all 21 and is worth reading — note
that several tests should end up asserting `requires_model_history_refresh=True`, since an advisory
read is intentionally not complete.

An independent triage of all 21 failures classified two of them as genuine behaviour bugs rather
than harness noise: `test_dispatch_candidate_without_proof_history_demotes_without_retry` and
`test_plain_reply_with_unproven_root_is_not_admitted_under_guessed_key`. Both say the same thing —
the dispatch snapshot read must be **strict**.

The swap maps `strict = mode in (DISPATCH_FULL, STRICT_FULL)`, so `DISPATCH_SNAPSHOT` takes the
non-blocking `read`. That is wrong where the snapshot decides *thread membership*: a non-blocking
read can return an incomplete conversation, an unproven root is then treated as proven or demoted
on incomplete evidence, and a reply gets admitted under a guessed coalescing key. The second test
asserts an exact `RuntimeError` for precisely that case.

Fix the production mapping before any more of the fixtures. Two of the twenty are load-bearing and
the rest are downstream noise, which is exactly the trap of grinding a failure list from the top.

`read-cutover-v8.patch` (854 lines) is the current state at 18 failures: v7 plus
`test_multi_agent_bot`'s bare `AsyncMock` replaced with `_make_matrix_client_mock()`, which fixed
two of its four by letting hydration succeed.

The triage also gives a per-test category and minimal fix for all 21 in
`CODEX_REVIEW10.md` — worth reading before touching any of them, since several need
`requires_model_history_refresh=True` rather than the value they currently assert (an advisory read
is intentionally not complete).

**Superseded: hydration is the cause and the way to see it is to instrument the reader.** That was
right for the four `test_multi_agent_bot` failures and is now fixed; it was not the whole story.

```
READ_STRICT RAISED _HydrationError:
  Could not fetch thread root '$thread_root_id': <AsyncMock name='mock.room_get_event()'>
```

Obtained by monkeypatching `ConversationReader.read_strict` to print and re-raise, then running the
test with `-s`. Reasoning about it produced two wrong answers first; one print produced the right
one in a single run.

The confusion was that `make_matrix_client_mock` *does* wire `room_get_event`,
`room_get_event_relations`, and `room_messages` correctly (`tests/conftest.py:833-839`) — but
`test_multi_agent_bot` does not use that helper. Its bot carries a bare `AsyncMock`, so
`room_get_event` returns a mock rather than a `RoomGetEventResponse`, `_fetch_thread` raises, and
the turn ends with no response and nothing in the log.

The fix is therefore per-harness, not global: every bot whose test drives a *threaded* turn needs
either `make_matrix_client_mock` or a pre-hydrated conversation. Check each failing file for which
client mock it builds before assuming the shared helper covers it.

Note the shape of this bug for the remaining triage: a hydration failure surfaces as a turn that
silently does nothing, which is indistinguishable from a behaviour regression until you look. The
same instrumentation will answer the rest.

**Superseded twice: first "the real blocker is hydration", then "that explanation is wrong". The
first was right; the retraction was checked against `make_matrix_client_mock`, which this test
does not use.**

The note that follows claims those tests fail because a strict read hydrates against a mocked
client. That was checked afterwards and does not hold: `make_matrix_client_mock` already wires
`room_messages` to a real empty `RoomMessagesResponse` with no end token (exhaustion),
`room_get_event` to a real `RoomGetEventResponse`, and `room_get_event_relations` to an empty
async iterator (`tests/conftest.py:833-839`). Hydration therefore succeeds in those tests.

Adding `install_hydrated_conversation` to `seed_thread_history` is still right on its own terms —
a seeded conversation should be a known one — and it is kept in v7. But it moved the total only
from 21 to 20, and the reason those four tests produce no response is still undiagnosed. Two
hypotheses have now been advanced and both were wrong; the next person should instrument the turn
rather than reason about it, because the failure is silent and reads like a behaviour regression
while the surrounding fixture noise makes it easy to assume otherwise.

That question — is any of the remaining 20 a real behaviour change rather than a fixture artifact —
is the one thing worth answering before any more of them are "fixed".

**Superseded and incorrect: the real blocker is hydration, not fixture equality.**

This is the thing to understand before touching the rest. Under the cutover a strict read calls
`ensure_hydrated` before it answers, and hydration talks to Matrix. Every bot in the test suite has
a mocked client, so `room_messages` returns a mock rather than a `RoomMessagesResponse`,
`_fetch_room` raises, and the turn produces nothing — with no visible error, which is why
`test_multi_agent_bot` fails with "Expected 'stream_agent_response' to have been called once.
Called 0 times" and reads like a behaviour regression rather than a harness problem.

v7 adds `install_hydrated_conversation` to `seed_thread_history`, which fixes it for the tests that
seed. It only moved the total from 21 to 20, because the tests that hurt most never call the
seeder: their old stubs returned an *empty* history, so the earlier survey concluded they needed no
seeding. That conclusion was right about the data and wrong about the consequence — an empty
conversation still has to be a *known* one.

So the remaining work is mostly one change in the shared bot fixture, not per-test edits: a bot
constructed for tests should have its conversations marked hydrated, so a strict read answers from
the projection instead of reaching for a mocked homeserver. `install_runtime_cache_support` in
`tests/conftest.py` is the natural place. Do that before grinding through individual files —
the per-file counts below are mostly downstream of this one cause.

**Superseded: `read-cutover-v6.patch`, 33 failures down to 21.**

It contains v5, the seeding-ordinal fix, and the mechanism assertions removed properly. Remaining:
8 in `test_thread_context_resolution.py`, 4 in `test_multi_agent_bot.py`, 3 in
`test_turn_dispatch_pipeline.py`, 2 in `test_thread_mode.py`, and one each in
`test_live_message_coalescing.py`, `test_multi_agent_e2e.py`, `test_turn_controller_focused.py`,
`test_tach_split_matrix_client_boundaries.py`.

Removing the assertions worked once done syntactically: parse the module, find `Expr` statements
whose call attribute is one of `assert_awaited*`/`assert_not_awaited` with a root `Name` starting
`mock_`, and `Assert` statements whose test mentions `await_args` on such a name, then drop those
`lineno..end_lineno` ranges. That removed 34 calls and 12 asserts cleanly. Regex on the same job
corrupted the file twice, because the calls span lines and deleting the first one strands its
arguments.

The 8 left in that file are a different shape: expectations built with `make_visible_message()`,
which defaults `thread_id` to `None` and leaves `timestamp` unset, compared by
`ThreadHistoryResult.__eq__` against projected messages that carry both. Give those expectations
the thread and the ordinal, exactly as the `_message()` call sites already do. The other 13 across
five files are still unexamined.

**Superseded: two of the three remaining problems are understood; do not remove assertions with a regex.**

The ordering cause is fixed by a three-line change to `seed_thread_history`: enumerate the
messages and use the position as both `origin_server_ts` and `message.timestamp`. Callers pass the
very list they later assert against, so setting the field on those objects keeps expectation and
projection in agreement, and the conversation reads back in the order it happened rather than
alphabetically by event ID. With that alone `test_thread_context_resolution.py` drops from 20
failures to 12.

The remaining 12 are all `mock_fetch.assert_awaited_once*` and `await_args.args` assertions on the
cache methods being deleted. They must go, but **not** by regex: the calls span multiple lines,
and deleting only the first line leaves an orphaned argument block that turns into an
`IndentationError` and then an unmatched `)`. Two attempts at this corrupted the file badly enough
to need `git checkout`. Delete each call as a whole statement — an editor that understands the
syntax, or `ast`-based removal, not line patterns.

**Superseded: the swap is not one test from green. Use `read-cutover-v5.patch`.**
That earlier claim came from running `tests/test_conversation_resolver.py` alone and reporting it
as the suite. With v5 applied the resolver file does pass, and the full suite has **33** failures:
20 in `test_thread_context_resolution.py`, 4 in `test_multi_agent_bot.py`, 3 in
`test_turn_dispatch_pipeline.py`, 2 in `test_thread_mode.py`, and one each in
`test_live_message_coalescing.py`, `test_multi_agent_e2e.py`, `test_turn_controller_focused.py`,
and `test_tach_split_matrix_client_boundaries.py`.

v5 also fixes a real bug in v4: `_resolver()` accepted a `conversation_reader` argument and then
ignored it, always passing the empty reader.

**The 20 are one cause, and it is a defect in `seed_thread_history`, not in the tests.**
Seeding preserves each message's timestamp, and `_message()` builds them through
`ResolvedVisibleMessage.synthetic`, which sets `timestamp=0`. So every seeded message shares
`created_ts=0`, the page order falls back to `logical_event_id`, and `$thread_msg` sorts before
`$thread_root` — the reverse of the conversation order the expectations assert.

Fixing it needs distinct, increasing creation times, and that reaches the expectations too,
because the adapter maps `created_ts` onto `ResolvedVisibleMessage.timestamp` and the assertions
compare whole message objects. Either give `_message()` a timestamp parameter and set increasing
values at each call site, or have `seed_thread_history` assign ordinals and relax the comparison
to the fields that carry meaning. The first keeps the assertions strict and is preferable.

Do not "fix" this by sorting the expectation to match the projection. The order a conversation
reads back in is the thing under test.

**Superseded: use `read-cutover-v4.patch`, not v3.** It carries everything v3 had plus the two resolutions
below, and is one failing test from green. Apply with `git apply --exclude='docs/*'`.

`test_reply_to_candidate_retries_strictly_after_degraded_dispatch_proof` is **deleted** in v4, and
that was a judgement rather than a fix. It stubbed a degraded dispatch read and a proving strict
read and asserted the resolver retried the second. Both modes now route to `read_strict`, so the
degraded-then-retry sequence it observes no longer exists — that backoff is the machinery being
deleted, and the projection has no degraded mode. Supplying it a page would have left it asserting
a retry that never happens. The scenario it covered is the test immediately above it.

`test_reply_to_proven_thread_root_joins_that_thread` still fails in v4 and is the last thing
standing. An independent review reproduced the same two failures in a scratch copy and traced it,
so the remaining step is mechanical:

Root proof is `any(message.event_id != thread_root_id)` (`matrix/thread_membership.py`), so the
page must contain the **child**, not the root. The projected child must carry `thread_id=_PARENT`;
the adapter preserves that field and maps `logical_event_id` to `event_id`. A root-only page
proves nothing. If the test is meant to model an ordinary non-redacted-root read rather than the
minimum proof page, the faithful page is `_PARENT` followed by `$child:localhost`, and the exact
event-ID assertion should be strengthened to match — a root-only page is wrong under either
reading.

An earlier version of this note said both tests merely need "a page containing the thread root".
That was wrong twice over: the first needs the *child* tagged into `_PARENT`, and the second
cannot retain its retry meaning through any page at all.

If the deleted retry test is instead kept and rewritten, its expected child must also be built
with `thread_id=_PARENT` — `make_visible_message()` defaults that field to `None` while
`ThreadHistoryResult.__eq__` compares whole message objects, so leaving it unset fails an equality
assertion that is otherwise correct. Correcting the fixture strengthens that assertion rather than
weakening it.

Do not make either pass by relaxing an assertion, faking a degraded page, or restoring
cache-method call assertions. Proving that a reply to a thread root joins that thread is the
behaviour this whole read path exists to get right.

**Superseded: the swap was two tests from green in `read-cutover-v3.patch`.**
Applied to head `2d91a4919` it leaves the full resolver suite at two failures, both in
`test_conversation_resolver.py`: `test_reply_to_proven_thread_root_joins_that_thread` and
`test_reply_to_candidate_retries_strictly_after_degraded_dispatch_proof`.

Those two prove thread membership by finding the reply target inside the returned history, so the
empty fake reader the other store-less harnesses use is not enough — they each need a page
containing the thread root. That is the only work left in the patch. Everything else is done: the
adapter, the public `ConversationReader`/`StaleConversationError`/`HYDRATED_PROMPT_WINDOW_MESSAGES`
names, the `conversation_reader` dependency and its `bot.py` wiring, the four-mode dispatch table
collapsed to `strict = mode in (DISPATCH_FULL, STRICT_FULL)`, the fake readers in the three
store-less harnesses, and the removal of the three `assert_awaited_once_with` calls that asserted
the cache method being deleted.

Do not re-add those awaited-once assertions. They pin the mechanism, not the behaviour, and the
returned history is what carries the meaning.

**The two remaining tests are not the same problem, and the second is not mechanical.**

`test_reply_to_proven_thread_root_joins_that_thread` is straightforward: its fake reader needs
`read_strict` to return a page containing `$child:localhost` in the `$PARENT` thread, because
`DISPATCH_FULL` now routes there. Nothing about what it asserts changes.

`test_reply_to_candidate_retries_strictly_after_degraded_dispatch_proof` is different. It stubs
`get_dispatch_thread_history` to return a *degraded* empty result and `get_strict_thread_history`
to return the proving child, and asserts the resolver retries the second after the first comes
back degraded. Under the cutover both `DISPATCH_FULL` and `STRICT_FULL` map to `read_strict`, so
there is no longer a degraded-then-retry sequence to observe — that backoff is precisely the
machinery being deleted, and the projection has no degraded mode.

So that test cannot be repaired by supplying a page. It has to be rewritten to assert the outcome
it actually cares about — a reply to a proven thread root resolves to that thread even when the
first read is unhelpful — or deleted as covered by the test above it. Decide which deliberately;
supplying a page that makes it pass would leave it asserting a retry that no longer happens.

**Measured again, and much smaller than the stub count suggested.**
Counting `patch.object` sites overstated the work by roughly an order of magnitude, because most
stubs return an *empty* history — and the projection returns empty by default too, so those tests
match after the swap with no seeding at all. Their stubs simply become unnecessary.

Across the six remaining files, exactly **one** stub carries messages
(`test_multi_agent_bot.py`); `test_multi_agent_e2e.py`, `test_live_message_coalescing.py`,
`test_turn_dispatch_pipeline.py`, `test_thread_mode.py`, and `test_turn_controller_focused.py`
have none. `test_thread_context_resolution.py` held nine and is fully migrated.

So the remaining migration is one seeded conversation plus the fake reader those store-less
harnesses need — not the sixty-site sweep the earlier inventory implied. The count below is
retained because it is still the right map of *where the stubs are*; it is the wrong measure of
how much has to change.

**Size, measured rather than estimated.**
In `test_thread_context_resolution.py` alone: 51 `patch.object(bot._conversation_cache, ...)` sites, of which 10 are the simple `get_thread_history` shape, and 36 references to `mock_fetch`.
Most of those 51 patch methods that survive the cutover (`get_thread_id_for_event`, `get_event`) and must be left alone — only the history reads move.
The `mock_fetch.assert_awaited_once()` and `mock_fetch.await_args.args` assertions have no replacement and should simply go: they assert that a particular cache method was called, which is the mechanism being deleted, and the returned history is what carries the meaning.

Use `read-cutover-v2.patch` rather than the first one; it is rebased over the narrow-views commit and carries the two helpers.
Apply with `git apply -3 --exclude='docs/*'`.
Note that the patch also reverts the hydrator to taking a client; the runtime-view change has to be redone on top, since later commits touched the same lines.

**Full inventory of what the migration has to touch.**
Two of the 44 failures are structural rather than behavioral — `test_tach_split_matrix_client_boundaries.py` wants explicit tach modules for the two new import edges (`conversation_reads` to `client_visible_messages`, `conversation_hydration` to `runtime_protocols`). Fix those by editing `tach.toml`, not the tests.

The remaining 42 are history stubs in three idioms, all of which become a `seed_thread_history(...)` call:

| Idiom | Example |
| --- | --- |
| Attribute assignment on a cache mock | `conversation_cache.get_dispatch_thread_history.return_value = ...` |
| `patch.object` on the bot's cache | `patch.object(bot._conversation_cache, "get_dispatch_thread_snapshot", AsyncMock(...))` |
| Class-level decorator | `@patch("mindroom.matrix.conversation_cache.MatrixConversationCache.get_dispatch_thread_history")` |

Stub-site counts per file, which exceed the failure counts because some stubs feed tests that never read the history: `test_thread_context_resolution.py` 20 (of 51 total cache patches), `test_thread_mode.py` 13, `test_multi_agent_e2e.py` 12, `test_live_message_coalescing.py` 5, `test_multi_agent_bot.py` 4, `test_turn_dispatch_pipeline.py` 4, `test_turn_controller_focused.py` 2.
Only the ones whose test actually asserts on history need to move; the rest can simply lose the stub.

**Why the reader could not simply be a value in the deps.**
Neither `ConversationHydrator` nor `_ConversationReader` was built anywhere in production; they existed and were tested, but only tests instantiated them.
The hydrator needed a `nio.AsyncClient`, and the bot does not have one when its collaborators are assembled — the client arrives at login, and `bot.py` sets `self.client` later.

Resolved by following the precedent already in the tree: the delivery gateway holds the runtime view and reads `runtime.client` at call time, raising if it is not ready.
The hydrator now does the same, so it is constructible at assembly and only requires a client when a read actually happens.

### 2. Conversation projection cutover

Add latest-visible projection storage, bounded reads, and one-time thread and room hydration.

Change conversation resolution, reply lookup, reaction lookup, stale-stream cleanup, hooks, and streaming thread targeting to use the bounded projection API.

Before the implementation PR can merge, remove the Matrix cache package, conversation-cache read variants, room-scan thread history and repair, cache trust, cache certification, advisory outbound cache writes, and cache write coordination.

Reduce checkpoint publication to successful durable admission plus nio's exact unrecovered-room result.

Do not preserve deleted cache interfaces to keep implementation-specific tests green.

#### Cached facts the projection does not yet own

An audit of what the Matrix cache persists against who consumes it found two durable facts that the latest-visible projection cannot represent as designed.
Both must have a named owner before the cache package is deleted, because deleting it otherwise removes a behavior rather than replacing it.

**Resolved sidecar text.**
`_download_mxc_text` in `src/mindroom/matrix/message_content.py` downloads, decrypts, and durably caches the plaintext behind an MXC reference, so rebuilding a conversation does not refetch and re-decrypt the same oversized message every time.
The projection stores the Matrix content, which holds the reference and not the resolved text.
Attach the resolved text to the visible revision as one nullable value, cleared whenever the revision changes: an edit, a redaction, or a membership epoch advance all invalidate it, and a value that outlived any of those would serve the wrong body.

**Tool-approval card recovery.**
`ApprovalManager._cached_trusted_pending_approval_for_card` in `src/mindroom/approval_manager.py` reads an arbitrary `io.mindroom.tool_approval` event and its edits from the cache to recover pending approval state after a restart.
Neither new owner can substitute for it: `visible_messages` models conversation messages, and the journal clears a settled event's payload on purpose.
Give approvals their own small durable projection, owned by the approval subsystem, rather than widening either.

### 3a. How MindRoom's own messages reach the conversation

Sync is the only source of conversation content, including MindRoom's own messages: the Client-Server API returns a client's own events in its timeline, carrying the transaction ID that sent them.
There is therefore no separate outbound ingestion path, and the advisory post-send cache notifications are deleted rather than reimplemented.

For a turn triggered by a room event, waiting for the echo costs nothing: the room timeline orders MindRoom's own message before the user's next one, so by the time the follow-up is admitted its own echo already has been.
The gap is elsewhere — a turn that reads the conversation after sending within the same turn, and a turn no room event triggered at all, such as a scheduled task or a todo poke.
Those would read a room they have already spoken in as one they have not, and `read_strict` cannot express "wait for my own echo": it can only wait on a refresh token.

The initial and final deliveries therefore seed the projection at acknowledgement, in the same transaction that records the acknowledgement.
This costs at most two writes per turn, because intermediate streaming edits never enter the outbox at all.

A seeded row is explicitly **provisional**, and the plan must not pretend otherwise.
A Matrix send response carries only `room_id` and `event_id` (`nio.RoomEventIdResponse`), while the projection orders messages and revisions by `origin_server_ts`, which only the server knows.
Seeding therefore stores provisional ordering metadata, and the self-authored sync echo replaces it with the authoritative values.
That replacement has to be written deliberately: `_project_original` currently inserts `ON CONFLICT DO NOTHING`, so today an echo could not correct a seeded row at all.
Once the authoritative values are installed, any repeated echo is a genuine no-op.

The accompanying rules:

- A self-authored event updates the projection and must never create a second semantic turn.
- Code editing a message it just sent uses the event ID from the send response, never a projection read.
- Recovered events use the same ingestion path as live ones.
- An outbound redaction takes effect immediately on acknowledgement, because MindRoom must stop serving deleted content without waiting for a round trip.

**Landed, and now superseded.** `send_text` and `edit_text` both seed on acceptance, which covers blocking answers and streamed ones — a streamed answer reaches its final text by editing, so seeding only the original would have a turn that reads immediately afterwards see the placeholder.

> **Superseded by the audit below (2026-08-06).** Provisional seeding is to be
> deleted: the sync echo is the only route into conversation content. The rest
> of this section describes machinery that is being removed, and is kept
> because the ordering hazards it documents are what the echo-ordering tests
> now pin. Read it as a record of why the mechanism was hard, not as a
> specification to build against.

The two halves need different mechanisms, and the reason is worth stating.
For an original the whole row is provisional, so the echo replaces it, guarded by a `provisional` column.
For an edit only the *time* is provisional: which logical message it revises is certain, so it takes the ordinary edit path.
What is not certain is ordering, and revisions are ordered by timestamp — so a bot whose clock runs ahead would install a revision stamped in the future and every genuine later edit would lose the comparison, freezing the answer at whatever it said first.

The echo of a seeded revision is recognised by identity rather than by a flag: it is already the installed `revision_event_id`, so it is not a competitor to compare against but the authoritative account of the revision already shown, and it is installed unconditionally.
Both hazards are pinned — `test_a_seed_from_a_fast_clock_does_not_outrank_later_edits` for originals and `test_the_echo_of_a_seeded_edit_replaces_its_guessed_time` for revisions — and both tests fail if the comparison is restored.

Not every outbound message is a two-stage response delivery.
Approval cards, Matrix-tool messages, and summaries have no turn and stage, so they are not seeded: they reach the conversation through sync like any other event.
Approval cards additionally need the durable projection described above, because their state must survive a restart and the journal clears a settled event's payload.

### 3. Delivery ownership cutover

Add the claimed deterministic outbox, which is the point at which a model result becomes durable.

Keep only the latest unsent streaming content in memory.

Before the implementation PR can merge, remove duplicate pending-visible, delivery-retry, handled-source, and response-reconciliation state when the journal, `TurnStore`, or outbox owns the same fact.

Preserve only unique model-execution, cancellation, redaction, and business-outcome data in `TurnStore`.

### 4. Final deletion and proof

Remove any remaining cache, repair, replay, or delivery owner made obsolete by the cutovers.

Update the architecture documentation to name one owner for each durable fact.

Run the complete backend, crash, performance, full repository, and real-server suites from the final state.

The implementation PR may not add a compatibility path merely to avoid deleting an old implementation-specific test.

## Merge Gates

The single implementation PR tracks these results as its internal checkpoints complete and reports the final values in its description.

| Gate | Required result |
| --- | --- |
| Lost actionable events | Zero across the crash matrix and restart recovery. |
| Duplicate turns | Zero. |
| Duplicate terminal responses | Zero. |
| Historical turns | Zero from `HISTORY` or a cold baseline. |
| Model reruns after durable completion | Zero. |
| Edit storage | One visible row per logical message and at most one unresolved row per target and sender. |
| Current-edit redaction | Strict reads return the server-authoritative prior revision after point refetch and never stale content. |
| Redacted revision exposure | Zero reads of any kind return a revision whose redaction was durably admitted. |
| Conversation reads | Bounded cursor pages using the conversation index. |
| Post-hydration room scans | Zero. |
| SQLite lock failures | Zero in the 50-conversation stress run. |
| Backend parity | The same behavioral contract passes on SQLite and PostgreSQL. |
| Competing owners | No replaced active path remains. |
| Source size | The PR reports additions, deletions, new-owner size, and deleted-owner size. |

Timing thresholds are manual release evidence rather than flaky CI assertions.

The initial performance targets are p95 durable admission below 50 milliseconds, p95 bounded conversation reads below 50 milliseconds, and p95 writer-queue wait below 100 milliseconds on the standard development host.

Those targets must be recorded with host details and may be revised only from measured prototype evidence.

The complete implementation diff must be materially net negative in production source lines.

The previous 8,000-line reduction remains a target, not proof by itself, because moving the same complexity into new modules would still be a failed simplification.

## Expected Deletions

The exact touched files are decided during implementation, but these ownership groups must disappear if their responsibility is replaced.

- `matrix/cache/` and its write coordinator.
- `matrix/client_thread_history.py` room-scan, refill, gap, snapshot, and repair behavior.
- The overlapping history-read variants and advisory outbound notifications in `matrix/conversation_cache.py`.
- `dispatch_obligations/`.
- `dispatch_admission.py`.
- `cold_history_fence.py`.
- `turn_settlement_retry.py`.
- Cache generation trust and certification.
- Duplicate handled-source, pending-visible, response-idempotency, and retry-source state.

The implementation may retain a small focused module under an old filename only if it still owns a unique fact and the PR explains that ownership.

## Explicit Non-Goals

- Retaining or reconstructing intermediate edit bodies.
- Preserving old cache schemas or internal cache APIs.
- Adding a MindRoom recovery classifier beside nio provenance.
- Adding room-wide fallback repair after strict hydration fails.
- Inferring recursive relation capability from the Matrix spec version.
- Adding a speculative nio batch-admission API before measurement proves per-event admission is the remaining bottleneck.
- Preserving implementation-specific tests for deleted owners.

## Stop Conditions

Stop and redesign if any of these occur:

- The prototype loses or duplicates an admitted actionable event.
- Accepted deterministic delivery can display content different from the durable model result.
- Correct hydration requires retaining edit chains or restoring room-wide repair scans.
- Current-edit redaction cannot recover the server-authoritative visible revision through the shared point hydrator without serving stale content.
- Principal isolation cannot be expressed through one bound store interface.
- SQLite still produces lock failures with one writer and a configured `busy_timeout`.
- A cutover needs the old and new active paths simultaneously after merge.
- A cutover adds as much durable state or production code as it removes.
- Three review rounds continue to reveal new correctness classes.

Passing these gates does not guarantee that every later cutover is easy.

It does establish that the core replacement is real before MindRoom undergoes another large rewrite that only sounds simpler in a plan.

## Audit: is durable outbound seeding load-bearing? (2026-08-06)

Reviewed after a request to make the Matrix sync echo the sole authoritative
route for outbound conversation content and delete provisional seeding.

### The architectural argument holds

Self-authored echoes already reach the projection through ordinary ingress.
`_event_class_for()` in `matrix/journal_ingress.py:91-101` classifies purely by
nio provenance, not by sender: a LIVE echo is `ACTIONABLE` and is admitted and
projected like any other event. What stops it from starting a turn is the
downstream echo drop in `ingress_validation.py`, not an ingress-level discard.

That matters because the ordering question answers itself. A later user message
and this bot's own echo arrive on the same timeline, ordered by the server. If
the user's message came after our send, the echo precedes it, so by the time the
user's turn is resolved the echo is already in the projection. Nothing needs to
be seeded for a *later* turn to see a *previous* answer.

Seeding is therefore only load-bearing for a read that happens before any sync
at all -- a genuine same-execution read-after-send.

### Two such readers do exist

The claim that no workflow reads after sending is too strong.

- `thread_summary._load_thread_history()` runs from a background task queued in
  `post_response_effects._queue_thread_summary()` immediately after delivery,
  and reads full history.
- The agent tool surface exposes both `_message_send_or_reply` and
  `_message_read` in `custom_tools/matrix_conversation_operations.py`, so one
  model run can send and then read.

Neither is ordered by the timeline, so seeding does not make either *correct* --
it only makes the just-sent message visible sooner. For a thread summary,
missing the newest reply degrades one summary and the next one recovers it.

### A cost of seeding, against it

`DeliveryGateway` calls `outbound_projection.record_sent()` on every send and
every edit (`delivery_gateway.py:533` and `:580`), including the "Thinking..."
placeholder. Transient UI state therefore enters the durable conversation record
and stays until the echo or the final edit supersedes it.

### Current surface

`matrix/outbound_projection.py` is 85 lines; `provisional` appears 44 times
across `src/mindroom`, 32 of them in `event_journal/projection.py` and 7 in
`event_journal/schema.py`. Tests naming `provisional`, `seed_outbound`, or
`OutboundProjection`: `test_outbound_projection.py`, `test_turn_store.py`,
`test_event_journal_store.py`, `test_turn_policy.py`, `test_team_mode_decision.py`,
`test_response_runner_agent.py`.

### Conclusion (authoritative)

Removal is justified, and the ordering guarantee -- not the absence of
read-after-send callers -- is the reason. Prove it first with the echo-ordering
tests before deleting anything, and decide explicitly what `thread_summary` is
allowed to miss rather than letting seeding hide the question.

**This decision overrides the "Landed" note earlier in this document**, which
described seeding as the mechanism for making a bot's own answer readable. Where
the two disagree, this section wins. Concretely:

- The sync echo is the only route into conversation content.
- Provisional seeding and its ordering machinery are deleted: `seed_outbound`,
  `OutboundProjection`, `SeedingView`, and the `provisional` /
  `revision_provisional` columns.
- The open "a rejected revision is never reconsidered" defect (task 11) is
  caused by provisional seeding and disappears with it, rather than needing its
  own fix.
- Reads issued in the same execution that sent a message are documented as
  echo-ordered, not read-your-writes. A turn that must know its own last message
  uses the event ID from the send response.

The echo-ordering tests landed first, as required: `TestEchoOrdering` in
`tests/test_journal_ingress.py` pins that a bot's own message is admitted and
ordered against the traffic around it, so deletion is now unblocked.

## Blocking: the projection serves truncated bodies for sidecar'd messages (2026-08-06)

> **Resolved.** These dated sections are kept as a log of how the work went, not as open items.
> Everything the census listed landed with the deletion of `matrix/cache/`; the reply-fallback
> defects and both wedges are fixed and pinned. Read them for the reasoning, not for the status --
> the status is the summary at the top of this document and the gate check above it.


Raised by the operator: most agent messages exceed the Matrix message size
threshold and are stored through the v2 large-message sidecar, so their event
content carries a preview body plus an MXC reference rather than the full text.

The read cutover exposed this. Both old read paths resolve the sidecar while
building a `ResolvedVisibleMessage` -- `client_thread_history.py:1046` and
`client_visible_messages.py:224` both call `resolve_event_source_content()`.
The projection does not. `projected_event()` in `matrix/journal_ingress.py:146-166`
stores `event.source["content"]` verbatim, and `projected_thread_history()` in
`matrix/conversation_reads.py` reads `message.content.get("body", "")` straight
out of it.

So every sidecar'd message now reaches prompt assembly as its preview. With most
agent turns over the threshold, that is most of the agent's own history.

Not caught by CI: no projection test carries a sidecar'd message.

### Where the resolved text should live

Resolving on every read is what the old path avoids by caching plaintext in the
event cache (`get_mxc_text` / `store_mxc_text`, keyed by room, event, and MXC
URL, guarded by the membership epoch). Deleting `matrix/cache/` deletes that
cache, so the journal has to own the same fact.

Resolving during admission is the wrong place: admission commits before nio
accepts the event, and an MXC download inside that path makes sync acceptance
depend on a media fetch that can fail or hang.

The shape that fits what already exists is lazy resolution on read, persisted
into the journal -- the same contract as `refresh_pending`. A sidecar body is
content the projection knows it does not have yet, which is exactly what a
pending refresh already models: a non-blocking read may serve the preview, a
strict read must resolve it before answering, and the resolved text is written
back so the fetch happens once.

### Implemented (2026-08-06)

Ownership: **Matrix owns the sidecar; the projected row owns the resolved
visible content.** No separate plaintext table -- the old `mxc_text` cache in
the event cache is not to be recreated, because a second owner needs its own
invalidation and, critically, its own redaction cleanup. Plaintext living in the
projected row means redaction removes it as a consequence of the projection
already working.

Storing resolved content in the projected row does not hurt replay: replay reads
`journal_events`, which keeps the exact admitted source. `visible_messages` is a
reduction and is allowed to hold canonical content.

Projection stays inside the admission transaction. An earlier sketch here
proposed splitting it out so a worker could resolve the sidecar before
projecting; that is rejected. It would break the crash invariant this document
states at the top, and it would strand context-only history, which is admitted
already settled with its source payload cleared and so never reaches a worker.
Recovering it would need a second obligation kind and a second fence -- a
recovery state machine bought to avoid one media fetch.

What is implemented instead reuses the mechanism redaction already established:

1. The projection refuses to store content whose text is a preview.
   `_stored_body` (`event_journal/projection.py`) writes a null body and a
   refresh token for any content still carrying sidecar metadata. This is a
   pure content inspection, so admission stays network-free.
2. That is the same row shape a redacted revision leaves behind, so the readers
   that already handle it need no change: `read_conversation` omits the message
   and reports it in `refresh_pending`, a non-strict read reports the page
   incomplete (`conversation_reads.py:71`), and a strict read resolves and
   re-reads before answering, raising `_StaleConversationError` if it still
   cannot (`conversation_reads.py:144`).
3. `ConversationHydrator._resolved_content` performs the fetch, from the point
   refetch path that redaction already used. Failure returns nothing, so the
   message keeps its token and stays repairable rather than settling the debt
   with the preview. `install_refetched_revision` refuses unresolved content as
   a backstop; a mutation test confirms that backstop alone keeps behaviour
   correct when the resolver is broken.
4. Redaction is unchanged. It already refetches the original plus surviving
   relations under the membership-epoch fence and the refresh token, and
   resolution now happens on that same path.

Collapse-before-download falls out of laziness rather than worker timing. An
earlier sketch proposed the worker "skip revisions already superseded by a later
edit"; that is rejected because a worker processing an edit cannot know a later
one is coming. Because each edit overwrites the visible row and nothing
downloads until a read asks, a streamed answer resolves only the revision that
won, whatever order the edits arrived in. `TestSidecarResolution::
test_a_streamed_answer_downloads_only_the_revision_that_won` pins one download
across three intermediate edits.

Resolution is bounded by the reader, not by hydration. Hydration installs rows
without fetching attachments, and the fetch happens per refetched message on the
strict read that wants it, so the bound is that read's page limit.
`HYDRATED_PROMPT_WINDOW_MESSAGES` (2_000, `matrix/conversation_hydration.py:61`)
is the hydration walk's ceiling and never a resolution budget. An earlier note
here claimed `execution_preparation.py:599` is "the real prompt trim"; that is
wrong -- it is the explicit `history_limit` selection used by scheduled and
tool-driven turns, and general prompt trimming happens through token-budget
selection elsewhere.

Steady state: one download per newly prompt-relevant visible revision, then
local reads until that revision is edited or redacted.

## Review of the read cutover, and what it found (2026-08-06)

An independent review of the branch raised seven findings. Two were correct
about code this cutover introduced, one was correct about the plan, and four
attacked a design that is not being built. Recording all of them, because the
refuted ones are the ones most likely to be raised again.

### Correct, and fixed

**A bounded page reported itself as full history.** `projected_thread_history`
derived `is_full_history` from whether the caller waited and whether a refetch
was owed, never from whether the page reached the start of the conversation. A
read that filled its limit and left a cursor behind reported the suffix as the
whole thread. `complete_thread_history` then marked it complete for summaries,
which count what they receive and record that count as the thread's size. The
cursor now participates in the answer.

### Correct, and open

**A first admitted event can prove an empty room.** `may_have_unread_history`
falls back to asking whether the journal holds any other event for the room, on
the reasoning that a room MindRoom has never seen an event in can have nothing
behind the event it just got. That is a fact about the journal, not the room. A
first run, or a rebuilt store, meets its first event in a room with years of
history and concludes that the empty page it can serve is complete; thread-root
proof reads a complete-looking empty page as "this root has no children" and
demotes a real threaded reply to a room-level message.

The hole is narrow -- it closes as soon as any second event in the room reaches
the journal -- but it is real, and it is worst exactly at startup.

**The obvious fix does not work, and this is the useful part.** Making
hydration the only proof of freshness (`return not await
conversation_is_hydrated(...)`) was tried and reverted. Non-blocking `read`
never hydrates; only `read_strict` does. So "degraded unless hydrated" makes
every dispatch read degraded forever, nothing ever hydrates on that path, and
the dispatch-safe property collapses into a strict read: eleven tests fail,
including one that asserts in as many words that a command must not block on a
strict read.

So the fix has to make hydration happen on the dispatch path rather than make
the predicate stricter -- a first dispatch read that reports degraded while
hydrating in the background, becoming complete once it lands. That is a real
piece of work, not a predicate change, and it needs its own phase.

The review instead proposed epoch-scoped projection watermarks and a durable
journal fence. Those are not needed: projection commits in the admission
transaction, so there is no window between admission and projection to fence,
and hydration already carries the epoch.

### Correct about the plan

The consumer-budget claim, the outbound-seeding contradiction, and the
split-projection sketch were all wrong in the plan text. All three are
corrected above.

### Refuted

**"Moving projection to the worker loses cold history"** and **"admission-to-
projection reads need a durable fence"** are both true of splitting projection
out of admission, which is why that split is rejected above. Projection stays
in the admission transaction, so cold history projects exactly as it always
did, and the window those findings describe does not exist. The remedy proposed
for them -- a second obligation kind (`projection_pending` alongside
`semantic_pending`) plus retained source payloads -- would add the recovery
state machine this architecture exists to remove.

**"Skip revisions already superseded is not enough"** is right that a worker
cannot know a later edit is coming, and moot: nothing downloads until a reader
asks, so only the revision that won is ever resolved.

**"Eager worker resolution contradicts the consumer budget"** describes eager
resolution, which is not what was built. Resolution is lazy and bounded by the
page the reader asked for.

## Delivery cutover: what the phase actually requires (2026-08-06)

`ResponseDelivery` (`src/mindroom/response_delivery.py`) is complete and has
**no production caller**. Nothing constructs it, so `enqueue_delivery`,
`claim_delivery`, `acknowledge_delivery`, and `unacknowledged_deliveries` are
reachable only from tests. The cutover is wiring, not new mechanism.

### The blocker

The outbox keys every row on `(turn_id, stage)`, and the transaction ID is
derived from them (`delivery_transaction_id(principal_id, turn_id, stage)`).
That is what makes a resend a no-op on the homeserver. **No delivery request
carries a turn identity.** `SendTextRequest`, `EditTextRequest`, and
`FinalDeliveryRequest` all take a `MessageTarget` and nothing that survives a
restart as the same turn.

The identity to use is `MessageEnvelope.source_event_id`: it is the Matrix
event that caused the turn, it is what the handled-turn ledger already keys on
(`same_turn_identity`, `turn_record.py:538`), and it is stable across restarts.
`ResponseIdentity` already carries the envelope, so `FinalDeliveryRequest` can
derive it today; `SendTextRequest` cannot.

### Do not route `send_text` wholesale

`SendTextRequest` has callers that are not response turns at all:
`visible_voice_echo.py`, `commands/config_confirmation.py`, `bot.py:424`,
`turn_controller.py:1682` and `:1768`,
`visible_response_reconciliation.py:152`. Giving those a synthetic turn ID
would put rows in the outbox that no recovery pass can reason about, and two
unrelated sends sharing a derived ID would silently collapse into one visible
message on the homeserver.

Only deliveries that carry a `ResponseIdentity` belong in the outbox.

### Order of work

1. Add the turn identity to the response-carrying delivery requests, derived
   from the envelope rather than generated, so the same turn re-derives it
   after a restart.
2. Construct `ResponseDelivery` in the bot and route `FinalDeliveryRequest`
   through it as `DeliveryStage.FINAL`. This is the delivery whose loss or
   duplication is visible to a user.
3. Route the streaming placeholder as `DeliveryStage.INITIAL`, which needs an
   identity on that one `SendTextRequest` call site
   (`response_runner.py:1309`) and not on the others.
4. Run `ResponseDelivery.recover()` at startup, after which contract 2 --
   settling the journal source at outbox enqueue rather than at TurnStore
   adoption -- becomes expressible.

Intermediate streaming edits stay off the outbox: contract 4 makes them
transport-only, and giving each one a durable row would put a claim-before-send
round trip in the streaming loop.

### What contract 2 protects against

Enqueue happens in the same transaction that settles the journal source. The
hazard is a crash between "the model produced an answer" and "the answer is
durably owed to a room": settling at TurnStore adoption marks the source done
while nothing durable yet says what to send, so recovery has no reason to send
anything and the turn is lost silently. Settling at enqueue means the source
stops being pending only once the delivery exists, and recovery finds it.

## Outbound seeding is deleted (2026-08-06)

Done. `seed_outbound`, its ordering key, `OutboundProjection`, `SeedingView`,
and the `provisional` / `revision_provisional` columns are gone, along with the
seed/echo race handling in the original and edit projection paths. Net −327
production lines.

The sync echo is now the only route into conversation content.

### What closed with it

**Task 11, "restore a revision discarded before a backwards canonicalization",
needed no fix.** Canonicalization existed only to promote a seeded row to
authoritative; with no seeded rows there is no canonicalization and no
discarded revision.

**The sidecar echo CASE went too.** An echo could replace an already-resolved
sidecar body with its own preview only because the seeded row was still marked
provisional and therefore yielded. That was a real bug and a real special case;
both are gone rather than maintained.

### What it costs, stated plainly

A turn that reads a conversation immediately after speaking in it sees the room
as it was before it spoke, until the echo lands. That is echo ordering, not
read-your-writes. Code that needs the identity of what it just sent uses the
send response, which is the only account of it that is certain at that moment.

The affected callers are the ones the earlier audit named: `thread_summary`, and the Matrix conversation tools.
The audit's conclusion holds for the conversation tools, which read to build context rather than to confirm their own last message.

It does not hold for `thread_summary`, and this corrects it.
That pass does not only build context: it counts the thread and writes the count into the summary notice, where it becomes the durable baseline every later threshold is measured from.
The message it is counting is the answer whose delivery queued the pass, so an echo-ordered read is one short of the thread by construction — and `should_queue_thread_summary` one layer up already counts that answer, so the two numbers could never agree.
A count is exactly the "must know its own last message" case named above, so the pass takes the send response too: `with_delivered_response` folds that one logical event into a projected read, and the fold collapses onto the echo as soon as it lands.

### Why the enumeration tests went

`TestSeedOrderingMatrix` enumerated every arrival order of two seeds, two
echoes, and an original, because six defects in that family had each been fixed
against the single ordering that exposed them. The orderings it enumerated
cannot occur any more -- there are no seeds to order against echoes -- so the
matrix is deleted rather than kept green against a mechanism that does not
exist. `TestEchoOrdering` in `tests/test_journal_ingress.py` is what now pins
that a bot's own message reaches the conversation, and it does so through
admission, which is the only route left.

## Delivery cutover status (2026-08-06)

Every delivery point that carries a turn's answer is now durable, and startup
resends anything whose outcome this process cannot know.

| Delivery point | Route | Stage |
| --- | --- | --- |
| Placeholder that creates the visible message | outbox | `INITIAL` |
| Final answer, sent (no placeholder) | outbox | `FINAL` |
| Final answer, edited onto a placeholder | outbox | `FINAL`, with `edits_event_id` |
| Streamed terminal text, edited onto a placeholder | outbox | `FINAL`, with `edits_event_id` |
| Streamed terminal text, as the stream's first event | outbox | `FINAL` |

The streamed cases go through callbacks the gateway hands to
`StreamingResponse` (`terminal_edit` and `terminal_send`), so no extra Matrix
round trip is added: the edit or send the stream was going to make anyway is
enqueued first and acknowledged after. An unacknowledged row therefore means
exactly "the terminal update never landed", which is the condition recovery
acts on and the only one.

Sends that are not turns stay direct, as decided above: voice echoes, command
confirmations, reconciliation notices. So do intermediate streaming edits,
cancellation notices, and failure updates, which are transport rather than a
turn's answer. A terminal update that still reads `Thinking...` is also direct,
because a stream that never answered must not settle the turn -- `deliver_final`
delivers the answer in exactly that case, and would find its own row
acknowledged and send nothing.

### Recovery is a retry loop, not a startup step

Recovery runs after a sync response rather than at startup, because nio refuses
ordinary sends into an encrypted room until sync has rebuilt device state --
so a startup pass would spend its retry budget failing in exactly the rooms
this exists for.

The first response is not always enough either. It can arrive while a room is
still unrecovered. `recover()` therefore reports what it still owes, and the
pass runs again on later sync responses until it owes nothing. Tying "recovery
finished" to "first sync observed" stranded any row that failed the first pass
until the process restarted.

An unacknowledged `INITIAL` is skipped whenever a `FINAL` row exists at all,
acknowledged or not. Both rows unacknowledged is the ordinary shape of a crash
between claiming the answer and recording it, and recovery walks rows oldest
first, so requiring acknowledgement put the placeholder into the room *before*
the answer. An edit-shaped `FINAL` cannot be stranded by the skip: its target
event ID only exists because the placeholder send returned one.

### What was open at the time, precisely — both closed since

Both defects below were real when written and are fixed: `e96dcaf18` moved the
transform ahead of the terminal payload build and deleted
`_finalize_visible_replacement_edit`, and `fd7c50190` prepares the wire payload
before the enqueue on both the send and edit paths. The statements are kept in
their original tense because the reasoning that found them is the useful part;
read them as a record, not as a work list.

**A final-response transform edits the answer outside the outbox.**
`finalize_streamed_response` applies `_apply_final_response_transform` after the
terminal edit has already been claimed and acknowledged. When the hook changes
the text, `_finalize_visible_replacement_edit` issues a second, direct edit, so
the room shows the transformed answer while the frozen `FINAL` row holds the
untransformed one.

Only one visible message exists either way -- the second edit revises the same
event -- so the outbox's central invariant holds. What diverges is the durable
record: a rerun turn reading the row back reports the untransformed body, and a
crash between the acknowledgement and the transform edit recovers to the
untransformed text.

The fix is not to update the frozen row, which is frozen for a reason: a retry
must resend what may already have been accepted. The right shape is to apply
the transform *inside* the terminal-edit callback, before the enqueue, so the
transformed text is both what is frozen and what is sent, and
`finalize_streamed_response` finds nothing left to change. That moves a hook
with its own cancellation semantics onto the streaming terminal path, so it is
its own change rather than a call-site edit.

**An oversized terminal edit freezes different bytes than it sends.**
`send_message_result` runs `prepare_large_message`, which for content above the
event limit uploads a sidecar and rewrites the payload with a fresh MXC URI --
and, in an encrypted room, fresh file keys. The row is frozen *before* that
rewrite, so recovery re-runs the upload and sends different bytes than the
first attempt did.

The visible outcome still converges, because Matrix deduplicates on the
transaction ID alone rather than on content. The costs are a redundant upload
on every recovered oversized answer, and a stored payload that does not
describe the event it produced. The fix is to prepare the wire payload before
enqueueing and give both the live and recovery paths a send-already-prepared
primitive, so nothing is rebuilt after the claim.

## Membership fencing is live (2026-08-06)

`advance_membership_epoch` had no production caller, which made every fence
built on it -- hydration, approval cards, unattempted outbox rows, and the
reply-fallback read -- correct but unreachable. Two independent reviews found
this in the same round.

`MembershipFence` (`src/mindroom/event_journal/membership.py`) now owns the
decision and `bot.py` calls it at the two membership transitions: immediately
on a local leave, and for sync-reported departures.

The interesting part is not the wiring but the exactly-once rule. One departure
reaches the bot twice -- locally, and again when sync reports it -- and both
reviews proposed guarding the second with `_local_departures_awaiting_sync`.
That set is wrong for the job: `_on_room_joined` discards from it, so a rejoin
between the leave and its echo re-arms the guard and the echo fences a second
time. The second fence deletes the conversation just hydrated under the *new*
membership along with any answer queued for it, which is the exact damage the
epoch exists to prevent.

The fence therefore keeps its own record of departures awaiting an echo, and a
join does not clear it: the echo is still owed, and when it arrives it still
describes the departure that was already accounted for.
`test_a_rejoin_before_the_echo_keeps_its_projection` pins this.

A cache-trust reset deliberately does not advance the epoch. Legacy
certification failure is not a Matrix membership transition.

## The read cutover's remaining reply-fallback defects (2026-08-06)

> **Resolved.** These dated sections are kept as a log of how the work went, not as open items.
> Everything the census listed landed with the deletion of `matrix/cache/`; the reply-fallback
> defects and both wedges are fixed and pinned. Read them for the reasoning, not for the status --
> the status is the summary at the top of this document and the gate check above it.


Two found by review, both real, one of them a genuine regression.

**A redacted revision was offered as a reply target.** `latest_visible_event_id`
returned `revision_event_id` unconditionally. When the revision currently on
screen is redacted, `_project_redaction` clears the body but keeps the row and
its revision pointer, so the query answered with a deleted event and a reply
quoting it renders as nothing. The row's logical event is not redacted -- a
redaction of the logical event deletes the whole row -- so it is the correct
answer in that window, and the query now returns it.

Not a regression: returning the *revision* rather than the logical event. The
old cache path did the same thing (`visible_event_id` is `latest_event_id`,
which is the edit's ID), so the spec argument against it, whatever its merits,
is about a choice this cutover inherited rather than one it made.

**A caller that just sent was made to guess.** Deleting outbound seeding made
reads after a send echo-ordered, and the plan already says a turn that must
know its own last message uses the event ID from the send response. Two
compound sends did not: the voice tool discarded `companion_event_id` and
re-queried, and the message tool passed only the thread root to its first
attachment. Both chained under the message before the one they had just sent.

`ConversationReader.latest_thread_event_id` now takes
`known_latest_thread_event_id` alongside the `reply_to_event_id` and
`existing_event_id` short-circuits it already owned, so one place decides what
outranks what. The shared test double follows the same precedence, because one
that answered a fixed value would let a caller silently stop passing it.

## The cache census, as the remaining work plan (2026-08-06)

> **Resolved.** These dated sections are kept as a log of how the work went, not as open items.
> Everything the census listed landed with the deletion of `matrix/cache/`; the reply-fallback
> defects and both wedges are fixed and pinned. Read them for the reasoning, not for the status --
> the status is the summary at the top of this document and the gate check above it.


Confirmed against HEAD by review. The 8c -> 8e -> 8f macro-order holds, with
the membership fence landing first (done above). The census added seven items
the phase list had missed:

- Raw-room replay proof (`turn_controller.py`, `dispatch_replay_guard.py`)
  reads recent room events and resolves their thread IDs through the cache.
- `ThreadReadMode` and the point-read `turn_scope` memoization still leak
  cache-specific semantics into the already-cut-over resolver and turn
  controller.
- Hook context reads the cache-only `AgentMessageSnapshot`.
- Sidecar resolution keeps legacy event-ownership and MXC branches in
  `message_content.py`, though the hydrator already resolves current-revision
  sidecars without the cache.
- Tool-runtime construction refuses to build a context without an event cache
  that no production tool consumes.
- Thread export is a third direct old-history consumer and needs its own slim
  Matrix pagination path, because the projection is bounded and non-exporting
  by design.
- `sync_continuity` imports its persisted checkpoint type from the
  certification owner slated for deletion, so that type must move first without
  changing the persisted format.

8c is narrowed on the same evidence: only two production consumers of
`get_event` exist, and one of them dies with the cache. The replacement is
relation resolution against the projection with a Matrix point fetch when the
event was never observed, memoized for the turn and persisting no raw event
JSON -- not a durable general-purpose lookup, which would rebuild the cache
this phase exists to delete.

## A second wedge, found while proving the first (2026-08-06)

The Classic-sync livelock has a fix, and it is not the whole story. A later
live run wedged again with none of that defect's signatures: zero
`matrix_sync_rebuild_retry_backoff`, zero `Abandoning recovery at the room
event cap`, zero `sync_recovery_incomplete`, zero
`matrix_sync_certification_uncertain`. The livelock is loud on every retry, and
that window was silent. It also needs more than fifty events in one sync window
to make the server report `limited`, which a short burst right after a restart
does not reach.

What the log does show is where the turn stopped. The event reached
`coalescing_gate_message_enqueued` and that line is the last
`mindroom.coalescing` entry in the file. Every other enqueue in the same run is
followed by `coalescing_gate_flush_started` within about a millisecond and then
`flush_finished outcome=dispatched`. This one never gets a `flush_started` at
all.

So the stall is in the coalescing gate's flush -- after admission, before
dispatch. That is the same "admitted, never dispatched" shape the journal
exists to make impossible, arrived at by a different route: the durable record
is correct and complete, and nothing schedules the work that would drain it.
The event loop was healthy throughout; a later event in the same process got a
complete streamed reply.

Two correlations worth chasing, neither yet a conclusion. The stranded enqueue
lands mid-startup, between `matrix_user_joined_room` and
`startup_phase_finished rooms_and_memberships`, which is not true of any
enqueue that did flush. And that same event fired
`matrix_event_callback_started` twice for the agent, 13 ms and 19 ms apart,
with one `Received message` and one enqueue -- duplicate delivery across the
restart.

`src/mindroom/coalescing.py` and `src/mindroom/ingress_lanes.py` are where to
look. This is a real open defect, distinct from the livelock, and it is the
reason the live proof cannot yet be called green.

### Found: a contended turn claim wedging its own flush

Both correlations were one mechanism, and neither is in the gate's own
scheduling. The flush task is created, alive, and on the right loop; it is
blocked, in `_wait_for_lane_slots`, on a lane slot that will never settle.

`_handle_message_inner` reserved the sender's lane slot and then claimed the
turn, so a callback that lost the claim waited in `wait_for_turn_settled` while
still holding its slot. The winner's batch does not flush until every
undelivered slot in that sender's lane settles; the loser's slot does not
settle until its callback returns; its callback does not return until the
winner's turn settles. Load cannot break a cycle, which is why the wedge is
restart-bound rather than load-bound: the duplicate callback that contends the
claim comes from the recovery pass, and a run with no restart never produces
one.

The duplicate is not itself a defect. The journal deliberately allows one
event's callback to run twice -- `PendingEventWorker.drain_once` does not
consult the pump's per-room lanes, and `JournalDispatcher.drain_once` clears
the deferral set first -- and relies on `TurnStore` claiming to make that safe,
which its own docstring says. Two router relays sharing a human alias contend
the same way with no restart involved. The claim wait now releases the lane
before waiting and takes a fresh lane position if it goes on to reclaim, which
is what the media path already did by claiming before reserving, and what the
edit and interactive-selection paths already did by releasing first.

The safety net could not have caught this. A turn-backed handler that defers
returns `None`, which puts its event in `PendingEventWorker._deferred`, and
`_collect_dispatchable` skips deferred events on every later scan. The durable
row still says the work is owed, and nothing in the running process looks at it
again: only `release`, `forget_all_deferrals`, settlement, or a restart clears
the deferral. `pending` means both "never started" and "started, someone else
owns it", and the only thing separating them is in memory, with no timeout and
no liveness check on the owner. Any owner that dies without releasing produces
this exact shape. Closing that class needs a bounded liveness check on deferred
events rather than a fix in any one owner; until then, a flush waiting on a
lane for more than a minute at least says so once
(`coalescing_gate_lane_wait_stalled`) instead of going silent.

## How this work was done, and why (2026-08-07)

The technical narrative above is the *what*. This section is the *how*, written down because it is not derivable from the diff and because it is the part that kept catching real defects.
Anyone picking this up should read it before writing code.

### Verification is mutation testing, not a green suite

A passing test proves nothing until it has been shown to fail.
Every fix in this work is pinned by reverting the production change and confirming the matching test goes red, and the mutation result is recorded in the commit message.
This found more than it cost.
Several tests that looked like coverage turned out to pin the defect instead: one blanked an outbox row's device without changing it, so no duplicate was reachable and its "no scan happened" assertion was recording the bug rather than preventing it.
Another asserted a terminal record travelled through the transport while injecting the binder, so it never reached the module that does the binding.

Four ways a mutation lies, all of which happened here:

- The mutant never ran because the file was rewritten underneath the run by a concurrent agent. Verify the patch applied, by checksum, immediately before trusting the result.
- The mutant left the file syntactically invalid, so pytest exited 4 — a collection error reports zero failures while testing nothing. Always read the exit code, never just the failure count.
- The failure grep used `^FAILED`, which matches nothing because pytest colourises and the line begins with an ANSI escape. Grep unanchored, or strip escapes.
- The test was aimed at the wrong seam. A surviving mutant more often means the test cannot reach the mutated module than that the code is unguarded. When a property spans two seams — a decision and its transport — each seam needs its own mutation.

This repo also prints no `N passed` summary line, so `grep passed` finds nothing even on a clean run.
Count `FAILED` unanchored and read `PYTEST_EXIT`.

### Reviews are untrusted input, including the ones that are right

Independent review ran continuously against the branch, and its findings were treated as claims to verify rather than instructions to follow.
That distinction earned its keep in both directions.

Reviews caught defects that would otherwise have shipped: hydration counting pages instead of logical messages; deleting attempted outbox rows; an entire installation's terminal truth being ignored on upgrade so the bot would re-answer its whole backlog; a non-atomic acknowledgement that let two PostgreSQL processes both claim a row.

Reviews were also wrong in ways that would have made things worse.
One proposed binding the Matrix transaction ID to the membership epoch, which converts a *suppressed* duplicate into a *visible* one.
Another proposed commit-then-publish for the ledger, closing a rare "failed write plus restart" duplicate while opening a common "any in-flight write" one.
A third classified a publish-before-commit window as a branch regression when `origin/main` had the same exposure with a larger window.
None of those could be told apart from the correct findings without reading the code.

So: reproduce before fixing, and say plainly which claims are wrong and why.
A review that is right four times out of five still needs the fifth checked.

### Live gates, and what each actually proves

Two gates run after every phase, and neither substitutes for the other.

`tests/manual/event_journal_live_proof.py` spins up a disposable Tuwunel and asserts 22 properties against a real homeserver — transaction-ID deduplication being *device-scoped*, edit churn collapsing to one logical row, sidecar upload and resolution, cold history starting zero pending work.
These are facts about Matrix that no fake can establish, and the harness's own weakness had to be fixed first: it originally demonstrated device scoping using a second registered account, which proves nothing about devices.

`scripts/testing/fuzz_live_matrix.py` runs 200 concurrent operations with periodic restarts against the same stack.
Its invariant is `_assert_no_wrong_replies`: exactly one reply per source, no strays.

**Pass `--restart-interval` explicitly, or the restarts do not happen.** It defaults to 100 (`fuzz_live_matrix.py:3078`), so the obvious `--steps 200` run produces a single restart and the restart pressure this gate exists for is absent while the run still reports `PASS`. The green run behind this document is `--seed 42 --steps 200 --threads 45 --restart-interval 10`, which produced **18 restarts** across 44 batches; the generator drops the trailing restart once the step budget is spent, which is why it is 18 and not 20. `--profile restart-regression` is a separate fixed profile that ignores this flag.
**`canonical_agent_replies` is not that check** — it is `len(oracle.expected_sources)`, a count of prompts issued, and comparing it across runs measures the scenario rather than the runtime.
That number was twice cited in this document as the duplicate gate before anyone read its definition.

### Deletion is an acceptance criterion, not a side effect

A cutover that adds a replacement while the replaced thing still stands is not a cutover.
Every remaining phase had to delete its predecessor in the same change and leave the tree net smaller, and the row tracking this went the wrong way three times before it turned.

The rule has teeth in both directions.
It is why `delivered_turn_repair.py` going away is the *proof* that terminal truth collapsed to one owner rather than gaining another reconciler — and why an approval-card change that was green, well-tested, and net **+68** was held back rather than merged.
It is also why unused scaffolding is not allowed to accumulate: a `TerminalTurnWrite` path was written, discovered to have no producer, and either had to be wired or deleted.

When a replacement cannot demonstrate deletion, the honest move is to stop and reconsider the design rather than add another repair layer.

### Prove it by compiling, not by tracing

Consumer traces are not proofs.
The history-debt deletion was traced by hand and declared clean; building it against the real nio branch surfaced a constraint no trace could have: `persist_recovery=True` is now *required*, because nio refuses an acknowledgement past an open gap unless the gap row outlives the process, and with it false every certified response raises `LocalProtocolError`.

The same applies to the object under test.
A green suite in a working tree says nothing about the commit if an untracked file is load-bearing — a marker registered only in an uncommitted `pyproject.toml` made seven tests pass locally and would have failed in CI.
Verify with the working tree clean and the tree equal to `HEAD`.

### Parallel agents share one index, and that is the hazard

Several agents worked concurrently. The collisions were not in the code, they were in git and in the filesystem:

- `git add` is per-file but `git commit` commits the whole index, so one agent's commit swept four files belonging to two others. Commits are serialized through one owner now.
- `pre-commit` stashes every unstaged file in the repository and restores it afterwards. With concurrent writers that window silently reverted another agent's `cp`-based mutation restore.
- Reading a file mid-edit yields errors that belong to nobody — `ImportError` for a symbol another agent is halfway through deleting. Do not chase those, and never "fix" them by editing outside your scope.

Give an agent its own worktree when it can have one, and check the base: a worktree auto-created from `origin/main` will look plausible and contain none of the branch.
When agents must share a tree, freeze ownership explicitly and say who owns which files.

### The pattern worth naming

Three times, a first fix was itself the bug: stamping the sending device at claim time, sending a fallback and then adopting it, and rolling back an in-memory record on cancellation.
Each was reproduced, each looked correct, and each was caught by the next review.
In all three cases the second attempt was *simpler* than the first — read the room, do not send at all, wait for the write to report.

Complexity added to fix a correctness bug deserves more suspicion than the bug did.
