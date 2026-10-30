# Tool registry per config, one plugin generation, one identity per tool call

This record keeps the decisions behind the October 2026 tool-system refactor that the code cannot show: why each fact has the owner it has, the alternatives design review rejected, and the limits that remain.

## Plugins: one published generation, everything else composed

A process has one published plugin generation, and only plugin publication changes it: on the primary the orchestrator's plugin commit (startup, a config reload that changes `plugins:`, `!reload-plugins`, the watcher), in a worker the sandbox runner.
A config's tools, tool hooks, skill roots, and OAuth providers are composed from that generation and the config's own plugin entries and MCP servers, so two configs alive at once (the API's newly loaded one and a draining bot's old one) never overwrite each other's tools.
Before, four process-wide registries were re-synced from whatever config each caller held, so a gateway call during a reload's drain removed a plugin's tools while bots still on the old config built agents with them.

An operation captures its plugin view once and keeps it: a Matrix turn when its tool context is built, the MCP gateway per request, a worker per request.
Agent builds inside a turn, team members built on their first delegation, delegated children, and deferred toolkits use the turn's capture, so a publication mid-turn never puts one generation's tool hooks around another generation's tools.
Hook registries are memoised on the generation by the config's ordered plugin entries, so a config edit that leaves plugin entries alone keeps the published registry object and `HookContext.is_active()` stays true; reordering `plugins:` counts as a plugin change because list order orders equal-priority hooks.

Rejected alternatives, in the order design review rejected them:
- A per-config activation cache that also owned plugin modules: memo entries keyed by config object diverged between the API and the orchestrator after a semantic no-op save, and entries keyed by plugin inputs kept failed candidates cached after their plugin was repaired and mixed MCP-only edits with stale plugin factories.
- Keys of `(config, generation)` for activations that execute plugin code: a stale config re-executed retired plugins after a generation bump.
- Installing candidate modules under their canonical names during validation and restoring them afterwards: a live callback's lazy import in another thread could see the candidate's code before the restore.

Validating a candidate config reuses the published generation's registrations for plugins whose source is unchanged and executes only new or changed plugins, in a separate module namespace through the same loader, then removes that namespace and cancels any task it started.
A separate namespace is what isolates candidate code from live imports, which is why validation still executes new or changed plugins instead of reading them from the registry.
Reload preparation still stages under canonical names on the event loop, so a plugin that needs the running loop at import keeps loading; a concurrent lazy import in a tool thread can briefly see staged code there, as before.

A plugin that fails to load is skipped everywhere, and a plugin that fails part-way has the tasks its executed modules started cancelled before it is skipped.
A reload that fails as a whole (duplicate manifest names, conflicting tool names) publishes nothing, so the previous plugins and their tasks keep running.

Tool availability has one owner, the composed registry, so configs record what was authored and a repaired plugin's tools return after `!reload-plugins` without a config edit.

## One identity per tool call

Who a call acts for is derived from the turn's tool context (`ToolRuntimeContext.execution_identity`), not stored beside it, so `replace()` on a context can never leave a stale copy.
Runners without a Matrix conversation (the OpenAI-compatible API, the MCP gateway, a worker, delegated children of those) bind a detached identity, and every reader asks one accessor that prefers the conversation's identity.
A team member's toolkits are built for the member and its calls run as the member answering through the team's account, so egress-broker attribution and the missing-key "who can help" list name the member; a job's owner fields stay byte-identical because the owner is hashed into its id.
The OpenAI-compatible requester authority (config, memberships, budget) stays a separate value: it is the request's authority, not its identity, and every identity bound inside one request names the same requester.

## Open

- MCP function-name collisions: discovery still fails whole servers for every agent while agent builds hide only colliding functions; enforcing once at build is waiting for the user's decision.
