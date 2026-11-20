# Router-Free MindRoom

This record covers removing the router as an account that features depend on.
It changes the backend (`mindroom`) only; MindRoom Chat follows in a later pull request.

## Goal

Bas wants MindRoom to work without the router agent being present, because one account that must be in every room is hard for people to understand.
Today the router account does three unrelated jobs: it picks who answers an unmentioned message, it does once-per-room work (commands, approval cards, voice transcripts, member tracking, room state, member hooks), and it owns the rooms MindRoom creates.
A room without it loses commands, tool approvals, `current_room_members` access, schedules' state, external triggers, plugin messengers and the voice transcript, each with its own failure text, and every agent carries an `invite_router` tool to work around that.

Success: everything that works in a managed room works in a room with one person and one agent, no code path tests for a specific account's presence, and a routed reply is the chosen agent answering the person's own message with no "@X could you help?" message in between.

## Prior art

Two read-only explorations of `origin/main` 55646059a, written independently on 2026-11-17, agree on the core and are the evidence base for this record.

- Exploration A (Claude Fable) splits the router into a selector, a per-room singleton and an owner account, derives the per-room singleton from room state, and keeps a demoted owner account for the rooms MindRoom creates.
- Exploration B (GPT-6 Astra) keeps coordination in the orchestrator, uses ordinary accounts as transport, and pins every visible effect to the account that sent it.
- Both name the same riskiest step: reply-access member tracking assumes one sync stream, and a mistake there either admits someone who left or locks everyone out.
- The synthesis of the two reports checked the live plugins and MindRoom Chat, which neither exploration had read.
  The plugins use `agents=(ROUTER_AGENT_NAME,)` in two ways: once per process (`restart-resume` and `error-notifier` on `bot:ready` and `config:reloaded`) and once per room event (`ping-hook` on `message:received`).
  MindRoom Chat depends on the router only for mention ranking, the voice transcript content key, and the `io.mindroom.accounts` and `io.mindroom.thread_mode` state events.

## Decisions

1. **Routing is hidden: the room's steward sends a route event that no client shows.**
   Picking a responder for an unmentioned message in a room with several candidates reuses `suggest_responder`, `router.model` and `router.judgment`.
   One bot per room, its steward, picks, and tells the chosen agent with a hidden `io.mindroom.route` event (decision 14).
   The event is a real Matrix event, so it is durable, every bot admits it through its own sync, and no cross-bot state is shared in the process.
   The chosen agent answers the person's original message for that person, and no relay message, notice or title is posted by the steward.
   Mentions, thread continuation, `reply_mode: mentions` and the multi-human silence rules stay as they are.
   Visible relay messages already in room history are still read and answered as before.
2. **Everything visible about a message comes from the agent that answers it.**
   - Voice: the transcript is produced in the process, routing reads the transcript, and the answering agent posts the transcript and then its reply; when nobody answers, no transcript is posted.
   - Approval cards are posted by the agent or team that asks, and each card records its sender so restart recovery edits the right card.
     Router cards pending at upgrade are ended once.
   - A command aimed at an agent (`@X !model`, or a command in X's thread) is handled by X; an untargeted command at room top level is routed like any message.
3. **The steward does invisible work only.**
   Per room, the steward is the MindRoom account with the highest power level among the MindRoom accounts joined there, ties going to the lowest Matrix ID.
   It tracks members for reply access, writes room state (thread mode, `io.mindroom.accounts`, schedule state), and runs member-join hooks and once-per-room plugin hooks.
   It never posts a message.
   Duties that must not run twice take a durable per-event claim.
4. **The owner account is the old router account, demoted.**
   It keeps the `mindroom_router` localpart, credentials and state directory, is shown as "MindRoom", cannot be mentioned and never answers.
   It creates and administers the rooms in `config.yaml`, the root Space and the emoji room, and posts the welcome in configured rooms.
   It is registered only when the config needs such rooms, and no feature depends on it being in a room.
   It counts as a MindRoom account for the steward rule, so in the rooms it created it is the steward by its power level, which keeps member tracking and state writes in managed rooms as they are today.
5. **Hooks gain `scope="room"` and `scope="service"`.**
   A room-scoped hook runs once per event; a service-scoped hook runs once per process.
   Room-scoped message and reaction hooks run on the first eligible emitter, the first bot that admitted the sender and emits the event, not on the steward.
   Room-scoped member hooks, and emissions without a Matrix event, run on the room's steward.
   `agents=(ROUTER_AGENT_NAME,)` stays as a deprecated alias: service scope on `bot:ready` and `config:reloaded`, room scope on every other event.
6. **Config keeps the `router:` section.**
   `model` and `judgment` configure routing.
   `access`, when set, stays an extra restriction on routing, because dropping it would widen access on deployments that set it.
   `accept_invites` loads with a deprecation warning and has no effect.
7. **The "nobody fits, please mention an agent" notice is dropped**, because no account is left to post it.
8. **Homeserver independence.**
   MindRoom relies only on ordinary client registration and in-room power levels: no server configuration, admin API or server default power levels.
   In a room a person created, agents start at power 0, so a state write there fails with "I need moderator rights in this room to <feature>" from the agent the person is talking to, never with advice to invite another account.
   The existing optional features that use the homeserver admin API (`ctx.matrix_admin`, personal-room `auto_join_requester`) stay optional and owner-backed; nothing new depends on them.
9. **Invariant.**
   Everything that works in a managed room works in a room with one person and one agent.
   No code path may test for a specific account's presence.
10. **Deleted:** the `invite_router` tool (its name answers "no longer needed" for one release), the visible relay message, the router's persona, presence line and avatar generation, and every router-only fallback branch.
    This pull request keeps publishing what today's MindRoom Chat reads.
11. **One pull request, least code.**
    Each task deletes the router path it replaces in the same commit; the only dual paths are the compatibility existing deployments need (old events, pending cards, old config, the plugin alias).
    Net source lines go down.
12. **Voice ordering (question A): the transcript already exists before routing, so only its publication moves.**
    Every bot transcribes a voice message before the message enters its coalescing lane, through one shared in-process transcription and a durable per-bot checkpoint (`voice/readiness.py`), so routing already reads the transcript.
    Today the router posts a "transcribing" placeholder before that and the other bots wait on an in-process barrier and on the router's receipt.
    Publication moves to after the plan: the bot whose plan is to answer posts the transcript, records the receipt in its own turn rows, and only then starts its reply.
    A restart replays the turn from the same checkpoint, a routed turn from the answering bot's own journal (decision 14), finds its own receipt and does not post again.
    The placeholder and the barrier go away, because the poster and the answerer are the same bot.
    A routed turn's responder posts the transcript its route carries when retention has cleared its own checkpoint.
    When two bots answer one voice message, a shared per-event claim lets one of them post the transcript and an in-process lock held during that send keeps the other's reply behind it.
    A bot that finds the claim's owner without a receipt waits 0.25 seconds for it and then answers.
    The transcript is sent under a Matrix transaction ID derived from the room and the voice event, so a retry is not a second message.
    An agent whose participation still decides whether to answer posts the transcript once it approves the reply, and a contact room gets no transcript.
    Consequence: the "transcribing" progress line disappears, since nobody is known to answer until the transcript exists.
13. **Member tracking (question B): one pinned observer per room, chosen by the steward rule when the room's row is built.**
    The index keeps its single snapshot, its refresh lock and its replay of live transitions, and each room row records the account that observes it.
    A refresh reads each room's members through that room's observer, picked by the steward rule among the bots whose own first sync has completed and whose own joined-rooms answer lists the room.
    Only the pinned observer's live membership events change a row; another bot's copy of the same event is ignored, so two sync streams are never merged.
    A row fails closed when its observer leaves the room, reports a sync gap, restarts its sync loop or stops, and the next refresh picks a new observer and reads the room again before the row is ready.
    A power-level change alone does not move the observer, because a joined, syncing account stays an authoritative witness of membership; this avoids a handover protocol.
    A grant room that no running MindRoom account has joined stays unready, which denies, and is reported as a config warning.
14. **Selection agreement (question C): the room's steward selects, and a hidden `io.mindroom.route` event carries the selection.**
    Two earlier designs kept the selection inside the process: one saved row per source event that every candidate reconciled, then one routing lane per room with a durable handoff between journals.
    Review kept finding cross-bot holes in both, with one root: the old visible relay was a real Matrix event, so it had durability, per-bot sync admission, requester identity, membership checks and deduplication for free, and each in-process replacement had to rebuild them.
    So the relay's transport stays and only its visibility goes.
    Every bot first answers by its own rules, which are the rules of `origin/main`: the only candidate, a mention, the thread's agent, a follow-up to its running reply, a team, adaptive participation.
    A turn those rules leave unanswered (several candidates, nobody mentioned, the thread names no responder and holds one person, `router.access` allows the requester when set) is routed by the room's steward alone, and every other bot ignores it, as it ignored it while a router existed.
    The steward (decision 3) may be the owner account or a candidate.
    It coalesces and normalizes the turn as any bot does, runs the selector once, and sends one event of type `io.mindroom.route`.
    The event's content names, under the `io.mindroom.route` key, the chosen account (`entity`), every message of the turn with the newest last (`event_ids`) and the thread the answer belongs in (`thread_id`), and carries the relay context the responder reads: the trusted-relay source kind, the original sender, attachment IDs, voice markers, the transcript of each routed voice message (`io.mindroom.voice_transcripts`), and a scheduled task's per-run settings.
    The responder keeps only those keys, and only an event journaled as a route is read as one: every other journal event loses the route key when it is rebuilt.
    It has no `m.relates_to` and no `m.mentions`, so no homeserver counts it in a thread and no mention rule fires for it, and its `body` is the steward's prompt for the turn: the batch as the steward coalesced it, with each voice message as transcribed.
    The steward read every message of the turn, which the responder may not: a batch can span threads, and a source can be older than the thread page the responder reads, or encrypted with a key it lacks.
    Every route carries that whole prompt, whatever the kinds of the messages it names.
    A route too long for one event is sent as any long message is: its content goes in an uploaded long-text file, encrypted in an encrypted room, the event keeps a preview and the route's keys, and a route whose file cannot be uploaded is not sent.
    The responder's current message is that prompt, and the messages the route names are left out of the thread history beside it.
    The responder reads the prompt from the route or from the route's file, and never makes one from the messages the route names.
    While it cannot read the file, and for a route without a `body`, it starts no turn and leaves the route pending.
    No model is ever sent a preview or a stand-in text.
    A route is a whole turn its steward selected, so it shares a turn with nothing: two routes waiting for one responder are answered one by one, whatever their source kind.
    A message its sender edits while it waits for its turn takes the edit, which settles then, and the journal stores the waiting message as edited: after a crash the message replays with the correction, for a steward and for an agent answering directly.
    Only the responder a route names processes it: every other bot settles it unread, having run its hooks for the person's own message.
    The steward routes under `router.access` alone, so a candidate steward whose own access denies the person still routes the person's message, and answers it itself, runs the person's commands and runs its own `message:received` hooks only when its own access allows.
    Every bot journals the event from sync, in encrypted rooms too, the way a silent schedule trigger is journaled, and only when its sender is an account the identity registry manages.
    The bot it names runs it as a mention through the existing trusted-relay path: the requester is the original sender, the bot's own access rules apply to that requester, and the answer replies to the person's event.
    Every other bot settles it without work.
    A candidate steward that the selection names answers the turn itself and sends no event.
    A turn nobody fits gets no event and no answer; when the selector fails, the candidate with the lowest Matrix ID is chosen.
    A selected responder that is still starting leaves the turn pending with nothing sent, and it is routed again once that responder is ready.
    The steward records the route as its answer to the turn, so a replay sends no second route, and a replay whose record a crash lost looks, before it selects anyone, for its own route in the room's history that names exactly the messages of its saved batch.
    A responder that receives a second route for a message it answered answers once, because the route names every message of the turn as an alias of its turn.
    A deletion of any of those messages reaches the answer; an edit regenerates it only when the route names that one message, as an edit of a relayed message did, and an edit of one of several routed messages changes nothing.
    A bot rechecks that it is still the room's steward when its selection returns, and when a replay returns to a turn it had selected itself for, and sends and answers nothing when it is not; two bots that both pass that check within the same moment can still both route, which is accepted.
    A turn that pauses for an approval stores the event its reply answers, so the resumed reply and its tools answer the person's message and not the route.
    The fast paths read nothing shared, so their rare races are the ones the router had: a candidate that answers by its own rules while the steward routes the same message can produce a second answer.
15. **Startup (question D): no account is required.**
    Every configured account starts through the same retrying path, with the owner account first when the config needs one.
    When room-backed access is configured, dispatch stays parked until every bot that started has finished its first own sync or has stopped, and then one refresh builds the member rows; a room whose observer is not ready stays unready and its messages wait, as they do today while the router's refresh is pending.
    An owner account that fails to log in degrades room creation and administration only.

## Rejected

- **Rename the router and leave it everywhere.** It fixes the name and keeps every "must be in the room" failure.
- **No dedicated account; an agent creates and owns the rooms.** In room versions with privileged creators the creator's power cannot be handed over, so removing that agent from the config would orphan the room, and the Space and emoji room have no agent at all.
- **The orchestrator assigns the steward and keeps a healthy incumbent.** It needs a handover protocol and saved assignments; deriving the steward from room state needs neither, and pinning the observer per row gives member tracking the stability it needs.
- **The steward or the owner posts system messages.** It keeps a second voice in the room, which is the confusion this record removes.
- **A visible "picked X" line in place of the relay.** It keeps the extra message per routed turn.
- **Selecting inside the process, with per-source claims or a durable handoff between journals.** Both were built on this branch; each had to rebuild what a Matrix event gives for free, and review kept finding cross-bot holes (decision 14).
- **Merging every bot's view of room membership.** Nothing orders one bot's join against another bot's kick, so a merged view can admit someone who left.
- **Rename `router:` to `routing:`.** It forces a config migration for no behavior change.
- **Routing in the homeserver, an appservice or the client.** It breaks decision 8 or excludes other Matrix clients and bridges.

## Delivery

One pull request on `refactor/router-free`, built as these tasks in order:

1. The steward function.
2. Routing through a hidden `io.mindroom.route` event from the room's steward.
3. Member tracking per room.
4. Steward duties: room state, member hooks, per-event claims.
5. Voice transcript from the answering agent.
6. Commands handled by the agent they are aimed at.
7. Approval cards from the asking agent or team.
8. Schedules and the schedule API without the router.
9. Triggers, call transcripts, plugin HTTP helpers, thread move and computer access without the router.
10. Hook scopes and the router alias.
11. Startup without a required router, and owner registration only when needed.
12. Owner demotion, `invite_router` removal, docs.
13. Final deletion pass.

A later MindRoom Chat pull request reads the steward's `io.mindroom.accounts` key, addresses model discovery to any MindRoom account, and drops the router from mention ranking.

## Testing

- Each task adds tests at the seam that owns the behavior (`TurnPolicy`, `TurnDispatcher`, the membership index and its sync, the approval transport, the hook registry), not through the `AgentBot` facade.
- Member tracking gets failure-injection tests: the observer leaves, the observer's sync reports a gap, the observer stops during a refresh, a second bot reports the same membership event, a join arrives during a refresh that already read a kick, and a grant room no account has joined.
- An actor by ingress-path grid covers a room with one person and one agent, a room with two agents and no owner account, and a managed room, for text, voice, a command, a gated tool call and a restart in the middle of each.
- A flow test covers upgrade: a pending router approval card, a router-owned schedule, an old relay event in thread history, a config with `router.access` and `router.accept_invites`, and a plugin hook that uses the alias.
- A live run on a disposable local stack with no owner account registered: text, voice, `!help`, a tool approval and a restart, with only a person and an agent in the room.
