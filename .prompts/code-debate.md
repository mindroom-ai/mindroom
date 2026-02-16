# Code Debate Protocol

Two Claude Code sessions review code changes by debating through a shared markdown file.
Both sessions receive the same prompt. Role is determined automatically by file existence.

## How to use

Open two Claude Code sessions in the same repo. In both, run:

```
/code-debate <subject>
```

Where `<subject>` is what to review (e.g., `commit abc123`, `the diff against main`, `PR #42`).
Start the first session, wait a few seconds, then start the second.

## Role detection

- If `reply.md` does **not** exist in the repo root → you are the **Reviewer**.
- If `reply.md` **already exists** → you are the **Responder**.

Do not ask the user which role to play. Detect it from file existence.

## Reviewer flow

1. Analyze the subject the user specified (run `git show`, `git diff`, `gh pr view`, etc. as appropriate).
2. If `.prompts/pr-review.md` exists, read it and apply its standards.
3. Write a thorough review to `reply.md` using the file format below.
4. Compute the file's checksum: `md5sum reply.md | cut -d' ' -f1`
5. Poll for changes: run a bash loop that checks md5sum every 5 seconds until the checksum changes.
   ```bash
   PREV=$(md5sum reply.md | cut -d' ' -f1); while true; do sleep 5; NOW=$(md5sum reply.md | cut -d' ' -f1); if [ "$NOW" != "$PREV" ]; then break; fi; done
   ```
6. Read the responder's reply.
7. If all points are resolved → append a `## CONSENSUS` section summarizing agreed outcomes and stop.
8. Otherwise append a `## Reviewer Follow-up N` section addressing unresolved points, then go to step 4.

## Responder flow

1. Read `reply.md` (it exists — that's how you know you're the responder).
2. Analyze the same subject the user specified.
3. If `.prompts/pr-review.md` exists, read it and apply its standards.
4. Append a `## Response N` section with a point-by-point reply.
5. Compute the file's checksum.
6. Poll for changes (same bash loop as above).
7. Read the reviewer's follow-up.
8. If the file contains `## CONSENSUS` → stop, debate is over.
9. Otherwise append another `## Response N` section, then go to step 5.

## Shared file format

```markdown
# Code Debate: <subject>

## Review
<reviewer's initial analysis>

---

## Response 1
<responder's point-by-point reply>

---

## Reviewer Follow-up 1
<reviewer's follow-up on unresolved points>

---

## Response 2
<responder's reply>

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

- Maximum 5 rounds (a round = one reviewer section + one responder section, so 10 total sections max excluding the initial review).
- If the maximum is reached without convergence, the last writer appends `## CONSENSUS` summarizing what was agreed and listing remaining disagreements.
- The `## CONSENSUS` heading is the stop signal. When you see it, stop polling and end.

## Guidelines

- Be specific. Reference file paths, line numbers, and code snippets.
- Be concise. Don't repeat points that are already agreed upon.
- Focus on substance — correctness, design, simplicity, edge cases — not style preferences.
- Don't re-review the entire subject each round. Only address unresolved points.
- The goal is convergence, not winning. Update your position when the other side makes a good argument.
