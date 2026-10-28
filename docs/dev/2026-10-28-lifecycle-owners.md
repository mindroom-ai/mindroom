# Lifecycle owners: reload decisions, entity lifecycle, and long-running services

This record holds the decisions behind one owner for what a config reload changes, one owner for each managed bot's lifecycle, one supervised shape for long-running services, and one config load per edit.
It records reasons, rejected alternatives, and accepted limitations; the code shows the mechanisms.

## Decisions

- The config update plan is the only place that decides what a reload restarts, starts, removes, and re-authorizes.
  Its restart, start, and remove sets are disjoint by construction, because a removed entity that was also treated as a restart was stopped by the restart path and then skipped by the removal path, so it never left its rooms and its approval recovery ran after its sender stopped.
- A reload stops every entity it changes before it publishes the new config.
  Removal runs first, while the current router still delivers unavailable-approval notices and before a replacement router's orphan cleanup could remove the departing accounts from their rooms; restarted entities stop next.
  Plugins are staged before anything stops, so a broken plugin fails the reload with every bot still running.
- Removal settles approval continuations before the config that removes the entity is published, through an explicit "removed" entry, so recovery never depends on which config is live.
  An entity whose frozen FINAL recovery is still unresolved does not leave its rooms, so the recovery retry can still deliver there.
- Three authorization predicates stay separate because they gate different things: any reply-authorization input (publication waits for replies), a continuing agent's responder access (background scripts are re-authorized; a broader trigger would end scripts whose bot is merely restarting, because an unavailable bot cannot confirm authority), and room-membership grant inputs (grants are revoked until the router refreshes, which blocks replies, so it must not fire on extra inputs).
- One entity fleet holds each entity's bot, sync task, start retry, and disabled flag in one slot, with one stop sequence per reason.
  A restart tolerates only a reply that outlived the drain, because the replacement replays it; removal and process shutdown keep a bot that still owns cleanup registered until its deferred stop finishes.
- A bot start is one attempt; the fleet owns every retry.
  The nested in-line retry inside the orchestrator's own retry loop is gone, so a non-router entity whose first attempt fails is reported degraded at once and retried in the background after two seconds.
- An edit made while MindRoom starts is applied once startup publishes its config: a reload request made before the runtime runs is kept, and startup reloads once if the config files changed since it loaded them.

## Rejected alternatives

- Stopping only MCP-dependent entities before the MCP sync and the rest after publication: it kept two stop phases and a reconciliation parameter between them; stopping every changed entity first meets the same need.
  Cost: an entity restarted in the same reload as an MCP server change is also down during the MCP sync, while admission is closed anyway.
- A listener protocol for entity lifecycle events: there is exactly one listener, the orchestrator, so the fleet takes plain hooks.
- Re-keying recurring-schedule checkpoints per task with a sender chosen at each Matrix call: every design round found migration and atomicity defects in it (checkpoint precedence across sender-keyed files, sequential sender histories, ambiguous sends across transports).
