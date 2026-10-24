# MindRoom Memory Consolidation and Auto-Flush Plan

This record explains why file memory is the primary backend and why auto-flush extracts memories in the background from stored conversations, and it lists the parts of the plan that are not built.

## Goals

- Keep memory simple and portable by making files the source of truth.
- Preserve `mem0` as an alternative backend.
- Avoid user-managed schedules for memory maintenance.
- Keep cross-thread memory fresh for the same agent.
- Read sessions through Agno session and storage APIs instead of raw SQL, to minimize low-level coupling to Agno (now the conversation store, since MindRoom [replaced Agno's runtime](2026-10-20-replace-agno-design.md)).

## Decisions

- Files are canonical because they keep memory portable and auditable; any semantic index over them is a secondary cache that can be discarded and rebuilt.
- Auto-flush reads stored conversations, the same source and reader knowledge-graph ingestion uses, and keeps a per-conversation marker of the last turn it flushed, so stored history decides what is pending and no reply-path state can drift from it.
- It takes what was said in each turn from the module skill reviews and the graph use, so the prompts MindRoom writes for job follow-ups and automations are not extracted as someone's words, and each line names its sender so a shared agent's notes say who said what.
- Each daily note names the last turn it covers, so a retry after the note was written but its progress was not saved finds the note and skips those turns instead of writing it again.
- A new conversation starts at the time auto-flush first ran, so an upgrade or first enablement never re-extracts history; an agent first seen later starts one pass interval before it was seen, so switching an agent to file memory does not extract its history either.
- A private instance shares its agent's saved start instead of getting one when it is first listed, so a long pass or a listing that missed it never costs a new instance its first turns.
- Freshness across threads comes from a short pass interval (five minutes by default) rather than from replies waking the worker.

## Rejected alternatives

- A pre-reply waiting window for pending flushes, and synchronous extraction while handling a request: both would put latency and timeout handling into the response path, so all flush work runs in the background and the reply never waits for it.
- A dirty-session queue written by every reply, with revisions, priority boosts for an agent's other sessions, in-flight marks, idle and age timers, and a stale-entry TTL: it duplicated what stored sessions already record and produced repeated bugs, including a storage error ending the worker, a stop lost to a wake-up, double flushes after a failed reread, and cooldowns counted from the wrong time.
- Converting the old queue's entries into markers on upgrade: it needs configuration-time scope resolution and a timestamp-to-turn mapping for a one-time window of minutes, so the turns still queued at the upgrade are dropped instead.

## Accepted limitations

- Cross-thread memory is eventually consistent: a fact reaches other threads once its conversation has been quiet for `idle_seconds` and the next pass runs.
- Each extraction sees only new turns plus a few existing memories, not the already-flushed turns before them, so a reply that only makes sense with earlier context can be extracted with less of it.
- Turns are ordered by creation time and run ID, so two turns of one conversation created in the same second can be ordered wrongly, and a turn regenerated in place under the same ID is not flushed again.
- Flushing waits while the agent's bot is not running, because only its event journal can say which messages were redacted.
- A turn already waiting for approval when its agent's start is set counts as earlier history once it finishes, so it is not flushed, like the turns the old queue held at the upgrade.

## Not implemented

- The optional curation pass that would have promoted durable facts from daily notes into `MEMORY.md` was superseded: the `dreaming` automation promotes them into `memory/` topic files and never edits `MEMORY.md`.
