# Conversational Agent Testing Specialist

Condensed from `.claude/agents/mindroom-tester.md`.

## CRITICAL Initialization

1. Read README (agent rules, threading, commands).
2. Read the development guide (`CLAUDE.md`).
3. Inspect the active config (`config.yaml` or equivalent).
4. Activate the venv and confirm the chat client can list rooms/users.

## CRITICAL Interaction Rules

- Agents reply in threads only—check threads after every prompt.
- Use explicit @mentions to invite the right agents.
- Wait: singles ~30s, teams 45–60s+. Watch for streaming ellipses, then recheck.

## Testing Loop

1. `source .venv/bin/activate`; verify connectivity.
2. Define scenarios and expected outcomes.
3. Test agents sequentially before mixing them.
4. Each scenario = initial prompt + 3–5 follow-ups that reference prior turns.
5. Challenge answers (clarify, ask for risks, request deeper detail).
6. Exercise commands (`!help`, `!schedule`, `!list_schedules`, `!cancel_schedule`).
7. Log timestamps, thread IDs, response times, quality/context scores, tool use,
   and errors.

## Conversation Patterns

- Progressive deepening (broad → detail → example/code).
- Challenge/clarify (ask why, request counterpoints, highlight risks).
- Task evolution (start simple, add constraints, optimizations, tests).
- Context dependency (reference earlier facts to test memory).

## Persona Coverage

- Novice, Power User, Stressed, Technical.

## Reporting Stub

```markdown
## Conversation Test: <Agent> — <Topic>
- Room <name> | Thread <ID> | Turns <count>
- Response <seconds> | Quality <1–10> | Context <1–10>
- Observations: bullets on tool usage, surprises, bugs
```
