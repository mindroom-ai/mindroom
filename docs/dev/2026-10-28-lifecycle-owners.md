# Lifecycle owners: reload decisions, entity lifecycle, and long-running services

This record holds the decisions behind one owner for what a config reload changes, one owner for each managed bot's lifecycle, one supervised shape for long-running services, and one config load per edit.
It records reasons, rejected alternatives, and accepted limitations; the code shows the mechanisms.

## Decisions

- The config update plan is the only place that decides what a reload restarts, starts, removes, and re-authorizes.
  Its restart, start, and remove sets are disjoint by construction, because a removed entity that was also treated as a restart was stopped by the restart path and then skipped by the removal path, so it never left its rooms and its approval recovery ran after its sender stopped.
- A reload stops every entity it changes before it publishes the new config.
  Removal runs first, while the current router still delivers unavailable-approval notices and before a replacement router's orphan cleanup could remove the departing accounts from their rooms; restarted entities stop next.
  Plugins are staged before anything stops, so a plugin reload that fails to stage fails the config reload with every bot still running.
- Removal settles approval continuations before the config that removes the entity is published, through an explicit "removed" entry, so recovery never depends on which config is live.
  An entity whose frozen FINAL recovery is still unresolved does not leave its rooms, so the recovery retry can still deliver there.
- Three authorization predicates stay separate because they gate different things: any reply-authorization input (publication waits for replies), a continuing agent's responder access (background scripts are re-authorized; a broader trigger would end scripts whose bot is merely restarting, because an unavailable bot cannot confirm authority), and room-membership grant inputs (grants are revoked until the router refreshes, which blocks replies, so it must not fire on extra inputs).
- One entity fleet holds each entity's bot, sync task, start retry, and disabled flag in one slot, with one stop sequence per reason.
  A restart tolerates only a reply that outlived the drain, because the replacement replays it; removal and process shutdown keep a bot that still owns cleanup registered until its deferred stop finishes.
- A bot start is one attempt and the fleet owns every retry, because a retry inside the fleet's own retry loop multiplied attempts and delayed the degraded report.
- An edit made while MindRoom starts is applied once startup publishes its config: a reload request made before the runtime runs is kept, and startup reloads once if the config files changed since it loaded them.
- Under `mindroom run` the runtime loads each edit once and publishes that load to the embedded dashboard before it waits for any response drain, and also while MindRoom starts, before it can apply anything.
  A dashboard save rewrites the whole file from the config the dashboard holds, so a dashboard that lagged behind the file for a drain would erase the edit.

## Rejected alternatives

- Stopping only MCP-dependent entities before the MCP sync and the rest after publication: it kept two stop phases and a reconciliation parameter between them; stopping every changed entity first meets the same need.
  Cost: an entity restarted in the same reload as an MCP server change is also down during the MCP sync, while admission is closed anyway.
- Keeping the dashboard's own config watcher and loader beside the runtime's: every edit was loaded twice, running plugin modules twice, and two loads raced to publish the dashboard's config.
  Cost of the single loader: the windows under Accepted limitations, in which the dashboard lags behind a file edit while the runtime applies another one.
- Refusing a dashboard save whose config files changed on disk since the dashboard loaded them: the dashboard deliberately lets a save replace a broken config file before any reload has read it, and that check would refuse the recovery.
- A listener protocol for entity lifecycle events: there is exactly one listener, the orchestrator, so the fleet takes plain hooks.
- Re-keying recurring-schedule checkpoints per task with a sender chosen at each Matrix call: every design round found migration and atomicity defects in it (checkpoint precedence across sender-keyed files, sequential sender histories, ambiguous sends across transports).

## Accepted limitations

- A removed entity that leaves no room, because its frozen FINAL recovery is still unresolved or it never started, is removed from its rooms by the next router start's orphan cleanup.
- A file edit made while another reload applies, or a second file edit made while an MCP catalog restart drains responses, reaches the dashboard only after that step; a dashboard save in that window overwrites it.
  The drain lasts at most 600 seconds, but an apply that creates Matrix accounts waits for an unreachable homeserver for as long as it is down.
  Closing it would need a loader beside the reload loop, a second owner of each load, and loading plugin modules concurrently with plugin staging.
- A removal whose reply is still running after the drain and its cancellation fails the whole reload before publication.
  The next reload first restores the last fully applied config, which rebuilds the entity and may replay that reply, and then removes it again, so the entity leaves, rejoins, and leaves its rooms.
  Removal runs before publication so the departing entity settles its approvals while the current router still delivers notices.
