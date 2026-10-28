# Approval records: one row per decision and card, one owner for what a paused reply does next

Status: implemented (2026-10-28).
Scope: the journal tables and modules that hold approval state, the approval manager, the paused-reply lifecycle, approval holds, and detached (job, background-script, scheduled-call) approvals.
The design went through four critique rounds before implementation; this record keeps what the code cannot show.

## Problem

Every approval kind grew its own storage, card lifecycle, recovery path, and resume path, so one fact had several owners and each new kind added a branch to every reader.

- A pending call's decision lived in `approval_continuation_calls` (paused replies) or `background_approval_calls` (jobs, scripts, scheduled calls), again in the card's terminal edit, and for scheduled calls a third time on `scheduled_call_approvals`.
  Two resolvers checked "already decided" differently, and a responder departure wrote an expired terminal edit without deciding the call, so the two could disagree.
- Card kind was inferred from a join plus the payload's `approval_target` (`DETACHED_GENERATION = -1` stood in for "no paused reply"), and card lifecycle from which rows existed in five places.
- The manager decided approver, expiry, and offered options by parsing stored card JSON, while the journal decided expiry from `expires_at_ns`.
- Eleven functions in four modules decided what a paused reply does next, reading "did the final answer reach Matrix" six ways; a permanently refused final answer ended four ways.
- A paused reply's failure cause was free text parsed back by string comparison and shown as the cards' denial reason, so a stopped reply's cards said "Tool approval request timed out.".
- Approval holds were an in-memory copy updated at five sites and repaired by a 30-second recheck that had an accepted race.
- Detached approvals duplicated the "does this need a person" pre-check, polled the journal every second, and retried failed job-card denials from an in-memory set a restart lost.

## Store topology

A runtime writes its journal through exactly one `EventJournalStore`: the orchestrator opens it, holds the database exclusively, and every bot borrows it.
The shared lease `open_event_journal` takes only keeps `mindroom journal adopt` away; it does not exclude a second store.
Only tests open a second store over one database, to exercise transaction races between independent writer connections.
In-process notifications (`ApprovalSignals`) therefore belong to the store and see every write in production; a test's second store notifies only its own subscribers.

## Decisions

### One decision per call, one row per card

- `approval_calls` is the only owner of a decision: a policy decision is stored with the call when a paused generation is inserted, and every later decision goes through `resolve_call`, which builds the terminal edit from the decision in the same transaction.
- A call's key leaves the principal out, so the database guarantees the global uniqueness card joins already relied on (`approval_id` was unique; detached run ids are UUID-based or install-unique job and task ids).
- A reply-owned call outlives its paused reply until every card that shows it retires, because a responder departure deletes the paused reply but keeps the router's terminal-edit debt, whose decision would otherwise have no owner.
  The call keeps its principal for the same reason.
- A card stores no `decided` or `finished` state: a request card is decided exactly when its call is, and finished when the outbox acknowledges its terminal edit; storing either again would recreate the two-owner bug.
- Every Matrix event known to show a card is a row in `approval_card_events`, written by the acknowledgement of the card's first delivery, because one delivery can have several events (transport-verified receipt copies) and a retired card must still be recognized on a click.
- A card's delivery tenure is its INITIAL outbox row's membership epoch, with no copy on the card, because departure cleanup moves a card's outbox rows to a rejoined membership and a copy would be a second owner of that tenure.
- A card's timed-approval consent (`approval_card_scopes`) is immutable, separate from the card's delivery tenure, because that move must not move the grant's fence with it.
- The frozen card payload is only the source of the terminal edit's content; Matrix wire payloads are unchanged, so clients see the same cards.
- Within one runtime every write runs on one writer connection, which the journal already relies on; the new tables take the locks the rows they replace took, in the same order, so the races the two-store tests pin stay pinned, and no wider lock order is promised.

### What a paused reply does next

- `approval/continuation_rules.py` is one pure `next_step(facts, trigger)`, in the style of `reply_lifecycle.py`, which keeps reply presentation and unapplied-Stop decisions.
- A characterization matrix pinned every former decider's outcome against the unchanged code before the rules replaced them, and the rules keep today's outcome wherever the matrix pinned one, including cases an earlier draft of this design would have changed (a dispatch that settles a waiting reply whose lease this runtime holds, a failing reply with an acknowledged final that runs a full settlement).
- Whether an acknowledged final owes the response lifecycle's finalization follows from the paused reply's state, not from which span wrote the final, because a failure-settlement note can be written by the resume span itself: a claimed reply's final is its resume's outcome and finalizes, a failing reply's may be a settlement note and does not.
- A refused final answer finishes the paused reply on every path; before, a live resume kept its claim until a restart.
- A live resume that fails keeps settling directly; deferring every settlement to dispatch would still need the live span ended and the final recovered first, and would put a direct settlement behind a wake.
- `failure_kind` (`error`, `user_stop`, `restart`, `interrupted`, `shutdown`) also names the cards' denial reason, so a Stop's cards say the reply was stopped and an error's give its detail.
- The failure owns the one redacted rendering of its text, shared by the reply note, the cards, and delegated children, because each rendering site redacting on its own let the cards show raw error text the note hid; the stored detail stays raw.
- Minimal-mode CLI exits keep their own exhaustive branch, evaluated before every other rule, because the enclosing response has already finalized and a stale claim belongs to another runtime.

### Holds and detached wake-ups

- Holds and detached wake-ups are derived from committed journal writes through one `ApprovalSignals` per store, instead of copies updated at each call site and repaired by rechecks.
- An operation records its signals on its transaction; the backend keeps them only when the operation's savepoint held and the batch committed, and the writer task publishes them in commit order before it resolves any caller, because a caller cancelled after its commit would otherwise never publish and callers on other loops resume in no defined order.
- A pause's hold applies at its commit, while the reply still holds the conversation lock, so no message is answered between the pause and its hold and no end precedes it; that is why the 30-second recheck and its accepted race could go.
- A detached waiter registers before its card is reserved and clears before each read, so a decision on either side of a read wakes it.
- Decisions the journal writes without the manager (a job's end, a departure) still reach Matrix at once, because the manager follows decided-call signals and flushes those cards; approval cards are flushed only by the manager, since the bots' generic outbox recovery skips their event type.

### Detached approvals

- One `human_gate` answers "needs a person, or decided already" (no approver, an `!auto_approve` window, the router missing) for every kind, so background scripts get the router-missing denial `docs/tool-approval.md` already promised.
- A job's end denies its pending cards in the transaction that saves its terminal snapshot, given a typed flag rather than a reading of job JSON; the job still cancels and joins its card requests first, so none reserves a card afterwards.

### Journal upgrades

- Both backends run one ordered list of upgrades against one schema inspector (`tables()`, `columns(table)`, `dialect`), so a new upgrade is one line instead of a detection query in each backend.
- The approval conversion runs before the schema statements because three new tables reuse old names.

## Conversion of existing records

- **Pending approvals survive**: a card pending before the upgrade can still be answered and its paused reply resumes, because every fact a decision or resume reads is copied; metadata the old rows never stored is left `NULL`.
  This differs from the v2026.10.228 upgrade, which cancelled pending paused-reply approvals because it could not rebuild their reply identity.
- **Pending job cards are denied**, with the reason a restart gives a job that waited for approval, because the restart that runs the upgrade interrupts every waiting job anyway and an earlier release could leave an ended job's card pending when its denial failed; the conversion reads no job JSON and runs whether background jobs are enabled or not.
- A detached call's kind comes from its scheduled binding, else its `tool-job:` run id, else script, because a scheduled call's card payload is usually retired by the time of the upgrade.
- A live card whose calls a departure deleted gets a decided call rebuilt from its frozen payloads, taking the card's principal and ordinals in delivery order; one that never got a terminal edit is expired so it can still retire.
- A retired card's kind and call are never read, so a retired card whose provenance the old rows cannot tell (a batch-approved request or a receipt) is recorded as a receipt, and a grant whose originating card left no grant-card row gets a retired request card with a placeholder call key.
- An old tombstone that matches no converted card gets such a placeholder card too, because tombstones never stored their delivery, and a click on it then takes the one retired-card path instead of a special terminal value.
- Free-text failure reasons convert to their kinds (`cancelled_by_user`, `sync_restart_cancelled`, `interrupted`, the fixed interrupted-before-final text), and any other text becomes an `error` with the text as detail.
- Rows recorded from the previous code for every approval kind and state (`tests/fixtures/approval_records/`) pin the conversion on both backends.

## Behavior changes

- A stopped, restarted, or failed reply's cards say why instead of "Tool approval request timed out.".
- A refused final answer finishes its paused reply on every path.
- Background scripts in a room without the router are denied with the router-missing text instead of failing the call.
- A job's cards are denied in the commit that records the job's end, so an ended job never leaves an answerable card.
- Detached waits end as soon as their decision commits.
- Cancelling a scheduled task with a stored call is refused while the approval runtime is not ready, instead of silently leaving its approval in place.

## Open decision

- Minimal-mode (CLI) approvals as detached waits, the way job approvals work, would remove the in-place claim, the suspension side channel, the `cli_call` blob, `continue_cli_approval`, and the `cli_exit` rules.
  It changes user-visible behavior (a minimal-mode call still unapproved at a restart or at the end of its 24-hour CLI lifetime would be reported as not run), so it waits for the owner's decision.

## Rejected alternatives

- A stored `decided`/`finished` card state: duplicates the call decision and the outbox acknowledgement.
- One `card_event_id` column on the card: cannot hold receipt aliases.
- Deleting reply-owned calls with their paused reply: departure keeps terminal-edit debt whose decision would then have no owner.
- Deriving the grant's consent epoch from the card's delivery tenure: departure cleanup moves delivery tenure.
- Settling every failed live resume from dispatch: see above.
- Publishing signals from the store method after its write returns: a caller cancelled after the commit never publishes.
- One notification registry per process keyed by database: only tests open a second store, and production refuses a second runtime.
- Keeping the 30-second hold recheck as a safety net: with every mutation publishing through one owner it would only hide a missing publication.
- Cancelling pending approvals at upgrade: the conversion preserves what a resume needs.
- From the task brief, unchanged: merging `!auto_approve` windows into timed grants (different products: all tools, no config or membership fence, no receipts), storing detached calls as paused replies (null-reply branches everywhere), making job approvals survive restart (a resume path the docs deliberately leave out), and rebuilding a paused reply's request from the journal (it would rerun ingress policy).
