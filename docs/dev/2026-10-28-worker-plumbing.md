# Worker plumbing: one launch authority, one wire protocol, one launch builder, one call result

This record keeps the decisions behind the October 2026 worker-plumbing refactor that the code cannot show: what each fact's owner is, the alternatives reviews rejected, and the limits that remain.

## Which config a dedicated worker launches with

The worker manager is identified by its backend settings alone, so a config reload never replaces it, and the config a worker launches with comes from one source per process: the config reload lifecycle's last fully applied config.
`mindroom run` binds that source before the API and the bots start and keeps it until the worker manager has shut down; a standalone API binds its last successfully loaded config instead; a second binding is refused.
Until startup records its first config as applied, workers launch from the config startup loaded: no older config precedes it, and refusing workers until the router's first sync would fail OpenAI-compatible and Computer calls that already worked.
Each Docker or Kubernetes provisioning reads the launch once inside the worker's lock and uses it for everything that provisioning does.

Rejected alternatives, in the order design review rejected them:
- Capturing the launch from the config a call's toolkit was built from: a turn still running after a reload would restart a worker back to the old config while newer turns restart it forward.
- The API's committed config as the authority: the API publishes edits and disk reloads before the orchestrator applies them, so workers could adopt a config whose application later fails.
- `orchestrator.config`: it is published before a reload finishes applying, so a reload that fails partway would still reach workers.
- Capturing the launch at the call and keeping a per-manager "newest launch wins" floor with launch generations: a late ensure could still take the floor before the worker's lock and restore an older launch, and the floor did not keep script recovery authority honest.
  Reading the launch inside the worker's lock removes the race instead of guarding it.

A background script run's recovery signature names the authority its worker launches with, so a reload that changes any script agent's authority keeps new script launches fenced until the lifecycle has published the new config as applied, or the reload failed; running scripts' tool calls reopen earlier, once the runtime update has run, because the rest of a reload (MCP sync, entity restarts) can take seconds.
A run reads its launch once admitted, and nothing pins it: an admitted launch finishes before an authority-changing reload proceeds, and any other reload leaves every script agent's authority as it was.
Rejected: pinning the launch a run read and refusing it at ensure when the applied launch had moved on, which failed runs on reloads that did not touch their authority; and ending the fence where the orchestrator published the config to its bots, which let a run start with the authority the reload had just revoked.

Accepted limit: the tool validation snapshot reflects the tool registry loaded when a launch is derived, so a launch derived while a plugin reload is in flight can carry the new registry with the previously applied config; the next derivation after the reload, or after its recovery, corrects it at the cost of one more Docker restart.
The snapshot is validation metadata the trusted primary sends and grants a worker nothing.

A Docker worker's launch hash covers only what that worker sees (its projected config and assets, the tool snapshot, and its startup manifest), because a hash over the whole config restarted every worker, ending its background shells, on edits it cannot observe.

## One typed wire protocol

Both sides import one set of request and response models that refuse unknown fields, and every request names its worker protocol.
Before, the runner's own models ignored fields they did not know, so every protocol bump from 2 to 5 had patched one dropped field after another.
From protocol 6 on, a worker or shared runner refuses a request naming another protocol with both versions, checked before the request's shape because a newer release's request usually carries fields this worker does not know, and an unknown field in a request of the same protocol fails it instead of being dropped.
The primary does not health-check a shared runner's protocol: every request already names its protocol, so only a runner from before protocol 6 can drop fields, a one-release window the upgrade notes cover.

The runner prepares every route in one step with one workspace rule, chosen after characterization tests showed that every request whose `base_dir` is the calling agent's workspace gets the same answer from all six former resolvers; the rule changes answers only where the former resolvers disagreed with one another.
An explicit `base_dir` that fails confinement is refused, never replaced by a fallback.

A worker counts a failure against itself only when its own typed answer says so, or when no typed answer arrived (transport errors, a missing route, a rejected token, a malformed answer); timeouts never do.
Before, the client guessed from HTTP status codes and response details.

Rejected: sending the call's own environment as explicit values applied last on the worker.
It is not equivalent: Agent Vault settings are composed only on the worker, the worker's own PATH entries must stay around a hook's PATH, and pass-through values are deliberately overridable by the workspace script.
The environment fields kept their meaning and were only grouped.

## One launch builder, per-backend rendering

The runner environment and startup manifest of a dedicated worker have one builder; Docker and Kubernetes render it with their own container layouts.
Kept per backend on purpose: knowledge delivery (Docker copies, Kubernetes mounts read-only, as `security-posture.md` requires), the per-worker token derivation (Docker's includes a runtime namespace, so two storage roots sharing one proxy token never share worker tokens), the process command (Docker's image entrypoint already runs `tini`), HOME, operator extra env filtering and telemetry opt-outs (unifying either would change what reaches a worker), and container security settings.
Both backends replace a worker of another protocol once and then fail; Docker pulls its image again only for a worker older than the primary, because no pull provides the newer runtime a newer worker needs.

## One call result for calls outside the model loop

`run_tool_call` returns what a call came to (ok, failed, declined, denied, connection required) with the tool's own value, and the tool-call policy marks the calls it refused itself, so a tool's own failed answer is never mistaken for a plugin decline.
Each origin projects that result into its own answer; their answers stayed as they were, except where they were wrong: the MCP gateway gained a `tool_failed` code for a tool's own failure, and scheduled calls no longer let a synchronous body's late exception replace a cancellation or an OAuth connection prompt.
Completion tracking is not part of `run_tool_call`, because how long a cancelled call's resources must outlive it is the resource owner's decision: a caller that waits in place differs from one that hands a cancelled call's toolkit to a retained cleanup.
