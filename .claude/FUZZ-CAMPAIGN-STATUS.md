# Matrix fuzz campaign living status

> This file is an uncommitted operational handoff document.
> Update it after every material finding, fix, campaign result, review result, push, or PR state change.
> Do not include it in a product PR unless the user explicitly requests that.

Last updated: 2026-07-24 07:34 PDT.

## Merge recommendation for PR #1638

PR #1638 itself is complete and merge-ready.
The two extension branches are separate worktrees based on its head and are not modifying PR #1638.
The newly found product bugs are follow-up findings and do not block the base harness PR.
The user may merge PR #1638 now.
After PR #1638 merges, retarget both extension branches and their eventual PRs from `test/matrix-cache-fuzz` to `main`.

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
- Stack fuzz-extension PRs on `test/matrix-cache-fuzz`, because `origin/main` does not contain the harness.
- Run real Tuwunel campaigns in isolated disposable stacks.
- Do not weaken an oracle or increase timeouts to hide a failure.
- Do not merge any PR on the user's behalf.

## Existing base PRs

### MindRoom PR #1638

- URL: https://github.com/mindroom-ai/mindroom/pull/1638
- Branch: `test/matrix-cache-fuzz`
- Head: `82711554dc470623e25833b0d1a6c01b13e0fbf4`
- Current GitHub state at this update: open, non-draft, clean merge state.
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

## Agent-cli sessions

### Codex state-machine session

- tmux session: `mr-fuzz-codex`
- Branch: `test/fuzz-state-machine-expansion`
- Worktree: `/Users/bas.nijholt/.codex/worktrees/fd26/mindroom-worktrees/test-fuzz-state-machine-expansion`
- Local task file: `.claude/TASK-1784887160-8027.md`
- Primary emphasis: model-based cache state, SQLite/Postgres parity, lifecycle resets, delayed relations, exact limited-sync recovery, and strict live reply cardinality.
- Current commits:
  - `40f677cd4cf984f171a795fa2723da48dfe55e3d` — `test: add model-based Matrix cache fuzzing`
  - `f8fc835d02bacdc78a5bbb0aeaef351d2940031c` — `test: add live limited-sync recovery fuzzing`
  - `a1d6c073973369ff4e40c8585dde9ba300965b3c` — `test: harden limited sync live oracle`
- Current working tree contains only the agent-cli task file.
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
- Current working tree contains only the agent-cli task file.
- Current commits:
  - `74e9f57e91975510eb41e38f47491b374b468333` — `Add composable live chaos profile with lifecycle disruptions`
  - `0b4f554712a6d4d15c828c437c67a77d1ea5899c` — `Keep oracle reply index clean across bookkeeping reads`
  - `68315282423bcdb72ecaeebf5e53533673bfc4d7` — `Register reply expectations at send time`
  - `0099e73fe21513e6af8058b06320c298f999d37a` — `Never await responses suppressed by source redaction`
  - `cdb332b763b0cb47391259154c898b8d926eb453` — `Model active-follow-up coalescing in the chaos oracle`
  - `a4fa5a36025025a00c8faf68129a4811feb1777f` — `Settle coalesced sources from the durable turn ledger`
  - `ad8fb814cfdf2e85fbd65a5e38407e30c925f84d` — `Honor same-sender supersede policy in chaos settlement`
  - `4a68007c13cf0ac4dc3c18ba3a5d569bf11f4a4d` — `Never let a coalesced sibling source supersede its own turn`
  - `78e676151a9836308a3ea084be9d78681915d42c` — `Recheck stale streams after the startup recency guard elapses`
  - `3fc214e88ab2e9a7915ebc7d28a4ce4df764a6a4` — `Accept recovered interruptions in the final body audit`
  - `8f16f5c6231ebc4bbc6c3707a1298b28365c9639` — `Drop replayed delivery of an in-flight-claimed source event`
- Claude is running post-fix verification campaigns over the original failing seeds and additional seeds.
- The branch's focused live-fuzz test suite reported 25 passed before the third product fix.
- Commit hooks passed for the committed changes, but the complete final suite and all-files pre-commit gate are still pending.

## Confirmed MindRoom findings on the Claude branch

### Finding 1: coalesced sibling falsely supersedes its own turn

Status: fixed in `4a68007c13cf0ac4dc3c18ba3a5d569bf11f4a4d`, with deterministic live reproduction and focused regression.

An edit can bump a source event's visible timestamp above the coalesced turn anchor.
The replay guard then mistakes that sibling source from the same current turn for a newer unresponded requester message.
The guard skips the entire combined turn and durably marks every batched source handled without a response.
The branch passes the current turn's `indexed_event_ids` into both full-history and degraded-cache replay guards and excludes those IDs from superseding evidence.
The minimized live trace is recorded in the Claude worktree's `.claude/REPORT.md`.

Review risk: the guard must still skip for a genuinely newer requester event outside the current turn.
Review risk: both full-history and degraded-cache paths must have equivalent exclusion semantics.

### Finding 2: fast restart permanently freezes recent interrupted streams

Status: fixed in `78e676151a9836308a3ea084be9d78681915d42c`, with focused startup-maintenance regressions and live reproduction.

Startup stale-stream recovery deliberately ignores events younger than the ten-second recency guard.
A fast restart performs all startup scans inside that guard and never revisits skipped streams.
Those replies can remain forever as `Thinking...` or truncated streaming edits.
The branch schedules one recovery recheck after the recency guard has elapsed.

Review risk: the current implementation keeps the startup-maintenance task alive while sleeping for roughly twelve seconds.
Review must prove shutdown cancellation, readiness, hot-reload, and repeated-start behavior remain correct.
If that lifecycle is awkward, extract the delayed recheck into an owned background task rather than weakening the recovery requirement.

### Finding 3: replayed delivery of an already in-flight source starves an innocent coalesced source

Status: fixed in `8f16f5c6231ebc4bbc6c3707a1298b28365c9639`, with repeated live fingerprint and focused ingress/turn-store regressions.

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

These were test-system defects or semantic gaps and must not be reported as MindRoom product bugs.

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
- Post-fix verification is rerunning original failing seeds and extra seeds.
- Campaign details and minimized traces live in the Claude worktree's `.claude/REPORT.md`.

## Required supervision gates before either extension PR

1. Let all current live campaigns finish and record exact results.
2. Replay every remaining failure and classify it as harness bug, intended semantics, MindRoom bug, mindroom-nio bug, Tuwunel bug, or unresolved nondeterminism.
3. Fix every confirmed in-scope bug at the owning layer.
4. Rerun the minimized reproducer and the original full campaign after each fix.
5. Force each agent to reread its local `.claude/TASK-*.md` and audit every requirement.
6. Run current-context `pr-review` against `origin/test/matrix-cache-fuzz` in each session.
7. Fix every blocker, rerun tests, commit, push, and prove local HEAD equals remote branch HEAD.
8. Send `/new` to each agent and verify the context reset.
9. Run a fresh-context `pr-review` against `origin/test/matrix-cache-fuzz`.
10. Repeat the fix, test, push, `/new`, and fresh-review loop until clean.
11. Force one final task-file reread.
12. Open normal non-draft stacked PRs with base `test/matrix-cache-fuzz`.
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
git -C /Users/bas.nijholt/.codex/worktrees/fd26/mindroom-worktrees/test-fuzz-state-machine-expansion log --oneline origin/test/matrix-cache-fuzz..HEAD
git -C /Users/bas.nijholt/.codex/worktrees/fd26/mindroom-worktrees/test-fuzz-live-chaos-expansion status --short
git -C /Users/bas.nijholt/.codex/worktrees/fd26/mindroom-worktrees/test-fuzz-live-chaos-expansion log --oneline origin/test/matrix-cache-fuzz..HEAD
```

```bash
gh pr view 1638 --repo mindroom-ai/mindroom --json state,isDraft,headRefOid,mergeStateStatus,url
gh pr view 20 --repo mindroom-ai/mindroom-nio --json state,isDraft,headRefOid,mergeStateStatus,url
```

## Operational cautions

- Do not kill all Docker containers or all tmux sessions.
- The fuzz runners own disposable stacks and must clean only their exact instances.
- Two agent sessions may run Tuwunel concurrently, so preserve their isolated instance names and ports.
- Do not commit `.claude/TASK-*.md`.
- Keep this status file current even if agent reports are incomplete.
- Before acting on a suspected mindroom-nio failure, verify the active module path and exact Git SHA inside the spawned MindRoom process.
