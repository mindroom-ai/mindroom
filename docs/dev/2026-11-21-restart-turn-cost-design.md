# Restart-interrupted turns keep their cost

This record covers how a reply reports the cost of every attempt at its turn after restarts, and why the first implementation of it was replaced.

## Goal

A turn that a restart cuts short is replayed, and before this change the reply reported only what the last attempt spent and received, so replies showed 87% of real subagent cost in production.
Success: a reply's `cost.usd` and `cost.subagents_usd` count every attempt at its turn exactly once, across restarts, model switches, empty retries, approval pauses, job follow-ups, and restart-continued subagent jobs, and no reply reports a run or a subagent turn twice.

## Prior art

- `recover_interrupted_turn` (`history/run_checkpoint.py`) already finds every unlinked attempt at a turn by its source event ids, and the replay names them in `CONTINUED_RUNS_METADATA_KEY`.
- Each run row keeps its own `metrics`, and `with_prior_usage` / `prior_usage_of` (`ai_run_metadata.py`) already carry a turn's spent attempts on the next run's row for approval resumes, as one summed value.
- Every run save already writes what the turn's subagents cost so far (`subagent_cost_metadata`, called from `session_storage/store.py`), and a new run in the same turn inherits that from the turn's metadata.
- A turn already starts from its context's run metadata in one case: an approval resume builds its context from the paused row.
- Every point that hands a subagent's cost to a turn has the child at hand, with its `delegation_id`.
- A cut-short child turn is settled by `interrupt_child` (`delegation/recovery.py`), and a stopped job's saved child is refreshed from that settlement (`reconcile_delegation`).
- `approval/continuation_runner.py` already reports an approval-resumed reply's cost undated, so budgets recount instead of adding usage a scan may have counted.

## Decisions

1. **Receipts are keyed by delegation id.**
   The per-turn holder is a map from the received child's `delegation_id` to its cost, and the reply publishes the sum.
   Receiving the same child twice changes nothing.
2. **Own usage is keyed by run id.**
   A turn's spent attempts are carried by the run id of each (`CompletedAttempt.attempt_run_id`), in the order they were spent.
   A retried empty reply has no row, but the next row names it by id, so a later row that repeats it merges away.
   Only the driver's carriers (`TurnResume.prior_usage`, `TurnRunState.prior_usage`) are maps; the builders that price a reply still take the usage values.
3. **One merge rule everywhere: per key, the larger value** (`merge_by_key`).
   Two views of one turn hold the same value under a key unless one is older, and what a turn spent only grows.
   A run save stores the row's receipts merged with the bound holder's, a delegation state's save stores its run's merged with the holder's, a metadata patch that carries receipts merges them into the row's, and a resumed, seeded, or recovered turn starts from a merge.
   Rows stay cumulative; the change is that two cumulative views can be merged.
4. **The maps are stored under new keys, beside the sums an earlier release reads.**
   `mindroom_subagent_receipts` sits beside `mindroom_subagent_cost_usd`, and `mindroom_prior_usage_by_run` beside `mindroom_prior_usage`; each sum is the total of its map.
   An older backend after a rollback, and scripts outside the repo, read the sums as before.
5. **Restart recovery returns the replay's run metadata.**
   It names the continued runs and holds the merge of the unlinked attempts' usage by run id and of their receipts by delegation id.
   A team attempt contributes one usage under its leader's run id, its leader's plus its stored member rows' (`team_run_usage`, which `teams/runtime.py` shares), and the receipts of the leader's and the members' rows, since a member's row can hold a receipt the leader's last checkpoint predates.
   The stored member rows of a team run that ended are exactly the runs its usage counts (`team_usage_runs`); a cut-short one's also include its running members' last checkpoints.
6. **A job's receipt is carried only by the turn whose run acknowledged the job.**
   A crash can save a collected job result on a row before the job is acknowledged, and any turn of the conversation may collect the job after it, before or after the replay.
   A subagent job therefore keeps the parent run whose saved tool result first acknowledged it, and recovery carries the job's receipt only when that run is one of the turn's unlinked attempts or a member run of one.
   A job no run acknowledged is left to the turn that collects it again, the replay included, which receives it then.
   The acknowledging run is kept in the job's adapter, not in a new job field, because an older backend rejects a job with a field it does not know; a tool's job keeps its adapter as admitted, which a reattaching call must match.
   A job acknowledged with no run recorded is left out too: a keyed receipt beside such a job can only come from an older backend that acknowledged it after a rollback, whose own reply reported it if it collected it, so carrying it could count it twice and leaving it out cannot.
   Of a saved job, recovery reads its consumed flag, its acknowledging run, and the delegation id its saved child names, and decodes no child, so a snapshot no reader decodes stops no replay.
   A filter by conversation was not enough for that: a session id names a room and thread, which several agents and requesters share, and a turn never collects their jobs.
   The jobs are the job runtime's or, on an instance that runs no jobs or has yet to recover them, the saved job records, read without taking them over.
   Turning jobs off keeps the saved ones for the next enabled start, whose turns still collect an unacknowledged one, so a replay without a runtime must leave that one out too.
7. **A turn starts from what its run metadata holds.**
   The driver merges the receipts and the prior usage of `ctx.matrix_run_metadata` into the holder it binds, a caller's holder too, and into the turn's prior usage.
   A replay needs no new request field or stream parameter, and the approval paths keep their explicit seeds, which hold the same ids.
8. **A child's receipts live on its run's rows; its handle keeps the sum.**
   `DelegationChild` is unchanged, because an older backend rejects a snapshot with a field it does not know (`from_dict` passes every key to the dataclass), and the handle, the job adapter, and a parent run's delegation state all hold such snapshots.
   An attempt that resumes a child's stored run adds that row's receipts to its holder, and pricing a child takes the larger of the handle's sum and its row's (`price_child`), so a settlement that finds a stale handle beside a newer completed row counts the row.
9. **A restart-continued subagent job receives its cut-short turn.**
   The continuation starts from what the job's earlier continuation started from, what the cut-short turn's row received, and that turn's own priced usage under its `delegation_id`.
   What the row received is carried under decision 6's rule, for the runs of its cut-short turn.
   The delegation records of a continued chain therefore overlap: each continuation's record includes the turns before it, so a total over a chain sums replies, not `run.json` records.
   The job adapter keeps that map (`continuation_receipts`) for a continuation whose process is killed before it stores a run, and the continuation's handle starts with its sum as `cost_usd`, so one refused before its first run still reports it.
   The continuation's rows and delegation record keep only its own usage, and the continuation cap is still checked first.
10. **A cut-short child turn is priced when it is settled.**
    `interrupt_child` prices the run it settles, so a job that ends interrupted, at the continuation cap or on a refusal, hands its known cost to the turn that collects it.
11. **Kept from the first implementation.**
    Job follow-up runs store their follow-up id as `matrix_source_event_ids`, so recovery continues them.
    A reply that continues recovered runs reports its cost to budgets undated.
12. **One rule for values stored as sums.**
    A row that holds a sum without its map reads as one entry under the reserved id `legacy`.
    A resume of the same run carries that entry, so a reply paused for approval across the upgrade reports what it would have before.
    A sum larger than its map's total reads the same way, with the difference as that entry: an older backend that resumed the run after a rollback added what it received to the sum alone.
    Restart recovery and a job's continuation drop it, because a cumulative sum may cover runs and receipts they also read by id.
13. **The usage ledger stays what it is.**
    Nothing is recorded twice and nothing new is written to it.

## Rejected

- **Disjoint per-run floats (the first implementation).**
  Each row was to hold only its own run's receipts, with a second "prior" bucket and a third key for rowless usage.
  Every Major in two review rounds was one transition that moved a value between buckets wrongly, and one double counted on an approval path with no restart at all.
  A float cannot say what it already contains, so each new transition needs its own rule.
- **Cumulative floats read from the latest attempt.**
  A float cannot merge away a job result collected again after a crash, and a lost latest row loses its whole chain.
- **Reshaping the existing keys into maps.**
  An older reader takes a map under `mindroom_subagent_cost_usd` for no cost and a map under `mindroom_prior_usage` for empty usage, and telling a summed usage dict from a map needs shape sniffing.
- **A receipts field on `DelegationChild`.**
  It is the simplest carrier, but it breaks every snapshot read of an older backend, and the rows already hold the map.
- **The usage ledger as the source of the turn's own usage.**
  A checkpoint never records usage, so a cut-short attempt is missing until recovery records it and one past the crash-loop limit never is; its snapshot also drops duration and time to first token.
- **Keying receipts by job id.**
  Foreground children have no job, and a continued job keeps its id while its child changes.
- **Reading sums in restart recovery**, as a maximum or per row.
  Per row sums one receipt over a model switch, and a maximum plus a job collected again counts that job twice.
- **Carrying the cut-short child turn as one inclusive receipt.**
  Its nested receipts must stay under their own ids, in case its continuation collects one of them again.
- **Carrying every receipt, or none, when no job runtime runs.**
  Without the job records a job's receipt reads like a foreground child's.
  Carrying all of them counted an unacknowledged job twice once jobs ran again; carrying none would drop the foreground receipts and the jobs the turn itself acknowledged, and the saved records are a few lines away.
- **Reading a job's consumed flag as the turn's own acknowledgement.**
  The flag says some run acknowledged the job, not which, so a job another turn collected after the crash counted in both replies.
- **Leaving out completed unlinked runs** to fix limitation 5.
  A model-switch run has the same shape, and its successor has no row until its first tool batch, so the rule would drop real cost.

## Reviewer findings

| Finding | Outcome |
| --- | --- |
| Model switch before a restart counts a receipt twice | Both rows hold the receipt under one id. |
| Billed empty retry dropped | The next row stores it under its run id; recovery merges it in. |
| Continued child's rows hold cumulative nested cost | Rows are keyed, so cumulative rows merge; nothing sums them. |
| Budget counts pre-restart usage twice | Undated cost for a reply that continues recovered runs. |
| Approval, then a same-turn continuation, publishes the paused run's receipts twice with no restart | The new run inherits the ids the holder has; there is no second bucket. |
| Child approval resume puts its total in the current bucket | No buckets; the resumed attempt adds its run's row by id. |
| Rows of an earlier release double count on the upgrade restart | Decision 12. |
| A saved but unacknowledged job result is charged again, by the replay or by a later reply | Decision 6: one receipt, in the reply of the turn that collects it. |
| A job another turn acknowledged after the crash counts in both replies | Decision 6: the job names its acknowledging run. |
| A continuation carries a nested job no run acknowledged | Decision 9. |
| A same-run resume after a rollback and a second upgrade loses what the older backend received | Decision 12, for receipts; limitation 9 for prior usage. |
| One undecodable saved child fails every replay, or every replay in its thread | Decision 6: only the id its child names is read. |
| A replay on an instance that runs no jobs carries a job it did not acknowledge, which its later collector counts again | Decision 6: the saved job records are read. |
| An acknowledgement changes the job's adapter before its save lands | The save publishes a copy, so a failed one leaves the job as it was. |
| Hook-refused continuation loses its known cost | Decision 9: the continuation's handle starts with it. |
| Job ended at the continuation cap reports no cost | Decision 10. |
| A member's receipt the leader's checkpoint predates is lost | Decision 5. |
| A completed child run settled after a crash misses what its row received | Decision 8. |
| A rollback cannot read the new child snapshot, and misreads reshaped keys | Decisions 4 and 8: old keys and the child snapshot keep their shape. |
| Two views of one id can disagree | Decision 3. |
| A caller's holder is not seeded | Decision 7. |
| A delegation state's save writes only the holder's receipts, and its patch replaces the row's | Decision 3. |
| Losing the latest child row drops earlier saved work | Not handled (limitation 3). |
| Regeneration, and other unlinked runs, are counted again | Not handled (limitation 5). |
| Follow-up rows of an earlier release are not matched | Not handled, as before. |

## Limitations

Each is at parity with the behaviour before this change or better, except 5.

1. An attempt cut short before its first checkpoint has no row, so its usage reaches no reply.
   A billed empty reply whose retry is cut short before its first checkpoint is lost the same way, since only the retry's row names it.
2. A receipt of a job no turn acknowledged reaches a reply only if a turn collects the job again.
   A job that left retention, 30 days after it ended, names no acknowledger any more, so a replay that late carries its receipt as any delegation's.
   A saved job record that does not parse is read the same way on an instance without a runtime; no runtime recovers past such a record to collect it.
3. A cut-short child turn whose latest run left no row carries only what its job's earlier continuation started from: its own usage, and what its earlier runs received, are lost.
4. A foreground child that a restart cuts short is not continued; its cost is on its handle, but no turn receives it.
5. Any run with the turn's source ids that no reply linked is counted by a later restart recovery of that turn.
   That covers a regeneration a restart interrupts after a completed run no reply linked, and the runs a replay continued before it failed, since an error reply links only its own run.
   Those runs were already linked to the later answer before this change; adding their cost is new.
6. Turns in flight at the upgrade restart report their runs' own usage and no earlier receipts, and follow-up runs written before it are not matched.
7. A minimal-mode CLI approval that starts a fresh envelope does not add the paused run's own usage, as before.
8. An older backend after a rollback reads the sums and ignores the maps and the job adapter's receipts, so it reports as it did.
9. After a rollback and a second upgrade, a job the older backend acknowledged names no run, so a replay leaves its receipt out, and prior usage the older backend added to the summed key alone is not read back, unlike receipts (decision 12).

## Testing

- Unit: saving a row twice, merging two rows, patching a row, and resuming from a row give the same receipts; recovery merges rows by id, drops sums and the jobs its runs did not acknowledge, and counts a team run's member rows; a subagent job keeps its first acknowledging run over a restart and a reread, and its adapter when the save fails; the saved jobs are read without a runtime past one that does not parse; an acknowledger is read beside the id a saved child names, whatever else it holds.
- Scenarios through the response runner: one and two restarts, a lost attempt, past the crash-loop limit, a model switch before a restart, a billed empty retry over one and two restarts, a job follow-up replay with the job acknowledged, collected again, or left for a later turn, a job another turn collects between the crash and the replay, a replay on an instance that runs no jobs for a job its run did and did not acknowledge, an undecodable job in another conversation and of another requester in the thread, and the undated budget cost.
- Continued subagent jobs: over two restarts, at the cap, refused by a hook, after a model switch and an approval, killed before the continuation's first row, from a row that stored a sum, and with nested jobs its runs did and did not acknowledge.
- Approval paths: an approval, then a continuation, then an approval with no restart, for a receipt held by id and for a sum; a child that goes on in a new run after its approval; a completed child run settled from a stale handle; a team turn cut short and replayed.
- A reply whose run metadata holds receipts reports them, streamed or collected into its caller's holder, and a delegation state's save keeps what its run holds.
- The usage ledger holds each run's own usage once in the restart scenarios.
- The first tests were written after the code, so each rule was then removed in a scratch copy to see its test fail; the reproductions of the third and fourth review rounds were written first and seen to fail.

## Size

Net `src/` lines against the base are +200, against +199 for the first implementation; no lock is added.
The change was sized for 160 and stood at +165 before the third review round, whose fixes, net of its simplifications, added 22; the fourth round's added 13.
The largest pieces are the keyed holder and its stored forms (`delegation/cost.py`, +50), recovery (`history/run_checkpoint.py`, +26), the job's acknowledging run and the read of the saved jobs (`tool_jobs/runtime.py`, +25, seven of them a signature that no longer fits one line), and the continuation's receipts (`delegation/continuation.py`, +17).
