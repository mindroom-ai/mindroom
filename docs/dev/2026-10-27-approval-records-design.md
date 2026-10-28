# Approval records: one row per decision and card, one owner for what a paused reply does next

Status: approved for implementation (2026-10-28), revision 4.
Scope: the journal tables and modules that hold approval state, the approval manager, the paused-reply lifecycle in `ResponseRunner`, approval holds, and detached (job, background-script, scheduled-call) approvals.

## Problem

Every approval kind grew its own storage, card lifecycle, recovery path, and resume path.
The same fact has several owners, and each new kind or feature added another branch to every reader.

- A pending call's decision lives in `approval_continuation_calls` (paused replies) or `background_approval_calls` (jobs, scripts, scheduled calls), again in the card's terminal edit payload, and for scheduled calls a third time as `decided_by`/`approved_scope` on `scheduled_call_approvals`.
  Two resolvers check "already decided" differently: the detached one reads the decision column, the continuation one reads the terminal edit.
  A responder departure writes an expired terminal edit without deciding the call, so the two can disagree.
- Card kind is inferred from a table join plus the payload's `approval_target`, with `DETACHED_GENERATION = -1` standing in for "no paused reply"; job cards are labelled `background_script`.
- Card lifecycle is inferred from which rows exist: `approval_cards`, the outbox, `approval_action_tombstones`, `approval_grant_cards.grant_id`, and `scheduled_call_approvals.consumed_delivery_id`.
  `card_event_id` is copied into four tables, and ten grant-scope columns are copied between `approval_grant_cards` and `approval_grants`.
- The manager decides approver, expiry, offered options, and target kind by parsing stored card JSON, while the journal decides expiry from `expires_at_ns`.
- Eleven functions in four modules each decide what a paused reply does next and read "did the final answer reach Matrix" six different ways.
- A paused reply's failure cause is free text parsed back by string comparison, and the same text becomes the card's denial reason; a stopped reply's cards say "Tool approval request timed out.".
- Approval holds are an in-memory copy updated at five sites and repaired by a 30-second recheck with an accepted race.
- Detached approvals duplicate the "does this need a person" pre-check, poll the journal every second, and retry failed job-card denials from an in-memory set lost on restart.

## Store topology

A runtime writes its journal through exactly one `EventJournalStore`: the orchestrator opens it, holds the database exclusively (`hold_exclusively`, so a second runtime against the same database refuses to start), and every bot borrows it.
The shared lease `open_event_journal` takes only keeps `mindroom journal adopt` away and does not exclude a second store.
Bots open their own store only when built outside the orchestrator, which only tests do; tests that open two stores over one database exercise transaction races between independent writer connections, and the lock order below keeps those safe, but notifications reach only the store that committed the write.
The notification owner below belongs to the store, and the design records this topology beside it.

## Target storage

### `approval_calls`: one row per gated call, whatever owns it

| Column | Fact |
|---|---|
| `owner_id`, `generation`, `tool_call_id` | Primary key: the paused reply's `approval_id` and generation, or the detached run id (`tool-job:<job>`, script run id, `scheduled-task:<task>`) with generation `0` |
| `principal_id` | The owner's principal, kept because a decided call can outlive its paused reply and its cards |
| `owner_kind` | `reply`, `job`, `script`, or `scheduled` |
| `call_ordinal` | Order within its generation |
| `tool_name`, `toolkit_name`, `invoking_agent`, `arguments_digest`, `human_approval_required` | What the call is and why it was gated |
| `approver_user_id` | Who may decide; `NULL` when policy decided |
| `expires_at_ns` | The only deadline |
| `decision`, `reason`, `resolved_by`, `resolved_at_ns` | The only decision |
| `approved_scope` | The scheduled scope the requester selected, else `NULL` |
| `grant_id` | The timed grant that approved the call, else `NULL` |

The primary key leaves the principal out, so the database itself guarantees what joins already rely on: `approval_id` is globally unique today, and detached run ids are UUID-based or named after install-unique job and task ids.
A reply-owned call outlives its paused reply until every card that shows it retires, so a card's decision always has its one owner.
Calls with no live card are deleted with their paused reply; the rest are deleted when their last card retires.
Detached calls are pruned by their owner after their cards retire, as today.

### `approval_cards`: one row per Matrix card or receipt

| Column | Fact |
|---|---|
| `principal_id`, `delivery_id` | The card principal and its outbox delivery |
| `kind` | `request` (asks for a decision), `grant_receipt` (a timed grant approved the call at once), `scheduled_receipt` (a scheduled call ran) |
| `owner_id`, `generation`, `tool_call_id` | The call it shows |
| `room_id`, `thread_id` | Where it was posted; immutable |
| `approvable` | Whether an approval can be honored: its arguments were reviewable when it was prepared |
| `membership_epoch` | Delivery tenure: the card principal's membership that owns its outbox rows, carried to a rejoined membership by departure cleanup |
| `retired` | Its outbox payloads are deleted; the row stays for click recognition and grant revocation |

No `decided` or `finished` state is stored: a request card is decided exactly when its call is, and finished when the outbox acknowledges its terminal edit.
A request card is actionable when it is not retired and its call is undecided and unexpired; receipts and retired cards are terminal.

### `approval_card_events`: every Matrix event known to show a card

`(principal_id, card_event_id)` primary key, `room_id`, `delivery_id`.
The acknowledgement of a card's INITIAL delivery inserts its row in the same statement sequence that binds the outbox row (one more statement in `outbox.acknowledge`, beside the reply-row edit-target update it already makes).
A transport-verified copy of an automatic receipt adds an alias row for the same delivery, which `remember_terminal_alias` does today through tombstones.
A click resolves its card through this table, so it replaces `approval_action_tombstones` and every approval-domain read of `initial.acknowledged_event_id`.
`delivery_id` is `NULL` only for a terminal identity whose card is unknown, which the conversion of old tombstones produces.

### `approval_card_scopes`: the consent a card offers for a timed approval

One row per card that offers "approve for N minutes", keyed by `(principal_id, delivery_id)`: `scope_key`, `operation`, `requester_id`, `entity_name`, `invoking_agent`, `responder_principal_id`, `responder_epoch`, `consent_epoch` (the card principal's membership epoch when the card was reserved).
The consent epochs are immutable; delivery cleanup moves only `approval_cards.membership_epoch`.
Whether a card offers timed approvals is this row's existence.

### `approval_grants`

`(principal_id, grant_id, delivery_id, expires_at_ns, revoked_at_ns, resolution_json)`, unique on `(principal_id, delivery_id)`.
`delivery_id` names the originating card; requester, entity, scope, operation, consent epochs, room, thread, and the card's event come from that card, its scope row, and its event row.
Revocation keeps today's order: revoking commits at once, the revocation edit is queued only after the original terminal edit settles, and that debt outlives the grant's expiry.

### `scheduled_call_approvals`

Keeps the stored call (`task_id`, room, thread, requester, agent, toolkit, tool, arguments, workflow digest, `execute_at_ns`, membership epoch) and its own lifecycle: `any_arguments_offered` (whether its card offered approval for any arguments), `armed_at_ns`, `armed_scope` (the scope `arm` narrowed the selection to), `revoked_at_ns`, `consumed_at_ns`, `outcome`.
`decided_at_ns`, `decided_by`, `approved_scope`, and `card_event_id` move to the call, its card, and its event row; the receipt is the `scheduled_receipt` card of the call.
`claim` keeps its one transaction: identity checks, timing window, membership fence, consumption, and receipt reservation.

### `approval_continuations`

`failure_reason TEXT` becomes `failure_kind` (`error`, `user_stop`, `restart`, `interrupted`, `shutdown`) plus `failure_detail TEXT` for `error`.
`approval_continuation_calls` folds into `approval_calls`.

### Dropped

`background_approval_calls`, `approval_continuation_calls`, `approval_grant_cards`, `approval_action_tombstones`, `DETACHED_GENERATION`, and the `_AUTOMATIC_RECEIPT`, `GRANT_RECEIPT`, and `RECEIPT` predicates.
`approval_grant_locks` stays.

## Writers and locks

One module owns each write.
Within one runtime every write runs on the store's single writer connection, which the journal already relies on (a pause writes its paused reply before its reply row).
The new tables take exactly the locks the rows they replace take today, in the same order, so the races today's two-store tests pin (reservation against a timed grant, a click against a failure fence) stay pinned; this design promises no lock order beyond them.

- **Reserve.** `reserve_reply_cards` (a paused reply's generation) and `reserve_detached_card` insert the calls, apply an active grant (which decides the call and inserts a `grant_receipt` card instead of a request card), or insert the request card with its scope row and outbox INITIAL.
  A paused reply's generation keeps its publication lease: it stays `waiting` with `runtime_generation` set until `activate` releases the lease, even when every call was decided automatically, so no decision can make it ready before its pause presentation commits.
- **Resolve.** `resolve_call(call_key, requested, reason, metadata)` is the only writer of a decision.
  It locks in the order above, returns an existing decision unchanged, and applies the fences: expiry from `expires_at_ns`; for `reply` owners the generation must match, the paused reply must be `waiting` or `failing`, and a `failing` reply turns an approval into a denial with its failure's card reason.
  It writes the decision, queues the terminal edit of the call's live request card built from the frozen INITIAL payload, and for `reply` owners moves a lease-free `waiting` reply whose calls are all decided to `ready`.
  Card clicks, timed-grant batches, deadline expiry, failure settlement, job and script settlement, scheduled withdrawal, and both departure paths call it.
- **Retire.** A request card retires after its terminal edit is acknowledged, a receipt after its INITIAL delivery is acknowledged or permanently refused, as today.
  Retiring deletes the card's outbox rows, sets `retired`, deletes a non-originating card's scope row, and deletes a reply-owned call whose paused reply is gone and that no live card shows.
  A card whose delivery was never attempted is still deleted outright by departure cleanup.
- The manager's click handling reads one typed `ApprovalCardView` (card, call, scope, scheduled offer) and never parses card JSON for a decision input; the frozen payload is only the source of the terminal edit's content.

## Paused replies: one rules module

`approval/continuation_rules.py` is pure, in the style of `reply_lifecycle.py`.
Reply presentation and unapplied-Stop decisions stay in `reply_lifecycle.py`; these rules decide only the paused reply's next step.

```python
next_step(facts: ContinuationFacts, trigger: Trigger) -> Step
```

`ContinuationFacts`: state (`waiting`, `ready`, `claimed`, `failing`); who holds the lease or claim (`none`, `self`, `other`); `failure_kind`; the claim span's outcome; and `FinalAnswer`.
`FinalAnswer` is computed by one read of the delivery `ApprovalContinuation.final_delivery_id` names (today's repeated `source_event_ids[0]`), after delivery recovery when the caller asks for it: `none`; `pending` (queued, not acknowledged); `answered` (acknowledged, its span completed); `ended` (acknowledged, its span did not complete); `refused` (permanently failed).
Whether an acknowledged final owes the lifecycle's finalization (post-response effects and cancellation hooks) follows from the paused reply's state, as today: a `claimed` reply's final is its resume's outcome, while a `failing` reply's may be a settlement note.

Triggers: `dispatch` (the owning bot's source worker), `resumed` (a live resume ended), `owner_unavailable` (orchestrator recovery of an entity without a bot), and `cli_exit` (a minimal-mode response's approval scope ended, carrying whether it suspended, whether it is the live or a recovered lifecycle, and whether the process is shutting down).
`cli_exit` exists only while minimal-mode approvals pause their reply; it goes if the CLI redesign is accepted.

Triggers and steps:

| Trigger | Facts | Step |
|---|---|---|
| `cli_exit` | any | the `cli_exit` rows below, before every other row |
| any | `answered` | `finish(finalize=True)`, except after a live resume completed: `finish(finalize=False)` |
| `dispatch`, `resumed` | `ended` while `claimed` | `finish(finalize=True)`, as today's frozen-final recovery finalizes any acknowledged resume outcome |
| `owner_unavailable`; `dispatch` while `failing` | `ended` | `finish(finalize=False)`, as today |
| any | `refused` | `finish(finalize=False)` |
| any | `pending` | `wait` (delivery recovery owns the outcome) |
| `owner_unavailable` | `none` | `discard_unavailable`: fence, expire cards, cancel delegations, send the unavailable notice through an available principal, discard once it is acknowledged |
| `dispatch` | `none`, `ready` | `resume` after the authorization recheck; a failed recheck settles `error` |
| `dispatch` | `none`, `waiting`, lease `other` | settle `error` "Tool approval card publication was interrupted and denied safely." |
| `dispatch` | `none`, `waiting`, lease `none` or `self`; or `claimed` by `self` | `wait` |
| `dispatch` | `none`, `claimed` by `other`, or `failing` with `shutdown` | settle `shutdown` (today's error note "Tool approval continuation was interrupted before final delivery and denied safely.") when the claim span ended failed, cancelled, or suppressed; otherwise `release` to replay |
| `dispatch` | `none`, `failing` with `user_stop`, `restart`, `interrupted`, `error` | settle with that note |
| `resumed(completed)` | `none` | impossible: a completed resume has acknowledged its answer |
| `resumed(advanced)` | next generation `ready` | `resume` the next generation; otherwise `wait` |
| `resumed(failed kind)` without raising | `none` | settle that kind |
| `resumed(raised)` | `none` | fence the observed claim with `error`, then settle `error` with the exception text |
| `resumed(cancelled kind)` | `none` | fence the observed claim with that kind (`shutdown` during process shutdown) and re-raise; dispatch settles |
| `cli_exit` | the scope never paused a call | nothing |
| `cli_exit` | suspended, paused reply `ready` | wake its sources |
| `cli_exit` | suspended otherwise, a recovered lifecycle, no `cli_call`, or `claimed` by another runtime | nothing (the enclosing response already finalized; a recovered lifecycle settles after its own final delivery) |
| `cli_exit` | live, `failing`, no `answered` | settle with its kind, except during process shutdown, which leaves it to recovery |
| `cli_exit` | live, a final that is not `refused` | `finish(finalize=False)` |
| `cli_exit` | live, otherwise | fence with the response's failure kind, except during process shutdown |

Before a `resumed` trigger the runner keeps today's order: it ends a live resume span unless the process is shutting down, recovers FINAL delivery, and then reads the facts; "FINAL acknowledged, then finalization raised" is `resumed(raised)` with `answered`, which finalizes again as today.
`settle` keeps today's executor: fence if not failing, expire the cards with the failure's card reason, cancel delegations, finish when the reply is gone or its FINAL settled, otherwise write the note and finish.
Fences always name the observed state, generation, and runtime owner.

A refused final answer finishes the paused reply on every path; today four paths handle it and all but the live one already finish through `may_finish`.

`approval/continuation_runner.py` runs these steps and absorbs `ApprovalResponseCoordinator`.
The approval lifecycle methods of `ResponseRunner` move there first as a unit, unchanged; the rules replace the deciders in a later commit, against a characterization matrix that pins each current decider's outcome per trigger, state, final answer, claim outcome, and failure kind.

`failure_kind` also names the cards' denial reason: a Stop's cards say the reply was stopped, a restart's that MindRoom restarted, an error's its detail.

## One notification owner: `ApprovalSignals`

`event_journal/approval_signals.py` owns, per `EventJournalStore`, two in-process facts derived from committed writes:

- the paused replies of every principal (`approval_id` to principal, room, thread), loaded when the journal is bound, before any bot starts;
- one wake-up per detached call someone waits on.

An operation records signals on its transaction (`transaction.signal(...)`): pause and advance, finish, release, discard, the departure delete in journal ingestion, `resolve_call` (so grant batches, departures, and job settlement too), and reservations decided at once by an active grant.
The backend keeps an operation's signals only when its savepoint succeeded and the batch committed, and the writer task publishes them in batch order, before it resolves any caller's future.
Publication therefore follows commit order and happens even when the caller is cancelled after the commit; subscribers on another event loop receive theirs through `call_soon_threadsafe`.
Tests pin cancellation after commit, a rolled-back operation, and add-then-remove order across loops.

**Holds.** `ResponseLifecycleCoordinator` subscribes for its principal: subscribing snapshots the current set and registers the listener in one synchronous call, so no change falls between them, and stopping a bot unsubscribes it before its replacement subscribes.
An added paused reply begins a response turn of its conversation, a removed one finishes it.
The pause's add applies at its commit, while the reply still holds the conversation lock, so no message is answered between the pause and the hold, and no end can precede it.
The five update sites, `hold_for_approval`/`release_approval_hold`, the 30-second recheck, and the accepted race go.

**Detached waits.** A waiter registers its wake-up before its card is reserved, clears it before each read of its call, and unregisters only its own registration.
The one-second poll goes; the wait ends at the decision or at `expires_at_ns`.

## Detached approvals

- **One pre-check.** `plan_approval_calls` splits into the policy evaluation and one `human_gate(...)`: no approver, an `!auto_approve` window, the router missing from the room.
  Background scripts call `human_gate` after the broker's policy gate, so they get the router-missing denial and the approver text every other kind gets.
- **Job end denies its cards in the same commit.** The job keeps cancelling and joining its card requests before it ends, so none reserves a card afterwards; then the save of its terminal snapshot, given a typed `ended` flag rather than reading job JSON, decides its pending calls `denied` and queues their terminal edits in that transaction.
  `ToolJobRuntime.unsettled_approvals` and its retry loop go, and `background-tool-jobs.md` loses the limitation that stale job cards can still create timed grants.
  An earlier release could leave an ended job's card pending when its denial failed, so the conversion denies every pending job-owned call, as the restart that runs it would for every job still waiting; it reads no job JSON and runs whether background jobs are enabled or not.
- **Targeted flush.** Detached denials, timeouts, and settlements flush only the cards they decided.

## Approvals sub-store

`PrincipalStore.approvals` returns an `ApprovalStore` over the same backend and principal, like `PrincipalStore.replies`; callers that need approvals take it, not `PrincipalStore`.

## Quick wins

- The manager takes one `ApprovalMatrixTransport` instead of nine of its bound methods; `_configure_transport` and most not-ready checks go.
- The six scheduled-call pass-throughs in `approval/tool_approval.py` go.
- One timeout text, one not-ready text, and one name for the final-answer delivery id.

## Journal upgrades

Both backends gain one schema inspector (`tables()`, `columns(table)`) and run one ordered list of upgrade functions, each taking the transaction and the inspector, inside their existing schema transaction; a new upgrade is one line in that list.
The approval conversion and the `failure_kind` conversion are two entries, after `upgrade_continuation_identity`.
A fresh database has none of the source tables and skips both; a converted database has the new tables and skips both.

## Conversion of existing records

| Source | Target |
|---|---|
| `approval_continuation_calls` row | `reply` call; `approver_user_id` from its request card's INITIAL payload, else `NULL`; `resolved_by`/`resolved_at_ns` from its FINAL payload when one exists, else `NULL` |
| `background_approval_calls` row | `scheduled` call when a `scheduled_call_approvals` binding names its delivery, `job` for a `tool-job:` run, else `script`; approver from its INITIAL payload when one remains, else from the binding's requester for scheduled calls, else `NULL`; resolution metadata as above, and for scheduled calls `resolved_by`/`resolved_at_ns` from the binding's `decided_by`/`decided_at_ns` |
| live `approval_cards` row whose calls are gone (a responder departure kept its terminal-edit debt after deleting the paused reply) | a decided `reply` call rebuilt from the card: tool, invoking agent, approver, and expiry from its INITIAL payload, decision, reason, and decider from its FINAL payload; ordinal `0`, toolkit and digest `NULL` |
| grant receipt whose paused reply and calls are gone | a decided `reply` call rebuilt the same way from the receipt's INITIAL payload, approved by its grant |
| `approval_cards` row | `request` card; room and thread from its INITIAL outbox row, `approvable` from its payload, event row from `acknowledged_event_id` |
| grant card with `grant_id`, no card row, INITIAL outbox row present | live `grant_receipt` card and its event row |
| grant card with `grant_id`, no card row, no outbox row | retired card: `request` when it is a grant's originating delivery, else `grant_receipt` (a retired card's kind is never read, so a batch-approved request recorded as a receipt changes nothing); event row from the grant's `card_event_id` when it is the originating card |
| decided detached call with no card left (its card retired without a grant scope or binding) | decided call with `tool_name`, `invoking_agent`, and `approver_user_id` `NULL`, kept so a repeated request of the same call reads its decision and the owner can prune it |
| grant card | scope row (consent epoch from its `membership_epoch`), `grant_id` onto its call |
| `approval_grants` row | grant pointing at `original_delivery_id` |
| `scheduled_call_approvals` row | trimmed row; selected scope from the FINAL payload's `scheduled_scope`, else `approved_scope`; `armed_scope` = `approved_scope` when armed; `any_arguments_offered` from the INITIAL payload, else `false` once decided; its request card from `delivery_id`, retired with its event row from `card_event_id` when its outbox rows are gone; receipt card from `consumed_delivery_id`, retired when its outbox row is gone |
| pending `job` call | denied with the reason a restart gives a job that waited for approval, its terminal edit queued |
| tombstone matching a converted card's event | that event row |
| tombstone matching no converted card | event row with `delivery_id` `NULL`: a terminal identity with no provenance, matched by event id alone |
| `failure_reason` | `cancelled_by_user`: `user_stop`; `sync_restart_cancelled`: `restart`; `interrupted`: `interrupted`; the fixed interrupted-before-final text: `shutdown`; other text: `error` with it as detail |

**Pending approvals survive the conversion**: a card pending before the upgrade can still be answered, and its paused reply resumes, because every fact a decision or resume reads is copied, and metadata that was never stored is left `NULL`.
This differs from the v2026.10.228 precedent, which cancelled pending paused-reply approvals because it could not rebuild their reply identity.
Fixtures recorded from the current code for every kind and state pin the conversion on both backends, and each legacy reader carries a `LEGACY_COMPAT:` marker with release provenance verified from tags.

## Implementation order

1. Characterization fixtures of current rows, and a characterization matrix of the current deciders.
2. `ApprovalStore` sub-store, unchanged methods.
3. The approval lifecycle methods move from `ResponseRunner` into `approval/continuation_runner.py`, unchanged.
4. New storage, writers, conversion, and typed manager reads.
5. `failure_kind`, the rules module, and the deciders replaced.
6. Detached pre-check, job settlement in the job's commit, targeted flushes, quick wins.
7. `ApprovalSignals`: holds and detached waits.
8. Documentation: architecture pages, the code map, `migrations.md`, and the user pages whose behavior changes.

## Behavior changes

- A stopped reply's cards say the reply was stopped instead of "Tool approval request timed out.", and a restarted or failed reply's cards say so.
- A refused final answer finishes its paused reply on every path.
- Background scripts in a room without the router are denied with the router-missing text instead of failing the call.
- Job cards are denied when the job's end is saved, so a card of an ended job never stays answerable.
- Detached waits end as soon as their decision commits instead of at the next one-second poll.

## Decisions waiting for the user

- Minimal-mode (CLI) approvals as detached waits, which would remove the in-place claim, the suspension side channel, the `cli_call` blob, and `continue_cli_approval`; nothing above depends on it.
  If accepted, the conversion cancels pending CLI approvals because they would have no resume path.

## Rejected alternatives

- A stored `decided`/`finished` card state: duplicates the call decision and the outbox acknowledgement.
- One `card_event_id` column on the card: cannot hold receipt aliases, and legacy tombstones have no card.
- Deleting reply-owned calls with their paused reply: departure keeps router-owned terminal-edit debt, whose decision would then have no owner.
- Deriving the grant's consent epoch from the card's delivery tenure: departure cleanup moves delivery tenure, which would move the grant's fence.
- Settling every failed live resume from dispatch: it would need the live span ended and FINAL recovered first anyway, and moves a direct settlement behind a wake.
- Publishing signals from the store method after its write returns: a caller cancelled after the commit never publishes, and callers on other loops resume in no defined order.
- One notification registry per process keyed by database: only tests open a second store, and production already refuses a second runtime.
- Keeping the 30-second hold recheck as a safety net: with every mutation publishing through one owner it only hides missing publications.
- Cancelling pending approvals at upgrade: the conversion preserves what a resume needs.
- From the brief, unchanged: merging `!auto_approve` windows into timed grants, storing detached calls as paused replies, making job approvals survive restart, and rebuilding a paused reply's request from the journal.
