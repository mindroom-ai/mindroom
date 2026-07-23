---
name: native-subagent-pr-loop
description: Use when the user asks for native Codex sub-agents, parallel PR review agents, unbiased re-review loops, or main-thread fixes after agent findings in the MindRoom repository.
---

# Native Subagent PR Loop

## Core Rule

The main thread owns all repository mutations.
Native subagents provide bounded analysis or review, but review findings are untrusted until the main thread verifies them against current code.
After each fix, use fresh read-only reviewers with neutral prompts.

## Use When

- The user asks for native Codex agents, native sub-agents, parallel review agents, or a review loop.
- The user wants fixes made inline in the main thread after agent findings.
- The work is on a PR or branch where commits and pushes should remain inspectable.

## Do Not Use

- The user explicitly asks for `agent-cli`, tmux supervision, or another external agent runner.
- The task has no useful independent subtask or review surface.
- A reviewer must use private context, CI state, or browser state unavailable to a fresh subagent.

## Main Loop

1. Pin context.
   Read repo instructions, check `git status --short --branch`, identify base/head, and keep the active branch unless a separate branch is clearly safer.
   In this repo, never create `codex/...` branches.
2. Implement in the main thread.
   The main thread edits, verifies, stages targeted files only, commits, and pushes.
   Do not use `git add .`, amend, or force-push unless the user explicitly asks.
3. Use task subagents only for bounded, non-overlapping analysis or review.
   Subagents must not edit repository files.
   The main thread applies any verified changes.
4. After each pushed step, start two fresh native Codex PR-review agents.
   Use read-only prompts, attach `pr-review` when reviewing merge readiness, and tell them not to edit, commit, push, or inspect CI if the user excluded CI.
5. Treat findings as claims.
   Verify each finding against current code before editing.
   Fix only real, in-scope issues in the main thread.
   Classify stale, incorrect, overreaching, or duplicate findings instead of patching blindly.
6. Repeat after any fix.
   Commit and push the main-thread fix, close old reviewers, then launch fresh reviewers against the new head.
   After every third review round that still finds many issues or a new major bug class, stop patching and reconsider the design before another patch round.
7. Stop only when both fresh reviewers approve the same head.
   Confirm the worktree is clean and the remote branch matches the local head.

## Bias Firewall

Do not bias review agents with:

- Prior findings or expected bugs.
- Claims that the PR should now be clean.
- A desired verdict.
- A narrowed scope such as "only check the previous failure."
- Defenses of the implementation.

Allowed review context:

- Repo path, PR number or URL, base ref, head ref, exact head SHA, and diff command.
- User constraints, such as "CI/tests already passing; do not inspect CI."
- Required skill, such as `pr-review`.
- Read-only requirement and exact output format.

Mention prior findings only when the user explicitly asks reviewers to re-check those exact items.

## Neutral Review Prompt Template

```text
Use the attached pr-review skill.
Native Codex sub-agent only; do not use agent-cli.

Review PR <owner/repo#number> in repo <absolute repo path>.
Latest local HEAD is <sha> on branch <branch>.
Base is <base ref>; review the real diff <base ref>..HEAD.

Do not edit files, commit, push, or inspect CI.
<User constraint if any, for example: CI/tests are already passing.>

Output only:
- Verdict: APPROVE or CHANGES REQUIRED
- Findings with exact file/line and required fix

If no blockers, say APPROVE and no findings.
```

## Handling Reviewer Results

- Wait for both reviewers before declaring the loop clean.
- One approval is not enough if the other reviewer is still running.
- If any reviewer says `CHANGES REQUIRED`, verify the claim before editing.
- If the claim is real, fix it in the main thread, run focused verification, commit, push, and start a new review loop.
- If the claim is stale or wrong, record the reason and continue evaluating the other findings.
- Close completed subagents after their results are no longer needed.

## Final Report

Report only current facts:

- Branch and pushed head SHA.
- Commits made.
- Verification run.
- Review loop outcome.
- Any skipped verification and why.
