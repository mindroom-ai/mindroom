# Moving a Thread to Another Room

This record explains why the `thread_move` tool copies a thread into another room the way it does, and which limits were accepted.

## Why

A user who keeps related work in separate rooms asked to move threads between rooms after they started, because thread tags alone did not help them organize that work.
Success means the moved thread reads like the original in the target room, the agents in it continue the conversation there without being re-briefed, and the original thread points to the new location.

## Constraints

- Matrix cannot move events: an event's room is part of its signed hash, so a "move" is always a copy plus a pointer.
- Copies must never trigger agent replies while they are posted.
- Requester authorization follows the existing cross-room tool rule: a human requester joined to the target room and allowed to address the acting agent there.
- The feature must stay small: no new storage, no durable move journal, no changes to session storage.

## Decisions

### Faithful copy without session copy (chosen)

Each managed entity that is running and joined to the target room re-posts its own messages from its own Matrix account, and the router re-posts all other messages with relay attribution.
The design relies on existing replay behavior: a thread without saved session history replays its Matrix transcript as model history, with each agent's own copies as assistant turns and relayed copies labeled by `com.mindroom.original_sender`.
Agents therefore continue the conversation in the new thread with no session work.
They lose earlier tool-call results and any compaction summary, but keep the visible conversation.

### Rejected: copy plus Agno session copy

Copying sessions would preserve tool-call results, but every agent, team scope, and private instance has its own session database, and copied runs embed Matrix event IDs (seen-event sets, response IDs, source revisions, compaction archives) and globally unique run IDs that would all need remapping.
Without that remapping the copied history and the copied messages are replayed twice.
The added code and failure surface are large compared with the benefit.

### Rejected: single-account copy

Having the router re-post every message with a name prefix is the least code, but agents would see their own earlier replies as user messages and would no longer count as thread participants, so untagged follow-ups in the new thread would stop reaching them.

### Visible attribution

Matrix clients do not render `com.mindroom.original_sender`, so router-posted copies also name the author visibly in the text or media caption.
A relay names the Matrix poster, not the person a post speaks for, and copies the visible text as it appeared, so handoff boilerplate and earlier move copies never gain a second or false name.

## Limitations

- No durable move journal exists, so a crash mid-copy leaves a partial copy and an untouched source thread.
- Messages posted in the source thread after the move starts are not copied.
- Only messages and tags move; per-thread model choices, agent modes, todos, scheduled tasks, external triggers, pending approvals, and attachment records stay with the source thread.
- Images, files, and videos are registered again as attachments when agents read the new thread; voice recordings keep their transcript, but MindRoom registers audio as an attachment only when it arrives through voice handling, so agents cannot open a copied recording.
- Copies have new timestamps, edits are flattened to their latest revision, and reactions and in-thread reply targets are not copied.
- Router-posted copies do not count as people in a thread, so until two people have posted in the moved thread, agents treat it as a one-person thread and may answer untagged messages.
  Counting the person named in `com.mindroom.original_sender` would also count scheduled-task creators, trigger owners, and delegation requesters who never posted, which silences agents in ordinary threads, so the limit stays.
