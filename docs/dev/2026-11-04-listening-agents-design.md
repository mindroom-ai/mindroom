# Listening Agents

This record covers why listening agents work as they do and what was rejected.
The behavior itself is in [Agent Configuration](../configuration/agents.md#listening).

## Goal

Let an agent learn from the messages in chosen rooms that are not addressed to it, without its silent turns writing there.
After a room goes quiet, the agent gets one silent turn over what it heard and acts only through a short list of tools.
The agent stays a normal agent in those rooms: it still answers when addressed, under the room's usual rules, because listening only adds silent turns and silencing the agent is a separate concern.

## Decisions

1. A polling service reads each listening agent's own event journal after a durable per-room marker, so one mechanism covers every room and a restart resumes after the last turn that finished.
   The markers live in `tracking/listening_markers.json`, and the service polls every 10 seconds.
   Listening does not join rooms: a listened room the agent has not joined waits, logged once, because joining is the job of the agent's `rooms` and its invitations.
   A restart in the middle of a turn runs that turn again, so its tool effects can repeat (see Risks).
   A room owing a sync-gap repair is repaired before it is read, because the messages a gap skipped are older than those after it, and a marker moved past those would never hear them.
2. The first run for a room starts at "now", so turning listening on never reads history from before it.
3. A silent turn runs as the agent, with its identity, instructions, and memory, but offers only `listen.tools` and drops its text, so no tool outside the list can act and the turn's own text is never posted.
   A listed tool keeps its effects, so a tool that sends can still post; config refuses `matrix_message`, whose purpose is posting, and `react` covers acknowledging.
   Tools that can still post when listed include `matrix_api`, `matrix_voice_message`, `thread_move`, `scheduler`, and the external messengers.
4. Every `listen.tools` entry must be among the agent's own `tools`, and config load fails otherwise.
   The cost is that an agent with `listen:` and the default tools needs `memory` and `react` in `tools:`.
   The list needs at least one tool, since a turn without one only spends tokens.
5. A silent turn hears only messages the agent's replies would answer, judged on the person a relay speaks for, so listening opens no new door into the agent.
6. Config load rejects `listen` on a private agent, and contact rooms are skipped.
   A per-requester instance has no single listener identity, and only a reply passes a contact's message policy.
7. A silent turn stores no run in session history, only its usage under the kind `listening`.
   Each turn is self-contained, with the heard messages in its prompt and the memory tool for continuity.
8. Budgets do not apply, because no requester asked for the turn.
   The caps and an optional cheaper `model` bound the cost.
9. No room stalls for good, and a skipped message is logged rather than silently lost.
   A page stops at the first message awaiting a refetch, and the marker waits before it; what came before it is heard, and after 10 minutes without the refetch that one message is skipped.
   A turn hears only the oldest messages that fit half its model's context window, leaving the rest for the agent's instructions, memory, tools, and requests, and the marker moves past only what it heard.
   Three failed turns in a row from one marker halve the next turn's messages, and a single message that still fails three times is skipped, so a poisoned or oversized message cannot hold a room.
   A turn that runs longer than 5 minutes is stopped and counts as failed, because the poll waits on each turn, so one hung turn would stop listening in every room.
10. A turn waits until nothing the agent hears has been said for `quiet_seconds`, so it hears a conversation once it pauses rather than mid-exchange.
    Only heard messages count, because counting every message let a chatty bot the agent may not hear, or the agent's own posts, hold the turn back forever.
    A turn runs anyway once the oldest heard message has waited ten quiet windows, so a room that never pauses is still heard; the cost is a turn in the middle of a very busy conversation.
11. The hourly cap is per agent, and the agent's rooms take turns at it, least recently run first, so a room that keeps failing cannot spend the whole cap.
12. A journal index on `(principal_id, room_id, created_ts, logical_event_id)` serves the room reads.
    Every poll repeats them, and the conversation index orders by thread first, so without it each poll sorted the room in a temporary B-tree, which the journal's hot-query rule forbids.
13. A separate `react` tool carries reactions, sharing `matrix_message`'s per-room rate limit.
    Its key is at most 8 characters with no whitespace, so a reaction cannot carry text to the room.
    Some emoji sequences, such as a couple with skin tones (10 code points), are longer and are refused.

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
- Holding a room until a refetch arrives, and retrying a failing turn forever.
  Either stalls the room for good when the refetch never resolves or the turn always fails, such as on a message too large for the model.

## Risks

- Prompt injection from heard text can drive the listed tools.
  The prompt tells the model that heard text is data, not instructions, but a model can still be persuaded, so the allowlist, the hidden approval-gated tools, the dropped text, and the sender authorization contain it, and operators must keep `listen.tools` narrow.
- Heard text crosses rooms and users through memory.
  A silent turn with `memory` can store what people who never addressed the agent said, and the shared memory can surface it in other rooms and to other users; the docs say so, and leaving `memory` out of `listen.tools` prevents it.
- Cost without budgets.
  `max_runs_per_hour`, `max_messages`, and a cheaper `model` bound it, and usage under `listening` makes it visible.
- A restart in the middle of a silent turn, after its tools acted, repeats that turn, so a memory write or reaction may happen twice.
- The hourly run count lives in memory and starts again after a restart.
