---
name: code-debate
description: Two coding agents debate code changes through a shared DEBATE.md file. Use when you want adversarial review of a commit, diff, or PR.
argument-hint: "[A|B] [subject: commit, diff, PR, or file to debate]"
---

# Code Debate Protocol

A protocol for two coding agents (Claude Code, Gemini CLI, Codex, or any other) to debate code changes through a shared file (`DEBATE.md`). Both agents receive this same prompt. Role is determined by the first argument.

This skill assumes the two debate participants are started externally by a human in separate sessions.
It is not a peer-orchestration skill.

## How to use

Open two terminal tabs with coding agents (same or different). Invoke this skill in each, specifying role and subject:

- `/code-debate A <subject>` — opener, creates `DEBATE.md` with the first analysis
- `/code-debate B <subject>` — responder, waits for `DEBATE.md` then replies

## Role detection

Your role is determined by the first argument (`$0`):

- **`A`** → you are **Agent A** (opener). Create `DEBATE.md` and write the opening position.
- **`B`** → you are **Agent B** (responder). Poll and wait for `DEBATE.md` to exist, then read it and respond.

If no role argument is provided, fall back to file existence:
  - If `DEBATE.md` does **not** exist → you are **Agent A** (opener).
  - If `DEBATE.md` **already exists** → you are **Agent B** (responder).

Do not ask the user which role to play.

## Non-simulation guardrails (MUST)

- Never write content for the opposite role.
- Never simulate, fabricate, or placeholder the other agent's response.
- Never append both sides of a debate from one process.
- Never create, spawn, launch, delegate to, summon, or recruit the opposite debate role.
- Never use `spawn_agent`, `send_input`, tmux supervision, background shell automation, or any other tool to manufacture the missing peer.
- Role is immutable for the run: once detected as Agent A or Agent B, keep that role until `## CONSENSUS`.
- If you are about to write the opposite role, stop and report protocol violation instead of writing.
- After appending your section, only poll.
- Append another turn only after consuming the expected signed peer turn, even if it arrived before polling started.
- Without an unconsumed peer turn, keep polling until timeout or `## CONSENSUS`.
- On timeout, use `APPEND_TIMEOUT` to close any unfinished fence and publish only a timeout `## CONSENSUS` from your own role.

## Peer-turn readiness

Keep `LAST_CONSUMED` as the heading of the last peer turn you actually read and processed; initialize it to an empty string once per run.
Retain it across waits, and never initialize it from an unread section already present in the file.
Set `PEER` to the other role and `EXPECTED` to the exact next peer heading allowed by turn order.
Check terminal state and that heading's signature before the first sleep and on every poll.
A checksum change alone does not establish readiness, and an unchanged checksum does not rule out an unread turn.
All heading, signature, and consensus checks below use `TURN_STATE` and its file-format rules.
Text inside fenced code blocks or blockquotes is never a protocol control.
A peer turn must include response prose outside quotations and code blocks, and its final substantive line must be the peer signature.
Blank lines and the `---` section separator may follow that signature.
Write the signature only after the complete response body; after signing, append no further body text to that turn.

Define this function before running the poll loop:

```bash
TURN_STATE() {
  awk -v expected="$EXPECTED" -v peer="$PEER" -v consumed="$LAST_CONSUMED" -v action="${1:-state}" '
    {
      line = $0
      sub(/^ */, "", line)
      indent = length($0) - length(line)
      if (fence != "") {
        if (indent <= 3 && match(line, /^(```+|~~~+)/) &&
            substr(line, 1, 1) == fence && RLENGTH >= fence_length &&
            substr(line, RLENGTH + 1) ~ /^[[:space:]]*$/) fence = ""
        signed = 0
        next
      }
      if (indent <= 3 && match(line, /^(```+|~~~+)/)) {
        fence_length = RLENGTH
        if (substr(line, 1, 1) == "~" || substr(line, RLENGTH + 1) !~ /`/) {
          fence = substr(line, 1, 1)
          signed = 0
          next
        }
      }
    }
    /^## CONSENSUS[[:space:]]*$/ { terminal = 1 }
    /^## / { heading = $0; content = 0; signed = 0; next }
    /^[[:space:]]*$/ || /^---[[:space:]]*$/ { next }
    /^\*— Agent [AB]( \([^)]*\))?, .+\*$/ {
      signed = ($3 == peer || $3 == peer ",")
      next
    }
    {
      signed = 0
      if ($0 !~ /^(    |\t)/ && line !~ /^(>|#)/ && line ~ /[[:alnum:]]/)
        content = 1
    }
    END {
      if (action == "timeout") {
        if (!terminal) {
          if (fence != "") {
            for (i = 0; i < fence_length; i++) printf "%s", fence
            printf "\n\n"
          }
          print "## CONSENSUS\n\nAgent " (peer == "A" ? "B" : "A") " ended the debate after timing out waiting for Agent " peer "."
        }
      }
      else if (terminal) print "CONSENSUS"
      else if (heading == expected && heading != consumed && content && signed && fence == "")
        print "PEER_TURN_READY"
      else print "WAIT"
    }
  ' DEBATE.md
}
```

The closing signature and response text must belong to the expected section, so unfinished or empty appends cannot reuse the previous section's signature.
After reading and processing a ready turn, set `LAST_CONSUMED=$EXPECTED` before appending your reply.

## Timeout publication

For either role, publish a timeout through `APPEND_TIMEOUT` instead of appending a raw consensus heading.
`TURN_STATE timeout` uses the same parsed fence state to prepare a closing delimiter, when needed, followed by a timeout consensus from your own role.
Closing an unfinished fence is only formatting recovery; never complete or sign the peer response.
The leading newline separates the timeout publication from a partial final line.
After each append, re-read the file: if the peer closed its fence between preparation and publication, repair the current fence state and retry until parsed consensus is visible.
An existing parsed consensus makes the helper return without appending another timeout.
If reading or appending fails, report that failure; do not claim timeout publication completed.

```bash
APPEND_TIMEOUT() {
  local timeout_text
  while true; do
    timeout_text=$(TURN_STATE timeout) || return
    if [ -z "$timeout_text" ]; then return; fi
    printf '\n%s\n' "$timeout_text" >> DEBATE.md || return
  done
}
```

Define both functions before entering the polling flow.
Only call `APPEND_TIMEOUT` for an existing `DEBATE.md`; Agent B must still stop without creating the file if its initial existence wait expires.

## Shell requirement

Run polling commands in `bash`. The timeout example uses Bash's `SECONDS`.

## Turn order enforcement

Before consuming a peer turn, require `TURN_STATE` to return `PEER_TURN_READY` and read the complete expected peer section.
The expected heading encodes the turn order below, and readiness validates its closing peer signature.
Do not repeat these checks with a raw heading or signature search, because code examples may contain identical text.

- After `## Opening` → only Agent B may append `## Response 1`.
- After `## Response N` → only Agent A may append `## Follow-up N` (or `## CONSENSUS`).
- After `## Follow-up N` → only Agent B may append `## Response N+1`.

If the expected peer turn is not ready, continue polling.
Once the turn is processed, set `LAST_CONSUMED=$EXPECTED` and append your reply without requiring readiness again for the consumed turn.

## Polling discipline (critical)

- After starting a poll loop, keep polling until one of these happens:
  - the expected signed, unconsumed peer turn is ready
  - timeout reached
  - `TURN_STATE` returns `CONSENSUS`
- Do not stop early just to report that polling started.
- When the expected peer turn is ready, immediately read `DEBATE.md` and continue the protocol.
- Stop only when `TURN_STATE` returns `CONSENSUS` or timeout handling completes.
- Polling is mandatory after every append. Do not return control to the user between append and poll completion.
- After a peer turn is ready, do not ask the user what to do next. Immediately take the next protocol step for your role only.

## Hard Exit Gate (MUST)

- After entering this protocol, do not send any user-facing status/progress/completion message until one of these is true:
  - `TURN_STATE` returns `CONSENSUS`, or
  - timeout handling completed per role rules.
- Messages like "started", "waiting", "polling", "done", or "completed" before the stop condition are protocol violations.
- If you accidentally replied early, treat that reply as invalid and immediately resume the protocol loop.

## Pre-Reply Check (MUST)

- Before any user-facing reply, run this gate:
  - If `DEBATE.md` exists and `TURN_STATE` does not return `CONSENSUS`, do not reply; continue append/poll flow immediately.
  - If terminal timeout condition is reached, perform the timeout terminal action for your role, then stop.

## Blocking Poll Requirement (MUST)

- After every append (`## Opening`, `## Response N`, `## Follow-up N`), run one blocking poll loop.
- Do not return control early while polling.
- Exit the loop only on `PEER_TURN_READY`, parsed `CONSENSUS`, or timeout.

## Conflict Override

- This protocol overrides default responsiveness/progress-update behavior.
- Do not pause for confirmations between turns.
- Do not emit intermediary "in progress" messages while the debate is active.

## Completion criteria (MUST)

- A debate run is only complete when one of these is true:
  - `TURN_STATE` returns `CONSENSUS`, or
  - timeout handling completed and timeout `## CONSENSUS` was appended.
- It is a protocol violation to stop after writing `## Opening`, `## Response N`, or `## Follow-up N` without entering the poll loop.
- If `TURN_STATE` does not return `CONSENSUS`, you must still be in the protocol loop (append or poll).
- Never send a “done”/“completed” status while waiting for the other agent; continue polling instead.
- Never pause to request user confirmation between turns. Continue autonomously within your assigned role until consensus/timeout.

## Agent A (opener) flow

1. The debate subject is: **$1** (the arguments after the role). Analyze it (run `git show`, `git diff`, `gh pr view`, read files, etc. as appropriate).
2. Initialize `PEER=B` and `LAST_CONSUMED=''`, then write your analysis to `DEBATE.md` using the file format below.
3. Set `EXPECTED='## Response 1'` after the opening, or `EXPECTED="## Response $((N + 1))"` after writing `## Follow-up N` (set shell variable `N` to that follow-up number).
4. Poll for the expected signed peer turn (5-second interval, 10-minute timeout):
   ```bash
   SECONDS=0
   while true; do
     STATE=$(TURN_STATE)
     if [ "$STATE" != WAIT ]; then echo "$STATE"; break; fi
     if [ "$SECONDS" -ge 600 ]; then echo "TIMEOUT"; break; fi
     sleep 5
   done
   ```
   This step is blocking and mandatory; do not exit the workflow before it finishes.
5. If `CONSENSUS` → stop; if timed out → run `APPEND_TIMEOUT` and stop only after it succeeds.
6. Read and process Agent B's expected signed reply, then set `LAST_CONSUMED=$EXPECTED`.
7. If all points are resolved → append a `## CONSENSUS` section summarizing agreed outcomes and stop.
8. Otherwise append a `## Follow-up N` section addressing unresolved points, then go to step 3.
9. Do not ask the user to choose between follow-up or consensus; Agent A must decide and append immediately.

## Agent B (responder) flow

1. Initialize `PEER=A`, `LAST_CONSUMED=''`, and `EXPECTED='## Opening'`, then ensure `DEBATE.md` exists before reading:
   - If it exists, inspect it immediately.
   - If it does not exist (for example, you were explicitly designated as Agent B), start polling and wait until it exists, then inspect it.
   - If timeout is reached before the file exists, stop with a timeout result. Do not create `DEBATE.md` as Agent B and do not start Agent A yourself.
   Example:
   ```bash
   SECONDS=0; while [ ! -f DEBATE.md ]; do sleep 5; if [ "$SECONDS" -ge 600 ]; then echo "TIMEOUT waiting for DEBATE.md"; exit 0; fi; done
   ```
2. If `TURN_STATE` returns `CONSENSUS`, stop; otherwise analyze the debate subject: **$1** (the arguments after the role).
   Run `git show`, `git diff`, `gh pr view`, read files, etc. as appropriate.
3. Run the same readiness loop as Agent A, checking the expected heading and signature immediately and on every poll.
   This step is blocking and mandatory; do not exit the workflow before it finishes.
4. If `CONSENSUS` → stop; if timed out → run `APPEND_TIMEOUT` and stop only after it succeeds.
5. Read and process Agent A's expected signed turn, then set `LAST_CONSUMED=$EXPECTED`.
6. Using the consumed peer heading, set shell variable `N` to 1 after `## Opening` or one greater than the consumed follow-up number, and append `## Response N` with a point-by-point reply.
7. Set `EXPECTED="## Follow-up $N"`, then return immediately to step 3 without resetting `LAST_CONSUMED`.
8. Do not ask the user whether to continue; continue automatically per turn order within Agent B only.

## File format

Reserve unindented top-level `##` headings for protocol sections; use `###` or deeper for response subsections.
Each nonterminal section must contain response prose and end with its author's signature line: `*— Agent A|Agent B (optional tool name), <timestamp>*`
Put examples in fenced blocks or prefix every quoted line with `>`.
Fences use at least three backticks or tildes, indented by at most three spaces.
Close each fence with the same character, at least the opening length, and no suffix except whitespace.

```markdown
# Code Debate: <subject>

## Opening
<Agent A's initial analysis>

*— Agent A, 2025-06-15T14:30:00Z*

---

## Response 1
<Agent B's point-by-point reply>

*— Agent B, 2025-06-15T14:32:00Z*

---

## Follow-up 1
<Agent A's follow-up on unresolved points>

*— Agent A, 2025-06-15T14:35:00Z*

---

## Response 2
<Agent B's reply>

*— Agent B, 2025-06-15T14:38:00Z*

---

## CONSENSUS
<final agreed outcomes and action items>
```

## Response format

Every point in a response must be explicitly categorized:

- **Agreed** — no further discussion needed.
- **Partially agreed** — state what you agree with and what you don't, with reasoning.
- **Disagreed** — state your reasoning.

If the agent has already completed its own review of the subject, it must also include an **Independent findings** section in its next debate turn:

- List its own review findings (with file paths/line references) even if they differ from the other agent.
- Clearly mark each finding as one of:
  - `same as other agent`
  - `new finding`
  - `not reproduced / disagreed`
- Do not limit the response to rebuttals only; independent findings are required when available.

## Convergence rules

- Maximum 5 rounds (a round = one follow-up + one response, so 10 sections max after the opening).
- If the maximum is reached without convergence, the last writer appends `## CONSENSUS` summarizing what was agreed and listing remaining disagreements.
- The unquoted, unfenced `## CONSENSUS` heading is the stop signal; when `TURN_STATE` returns `CONSENSUS`, stop polling and end.

## Guidelines

- Be specific. Reference file paths, line numbers, and code snippets.
- Be concise. Don't repeat points that are already agreed upon.
- Focus on substance — correctness, design, simplicity, edge cases — not style preferences.
- Don't re-analyze the entire subject each round. Only address unresolved points.
- The goal is convergence, not winning. Update your position when the other side makes a good argument.
- `DEBATE.md` is a scratch file; delete it after the debate unless you want to keep it as a record.
