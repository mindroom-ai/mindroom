# Code Debate Protocol

A protocol for two coding agents (Claude Code, Gemini CLI, Codex, or any other) to debate code changes through a shared file (`DEBATE.md`). Both agents receive this same prompt. Role is determined automatically by file existence.

## How to use

Open two terminal tabs with coding agents (same or different). Give both this prompt along with a subject to discuss — e.g., a commit, a diff, a PR, or one agent's review of another agent's work.

The first agent to run will create `DEBATE.md` and become the opener. The second agent will find the file already exists and become the responder.

## Role detection

- If `DEBATE.md` does **not** exist → you are **Agent A** (opener). Write the opening position.
- If `DEBATE.md` **already exists** → you are **Agent B** (responder). Read what's there and respond.

Do not ask the user which role to play. Detect it from file existence.

## Non-simulation guardrails (MUST)

- Never write content for the opposite role.
- Never simulate, fabricate, or placeholder the other agent's response.
- Never append both sides of a debate from one process.
- Role is immutable for the run: once detected as Agent A or Agent B, keep that role until `## CONSENSUS`.
- If you are about to write the opposite role, stop and report protocol violation instead of writing.
- After appending your section, only poll. Do not append another turn unless checksum changed and turn order confirms it is now your turn.
- If checksum does not change, keep polling until timeout or `## CONSENSUS`.
- On timeout, append only a timeout `## CONSENSUS` from your own role.

## Checksum command

Use a portable checksum. Pick the first available:

```bash
CKSUM() { { md5sum "$1" 2>/dev/null || md5 -q "$1" 2>/dev/null || shasum "$1"; } | awk '{print $1}'; }
```

Define this function before running the poll loop.

## Shell requirement

Run polling commands in `bash`. The timeout example uses Bash's `SECONDS`.

## Turn order enforcement

Before appending a section, check the last `## ` heading in `DEBATE.md`:

- After `## Opening` → only Agent B may append `## Response 1`.
- After `## Response N` → only Agent A may append `## Follow-up N` (or `## CONSENSUS`).
- After `## Follow-up N` → only Agent B may append `## Response N+1`.

If it is not your turn, re-read the file and go back to polling.

Before appending, also verify the last signature line role:

- If the last signature is your role, it is not your turn.
- Append only when heading order and signature role both allow your turn.

## Polling discipline (critical)

- After starting a poll loop, keep polling until one of these happens:
  - checksum changed
  - timeout reached
  - `## CONSENSUS` exists
- Do not stop early just to report that polling started.
- When checksum changes, immediately read `DEBATE.md` and continue the protocol.
- Stop only when `## CONSENSUS` exists or timeout handling completes.
- Polling is mandatory after every append. Do not return control to the user between append and poll completion.
- After checksum change, do not ask the user what to do next. Immediately take the next protocol step for your role.

## Completion criteria (MUST)

- A debate run is only complete when one of these is true:
  - `## CONSENSUS` exists, or
  - timeout handling completed and timeout `## CONSENSUS` was appended.
- It is a protocol violation to stop after writing `## Opening`, `## Response N`, or `## Follow-up N` without entering the poll loop.
- If `DEBATE.md` does not yet contain `## CONSENSUS`, you must still be in the protocol loop (append or poll).
- Never send a “done”/“completed” status while waiting for the other agent; continue polling instead.
- Never pause to request user confirmation between turns. Continue autonomously until consensus/timeout.

## Agent A (opener) flow

1. Analyze the subject the user specified (run `git show`, `git diff`, `gh pr view`, read files, etc. as appropriate).
2. Write your analysis to `DEBATE.md` using the file format below.
3. Compute the file's checksum using `CKSUM DEBATE.md`.
4. Poll for changes (5-second interval, 10-minute timeout):
   ```bash
   PREV=$(CKSUM DEBATE.md); SECONDS=0; while true; do sleep 5; if grep -q '^## CONSENSUS' DEBATE.md; then break; fi; NOW=$(CKSUM DEBATE.md); if [ "$NOW" != "$PREV" ]; then break; fi; if [ "$SECONDS" -ge 600 ]; then echo "TIMEOUT"; break; fi; done
   ```
   This step is blocking and mandatory; do not exit the workflow before it finishes.
5. If timed out → append `## CONSENSUS` noting the timeout and stop.
6. Read Agent B's reply.
7. If all points are resolved → append a `## CONSENSUS` section summarizing agreed outcomes and stop.
8. Otherwise append a `## Follow-up N` section addressing unresolved points, then go to step 3.
9. Do not ask the user to choose between follow-up or consensus; Agent A must decide and append immediately.

## Agent B (responder) flow

1. Read `DEBATE.md` (it exists — that's how you know you're the responder).
2. Analyze the same subject the user specified.
3. Verify it is your turn (check the last `## ` heading). Example:
   ```bash
   LAST=$(grep -E '^## ' DEBATE.md | tail -n1)
   ```
   If not your turn, poll until it is.
4. Append a `## Response N` section with a point-by-point reply.
5. Compute the file's checksum.
6. Poll for changes (same loop with 10-minute timeout).
   This step is blocking and mandatory; do not exit the workflow before it finishes.
7. If timed out → append `## CONSENSUS` noting the timeout and stop.
8. Read Agent A's follow-up.
9. If the file contains `## CONSENSUS` → stop, debate is over.
10. Otherwise go to step 3.
11. Do not ask the user whether to continue; continue automatically per turn order.

## File format

Each section should end with a signature line: `*— Agent A|Agent B (optional tool name), <timestamp>*`

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
- The `## CONSENSUS` heading is the stop signal. When you see it, stop polling and end.

## Guidelines

- Be specific. Reference file paths, line numbers, and code snippets.
- Be concise. Don't repeat points that are already agreed upon.
- Focus on substance — correctness, design, simplicity, edge cases — not style preferences.
- Don't re-analyze the entire subject each round. Only address unresolved points.
- The goal is convergence, not winning. Update your position when the other side makes a good argument.
- `DEBATE.md` is a scratch file; delete it after the debate unless you want to keep it as a record.
