# Steer and Queue

This record covers how a person chooses what a message does when it arrives while an agent is replying: steer the running reply, queue for after it, or let the mid-turn judge choose.
It spans the backend (`mindroom`) and MindRoom Chat (`mindroom-chat`).

## Goal

Bas wants the control Codex and Claude Code give: while the agent works, a message either steers the work, which reads it and carries on, or waits for the reply to finish.
Today neither happens on request.
A message that arrives during a reply is always queued; after each completed tool batch the reply either continues (the judge let the message wait) or gets `QUEUED_MESSAGE_NOTICE_TEXT`, which tells the model to summarize and stop without showing it the message, after which the message runs as its own turn.

Success: from the chat, a person picks Steer, Queue or Auto per message (holding Send) or by default (a chat setting).
Steer reads like Bas described it: the agent stops its current message, and a new reply appears below the person's message, in which the agent carries on with its task, taking the message into account.

## Prior art

Read from the source on 2026-11-16 (Codex `~/repos/external/codex`, Claude Code 2.1.325 binary strings); the full table is in the `developing-mindroom-backend` skill's `references/mid-turn-messages.md`.

- Codex: Enter steers and Tab queues (`tui/src/keymap.rs`). A steer is drained at the top of every sampling-loop pass and recorded as a user message before the next model call (`core/src/session/turn.rs`). If the model finished as the steer arrived, the task loops again. With no active turn the steer becomes a new turn.
- Claude Code: Enter while working queues the message for "its next tool round", which is steering; there is no separate queue mode.
- Neither has a summarize-and-stop step.

What the model reads is the same in all three: its own tool calls and results, then the person's new message, then it carries on.
Codex keeps one turn; MindRoom starts a new one at that point (decision 3), because in a chat the continuation must be a new message below the person's, and a MindRoom turn is exactly one reply message with its own Stop, approvals and recovery.

## Decisions

1. **The mode travels on the message.**
   MindRoom Chat adds `io.mindroom.mid_turn` to the content of an outgoing text message: `"steer"`, `"queue"` or `"auto"`.
   A message without the key, such as one from Element, is Auto, so no other client changes.
   The backend reads the key at ingress from the event content and carries it on the queued entry (`QueuedMessage`) as `mode`; an unknown value counts as Auto.
2. **Queue** lets the message wait without asking the judge, adds the agent's `defer_reaction` if it has one, and runs it as its own turn once the reply ends, as a message the judge let wait does today.
   It does not release a tool call waiting to move to the background.
3. **Steer ends the running reply after its current tool batch, and the steered message continues the work as a new reply.**
   - The running reply's after-batch hook (`ai_runtime.py` `queued_notice_after_tool_batch` and `queued_notice_on_resume_entry`), finding a Steer message queued, ends the run once that batch's results are recorded, through the existing stop-after-tool-call path (`agent_loop/model_loop.py` `_batch_stops_run`). No pause notice and no further model call: the model writes no summary.
   - The reply's Matrix message finishes as it stands (its text so far and its tool trace, `stream_status: completed`), and the run is stored with its tool calls and results like any finished run.
   - The queued message then runs as its own turn, as queued messages already do once the reply ends, answered without the participation check, as a message the judge stopped a reply for is today (#4046). Its reply is a normal reply below the person's message, with its own Stop, approvals and recovery.
   - That turn's model prompt is the person's message followed by one line saying the agent's previous reply was cut short by this message after its last tool calls, and to carry on with the task, taking the message into account. The line follows the person's text, as every model-only addition does, so their own words still open the prompt. The previous run's tool calls and results reach it through ordinary history replay.
   - A reply the Steer ends with nothing visible (it wrote no text and its tool calls are hidden) has its placeholder removed, with no failure note.
     Accepted exception: when that reply is an approval resume, it ends with the existing text "Tool approval continuation completed" instead, because a reply held by an approval ends only through an acknowledged final message (see `docs/architecture/reply-messages.md`).
   - A Steer message releases a tool call waiting to move to the background, as a "cannot wait" verdict does today, so a long wait does not hold the steer back.
4. **Who can steer.**
   Only the person the running reply answers (its requester) can end it, because a steer changes a task someone else started.
   A Steer message from anyone else is handled as Auto.
5. **Fallbacks.**
   If the reply ends before another tool batch completes (the model was writing its final text), the Steer message simply runs next as its own turn, like a queued one, still without the participation check and still with the carry-on line.
   A Steer message that arrives when no reply is running is an ordinary message.
6. **Auto** is today's behavior with one change: with a judge configured, an explicit "cannot wait" verdict steers (decision 3) instead of sending the pause notice.
   A skipped, failed or timed-out judgment still sends the pause notice, and without a judge Auto keeps the pause notice.
   Whether to retire the pause notice entirely is decided after Steer has been used for a while.
7. **MindRoom Chat: hold Send for a menu, set the default in settings.**
   - Holding the Send button (500 ms; touch and mouse, through a pointer-based variant of `lib/hooks/useLongPress.ts`) while an agent is replying in the thread opens a small menu above it: Steer, Queue, Auto, each with a one-line hint. Picking one sends the message with that mode.
   - A tap or Enter sends with the default from a new setting on the Chats page, "When an agent is replying, Send" (Steer, Queue or Auto; default Steer, as in Codex and Claude Code). The setting roams with the account, which needs `roamingSettings.ts` to accept this enum.
   - When no agent is replying in the thread, holding Send does nothing special and the message carries the default mode.
   - Keyboard shortcuts for the other modes and a "Steer now" action on a queued message are left for later.
8. **Docs.** `docs/configuration/threads.md` "Mid-Turn Coalescing" gains a "Steer, Queue and Auto" section describing the key, the three modes, who can steer, and the fallbacks; no config key is added.

## Rejected

- **Steer inside one run, as Codex does (the first version of this design).** Injecting the text into the running loop and moving the run to a new Matrix event made one run span several replies. A pre-flight review (Sol, 2026-11-16) found two critical and eight important problems in that plan: approvals resumed against the old reply's sources, a steered message's turn claim could be stranded, several steers broke journal settlement, history indexes missed the steered text, and Stop at the hand-off had no defined outcome. Ending the run at the batch and answering the message as its own turn gives the person the same thread and the model the same context, with none of that machinery.
- **Steer as a forced pause.** The reply would spend a model call on a summary, and the person's message would arrive as an unexplained new task.
- **A per-agent default mode in `config.yaml`.** Not needed while the client always sends the key; other clients get Auto.
- **Letting anyone steer.** In a shared thread one person could cut short another's task; Auto already lets the judge decide for them.

## Delivery

Two pull requests:

1. Backend: the key, Queue, Steer, Auto's judge steering, docs (decisions 1-6, 8).
2. MindRoom Chat: the hold menu, the setting, and the key on outgoing messages (decision 7).

## Testing

- Backend unit tests next to `tests/test_mid_turn*.py` and the queued-notice tests: each mode with and without a judge; a Steer message ends the run after the batch with no further model request and no notice; the follow-up turn skips the participation check, carries the carry-on line, and its request contains the previous run's tool calls; Steer by a non-requester; Steer while the model writes its final text; Queue never asks the judge and does not release a waiting call; Auto's explicit "cannot wait" steers while a failed judgment pauses.
- Live test on `mindroom-lab.service` from the worktree: a long shell task steered with "also print the date", queued with "thanks", and Auto with a judge.
- Chat: unit tests for the long-press menu, the content key, and the setting; a Playwright spec that holds Send while a stubbed reply streams; screenshots in the pull request.
