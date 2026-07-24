# Matrix fuzz campaign living status

> This file is an uncommitted operational handoff document.
> Update it after every material finding, fix, campaign result, review result, push, or PR state change.
> Do not include it in a product PR unless the user explicitly requests that.

Last updated: 2026-07-24 07:52 PDT.

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
- Stack fuzz-extension PRs on `test/matrix-cache-fuzz`, because `origin/main` does not contain the harness.
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
- State at this update: open and non-draft.
- Scope: live chaos harness, three current MindRoom fixes, regression tests, and this living handoff.
- Merge status: active investigation and not merge-ready.
- This living handoff must be removed before PR #1639 is merge-ready.

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
- The C2-seed4 chaos run previously failed with a harness premature-audit defect (settlement fired mid-stream on a trailing batch after the last checkpoint); fixed by `22e8cd078`. The `C2-seed4-refix` re-run PASSED: 200 ops, 111 canonical replies, 107 completed final bodies, 109 ledger-attributed sources, 1 kill-restart, 1 tuwunel restart, 1 outage, zero cache/dispatch timeouts, zero event-loop stalls, wall 66 s. The full verification matrix (S1 seed 7, S2 seed 11, V seed 19, V seed 23, C2 seed 4) is now green.
- Review diffs for follow-up PR #1639 are taken against `origin/main` (PR #1638 is merged; base is `main`).
- Local HEAD equals pushed `origin/test/fuzz-live-chaos-expansion` at `2dc2e52d5`, which includes the oracle fix `22e8cd078`.
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
