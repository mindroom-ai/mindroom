# PR #1641 thread edit integrity handoff

## Current correction

- Exact candidate `a684eb2b5d4de8c174c193c8492cc2b3643dbcda` failed full pytest with 16 thread-membership regressions.
- The regressions came from removing the advisory cached-index fallback entirely while fixing a stale index overriding definitive `NOT_A_THREAD_ROOT`.
- Existing tests prove the index fallback is required for reply-chain, live edit, dispatch, redaction, and command routing paths.
- Narrow correction restores the fallback except when an authoritative relation-free source plus negative root proof proves the index stale.
- Four stale tests represented known threaded parents as relation-free events while supplying a contradictory cached index.
- Those fixtures now carry the actual explicit `m.thread` relation; the exact 16 full-suite failures plus the stale-index regression pass 17/17.
- Fresh exact-`a684eb2b5` review also reproduced a separate retained-source bug: an opaque fetched duplicate can replace a certified clear retained representation before canonical identity reconciliation.
- Add a strict failing regression at `_merge_retained_thread_event_sources`, then make the smallest canonical same-ID merge correction without weakening fetched-redaction authority.
- Rerun all 16 exact failed tests, the membership owning suite, full pytest, fresh reviews, CI, PostgreSQL, hooks, and live Tuwunel.
- Remove this file before the next exact-head freeze.
- Never amend, force-push, merge, or use temporary storage for durable evidence.
