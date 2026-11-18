# Thread Workflow Groups in `matrix_room` Threads

This record covers how an agent learns which threads wait for the person it talks with.
It spans the backend (`mindroom`) and one test in MindRoom Chat (`mindroom-chat`).
Code references are to `mindroom` at `09807d9f1`, `mindroom-chat` at `2f968f654` and `mindroom-tuwunel` at `c35b300dc`.

## Goal

Bas wants to call an agent, ask "what needs me?", and walk through those threads by voice.
Today `matrix_room(action="threads")` lists thread roots with a preview, a reply count and the latest activity, so an agent cannot tell a thread that waits for an approval from one that is resolved.

Success: one `matrix_room(action="threads", room_id="all", workflow="needs_you")` call returns the threads that wait for the requester across the rooms they share with the agent, each with the reason, and a thread's group there is the group MindRoom Chat computes from the same facts.

## Prior art

MindRoom Chat groups threads in two places, and they do not agree on what "Needs you" holds.

- The room overview's tabs and the sidebar's sections use `classifyThreadWorkflow` alone (`src/app/event-store/threads/threadWorkflow.ts:42-52`): resolved is done, an active snooze is later, a background approval is needs you, a streaming reply or a background job is working, a pending scheduled task is later, and everything else is needs you.
  The sidebar shows that last bucket as "Idle" (`src/app/sidebar/threads/threadNavCategoryUtils.ts:60-76`).
- The Threads page puts a thread in Needs you only when `isWaitingOnYou` holds (`src/app/threads/page/threadGroups.ts:38`, `threadWorkflow.ts:28-39`): a pending approval card, an unanswered agent question, or a background approval in an unresolved thread.
  It tests that before anything else, so such a thread is in Needs you even when it is snoozed or an agent is working in it.
  The other threads go to Unread, Working, Recent, Later or Automations (`threadGroups.ts:39-50`).
- A later thread's return time is the end of its active snooze, else its next scheduled run (`threadReturnTs`, `threadWorkflow.ts:59-64`).

The client derives the inputs from events it holds in memory.

- Resolved and snoozed come from the thread's tag state; a snooze counts only while its `until` is later than now (`threadSnooze.ts`).
- Scheduled tasks come from `com.mindroom.scheduled.task` state: pending, bound to an existing thread, and not a one-time task whose time has passed; the next run is known only when every such task has a time, so a recurring task has none (`threadScheduledStatus.ts:28-63`).
- Background jobs come from `io.mindroom.background_jobs` state that an agent published under its own user ID: a job with status `awaiting_approval` is a background approval, any other listed job is a running job (`threadBackgroundJobs.ts:12-27`).
- A reply in progress is read from the thread's ten newest loaded events, each as its newest edit shows it, with a server check for replies the client did not see arrive (`useThreadStreamingState.ts:73-137`, `streamingReplies.ts`).
- A pending approval and a pending question are read from the thread's newest 40 messages and all loaded approval cards (`src/app/threads/cross-room/threadSignals.ts:293-344`).
  A question counts only in a thread the person takes part in (`threadSignalsReader.ts:72-74`), stops counting after seven days (`QUESTION_WAITS_MS`), and both stop counting once the thread was resolved after they were asked.

The backend can read the same state in one request per room since #4137 (`MindRoomAsyncClient.room_get_state`, `src/mindroom/matrix/client_session.py:120`).
Thread tags and scheduled tasks already have parsers for its events (`parse_thread_tag_state_key` and `parse_thread_tag_record`, `src/mindroom/thread_tags.py:411-458`; `task_records_with_writers`, `src/mindroom/scheduling/tasks.py:531-556`); the jobs state has only its writer (`src/mindroom/tool_jobs/publication.py:48-57`), whose shape a reader follows.
MindRoom's homeserver adds `io.mindroom.latest_reply` to each thread root's `m.thread` bundle on `/threads`: the newest thread event that is not a title notice, with its newest edit, in unencrypted rooms only (`mindroom-tuwunel` `src/service/rooms/pdu_metadata/bundling.rs:571-660`, called from `src/api/client/threads.rs:97`).

## Decisions

1. **One parameter, four values.**
   `matrix_room(action="threads")` takes `workflow`: `needs_you`, `working`, `later` or `all`.
   Bas's ruling: without `workflow` a `threads` call is unchanged (no state read, no new fields); any `workflow`, `all` included, labels the rows, and a failed state read then returns the tool's error payload.
   The backend drops the rows of other groups before it builds their previews, so a filtered call costs the model only the rows it asked for.
   No tool and no config option is added.
2. **Every row of a `workflow` call is labelled.**
   A row keeps its fields and gains `workflow`: `needs_you`, `working`, `later` or `done`.
   A needs-you row has `why`: `approval`, `question` or `your_turn`.
   A later row has `back_at`, a millisecond timestamp, when the return time is known; a thread that waits on a recurring task has none, as in the client.
   `all` returns every row, resolved ones included, labelled `done`; there is no `done` filter.
3. **The group is the Threads page's combination of the two client rules, in this order.**
   A pending approval card for the requester, or a background approval in an unresolved thread, is `needs_you` with `approval`.
   A pending question is `needs_you` with `question`.
   Then `classifyThreadWorkflow` decides: resolved is `done`, an active snooze is `later`, a reply in progress or a background job is `working`, a pending scheduled task is `later`, and the rest is `needs_you` with `your_turn`.
   The port is one pure function in `src/mindroom/thread_workflow.py`, and both repositories run the same case file against their implementation (decision 9).
4. **`needs_you` is wider than the Threads page's Needs you group.**
   The page's group is the rows whose `why` is `approval` or `question`.
   A `your_turn` row is what the room overview's Needs you tab holds beyond those and what the sidebar calls Idle: an open thread in which nothing runs and nothing is scheduled.
   The tool description says so in one sentence, so an agent asked "what needs me?" leads with the approvals and questions.
5. **Resolved, snoozed, scheduled and background jobs come from one room-state read.**
   The read is `client.room_get_state`, once per room and call; the existing tag and task parsers read its events, and a few lines read the jobs.
   Tags are merged as the client merges them: per-tag events over a thread's legacy `{"tags": ...}` event, `{}` removing a tag; a thread is resolved at its `resolved` tag event's server time (`origin_server_ts`), as in the client, not at the writer-supplied `set_at`; a root in `m.room.pinned_events` is not resolved, as the client reopens a pinned thread.
   Known limit: like the client, tag events are not checked for the writer's power level, so any member who may write them can resolve or snooze a thread.
   A scheduled task counts when a MindRoom account of this runtime wrote it, it is pending, it names an existing thread, and it is not a one-time task whose time has passed.
   A jobs event counts when its sender is its state key, a configured MindRoom bot of this runtime, and still joined (the client ignores state of an agent that left); a listed job counts only with the fields the client requires.
   A malformed record is skipped; the other records still count.
   A call whose state read fails returns an error instead of unlabelled rows, because a filter over unknown groups would hide threads silently.
6. **A reply in progress comes from the runtime's own record of running replies.**
   Each bot already knows the threads of a room in which it holds an active reply (`ResponseLifecycleCoordinator.active_thread_ids_for_room`, `src/mindroom/response_lifecycle.py:490`); the tool asks every bot of its orchestrator.
   This covers every agent, team and router of the MindRoom runtime the calling agent runs in, from the moment a turn starts, in encrypted rooms too, and it cannot go stale after a crash.
   It does not cover an agent that another MindRoom runtime hosts in a shared room, and a tool call outside an orchestrator sees no running reply; such a thread is then classified by its other facts.
   A background job makes the thread `working` only when one of this runtime's bots published it (decision 5); another runtime's jobs are not seen.
   Known limit: a reply parked on another person's foreground approval still counts as a running reply, so the thread is `working` for everyone but the approver until the card is decided; MindRoom Chat shows them neither a stream nor a card.
7. **A pending approval comes from the approval journal, not from the thread's events.**
   The journal holds every request card this runtime published, with its room, thread, approver, deadline and creation time (`ApprovalCardView`, `src/mindroom/event_journal/approval_cards.py:82`).
   A card counts when it is published, undecided, not past its deadline, its call still exists, and its approver is the requester or one of the requester's aliases.
   A card that can only be denied and a card for a scheduled call count too, because the client asks the person about both.
   When the thread is resolved, the card counts only if it was created after the resolve.
   Divergence: a card of another runtime's agent is not seen.
   Known limit: a card is timed by when this runtime queued it, the client by its event's timestamp; a card whose first send failed and that was published after a resolve is `done` here and Needs you in MindRoom Chat.
8. **A pending question is read from the root's bundled latest reply, which costs no request.**
   It counts when all of these hold: the requester started the thread; the bundled `io.mindroom.latest_reply` is from a MindRoom account of this runtime; its content as its newest same-sender edit shows it carries an `io.mindroom.interactive` prompt with question text and at least one option keyed by its reaction, not only by number (the client's `readInteractiveQuestion`); it does not name another person as its acting requester (`com.mindroom.acting_requester`, read from the newest edit first, as a streamed reply carries it there), as the client skips a question asked of someone else; it was asked less than seven days ago; and the thread was not resolved since.
   The client instead scans the newest 40 messages; the bundle agrees with it whenever the question is still the thread's newest event apart from title notices, which is the case until someone answers.
   Divergences, each of which turns a `question` row into the row its other facts give, most often `your_turn`:
   - a room that is encrypted, or a homeserver without the bundle, yields no question;
   - any thread event after the question hides it, where the client hides it only for a message from the person or a newer agent reply;
   - a thread an agent started, in which the person later wrote, yields no question, where the client counts it;
   - for the seconds between a reaction that answers the question and the agent's next reply, the row still says `question`.
9. **One case file, committed identically in both repositories.**
   `tests/fixtures/thread-workflow-cases.json` in `mindroom` and `src/app/event-store/threads/fixtures/thread-workflow-cases.json` in `mindroom-chat` hold the same bytes: for each case the time, the status and signal inputs, and the expected `workflow`, `why` and `back_at`.
   The backend test calls the port; the client test calls `isWaitingOnYou`, `classifyThreadWorkflow` and `threadReturnTs` and combines them as `threadGroups.ts:38-50` does.
   A rule change in either repository fails that repository's test until the file and the other side follow.
10. **`room_id="all"` lists the rooms the requester and the agent share, and no others.**
    The candidates are the rooms the agent's client has joined; each one passes the same check a named room passes today (`room_access_allowed`, `src/mindroom/custom_tools/attachment_helpers.py:46`), which requires that the requester may talk to this agent there; a listing also requires a fresh joined-member match in every candidate, the invoking room and contact rooms included (`require_membership=True`), one membership read per room.
    This is a privacy boundary: a thread of a room the requester is not in is never returned, whatever the agent itself can read, and a room whose membership cannot be read counts as not shared.
    Each row of such a call also has `room_id`.
    The literal `all` is taken before room names are resolved, for the `threads` action only.
    Known limit: membership is read once, before a room's threads, as for a named room; a requester removed during that one request can receive a thread started within it.
11. **`all` reads one page per room and does not paginate.**
    Each shared room contributes its newest `limit` thread roots; the matching rows are merged, the rows that wait on an approval or a question first and then the newest activity first, and cut to `limit`.
    `has_more` is true when rows were cut or a room has older threads, and `page_token` with `all` is an error; to go further the agent names one room and pages there.
    A cursor across rooms would need one token per room in a value the model must copy back.
    A room whose state, thread list or approval journal cannot be read is left out and named in `failed_room_ids`; a room whose membership cannot be read is left out silently, since its access is unproven.
    Rows are ranked and cut from their bundled metadata first; previews and summaries are read only for the rows returned.
    In one room, `limit` and `page_token` keep their meaning: a page of `limit` roots is read and filtered, so a filtered page can hold fewer rows, even none, while `has_more` is true.
12. **Scope and automations are not mirrored.**
    The tool answers for every thread with replies in the rooms, which is what `/threads` lists and what the client's own eligibility rule keeps (`isCrossRoomThreadEntryEligible`, `crossRoomThreadOrder.ts:7-8`).
    The Threads page's Following scope and its Automations group both depend on who wrote in a thread (`threadGroups.ts:39`, `threadSignals.ts:356-357`), which no bundle carries.
    So against the page's All scope, a thread that agents run alone appears in `working`, `later` or as `your_turn`, where the page files it under Automations; against Following, the tool also lists threads the person took no part in.
    Foreground approval and question rows are unaffected, since both require the requester as approver or as the thread's starter; a background approval counts whoever approves it, as in the client.
13. **Cost.**
    One room: one `/threads` request and one state request, plus the membership request a named other room already costs; approvals and running replies are read from memory and the local journal.
    The state request is added only when `workflow` is given, `all` included; a call without `workflow` costs what it does today.
    `all` with N rooms the agent is in, M of them shared: N membership requests, M state requests and M `/threads` requests, at most 3N, six rooms at a time.
    Previews (which may download a sidecar) and `include_summaries` read only the returned threads, after the cut.

## Rejected

- **A separate tool or a `done` filter value.** The decision is one optional parameter with four values; resolved threads stay reachable through `all`.
- **Deciding a reply in progress from the bundled latest reply's stream status.** It needs no new code path in the bots and covers other runtimes, but it yields nothing in encrypted rooms, a message queued behind a streaming reply hides it, and a writer that crashed needs the client's 30-minute idle rule.
- **Reading each thread's newest events to match the client's question, approval and automation rules exactly.** That is one request per listed thread, 20 to 50 per room, for differences that mostly change a `question` row into a `your_turn` row.
- **Computing a recurring task's next run for `back_at`.** The client shows no time there, and a row would then disagree with the app.
- **A cross-room page token.** See decision 11.
- **Reading the requester's membership from the state read instead of `room_access_allowed`.** It saves N-1 requests but would copy the access rule, which also covers contact rooms and who may talk to the agent.
- **Room names on the rows of an `all` call.** `room-info` gives a room's name; it can be added when voice use shows the need.
- **Labelling every `threads` call.** It would add a state read and new fields to calls that never asked for groups; only an explicit `workflow` labels rows.

## Delivery

Two pull requests.

1. Backend, branch `feat/threads-workflow-filter`: the port and its case file, the room-state inputs and the `workflow` parameter, approvals and questions, `room_id="all"`, and the tool's page in `docs/tools/matrix-and-attachments.md`.
2. MindRoom Chat, branch `test/thread-workflow-shared-cases`: the same case file and a test that runs it; no product code.

The estimate for the backend is about 190 product lines, above the 100 to 150 that were asked for: the port 40, the state inputs and the parameter 70, approvals and questions 40, `room_id="all"` 40.

## Testing

- The case file runs in both repositories and covers each rule's precedence: an approval or a question ahead of resolved, snoozed and working; a background approval in a resolved thread; a snooze that ends exactly now; working ahead of scheduled; a later thread with and without a known return time; an expired snooze with a scheduled run.
- Backend tool tests through `matrix_room` with a served room state: each filter returns only its group; a resolved, a snoozed, a scheduled and a working thread get the right label; an approval older than the resolve and a question older than seven days do not count; a failed state read is an error.
- `room_id="all"`: a room the requester is not in contributes nothing, also when the agent is in it; rows carry `room_id`; rows waiting on an approval come first; `page_token` is refused; a failing room is named and the others are returned.
- Live check on a fresh throwaway deployment with the backend from the worktree: snooze one thread, resolve one, schedule one, leave a question open in one, and compare the tool's answer with the Threads page.
