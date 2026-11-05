# Listening Agents

This record covers why listening agents work as they do and what was rejected.
The behavior itself is in [Agent Configuration](../configuration/agents.md#listening).

## Goal

Let an agent learn from rooms it does not reply in, without ever posting there.
After a room goes quiet, the agent gets one silent turn over what it heard and acts only through a short list of tools.

## Decisions

1. A polling service reads each listening agent's own event journal after a durable per-room marker, so one mechanism covers every room and a restart resumes after the last turn that finished.
   A restart in the middle of a turn runs that turn again, so its tool effects can repeat (see Risks).
   A room owing a sync-gap repair is repaired before it is read, because the messages a gap skipped are older than those after it, and a marker moved past those would never hear them.
2. The first run for a room starts at "now", so turning listening on never reads history from before it.
3. A silent turn runs as the agent, with its identity, instructions, and memory, but offers only `listen.tools` and drops its text, so no tool outside the list can act and nothing can be posted.
4. Every `listen.tools` entry must be among the agent's own `tools`, and config load fails otherwise.
   The cost is that an agent with `listen:` and the default tools needs `memory` and `react` in `tools:`.
5. A silent turn hears only messages the agent's replies would answer, judged on the person a relay speaks for, so listening opens no new door into the agent.
6. Private agents and contact rooms are skipped.
   A per-requester instance has no single listener identity, and only a reply passes a contact's message policy.
7. A silent turn stores no run in session history, only its usage under the kind `listening`.
   Each turn is self-contained, with the heard messages in its prompt and the memory tool for continuity.
8. Budgets do not apply, because no requester asked for the turn.
   The caps and an optional cheaper `model` bound the cost.
9. A page of heard messages stops at the first message awaiting a refetch, and the marker stays before it.
   Never losing a heard message beats progress, so a stuck refetch stalls that room, with a warning after 10 polls.
10. A journal index on `(principal_id, room_id, created_ts, logical_event_id)` serves the room reads.
    Every poll repeats them, and the conversation index orders by thread first, so without it each poll sorted the room in a temporary B-tree, which the journal's hot-query rule forbids.
11. A separate `react` tool carries reactions, sharing `matrix_message`'s per-room rate limit.
    Its key is at most 8 characters with no whitespace, enough for any emoji sequence, so a reaction cannot carry text to the room.

## Rejected alternatives

- Room memory.
  MindRoom removed it before, and the `memory` tool already lets an agent keep what it chooses.
- A per-message "heard" log.
  The event journal already records every message, and the marker says where the agent has read to.
- A `message:received` hook.
  It also fires for messages an agent answers, and it fires N times in a room with N agents.
- `matrix_message` for reactions.
  It can also send, which a silent turn must never do.
- Granting listen tools beyond the agent's tools.
  It would bypass `worker_scope` checks, tool limits, and reload restarts, so config validation requires a subset instead.
- Storing silent turns in session history.
  Redacted heard messages would linger in the stored runs.

## Risks

- Prompt injection from heard text can drive the listed tools.
  The allowlist, the hidden approval-gated tools, the dropped text, and the sender authorization contain it, and operators must keep `listen.tools` narrow.
- Cost without budgets.
  `max_runs_per_hour`, `max_messages`, and a cheaper `model` bound it, and usage under `listening` makes it visible.
- A restart in the middle of a silent turn, after its tools acted, repeats that turn, so a memory write or reaction may happen twice.
- The hourly run count lives in memory and starts again after a restart.

## Open questions

- `listen.tools: []` validates, though a silent turn without tools only spends tokens.
- The quiet window counts messages the agent does not hear and its own, and has no maximum wait, so a busy room can postpone a turn.
- The prompt does not yet say that heard text is data rather than instructions.
