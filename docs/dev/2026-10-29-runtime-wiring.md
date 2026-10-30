# Runtime wiring: one production path for bots and the dashboard API

This record holds the decisions behind giving bots and the dashboard API one production wiring path, holding the runtime's shared state in one owner, and moving router duties and leftover feature logic out of `bot.py` and `orchestrator.py`.
It records reasons, rejected alternatives, and accepted limitations; the code shows the mechanisms.

## Decisions

- A bot always belongs to a runtime: it is built with its orchestrator and reads the published config, the response admission gate, the reply-membership index, the event-journal store, and the hook registry through it.
  Every bot-without-orchestrator branch existed only for tests, so tests exercised a copy of runtime behavior production never ran; tests now build bots inside a started runtime whose fleet liveness the test sets.
- The orchestrator receives the config startup loaded when it is built, so its config is never missing, and a reload's publication is the one write every running bot reads from that instant.
  Admission is closed for the whole apply window, so no reply starts under a mixed view; replies of unchanged bots that keep running across a live-field reload read the new config a little earlier than before, which live fields allow by definition.
- What a running bot derives from the config (personal-room settings, a call manager's authorization) is refreshed for every running bot in the same synchronous step that publishes the config, so no bot's derived state lags behind the config it reads.
- The active hook registry has one holder, and each lifetime that emits hooks reads it through a view that ends when the lifetime ends: each bot generation, the scheduled-task runtime, and the runtime's own emissions.
  A hook context is active while its view still holds the snapshot it was bound to, so a stopped generation's hooks turn inactive at its stop, as before, while a plugin reload reaches every live generation without a push loop.
  The runtime's view ends at the start of shutdown and the scheduled-task runtime's view with its stop, the first step of shutdown, as the scheduler's former module global did; bot views end only at each bot's stop, after its `agent:stopped` hooks ran.
- "Is this bot generation accepting work" is one run phase: accepting, draining (a restart, removal, or generic stop is draining what it owns), or shutting down (orderly process shutdown, final), plus a latch that keeps a quiesced generation's late sync loop from reopening it.
  The admission fence around an authorization decision is derived from the phase and the runtime's gate in one place.
  The journal worker keeps its own started/stopped state, because that is its task lifecycle rather than an admission fact.
- The dashboard API serves only inside `mindroom run`; the standalone API process was deleted, because it loaded, watched, and migrated config and storage on its own and kept its own knowledge scheduler, watcher, and worker-launch config, a second copy of runtime duties that only it ran.
  What `mindroom run` lends its API is one frozen value bound once before the server serves and cleared once after it stops; the API reads it as always present, and an app served without it fails at startup.
  Values that change while the runtime runs, such as reload status and active calls, are callables into their owners, because a frozen copy would go stale.
  Bindings the runtime replaces per published config generation, such as external-trigger delivery and the computer runtime, stay separate, because they are not set once.
- Every startup migration and the credential sync run through one ordered step; old worker mounts of whole state roots retire before conversation storage converts.
  Only `mindroom run` prepares a storage root and deployments replace its process rather than overlap two, so startup migrations take no lock against a second starting process.
- The router's early skip before shared ingress work and its dispatch plan share one mention rule and one thread rule; the early skip only drops what the plan would ignore, and a missing thread snapshot means continue through normal ingress.
- The router bot's own duties, keeping room-backed reply grants current from its sync, setting up and welcoming the rooms it joins, cleaning orphaned bot accounts, and emitting room-membership hooks, live in one collaborator only the router builds.
- One room-setup step binds the trigger delivery runtimes after setting up the rooms of bots that just started, at startup, after background starts, and after MCP restarts; it retries only where nothing waits on it, binds even when a setup fails, and never binds once shutdown began.
  A config reload keeps its own conditional room setup and binds in its final step, because its repair path must cover the binding too.
- Turn-journal recovery evaluates readiness at three moments on purpose: a ready signal releases newly ready bots at once even while a pass drains, a pass releases its ready set before draining it, and each drain resolves the entity's current generation again, because a replacement during an earlier drain can retire the generation the pass released.
- A minimal-mode turn's CLI calls stay authorized across a reload unless the reload changed what the agent's toolkits were built from: the whole config, including derived private state such as tools a failed plugin load made unavailable, except other agents' and teams' entries, the agent's own prompt-only fields, and private state that only records where the config came from.
  Comparing the resolved agent was rejected, because it omits inputs such as graph-edit permission, approval rules, MCP server definitions, and plugin settings; listing authority fields instead of prompt fields was rejected, because every field added later would silently stop counting.
  Native tool calls are not rechecked against config: a native turn's toolkits are objects built when the turn starts and live edits apply from the next reply, while a CLI grant's calls arrive over HTTP for as long as the turn runs and are each re-authorized against current requester access.
- The invited-rooms file is the only record of an entity's invited rooms; readers read it, and the store answers synchronous readers from a cache keyed by the file's modification time and size, so a write from anywhere is seen at the next read.
  A failed forget raises, and the membership effect that called it retries; the in-memory set of rooms whose forget had not reached disk approximated that retry.
  Call-room ownership considers only call agents the runtime runs, so an absent agent's record cannot make a room ambiguous.

## Rejected alternatives

- One shared hook-registry holder without lifetimes: a stopped generation's hooks would stay active.
- Blanking the shared holder at process shutdown: bots stop after it and would skip their `agent:stopped` hooks.
- A typed fake orchestrator reimplementing the runtime's answers: bot tests would again exercise a copy; the test runtime is the production orchestrator with test-set liveness instead.
- Moving MCP catalog restarts out of `orchestrator.py`: the restart is lifecycle coordination (replace entities under the config-update lock with startup maintenance paused, then set up rooms), so a separate module would call back into the orchestrator for each step.

## Accepted limitations

- Rooms that a failed MCP-restart room setup missed are reconciled by the next config reload or entity start.
- The call manager keeps its own derived copy of the config (owned by the transport package); the publication step refreshes it with the config.
