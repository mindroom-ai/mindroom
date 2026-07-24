# Matrix fuzz campaign living status

> This file is an uncommitted operational handoff document.
> Update it after every material finding, fix, campaign result, review result, push, or PR state change.
> Do not include it in a product PR unless the user explicitly requests that.

Last updated: 2026-07-24 (fresh Fable session, supervisor authorized continuation; Items A and B RESOLVED).

## Fresh-session TL;DR (read this first)

- Branch: `test/fuzz-live-chaos-expansion`. Worktree: `/Users/bas.nijholt/.codex/worktrees/fd26/mindroom-worktrees/test-fuzz-live-chaos-expansion`.
- Local HEAD == remote `origin/test/fuzz-live-chaos-expansion` == PR #1639 head == `64ca7c360` (handoff `bc241598c` + Item A `13da9895d` + Item B `64ca7c360`).
- Pinned review base: `origin/main` == `66dd4f4a68bcfd1a5e43b2cac20a1b464f306ab1`. Always diff against `origin/main`, never the old `test/matrix-cache-fuzz`.
- All three MindRoom product fixes are verified correct by fresh-context review.
- Item A (reviewer blocker, cached-path regression for Fix 1): DONE in `13da9895d` (test-only, 4 passed in `tests/test_dispatch_replay_guard.py`).
- Item B (Qodo, full-maintenance replay during recheck sleep): DONE in `64ca7c360` (`recheck_pending` resumable phase; 8 passed in `tests/test_startup_maintenance.py`); Qodo inline thread answered (reply id 3646283901).
- Postgres 45-thread fanout ISOLATED RERUN: 2 passed solo (postgres call 10.29 s, sqlite 1.72 s, 60 s timeout untouched) — prior pressure was xdist contention, not a product slowdown.
- Full suite CLEAN at this head: `11526 passed, 54 skipped, 68 warnings in 84.04s`. Pre-commit all-files: every hook passes except main-owned prettier drift (see gate section).
- Fresh-context pr-review #2 (agent `prreview1639b`, base `origin/main`, HEAD `e60601ff4`): verdict MERGE-READY after living-doc removal, ZERO code blockers. All three product fixes plus Items A/B re-verified independently. Review loop is clean.
- Swarm notes directory (crash-safe, shared): `/Users/bas.nijholt/.codex/campaigns/mindroom-fuzz-2026-07-24/` — fourteen read-only agents each own one file there; treat every claim as provisional until verified against the exact current head.
- Living-doc rule: this file must be REMOVED from the branch before PR #1639 is merge-ready (only when the supervisor says so). Do not merge anything.

## Merge recommendation for PR #1638

PR #1638 was squash-merged at `66dd4f4a68bcfd1a5e43b2cac20a1b464f306ab1`.
Both extension branches were rebased onto current `origin/main` before their first push.
Their follow-up PRs now target `main`.

## Objective

Extend MindRoom PR #1638's Matrix fuzz harness through two independent agent-cli sessions.
Use model-based cache fuzzing and real-Tuwunel chaos/saturation testing to find correctness, concurrency, recovery, and performance bugs.
Replay and minimize every failure before calling it a bug.
Fix confirmed MindRoom bugs in MindRoom.
Fix confirmed mindroom-nio bugs in mindroom-nio without adding MindRoom workarounds.
Keep all work unmerged until review, CI, and user approval are complete.

## Hard workflow rules

- Never create a branch named `codex/...`.
- Use Git author `Bas Nijholt <bas@nijho.lt>`.
- Never amend or force-push.
- Stage files individually and never use `git add .`.
- Open normal non-draft PRs because draft PRs do not trigger the desired AI reviews.
- PR #1638 is merged; PR #1639 now targets `main`. Use `origin/main` (`66dd4f4a68...`) as the review base for ALL diffs. The old stacking base `test/matrix-cache-fuzz` is retired.
- Run real Tuwunel campaigns in isolated disposable stacks.
- Do not weaken an oracle or increase timeouts to hide a failure.
- Do not merge any PR on the user's behalf.

## Existing base PRs

### MindRoom PR #1638

- URL: https://github.com/mindroom-ai/mindroom/pull/1638
- Branch: `test/matrix-cache-fuzz`
- Head: `82711554dc470623e25833b0d1a6c01b13e0fbf4`
- Current GitHub state at this update: merged at `66dd4f4a68bcfd1a5e43b2cac20a1b464f306ab1`.
- Worktree: `/Users/bas.nijholt/.codex/worktrees/fd26/mindroom`
- PR scope: direct cache fuzzing, real-Tuwunel fuzzing, deterministic trace replay, saturation profile, and duplicate-turn hardening.
- CI and Greptile were green on this exact head before the extension campaign began.

### mindroom-nio PR #20

- URL: https://github.com/mindroom-ai/mindroom-nio/pull/20
- Branch: `fix/limited-sync-recovery-loss-v2`
- Head: `b1106e31ab574e98c96342f78d6cd6d36251d8a8`
- Current GitHub state at this update: open, non-draft, clean merge state.
- Working checkout used by the primary session: `/tmp/mindroom-nio-pr19.UVfDpV`
- PR scope: exact backward limited-sync recovery, per-event deduplication, and a private last-fully-processed sync token.
- CI, Python 3.10 through 3.14, coverage, pre-commit, and Greptile were green on this exact head before the extension campaign began.

## Prior live evidence

- The original saturation profile passed `209/209` canonical replies twice with zero duplicates, missing replies, coordinator timeouts, dispatch timeouts, or event-loop stalls.
- The original mixed campaign passed seed `1638` with 500 operations, 45 threads, 61 concurrent batches, four MindRoom restarts, and `240/240` canonical replies.
- The full mindroom-nio suite passed with 519 tests passed and three skipped before this extension campaign.
- These results belong to the exact base heads above and must not be silently attributed to later extension commits.

## Open follow-up PRs

### MindRoom PR #1639

- URL: https://github.com/mindroom-ai/mindroom/pull/1639
- Branch: `test/fuzz-live-chaos-expansion`
- Base: `main`
- Pushed head at creation: `1acaac8b58342241df7ca31c42570ac329889691`
- Current pushed head: `aba3c9067` (see "Exact heads at this handoff").
- State at this update: open and non-draft.
- Scope: live chaos harness, three current MindRoom fixes, regression tests, and this living handoff.
- Merge status: active investigation and not merge-ready. Fresh-context review verdict: CHANGES REQUIRED (one blocker, see below).
- This living handoff must be removed before PR #1639 is merge-ready.

## Exact heads (2026-07-24, after Items A and B)

- Local HEAD == remote == PR #1639 head: `64ca7c360` (verify with `git rev-parse HEAD` vs `git ls-remote`).
- `origin/main` (pinned review base): `66dd4f4a68bcfd1a5e43b2cac20a1b464f306ab1`.
- New commits this session (both pushed): `13da9895d` (Item A cached-path regression test) and `64ca7c360` (Item B recheck-only replay + lifecycle tests).

## Full commit list vs origin/main (oldest to newest)

```
35e860eae Add composable live chaos profile with lifecycle disruptions
11e82d076 Keep oracle reply index clean across bookkeeping reads
fb2375b14 Register reply expectations at send time
c4234f3c8 Never await responses suppressed by source redaction
c9743e92e Model active-follow-up coalescing in the chaos oracle
2e4b17d3f Settle coalesced sources from the durable turn ledger
0d97ac285 Honor same-sender supersede policy in chaos settlement
3096abd68 Never let a coalesced sibling source supersede its own turn   (Fix 1)
7f4a38fe5 Recheck stale streams after the startup recency guard elapses  (Fix 2)
98d7acd40 Accept recovered interruptions in the final body audit
fcbcfaca4 Drop replayed delivery of an in-flight-claimed source event    (Fix 3)
1acaac8b5 docs: add living fuzz campaign handoff
22e8cd078 Block live fuzz settle on incomplete streaming replies
2dc2e52d5 docs: record follow-up PR state
28b45c004 docs: record oracle streaming-settle fix and C2 seed 4 pass
aba3c9067 Fix stale-stream test ref to public recency-guard constant
bc241598c docs: refresh fuzz handoff for fresh-session pickup
13da9895d Cover cached-path coalesced-sibling exclusion in replay guard   (Item A)
64ca7c360 Replay only the pending recency recheck after config reload     (Item B)
```

### MindRoom PR #1640

- URL: https://github.com/mindroom-ai/mindroom/pull/1640
- Branch: `test/fuzz-state-machine-expansion`
- Base: `main`
- Pushed head at creation: `0564386e50ac696b1065a349f2a5070573a5caf2`
- State at this update: open and non-draft.
- Scope: model-based cache fuzzing and limited-sync recovery fuzzing.
- Merge status: active investigation and not merge-ready.

## Agent-cli sessions

### Codex state-machine session

- tmux session: `mr-fuzz-codex`
- Branch: `test/fuzz-state-machine-expansion`
- Worktree: `/Users/bas.nijholt/.codex/worktrees/fd26/mindroom-worktrees/test-fuzz-state-machine-expansion`
- Local task file: `.claude/TASK-1784887160-8027.md`
- Primary emphasis: model-based cache state, SQLite/Postgres parity, lifecycle resets, delayed relations, exact limited-sync recovery, and strict live reply cardinality.
- Current commits after rebasing onto `main`:
  - `ca84e05ec90c3c5d4f9a8d77f09db43834d108ff` — `test: add model-based Matrix cache fuzzing`
  - `6bc6ce076e6ddfa212da870cb0bc0e3f3b168411` — `test: add live limited-sync recovery fuzzing`
  - `0564386e50ac696b1065a349f2a5070573a5caf2` — `test: harden limited sync live oracle`
- Current working tree has an in-progress fixture rename, matching test edits, and the agent-cli task file.
- The latest tracked recovery replay passed `51/51` sources with 57 operations, six retries, two restarts, seven degraded thread reads, zero dispatch read timeouts, 31.167 seconds internal runtime, and 33.524 seconds wall time.
- The same logical trace previously lost `op:2` in three runs, while an independent bounded `/messages?dir=b&to=since` audit saw the source and MindRoom's callback did not.
- Because the latest replay passed, the source-loss candidate is currently a nondeterministic hypothesis rather than an accepted new mindroom-nio bug.
- Codex is running additional isolated replays to measure reproducibility before assigning ownership or proposing a fix.
- A temporary diagnostic mindroom-nio checkout exists at `/tmp/mindroom-nio-pr20.xxIV9X/repo`.
- The diagnostic checkout briefly exposed an environment trap where a built wheel, rather than the edited source checkout, was active in the MindRoom venv.
- Never trust diagnostic conclusions unless the active `nio.client.async_client.__file__` proves the intended exact source is loaded.

### Claude live-chaos session

- tmux session: `mr-fuzz-claude`
- Branch: `test/fuzz-live-chaos-expansion`
- Worktree: `/Users/bas.nijholt/.codex/worktrees/fd26/mindroom-worktrees/test-fuzz-live-chaos-expansion`
- Local task file: `.claude/TASK-1784887170-88f8.md`
- Agent report: `.claude/REPORT.md`
- Primary emphasis: real-Tuwunel multi-room/multi-client chaos, streaming overlap, lifecycle disruptions, exact final-state audit, latency summaries, and durable ledger attribution.
- Current working tree has in-progress live harness and test edits plus the agent-cli task file.
- Current commits after rebasing onto `main`:
  - `35e860eae4f1d1c67cc4a79a072f8401330d4089` — `Add composable live chaos profile with lifecycle disruptions`
  - `11e82d07638432015bb68fdcac31466be38455b7` — `Keep oracle reply index clean across bookkeeping reads`
  - `fb2375b14c3b7380bf9f2c802867b22dd28a6689` — `Register reply expectations at send time`
  - `c4234f3c8a8a683725d324831f941498a1eea5bd` — `Never await responses suppressed by source redaction`
  - `c9743e92ea6fcc62f9ae985668e57d477775e9f4` — `Model active-follow-up coalescing in the chaos oracle`
  - `2e4b17d3f4502a543ee4257859db15102d4b2f51` — `Settle coalesced sources from the durable turn ledger`
  - `0d97ac2858486381c51327d83ee21c5a37e4463c` — `Honor same-sender supersede policy in chaos settlement`
  - `3096abd6815b5027acc14b65125de2771ad3bcdb` — `Never let a coalesced sibling source supersede its own turn`
  - `7f4a38fe5bf9b2520245db37a4f3e30a665dbf02` — `Recheck stale streams after the startup recency guard elapses`
  - `98d7acd409664dabccf7db11661824a382f0bd66` — `Accept recovered interruptions in the final body audit`
  - `fcbcfaca43893933cac592701b26f67ffc2ca4cf` — `Drop replayed delivery of an in-flight-claimed source event`
  - `1acaac8b58342241df7ca31c42570ac329889691` — `docs: add living fuzz campaign handoff`
- Claude is running post-fix verification campaigns over the original failing seeds and additional seeds.
- The branch's focused live-fuzz test suite reported 25 passed before the third product fix.
- Commit hooks passed for the committed changes, but the complete final suite and all-files pre-commit gate are still pending.

## Confirmed MindRoom findings on the Claude branch

### Finding 1: coalesced sibling falsely supersedes its own turn

Status: fixed in `3096abd6815b5027acc14b65125de2771ad3bcdb`, with deterministic live reproduction and focused regression.

An edit can bump a source event's visible timestamp above the coalesced turn anchor.
The replay guard then mistakes that sibling source from the same current turn for a newer unresponded requester message.
The guard skips the entire combined turn and durably marks every batched source handled without a response.
The branch passes the current turn's `indexed_event_ids` into both full-history and degraded-cache replay guards and excludes those IDs from superseding evidence.
The minimized live trace is recorded in the Claude worktree's `.claude/REPORT.md`.

Review risk: the guard must still skip for a genuinely newer requester event outside the current turn.
Review risk: both full-history and degraded-cache paths must have equivalent exclusion semantics.

### Finding 2: fast restart permanently freezes recent interrupted streams

Status: fixed in `7f4a38fe5bf9b2520245db37a4f3e30a665dbf02`, with focused startup-maintenance regressions and live reproduction.

Startup stale-stream recovery deliberately ignores events younger than the ten-second recency guard.
A fast restart performs all startup scans inside that guard and never revisits skipped streams.
Those replies can remain forever as `Thinking...` or truncated streaming edits.
The branch schedules one recovery recheck after the recency guard has elapsed.

Review risk: the current implementation keeps the startup-maintenance task alive while sleeping for roughly twelve seconds.
Review must prove shutdown cancellation, readiness, hot-reload, and repeated-start behavior remain correct.
If that lifecycle is awkward, extract the delayed recheck into an owned background task rather than weakening the recovery requirement.

### Finding 3: replayed delivery of an already in-flight source starves an innocent coalesced source

Status: fixed in `fcbcfaca43893933cac592701b26f67ffc2ca4cf`, with repeated live fingerprint and focused ingress/turn-store regressions.

The same source event can re-enter ingress while an earlier delivery already owns an in-flight turn claim.
The replayed source can then be folded into a later active-follow-up batch containing a new source.
The all-or-nothing claim collision drops the later combined turn and permanently starves the innocent source.
The branch adds an early in-flight ownership check before a replayed source can enter coalescing again.

Review risk: the early check must remain fail-closed and must not suppress legitimate recovery after a claim is released, failed, or terminal.
Review risk: durable handled events, currently active claims, and stale abandoned claims must remain distinct states.
Review risk: the same event must not appear in a prompt or visible reply twice.

## Harness and oracle defects found and fixed

- `0b4f55471` removes false unexpected-reply entries created by reading a `defaultdict`.
- `683152824` registers source expectations at send time so a fast reply cannot beat post-batch bookkeeping.
- `0099e73fe` stops waiting for a response that MindRoom correctly suppresses after source redaction.
- `cdb332b76` models intended active-follow-up coalescing instead of demanding one visible reply per queued source.
- `a4fa5a360` uses the durable turn ledger to prove every coalesced source was covered by a completed turn.
- `ad8fb814c` models same-sender supersede policy during chaos settlement.
- `3fc214e88` accepts an interrupted visible body only when a completed auto-resume reply exists and separately audits that resumed reply.
- `22e8cd078` blocks settlement while an observed required source's covering reply is still streaming a non-terminal body, so the final audit can no longer read a mid-stream `Thinking...` placeholder as a non-canonical final body. This tightens the oracle without loosening any timeout: a genuinely frozen stream still fails at the checkpoint deadline.

These were test-system defects or semantic gaps and must not be reported as MindRoom product bugs.

## Open review items (RESOLVED 2026-07-24 after supervisor authorization)

### Item A — reviewer blocker: Fix 1 cached-path exclusion has no regression test — RESOLVED in `13da9895d`

Resolution: added `test_cached_guard_never_treats_own_coalesced_sources_as_superseding` plus a sibling-outside-turn control (`test_cached_guard_skips_for_newer_cached_event_outside_current_turn`) to `tests/test_dispatch_replay_guard.py`, exercising `has_newer_unresponded_cached_thread_event` with `current_turn_event_ids`. Test-only change; 4 passed. Original spec follows.

Source: fresh-context `pr-review` (agent `prreview1639`), verdict CHANGES REQUIRED, single blocker. All three product fixes were independently verified correct, minimal, and architecture-respecting; the harness was verified clean; all 29 harness tests and all 11 fix-regression tests pass. The sole gate is missing coverage.

- Invariant under test: a coalesced sibling source from the *current* turn must never be treated as a newer unresponded requester event that supersedes its own turn — on BOTH the full-history path and the degraded/cached path.
- Production code, cached path: `src/mindroom/dispatch_replay_guard.py:142` — `_unresponded_requester_event_id` returns `None` when `event_id in current_turn_event_ids` (the cached-path twin of the full-history exclusion at `dispatch_replay_guard.py:60`). Reached via `has_newer_unresponded_cached_thread_event`.
- Wiring: `src/mindroom/text_ingress_dispatch.py:319-327` passes `current_turn_event_ids = prepared.handled_turn.indexed_event_ids` into BOTH guard paths identically.
- Gap: `tests/test_dispatch_replay_guard.py` only exercises `has_newer_unresponded_in_thread` (full-history). No test passes `current_turn_event_ids` into `has_newer_unresponded_cached_thread_event`. The degraded-path tests in `tests/test_live_message_coalescing.py:4204+` (`test_backlog_replay_degraded_*`) cover positive proof, equal-timestamp, and trusted voice-command bodies, but NOT the coalesced-sibling exclusion this PR adds on the cached path.
- Why it gates: the task explicitly required proving the two paths equivalent. The cached path is the exact path hit after a cold restart / degraded sync — precisely what the chaos harness stresses — so an unguarded regression there would stay green.
- Minimal fix (≈30 lines, NO production change): add one cached-path test mirroring `test_guard_never_treats_own_coalesced_sources_as_superseding` — call `has_newer_unresponded_cached_thread_event` with a newer cached event whose `event_id` is in `current_turn_event_ids`, assert `False`; plus a sibling-outside-turn control asserting `True`.

### Item B — Qodo inline claim (PR #1639): config reload during Fix 2 recheck sleep replays full maintenance — RESOLVED in `64ca7c360`

Resolution: `StartupMaintenanceController` now tracks the pending recency recheck as an explicit resumable phase (`recheck_pending` field, set after runtime-support ready, cleared after the recheck phase). `restart_after_config_reload` replays only the sleep + recheck when `recheck_pending` is set; a fresh `start()` resets the flag so a new startup generation runs every phase. Qodo's Option B as written (make `cancel()` return False after main phases) was rejected because it would silently drop the pending recheck and resurrect the frozen-stream bug. Skipping runtime-support re-marking on the recheck-only path is safe because `_finalize_config_reload` (orchestrator.py:1453) re-syncs and re-marks it on every successful reload. Three lifecycle tests added (reload-during-sleep, repeated reloads, fresh-start reset); 8 passed in `tests/test_startup_maintenance.py`. Qodo inline thread answered (reply id 3646283901). Original spec follows.

Source: GitHub Qodo inline comment on PR #1639, relayed by supervisor. VERIFIED against code and CONFIRMED as a real behavioral wart introduced by Fix 2. Not yet fixed (implementation stopped). This must be addressed and the inline thread answered before merge-readiness.

- Mechanism (confirmed by reading `src/mindroom/startup_maintenance.py`):
  - `StartupMaintenanceController._run` (lines 81-132) runs all recovery phases, calls `mark_runtime_support_ready()` (line 123), then `await asyncio.sleep(self.recency_recheck_delay_seconds)` (line 127, delay = `STALE_STREAM_RECENCY_GUARD_MS/1000 + 2.0` ≈ 12 s), then the final recency-guard recheck phase (lines 128-132).
  - Fix 2 widened the post-ready sleep window from ~0 to ≈12 s. During that sleep, `self.task.done()` is `False`.
  - On config reload, `orchestrator.py:1489` calls `_startup_maintenance.cancel()`, which returns `should_replay = task is not None and not task.done()` → `True` (startup_maintenance.py:63).
  - `orchestrator.py:1568-1571` then calls `restart_after_config_reload`, which calls `self.start(...)` → `_run` from the TOP (startup_maintenance.py:74-79, 50-57).
  - Net: a config reload landing in the ≈12 s recheck window replays ALL maintenance phases (initial recovery, room setup, joined-room delta, runtime support, sleep, recheck), not just the one pending recheck.
- Severity judgment: the recovery phases are idempotent (re-scanning finds nothing new), so this is not a correctness bug; it is wasteful redundant work on the exact restart/reload path the chaos harness stresses, and the window is now large. The fresh-context reviewer called the lifecycle "verified correct" on the axis of "replay-on-cancel is intended"; Qodo is right on the axis of "replaying 4 done phases to reach 1 pending phase is wrong." Both observations are compatible.
- Do NOT blindly apply Qodo's suggestion. Correct fix direction (per the Finding 2 review-risk note already in this doc): track the pending recency recheck as a separate owned task or explicit resumable phase so cancel/replay only re-runs the outstanding recheck, not the whole sequence. Add reload/shutdown/repeated-start lifecycle tests in `tests/test_startup_maintenance.py`.
- After fixing, reply on the Qodo inline thread describing the resolution.

### Non-blocking review observation (do not gate)

`prreview1639` flagged this file (`.claude/FUZZ-CAMPAIGN-STATUS.md`) as internally inconsistent: it referenced the retired stacking base `origin/test/matrix-cache-fuzz` in the hard-rules, gates, and resume-command sections while correctly stating elsewhere that the base is now `main`. Those stale references are process notes, not code claims, and this file is slated for removal before merge. The stale-base references in the sections below have now been corrected in this handoff.

## Full-suite gate results

- FULL SUITE CLEAN (2026-07-24, at `64ca7c360` + doc edits): `uv run pytest -n auto --no-cov` → `11526 passed, 54 skipped, 68 warnings in 84.04s (0:01:24)`. Zero failures, zero errors.
- Prior stale-stream collection error (`_STALE_STREAM_RECENCY_GUARD_MS` rename miss) was fixed in `aba3c9067`; confirmed cleared by the full run above.
- Focused suites at this head: `tests/test_dispatch_replay_guard.py` 4 passed; `tests/test_startup_maintenance.py` 8 passed; `tests/test_stale_stream_cleanup.py` 80 passed; `tests/test_live_matrix_fuzz.py` 29 passed (pre-session).
- `uv run pre-commit run --all-files` (2026-07-24): every hook passes except `prettier`, which deterministically reformats 7 `frontend/src/` files that are byte-identical to `origin/main` on this branch (verified `git diff origin/main -- frontend/` empty). That is pre-existing main-side formatting drift under this environment's prettier, NOT branch-owned; the churn was stashed (`prettier all-files drift on main-owned frontend files`) and must not be committed to PR #1639. Commit-scoped hooks are unaffected because the branch touches no frontend files.

## Current live campaign state

### Codex

- The strict recovery trace is stored at `/tmp/mindroom-recovery-1r-51-6c-seed1638.json`.
- Failure logs use the prefix `/tmp/mindroom-recovery-1r-51-6c-seed1638`.
- Earlier replays lost logical `op:2` with zero duplicates while later thread-history reads could see it.
- The independent observer's bounded gap audit reported `bounded_gap_missing=[]`, which ruled out a simple Tuwunel `/messages` omission for that observer's token range.
- The latest exact-head replay passed `51/51`, so additional isolated repetitions are running before the candidate can be called reproducible.

### Claude

- The chaos profile supports warm restart, kill restart, cold restart, Tuwunel restart, MindRoom outage windows, checkpoints, multi-client authorship, multi-room mapping, slow streaming calls, and exact trace replay.
- The final auditor independently paginates Matrix history, checks latest edits and redactions, validates reply cardinality, and cross-checks the durable handled-turn ledger.
- Post-fix verification reran the original failing seeds and extra seeds.
- The C2-seed4 chaos run previously failed with a harness premature-audit defect (settlement fired mid-stream on a trailing batch after the last checkpoint); fixed by `22e8cd078`. The `C2-seed4-refix` re-run PASSED: 200 ops, 111 canonical replies, 107 completed final bodies, 109 ledger-attributed sources, 1 kill-restart, 1 tuwunel restart, 1 outage, zero cache/dispatch timeouts, zero event-loop stalls, wall 66 s.
- Full campaign verify matrix is GREEN: M1-minimal, C1-seed2, C3-seed1, C4-long, C5-saturation, C6-recovery-pr20, S1-seed7 (2 restarts / 118 attributed), S2-seed11 (4 restarts / 95 attributed), V-seed19, V-seed23, C2-seed4-refix — all PASS.
- Hardening seeds: H-seed31 PASS (exit 0). Seeds 37 and 44 were queued as additional hardening runs but were NOT run before implementation stopped — they are OPTIONAL extra coverage, not a gate. If resumed, run them as isolated serialized background commands with literal (non-dynamic) output paths under `/tmp/livefuzz/`.
- Review diffs for follow-up PR #1639 are taken against `origin/main` (PR #1638 is merged; base is `main`).
- Local HEAD equals pushed `origin/test/fuzz-live-chaos-expansion` at `aba3c9067` (plus the handoff commit added by this update).
- Campaign details and minimized traces live in the Claude worktree's `.claude/REPORT.md` (gitignored working artifact — never commit or force-add it).

## Fresh-context review #2 (2026-07-24, post Items A/B)

- Reviewer: fresh-context subagent `prreview1639b`, base `origin/main` (`66dd4f4a6`), HEAD `e60601ff4`.
- Verdict: MERGE-READY after living-doc removal. Zero code blockers.
- Independently re-verified: Fix 1 exclusion threaded through both guard paths with both `text_ingress_dispatch` call sites wired; Fix 2 lifecycle (shutdown-cancel keeps `recheck_pending`, reload-during-sleep replays recheck-only with no double main phases, repeated reloads stay recheck-only, fresh `start()` runs all phases, completed recheck makes `cancel()` return False); Fix 3 claim check reads the pending set under the mutation lock, placed after `is_handled`, edit-guarded, event-id-keyed.
- Harness (~1875 lines) scanned clean: public-only imports, no swallowed exceptions that could hide failures, sound oracle, temp-dir-only writes.
- Only pre-merge item: remove `.claude/FUZZ-CAMPAIGN-STATUS.md` (intentional, awaiting supervisor order).

## Swarm notes directory (added 2026-07-24)

- Path: `/Users/bas.nijholt/.codex/campaigns/mindroom-fuzz-2026-07-24/` (crash-safe shared notebook, outside any repo).
- Fourteen read-only agents each own exactly one Markdown file; they must not touch product repos, branches, PRs, or each other's files.
- Every swarm claim is provisional until the primary session verifies it against the exact current head; this file remains the aggregate document.
- Inventory at this update (16 files): README plus dispatch_races, fuzz_gap_design, harness_cleanup_perf, matrix_semantics, nio_event_cap, nio_full_review, nio_token_complete, op2_forensics, pr_overlap, review_pr1639_oracle, review_pr1639_prod, review_pr1640_cache, review_pr1640_live, startup_lifecycle, test_determinism.
- Early signal: `review_pr1639_prod` (pinned at `e60601ff4`) reports no blockers in the dispatch replay guard or in-flight ingress claims; still inspecting stale-stream cleanup and startup lifecycle at the time of its last write.
- Swarm agents are still writing; do NOT block on full swarm completion for session progress (supervisor instruction).

## Postgres 45-thread fanout note (supervisor-flagged) — RESOLVED 2026-07-24

- Isolated rerun DONE with the rest of the suite idle: `uv run pytest "tests/test_matrix_event_cache_fuzz.py::test_forty_five_thread_fanout_matches_every_cache_backend" -n 0 --no-cov` → 2 passed in 14.29 s (postgres call 10.29 s + 1.87 s container setup; sqlite call 1.72 s).
- Verdict: comfortable margin under the 60 s per-test timeout when solo, so the earlier pressure was xdist/Postgres contention, not a MindRoom slowdown. Timeout left untouched.

## Required supervision gates before either extension PR

1. Let all current live campaigns finish and record exact results.
2. Replay every remaining failure and classify it as harness bug, intended semantics, MindRoom bug, mindroom-nio bug, Tuwunel bug, or unresolved nondeterminism.
3. Fix every confirmed in-scope bug at the owning layer.
4. Rerun the minimized reproducer and the original full campaign after each fix.
5. Force each agent to reread its local `.claude/TASK-*.md` and audit every requirement.
6. Run current-context `pr-review` against `origin/main` in each session.
7. Fix every blocker, rerun tests, commit, push, and prove local HEAD equals remote branch HEAD.
8. Send `/new` to each agent and verify the context reset.
9. Run a fresh-context `pr-review` against `origin/main`. (DONE for PR #1639: verdict CHANGES REQUIRED, one blocker — Item A above.)
10. Repeat the fix, test, push, `/new`, and fresh-review loop until clean.
11. Force one final task-file reread.
12. PRs are already open and non-draft with base `main` (#1639, #1640).
13. Wait for GitHub AI reviews and CI, validate every comment against current code, and address only real in-scope blockers.
14. Do not merge.

## Exact resume commands

```bash
tmux capture-pane -p -t mr-fuzz-codex | tail -n 120
tmux capture-pane -p -t mr-fuzz-claude | tail -n 120
tmux attach -t mr-fuzz-codex
tmux attach -t mr-fuzz-claude
agent-cli dev status
```

```bash
git -C /Users/bas.nijholt/.codex/worktrees/fd26/mindroom-worktrees/test-fuzz-state-machine-expansion status --short
git -C /Users/bas.nijholt/.codex/worktrees/fd26/mindroom-worktrees/test-fuzz-state-machine-expansion log --oneline origin/main..HEAD
git -C /Users/bas.nijholt/.codex/worktrees/fd26/mindroom-worktrees/test-fuzz-live-chaos-expansion status --short
git -C /Users/bas.nijholt/.codex/worktrees/fd26/mindroom-worktrees/test-fuzz-live-chaos-expansion log --oneline origin/main..HEAD
git -C /Users/bas.nijholt/.codex/worktrees/fd26/mindroom-worktrees/test-fuzz-live-chaos-expansion rev-parse HEAD
git ls-remote origin refs/heads/test/fuzz-live-chaos-expansion
```

```bash
gh pr view 1638 --repo mindroom-ai/mindroom --json state,isDraft,headRefOid,mergeStateStatus,url
gh pr view 20 --repo mindroom-ai/mindroom-nio --json state,isDraft,headRefOid,mergeStateStatus,url
```

## Pending work (2026-07-24, after Items A and B resolved)

1. DONE: git state confirmed (`64ca7c360` everywhere), Item A (`13da9895d`), Item B (`64ca7c360`), Qodo reply (id 3646283901), Postgres fanout isolated rerun (2 passed solo).
2. Re-run the FULL suite clean and record the exact `N passed` summary line in this doc (in progress this session).
3. Run `uv run pre-commit run --all-files`.
4. Run fresh-context `pr-review` against `origin/main`; fix all valid blockers; repeat until clean.
5. Commit each tested increment individually (`git add <file>`, never `git add .`; author `Bas Nijholt <bas@nijho.lt>`; no amend/force-push), push, and update this handoff.
6. Before PR #1639 is merge-ready: REMOVE this file (`.claude/FUZZ-CAMPAIGN-STATUS.md`) from the branch — only when the supervisor says so. Do not merge.

## Precise commands

```bash
# Verify heads
cd /Users/bas.nijholt/.codex/worktrees/fd26/mindroom-worktrees/test-fuzz-live-chaos-expansion
git rev-parse HEAD; git ls-remote origin refs/heads/test/fuzz-live-chaos-expansion; git rev-parse origin/main
git log --oneline --reverse origin/main..HEAD

# Full suite (NixOS: prefix with `nix-shell shell.nix` per CLAUDE.md)
uv run pytest -n auto --no-cov

# Targeted tests for the two open items
uv run pytest tests/test_dispatch_replay_guard.py -n 0 --no-cov
uv run pytest tests/test_startup_maintenance.py -n 0 --no-cov
uv run pytest tests/test_stale_stream_cleanup.py -n 0 --no-cov     # currently 80 passed
uv run pytest tests/test_live_matrix_fuzz.py -n 0 --no-cov          # currently 29 passed

# Fresh-context review base (always origin/main)
git diff origin/main..HEAD

# Living-doc removal before merge-readiness
git rm .claude/FUZZ-CAMPAIGN-STATUS.md
```

## Operational cautions

- Do not kill all Docker containers or all tmux sessions.
- The fuzz runners own disposable stacks and must clean only their exact instances.
- Two agent sessions may run Tuwunel concurrently, so preserve their isolated instance names and ports.
- Do not commit `.claude/TASK-*.md`.
- Keep this status file current even if agent reports are incomplete.
- Before acting on a suspected mindroom-nio failure, verify the active module path and exact Git SHA inside the spawned MindRoom process.
