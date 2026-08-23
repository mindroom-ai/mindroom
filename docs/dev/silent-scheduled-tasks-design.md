# Silent Scheduled Tasks

## Goal

Add a per-schedule `silent` mode that runs an agent without posting the scheduled trigger as a visible Matrix message.
Silent schedules should publish a final agent response only when the run has something to report.
Scheduled execution failures and messages explicitly posted by tools must remain visible.

## Non-goals

This change does not hide or suppress normal schedules.
This change does not infer whether nonempty model text is important.
This change does not make scheduled execution serverless or bypass the Matrix-backed delivery and recovery model.

## Existing flow

`ScheduledWorkflow` is persisted in Matrix room state and restored into an in-process timer.
When a timer fires, `scheduling_executor.execute_scheduled_workflow` emits `schedule:fired`, builds an `m.room.message`, and sends that visible event to Matrix.
The visible event is also the durable fanout mechanism because every joined bot admits it through the event journal and the normal turn pipeline decides which entity responds.
Setting `ScheduleFiredContext.suppress` currently cancels the fire before the event is sent, so exposing that field as a schedule option would prevent the agent from running.
The response driver retries a completed empty model run once and then emits an empty-response notice, which also conflicts with intentional silence.

## Public schedule contract

`ScheduledWorkflow` gains a `silent: bool = False` field, so previously persisted records remain normal schedules.
The scheduler tool exposes `silent` when creating a schedule and an optional `silent` override when editing one.
Natural-language schedule parsing recognizes requests to make a task silent or visible, and edit parsing preserves the current value when silence is not mentioned.
Schedule creation, editing, listing, and read models display the selected delivery mode.

## Silent trigger transport

Normal schedules continue to send their existing `m.room.message` payload unchanged.
Silent schedules send a dedicated `io.mindroom.scheduled.trigger` timeline event whose content carries the same formatted task body, mentions, requester identity, thread relation, and history limit metadata.
Matrix clients ignore the custom event because it is not a room-message event, so the task body does not create a visible timeline entry.
The Matrix client encrypts custom event types in encrypted rooms using the same room-send path used for messages.
The `schedule:fired` hook still runs before transport and can transform `message_text` or cancel the entire fire through its existing `suppress` contract.
A hook-transformed message that is empty or whitespace-only fails visibly before transport instead of being accepted as a delivered trigger.

## Durable journal dispatch

The event journal gains a dedicated turn-backed scheduled-trigger kind.
Live and recovered custom trigger events are actionable, while cold history remains context-only and cannot start old jobs.
The custom trigger is not projected into visible conversation history.
Journal dispatch validates the custom event content, converts it into an equivalent in-memory formatted text event with the same event identity and security metadata, and calls the existing message callback.
Malformed custom triggers settle as intentionally ignored work with a diagnostic log instead of poisoning the recovery queue.
The existing turn store settles the custom source only after the answer is durably owed or the turn deliberately produces no answer.

## Thread placement

A silent fire targeting an existing thread keeps that thread relation and delivers any nonempty response there.
A silent `new_thread` fire has no visible event that can serve as a thread root, so its first nonempty response becomes a new room-level root.
An empty silent `new_thread` fire creates no visible root at all.
Conversation target resolution treats a room-level silent trigger as room mode so it never builds an orphaned reply relation to the hidden event.

## Quiet response policy

Silent scheduled turns use a distinct trusted automation source kind.
The response payload adds a non-persistent system instruction telling the entity to return exactly `NO_REPLY` when the check has nothing to report and to report findings or failures normally.
Silent scheduled turns disable streaming so placeholders and incremental tool narration cannot become visible before the final result is known.
The shared response driver accepts the first completed empty run for this source kind instead of retrying it or generating the empty-response notice.
After before-response hooks run, final delivery suppresses a silent scheduled response when its entire trimmed text is empty or exactly `NO_REPLY`, regardless of internal tool trace metadata.
The acknowledgment comparison is case-insensitive, but decorated tokens and responses that merely mention `NO_REPLY` are delivered normally.
Nonempty final text follows the normal durable delivery path, and tools that explicitly send Matrix messages remain unaffected.

## Failure behavior

Failure to send or admit the custom trigger produces the existing visible scheduled-task failure notice.
Model, tool, hook, or response-generation failures continue through existing visible error handling because no-report suppression applies only to successful final responses.
One-time schedules are marked completed only after the custom trigger is accepted by Matrix, matching the current trigger-delivery boundary.

## Testing

Scheduling tests cover persistence defaults, explicit create and edit overrides, list rendering, hook transformation, normal transport stability, and custom-event transport.
Journal tests cover live admission, cold-history rejection, replay, malformed content, security metadata preservation, and turn-backed settlement.
Turn and delivery tests cover non-streaming selection, system guidance, first-empty acceptance, exact no-report acknowledgment suppression, nonempty findings, and visible failures.
Compatibility tests prove ordinary schedules and ordinary empty responses retain their current behavior.

## Security and privacy

Only events sent by a trusted managed automation sender can promote requester and source metadata.
The custom event carries the task body through Matrix and the local durable journal even though clients do not render it.
The feature promises a quiet room timeline, not deletion from encrypted transport, server storage, audit logs, or local recovery state.
