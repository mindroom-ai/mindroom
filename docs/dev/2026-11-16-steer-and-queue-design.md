# Steer and Queue

This record covers how a person chooses what a message does when it arrives while an agent is replying: steer the running reply, queue for after it, or let the mid-turn judge choose.
It spans the backend (`mindroom`) and MindRoom Chat (`mindroom-chat`).

## Goal

Bas wants the control Codex and Claude Code give: while the agent works, a message either steers the running reply, which reads it and carries on, or waits for the reply to finish.
Today neither happens on request.
A message that arrives during a reply is always queued; after each completed tool batch the reply either continues (the judge let the message wait) or gets `QUEUED_MESSAGE_NOTICE_TEXT`, which tells the model to summarize and stop without showing it the message, after which the message runs as its own turn.
That pause is not steering: the running reply never reads the new text.

Success: from the chat, a person picks Steer, Queue or Auto per message (holding Send) or by default (a chat setting), and Steer behaves like Codex: the text reaches the model before its next model call in the same reply, the reply carries on, and the continuation appears below the person's message.

## Prior art

Read from the source on 2026-11-16 (Codex `~/repos/external/codex`, Claude Code 2.1.325 binary strings); the full table is in the `developing-mindroom-backend` skill's `references/mid-turn-messages.md`.

- Codex: Enter steers and Tab queues (`tui/src/keymap.rs`). A steer is drained at the top of every sampling-loop pass and recorded as a user message before the next model call (`core/src/session/turn.rs`). If the model finished as the steer arrived, the task loops again (`core/src/tasks/regular.rs`). With no active turn the steer becomes a new turn; a turn that cannot take steers (review, compaction) gets it at the end.
- Claude Code: Enter while working queues the message for "its next tool round", which is steering; there is no separate queue mode.
- Neither has a summarize-and-stop step.

## Decisions

1. **The mode travels on the message.**
   MindRoom Chat adds `io.mindroom.mid_turn` to the content of an outgoing text message: `"steer"`, `"queue"` or `"auto"`.
   A message without the key, such as one from Element, keeps today's behavior, so no other client changes.
   The backend reads the key at ingress from the event content and carries it on the queued entry (`QueuedMessage`) as `mode`.
   An unknown value counts as absent.
2. **Queue** lets the message wait without asking the judge, adds the agent's `defer_reaction` if it has one, and runs it as its own turn once the reply ends, as a message the judge let wait does today.
3. **Steer** puts the message into the running reply.
   After the current tool batch completes, at the point where the pause notice is appended today (`ai_runtime.py` `queued_notice_after_tool_batch` and `queued_notice_on_resume_entry`), the loop appends one user message:
   `The user sent a new message while you were working:` followed by the message text, attributed to its sender as other thread messages are.
   The reply then continues with its next model call. No pause notice is added for a steered message.
   The appended message carries a `provider_data` marker (`mindroom_steered_message`, holding the event ID) so history readers, compaction, and request identity treat it as the person's message and do not drop it as a notice.
4. **A steered message is consumed.**
   Once appended, the event becomes a source of the running reply: it is removed from the per-reply queue (`_pending_messages`) and from the coalescing gate's follow-up backlog, so it never runs as its own turn, and the turn store records it as answered by this reply, so a restart does not answer it again.
   An edit of a message already steered is not applied to the running reply; it is handled like an edit of an answered message.
5. **Who can steer.**
   Only the person the running reply answers (its requester) can steer it, because a steer changes a task someone else started.
   A Steer message from anyone else is handled as Auto.
6. **What cannot be steered falls back.**
   A message with an attachment, a voice message whose transcript is not ready, a reply in `thread_mode: room`, and a reply by a team are not steered in this version; a Steer message there is handled as Auto.
   If the reply has already finished, a Steer message runs as its own turn, answered without the participation check, as a message the judge stopped a reply for is today (#4046).
   If the model ends the reply in the same model call in which a steer arrived (no further tool batch), the message likewise runs as its own turn; looping the same reply, as Codex does, is left for later.
7. **Auto** is today's behavior with one change: with a judge configured, "cannot wait" steers the message (when it can be steered under 5 and 6) instead of sending the pause notice.
   Without a judge, Auto keeps today's pause notice.
   A message without the key is Auto.
   The pause notice stays only as Auto's fallback; whether to retire it is decided after Steer has been used for a while.
8. **A steer ends the current reply message and starts a new one below the person's message.**
   This is part of Steer itself, not a later polish: the thread must read in order, so the agent's continuation is never streamed into a message above the steering text.
   When a steer is appended, the reply finishes its current Matrix event as it stands (its text so far, its tool trace, `stream_status: completed`) and continues the same run in a new event, sent after the person's message, as a normal reply in the thread.
   The reply's current event ID moves to the new event, so the 🛑 Stop reaction, approval cards, progress edits and visible-response reconciliation all follow the newest event; earlier events of the run are kept on the reply record for recovery.
   This is the first time one run spans several events for a reason other than segmenting long content (`matrix/segmented_messages.py`).
9. **MindRoom Chat: hold Send for a menu, set the default in settings.**
   - Holding the Send button (500 ms; touch and mouse, through a pointer-based variant of `lib/hooks/useLongPress.ts`) while an agent is replying in the thread opens a small menu above it: Steer, Queue, Auto, each with a one-line hint. Picking one sends the message with that mode.
   - A tap or Enter sends with the default from a new setting on the Chats page, "When an agent is replying, Send" (Steer, Queue or Auto; default Steer, as in Codex and Claude Code). The setting roams with the account, which needs `roamingSettings.ts` to accept this enum.
   - When no agent is replying in the thread, holding Send does nothing special and the message carries the default mode, which only matters if a reply starts before it is read.
   - Keyboard shortcuts for the other modes and a "Steer now" action on a queued message are left for later.
10. **Docs.** `docs/configuration/threads.md` "Mid-Turn Coalescing" gains a "Steer, Queue and Auto" section describing the key, the three modes, who can steer, and the fallbacks; `docs/configuration/agents.md` is unchanged because no config key is added.

## Rejected

- **Steer as a forced pause.** Cheaper (the `judge=False` path exists), but the reply never reads the message, which is the problem Bas wants solved.
- **A per-agent default mode in `config.yaml`.** Not needed while the client always sends the key; Element keeps today's behavior.
- **Letting anyone steer.** In a shared thread one person could redirect another's task; Auto already lets the judge decide for them.
- **Keeping the continuation in the original event, even as a first step.** It puts the reply's continuation above the person's message, so the thread reads out of order (Bas, 2026-11-16).

## Delivery

Two pull requests:

1. Backend: the `io.mindroom.mid_turn` key, Queue, Steer (injection, consumption, and the new reply message below the steering text), Auto's judge steering, docs (decisions 1-8, 10). Steer never ships without the message split.
2. MindRoom Chat: the hold menu, the setting, and the key on outgoing messages (decision 9).

## Testing

- Backend unit tests next to `tests/test_mid_turn*.py` and the queued-notice tests: each mode with and without a judge; a steer appended after a batch and consumed (no follow-up turn); steer by a non-requester, with an attachment, from a team, in room mode, and after the reply ended; edit of a steered message; history replay and compaction keep the steered message once; restart does not answer a steered message again.
- Event split: the Stop reaction, an approval and recovery address the new event; the old event ends `completed` with its tool trace.
- Live test on `mindroom-lab.service` from the worktree: a long shell task steered with "also print the date", queued with "thanks", and Auto with a judge.
- Chat: unit tests for the long-press menu, the content key, and the setting; a Playwright spec that holds Send while a stubbed reply streams; screenshots in the pull request.
