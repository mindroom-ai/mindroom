# Reply-grant effects: one sequence for every grant change

This record holds the decisions behind giving the effects of a room-backed reply-grant change on calls and invites one owner, the reply-membership sync, instead of the orchestrator and the router's duties.
It records reasons, rejected alternatives, evidence, and accepted limitations; the code shows the mechanisms.

## Decisions

- Every grant change reaches the fleet through one sequence: recheck each bot's invites in the background, end each bot's denied calls and wait for all of them, then recheck each bot's calls in the background.
  The authoritative refresh and the router's live membership transitions both run it, so "no call starts for a newly allowed requester until every bot finished ending denied calls" has one owner.
  One sequence reads the fleet once; a bot started during its revocation reconciles its calls at its own first frame instead.
- The invite recheck stays in that sequence although invite acceptance does not depend on grants, because the authoritative refresh that room setup runs at startup and on reload is the only trigger that retries invites recorded before a bot started or whose join failed; #1916 added it when invite acceptance still read grants, and #1931 removed that dependency.
- Every grant change starts calls after its revocation, including an authoritative refresh that still leaves a refresh owed: each call start rechecks its requester against the current grants, so a grant room the router cannot query denies only the calls it alone would allow.
- Every loss of grant authority restarts the refresh backoff, so the next router sync refreshes at once.
  The backoff bounds retries that no new loss asked for; an invalidation makes every grant room unready, so waiting out an earlier backoff held every grant-room requester's replies for up to 300 s whenever one grant room the router cannot query kept the refresh failing.
  A failing refresh therefore runs once per loss plus its backed-off retries, the rate a healthy refresh already runs at.
- Every loss of grant authority ends the calls it denied: each invalidation and each room a pre-admission fence makes unready schedule every bot's revocation.
  The former latch issued one revocation wave per gap and reopened only when nothing remained to refresh, so a refresh that readied one grant room while another stayed unqueryable restored authority without reopening it, and a call authorized through the ready room survived the next invalidation until something else rechecked it.
  The latch came from #1921, which turned invalidation from full reconciliation (a room-state read per call room) into revocation only; revocation reads no Matrix state for calls that still pass.
  A batch with several history-loss records schedules one revocation per record and call-enabled bot; the passes share the teardown of a denied call, and a test pins that a batch ends a denied call, joined or still starting, once.
- Background invite, revocation, and call-reconciliation tasks belong to the bot whose room lifecycle or call manager they act on, so that bot's stop waits for them and cancels what still runs at its drain deadline.
  The live path and the pre-admission fence used to run one router-owned task gathering every bot, a shape #1878 created by backgrounding awaited orchestrator gathers in place.
  A router replacement or stop therefore no longer waits for, or cancels at its drain deadline, another agent's invite join, call start, or call revocation.
  The revocation each sequence waits for still runs in its caller: the router's ingestion pump on the live path and the refresh caller on the authoritative path.
- A bot schedules invite rechecks and call starts only while it accepts work; revocation runs in every phase.
  A draining or stopped bot therefore gets no new positive work after its drain began, and a resumed bot catches up on calls through the full call reconciliation its next sync loop requests.
  Frame publication rechecks acceptance right before it consumes pending call reconciliation, because draining can begin during the frame's awaited side effects.
- The call manager ignores the bot's own member events; its own membership arrives only from durably admitted sync sections.
  The bot used to filter those events out first, which left the manager's own-membership branch unreachable.

## Rejected alternatives

- A per-bot call owner wrapping the optional call manager, which both audits proposed: its gains came from the other changes (the orchestrator gathers left with the fan-out, the test seam was fixed by patching the manager builder, the dead branch left with the filter), and what remained moved each optional-manager check into a forwarding method of another class.
- Keeping the fan-out in the orchestrator: the order and the revocation rules would stay split across the orchestrator and the router's duties, with runtime-protocol entries that existed only so the router could reach a gather.
- Keeping the latch and resetting it when a refresh publishes: fixes the partial-refresh hole but keeps state whose only purpose is skipping cheap work, where a wrong reset rule fails open.
- Keeping the router as owner of the live path's tasks: a parameter whose only purpose is an incidental owner, keeping the cross-bot cancellation.
- A joined-room scan without its own revocation as the positive pass: grants can change between the sequence's revocation and the background task.
- Starting calls only after a refresh that leaves no refresh owed: one grant room the router cannot query then kept every call authorized through a ready grant room ended until the caller's next call event.
- Keeping the backoff across new losses so repeated uncertain sync responses cannot bypass it, as #1840 chose: a failing refresh then ran less often than a healthy one, at the cost above.

## Accepted limitations

- The sequence orders one grant change; overlapping changes and earlier background tasks can interleave, and joins stay fail-closed through the index's fencing of refreshes an invalidation voided and the call manager's authorization check at call start.
- Calls authorized through a grant room end when the router's view of membership becomes uncertain (a sync restart, a router replacement, a history gap) and start again after the next authoritative refresh readies that room.
- A loss of authority while a refresh is in flight voids that refresh, which counts as a failed attempt, so the retry waits the first backoff step of 5 s.
- A call teardown that a revocation started finishes on its own when the bot's drain deadline cancels that revocation, because a session's stop is shielded and the call manager stops tracking a session once it detaches it; this predates the change, which only moved the fence revocation from the router's drain to the agent's.
- An invite recheck skipped while a bot drains waits for the next grant change or invite-policy change; invite acceptance does not depend on grants, so nothing it would have accepted became newly allowed.

## Evidence

- Design critique in three rounds against `62afc28b38`, which accepted the sequence owner, the latch removal, the live-path choice, the per-bot owners with the eligibility rule, and the frame recheck.
- A live run against a local homeserver with only the call media stubbed: a requester who left the grant room lost the agent's call within 70 ms while a requester who stayed kept theirs, on `main` and with this change.
