# Code Debate Protocol

A protocol for two coding agents (Claude Code, Gemini CLI, Codex, or any other) to debate code changes through a shared file (`DEBATE.md`). Both agents receive this same prompt. Role is determined automatically by file existence.

## How to use

Open two terminal tabs with coding agents (same or different). Give both this prompt along with a subject to discuss — e.g., a commit, a diff, a PR, or one agent's review of another agent's work.

The first agent to run will create `DEBATE.md` and become the opener. The second agent will find the file already exists and become the responder.

## Role detection

- If `DEBATE.md` does **not** exist in the repo root → you are **Agent A** (opener). Write the opening position.
- If `DEBATE.md` **already exists** → you are **Agent B** (responder). Read what's there and respond.

Do not ask the user which role to play. Detect it from file existence.

## Agent A (opener) flow

1. Analyze the subject the user specified (run `git show`, `git diff`, `gh pr view`, read files, etc. as appropriate).
2. Write your analysis to `DEBATE.md` using the file format below.
3. Compute the file's checksum: `md5sum DEBATE.md | cut -d' ' -f1`
4. Poll for changes: run a bash loop that checks md5sum every 5 seconds until the checksum changes.
   ```bash
   PREV=$(md5sum DEBATE.md | cut -d' ' -f1); while true; do sleep 5; NOW=$(md5sum DEBATE.md | cut -d' ' -f1); if [ "$NOW" != "$PREV" ]; then break; fi; done
   ```
5. Read Agent B's reply.
6. If all points are resolved → append a `## CONSENSUS` section summarizing agreed outcomes and stop.
7. Otherwise append a `## Follow-up N` section addressing unresolved points, then go to step 3.

## Agent B (responder) flow

1. Read `DEBATE.md` (it exists — that's how you know you're the responder).
2. Analyze the same subject the user specified.
3. Append a `## Response N` section with a point-by-point reply.
4. Compute the file's checksum.
5. Poll for changes (same bash loop as above).
6. Read Agent A's follow-up.
7. If the file contains `## CONSENSUS` → stop, debate is over.
8. Otherwise append another `## Response N` section, then go to step 4.

## File format

```markdown
# Code Debate: <subject>

## Opening
<Agent A's initial analysis>

---

## Response 1
<Agent B's point-by-point reply>

---

## Follow-up 1
<Agent A's follow-up on unresolved points>

---

## Response 2
<Agent B's reply>

---

## CONSENSUS
<final agreed outcomes and action items>
```

## Response format

Every point in a response must be explicitly categorized:

- **Agreed** — no further discussion needed.
- **Partially agreed** — state what you agree with and what you don't, with reasoning.
- **Disagreed** — state your reasoning.

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
