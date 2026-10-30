# Reply core: one typed turn ending, one owner of a turn's sources, one-way approval seam

Decisions behind the 2026-10 refactor that gave a reply turn's ending one typed value, made the response path return what it did with its sources, and split the two-way dependency between the response runner and the approval continuation runner.
The code shows what each owner does; this record keeps why, what was rejected, and what stays limited.

## Why

- How a turn ended was a `terminal_status`, a cancel source, a `suppressed` flag, and a free `failure_reason` string that did four jobs: it was parsed back into a cancel source, string-matched to pick the participation-decline reaction, passed to `message:cancelled`, and rendered as the reply's error note.
  Internal codes therefore reached users (`suspension_presentation_delivery_failed`, `terminal_update_failed`, `stream_completed_without_visible_body` in notes; `suppressed_by_hook` as an approval card's denial reason), and an unexpected agent error reached the note raw and unredacted while a team's was redacted friendly text.
- `"suspended"` meant both "paused for approval" and "final answer recorded but not acknowledged".
- What happened to a turn's journal sources was signalled through four callbacks and an `asyncio.Event` on the request, and each of four entry points (dispatch, edit regeneration, job follow-up, interactive selection) re-derived "did a reply take my sources" with flags, Events, and inbox `on_failure` retries that skipped the inbox's own wait-to-claim rule.
- The approval continuation runner was built by the response runner with `host=self` behind a function-level import, through a 15-member protocol, so neither could change the shared settlement alone.

## Decisions

### The ending

- A turn ends as exactly one of `Completed`, `Suppressed(reason)`, `Cancelled(source)`, `Failed(cause, detail)`, `AwaitingApproval`, `Owed`.
  Success endings are decided where the answer row is written, as the one-turn-driver record requires; every other ending is decided once, at the settlement point that ends a span its outcome did not end, from the typed value, and a Stop recorded meanwhile is read from the reply's records.
- `Failed.detail` is display text only for an `ERROR`, rendered through the one redaction the approval failure and the loop's stored run error already use; it is never parsed.
  The `message:cancelled` reason is the ending's code; an error no longer passes its text to hooks (no fleet plugin read it).
- `Owed` and `AwaitingApproval` both stay "suspended" to the response lifecycle and to a minimal-mode CLI approval scope.
  Which effects run is still decided by `ResponseOutcome.run_succeeded`: an ordinary owed answer runs its run-only effects at once, while an approval's owed final runs nothing until its continuation finishes.
- An unexpected error escaping an agent attempt is answered like a team's (redacted friendly text, a completed reply), because handled agent run errors and team errors already answer that way.
- The attempt runner returns the attempt's outcome or its cancellation, and keeps owning a straggling attempt after a forwarded cancellation until it exits, so a late progress write can never follow the span's ending.
  An attempt that delivered before its owner's cancellation landed keeps its delivery as the turn's ending.

### Sources

- The response path returns a `SourceFate`: `Owned` when the exact pending ids are held by the span the request claimed, a refused claim, or an approval; `Unclaimed(reason, preparation)` otherwise, and one `settle_source_fate` rule settles the caller's own sources.
  An earlier reply ending through its records does not make the fate `Owned`, because those records settle the original sources, not an edit's revision id.
- After a claim the response returns once its sources are durably settled, released, or transferred; it raises before a claim, on a retry signal, on a cancellation of the response task, on admission refusal, and on process shutdown.
  The edit regenerator keeps its own claimed flag only for its documented settle-on-failure rule.
- The per-task `on_failure` retries are gone: the inbox already retries every finished task's sources unless they wait to claim.

### The approval seam

- Both runners settle through one locked-turn owner that `bot.py` builds and injects: the response runner hands a paused turn to the approval continuation runner, the approval runner calls the locked-turn owner, and nothing points back.
  The host protocol, its pass-throughs, and the deferred import went with it; the runner reads its dependencies through the locked-turn owner, so the two never hold separate copies.

### Notes, Stop, and hooks

- A span's note, an approval's failure note, and an owed note go through one loop that composes the row from the reply as it stands and composes it again when the reply changed first.
- A Stop the reply has not applied decides every span note, also on the exit of a span resuming an approval.
  The exit already applied such a Stop when it saw the Stop on its first read, and the rules end a resume span the same way whichever note it renders, so skipping the Stop only for a Stop that landed mid-write made one exit report two endings; an approval's own failure note always shows the Stop, as `approval_failure_note` requires.
- The response hooks and the silent-schedule result record belong to the response lifecycle; the gateway calls `message:before_response` and `message:final_response_transform` at its write points.
  Running `message:before_response` in the lifecycle before it calls the gateway was rejected: a stream that ends with only a final body reaches the whole-answer write inside the gateway, so the whole-answer versus streamed-final split would gain a second owner.

### Streaming stop

- One stop routine joins every progress writer and returns their failures, and each caller keeps its own policy for them.
  Abandoning a stuck writer after a timeout was rejected, because its edit could land after the reply's terminal write.

### Turn context

- Agent and team turns build their shared context fields in one place; the team's requester fallback to the identity it runs as stays an explicit argument.
- Once a turn holds its conversation's lock, its request names the target its reply answers in, so no separate thread-root override or re-locked copy of the request remains.

## Rejected alternatives

- Deriving every outcome from the reply records: rejected again for the reasons in [one turn driver](2026-10-28-one-turn-driver.md); only non-success endings moved to one decider.
- Reusing `ApprovalFailure` itself as the turn ending: its default texts are approval-specific and it has no suppressed, awaiting, or owed case; a derived view keeps one mapping.
- Unifying the note formats of reply errors, approval failures, and dispatch failures: user-visible and unrequested.
- Treating an owed answer in a minimal-mode CLI scope as finished: `finish_approval` requires an acknowledged or refused FINAL, so the continuation is finished by dispatch once the outbox delivered it.
- Building the interrupted turn's replay row from the loop's terminal record (#3108): model-visible and a stored-data change; left to that roadmap item.
- Finer source fates (answered, handed off): no entry point acts on an owned fate.

## Limitations

- Keeping an owed answer suspended in a minimal-mode CLI scope can run a resumed reply's run-only effects twice; [reply messages](../architecture/reply-messages.md#accepted-limitations) lists it.
