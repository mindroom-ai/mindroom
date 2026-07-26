# PR #1641 thread edit integrity handoff

## Current correction

- Exact candidate `a684eb2b5d4de8c174c193c8492cc2b3643dbcda` failed full pytest with 16 thread-membership regressions.
- The regressions came from removing the advisory cached-index fallback entirely while fixing a stale index overriding definitive `NOT_A_THREAD_ROOT`.
- Existing tests prove the index fallback is required for reply-chain, live edit, dispatch, redaction, and command routing paths.
- Narrow correction restores the fallback except when an authoritative relation-free source plus negative root proof proves the index stale.
- Four stale tests represented known threaded parents as relation-free events while supplying a contradictory cached index.
- Those fixtures now carry the actual explicit `m.thread` relation; the exact 16 full-suite failures plus the stale-index regression pass 17/17.
- Fresh exact-`a684eb2b5` review also reproduced a separate retained-source bug: an opaque fetched duplicate can replace a certified clear retained representation before canonical identity reconciliation.
- Strict RED reproduced that exact opaque/clear pair.
- `_merge_retained_thread_event_sources` now applies the shared immutable-representation transition, preserves authoritative fetched redactions, removes true conflicts, and reports only incorporated or terminally superseded retained IDs.
- Repair-delta acknowledgement now consumes that reported set rather than every ID merely presented to reconstruction.
- Focused clear/opaque, redaction, conflict-quarantine, and acknowledgement tests pass.
- Exact-`796974c94` review then reproduced two blockers: malformed self-thread sources could still trust a stale index, and repair acknowledgement tracked only IDs across attempts/races.
- Strict RED covered self-thread demotion, final-attempt replay replacement, rejected-conflict acknowledgement, and late same-ID overwrite.
- The membership fallback now trusts an index only when current source evidence is unavailable.
- Retained repair bookkeeping now records exact presented and replayed representations for only the current attempt.
- Stored and existing-winner acknowledgement compares the current pending representation against final-attempt evidence and canonical cached coverage; rejected or overwritten evidence remains pending.
- A final RED showed that same-ID retained evidence with different content could still skip pre-repair invalidation because preparation compared IDs only.
- Repair preparation now uses the same canonical representation-coverage rule as merge and acknowledgement.
- The strict focused regressions pass, and seven owning suites pass across SQLite and PostgreSQL after this correction.
- Full pytest for this exact source passed `12299` tests with `54` skips; all-file pre-commit and Tach also pass.
- One earlier full run exposed only a Rich assertion sensitive to a long basetemp path; the exact test and full suite pass with a short external basetemp.
- Every exact-head review, CI, and live gate must restart after push.
- Rerun all 16 exact failed tests, the membership owning suite, full pytest, fresh reviews, CI, PostgreSQL, hooks, and live Tuwunel.
- Remove this file before the next exact-head freeze.
- Never amend, force-push, merge, or use temporary storage for durable evidence.
