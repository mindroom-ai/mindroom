---
name: agent-cli-tmux-supervision
description: Use when spawning and supervising multiple agent-cli Codex worktrees through tmux, especially when you must poll progress, force prompt rereads against the local TASK file, run current-context and fresh-context pr-review loops, and verify that review fixes are actually pushed before merge.
---

# Agent CLI + tmux Supervision

Use this skill when you are supervising parallel Codex branches launched with `agent-cli dev new ... --tmux-session ...`.

This is not just a launch guide.
It is the full supervision protocol for getting from spawned worktree to reviewed PR without letting agents stop early.

## When to use

Use this skill when:

- multiple Codex agents are working in separate `agent-cli` worktrees
- each branch needs active tmux supervision instead of fire-and-forget delegation
- you need repeated review loops before considering a PR done
- you need to prove that review fixes are pushed, not just described

Do not use this skill when:

- there is only one local branch and no tmux-supervised sub-agents
- the work is small enough to complete directly in the current session

## Core launch pattern

Launch each branch with an explicit base, Codex as the agent, a dedicated tmux session, and a prompt file.

```bash
agent-cli dev new <branch-name> \
  --from origin/main \
  --agent codex \
  --tmux-session <session-name> \
  --prompt-file <prompt-file>
```

Use a feature branch as `--from` only when the prompt explicitly depends on unmerged branch work.

## What the agent actually reads

The original prompt file path does not have to exist inside the spawned worktree.

`agent-cli` copies the prompt into the worktree as `.claude/TASK-*.md`.

When telling an agent to reread its instructions, always point it at the local `.claude/TASK-*.md` file, not the original prompt path from the main repo.

## Known environment caveat

If `uv sync --all-extras` fails during setup but the tmux session still launches, treat that as a setup caveat, not an automatic launch failure.

In this repo, a known example is `python-olm` failing to build while Codex still starts successfully in tmux.

## Sending messages reliably

Do not trust one combined `tmux send-keys ... Enter` command.

The reliable pattern is:

```bash
tmux send-keys -t <session> 'your message here'
tmux send-keys -t <session> Enter
```

Then verify that the message actually started running:

```bash
tmux capture-pane -p -t <session> | tail -n 20
```

Look for `Working`.

If the text is visible at the prompt but not running yet, send another bare `Enter`.

Do not use `C-c` just to submit buffered text.

## Monitoring loop

When actively supervising agents, do a real polling loop instead of assuming they are still working.

1. Sleep for about a minute.
2. Capture the last 20-50 lines from each relevant tmux session.
3. Classify each branch as:
   - still working
   - parked at a prompt
   - done enough for review
   - blocked and needing intervention

Useful commands:

```bash
tmux list-panes -a -F '#{session_name}|#{pane_current_command}|#{pane_dead}|#{pane_title}'
tmux capture-pane -p -t <session> | tail -n 40
```

## Do not accept “done” too early

Passing tests is not enough.

A clean self-review is not enough.

A PR is not done just because the agent says the prompt is satisfied.

Before review, force the agent to reopen its exact `.claude/TASK-*.md` file and compare the branch against:

- the goal
- the problem statement
- the design target
- the scope
- the important notes

The key question is:

- did the branch actually deliver the intended simplification and source-of-truth cleanup, or did it only move logic around and pass tests?

If there is still a prompt gap, the agent should keep working before review.

## Review loop

Once the agent has completed the prompt-reread pass, run this loop.

### 1. Current-context review

Tell the agent to run `pr-review` in its current context against `origin/main`.

Require it to:

- fix every blocker
- rerun relevant tests
- commit and push if it changed anything

### 2. Explicit pushed-state confirmation

If the review found blockers, do not move on until the agent confirms:

- the blockers are fully fixed
- the exact HEAD SHA
- the remote branch SHA matches
- the PR head SHA matches

Do not accept vague answers like “should be fixed”.

### 3. Fresh-context review

After the current-context review is clean, send:

```text
/new
```

Verify the pane actually reset.

Then tell the agent to run `pr-review` again in the fresh context.

Fresh-context review is required because it often catches issues that current-context review misses.

### 4. Repeat until clean

If the fresh-context review finds blockers:

- fix them
- rerun relevant tests
- commit and push
- explicitly confirm the pushed SHA state
- send `/new` again
- rerun fresh-context `pr-review`

Keep looping until the fresh-context review is clean.

## Finalization gate

After the clean fresh-context review, force one last prompt reread against `.claude/TASK-*.md`.

Only then should the agent:

- open or update the PR
- report PR number
- report PR URL
- report exact HEAD SHA

## PR sync check

Before considering the branch settled, confirm that the local branch, remote branch, and PR head all match.

Useful commands:

```bash
git rev-parse HEAD
git rev-parse origin/<branch>
gh pr view <number> --json headRefOid,updatedAt,url
```

If those SHAs do not match, the branch is not actually settled yet.

## Full-suite instruction

If an agent chooses to run the whole repository suite, tell it to use:

```bash
pytest -n auto --no-cov
```

## Completion checklist

A supervised branch is only complete when all of these are true:

1. The agent reread its local `.claude/TASK-*.md` and confirmed full prompt coverage.
2. Current-context `pr-review` was run.
3. Any blockers were fixed, tested, committed, and pushed.
4. Pushed-state SHA confirmation was given.
5. `/new` was sent and verified.
6. Fresh-context `pr-review` was run.
7. The fix/push/`/new`/fresh-review loop repeated until clean.
8. Final prompt reread was done.
9. The PR was opened or updated.
10. The local SHA, remote SHA, and PR head SHA all match.
