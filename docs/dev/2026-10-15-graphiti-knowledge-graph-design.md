# Graphiti knowledge graph and autonomous exploration

Status: design, not implemented.

## Intent

What the user asked for:

- Support [Graphiti](https://github.com/getzep/graphiti), a temporal context graph, in MindRoom.
- Enable it per agent, with exploration budgets.
- The agent sees every conversation it had with people, including which tools were called and what they returned.
- The agent calls those tools itself, on a background loop, to keep a graph that represents reality.
- Data sources include MCP servers and ordinary tools.

Assumptions, open to correction:

- "Represents reality" means entities and relationships (people, organizations, projects, documents, tasks, events) with validity windows and a source for every fact, answerable as "what connects these, who owns what, what changed".
- The graph complements the existing memory backends; it does not replace `mem0` or `file` memory.
- Background means unattended on a schedule, not invisible: an audit trail the user can read and stop is acceptable.
- A first version needs agents, not teams.

Success: an agent with the graph enabled and two real sources (for example an MCP server and GitHub) answers "what connects project A and project B, who owns what, and what changed this week?" with correct facts and links to the conversations or tool results behind them.
A labeled evaluation of extracted facts against their sources measures precision before any claim of factual reliability, because extraction and entity matching can be wrong.

## Decisions

### Three units with one store

1. **Graph store**: one Graphiti instance per runtime over an embedded FalkorDB Lite database by default, or an external FalkorDB server.
2. **Passive ingestion**: a background worker turns new conversation content, including tool calls and their results, into Graphiti episodes in code, without an agent turn.
3. **Active exploration**: a built-in `exploration` automation asks the agent, on a cron schedule, to call an operator-allowlisted set of its tools; ingestion then records the raw tool results as evidence.

The agent reads the graph through a read-only `knowledge_graph` tool.
Nothing the model writes in prose enters the graph as evidence from an exploration run, so the agent cannot launder its own summaries into facts.

### Evidence enters only from transcripts and tool results

Episodes come from stored sessions, never from a model-callable write function.
Ordinary conversations ingest user, assistant, and tool messages with speaker labels, because a person confirming or correcting the agent is evidence.
Exploration threads ingest only tool calls and their results, because the assistant text there is the agent's own synthesis.
Other automation threads (`dreaming`, `prompt_curation`, plugin automations) are not ingested.
A thread's kind is derived from the trusted hook source of its root prompt (`automation/exploration`, other `automation/` sources, or none), not from a record written after the run, so ingestion classifies a thread correctly however early it reaches it.
Stored runs do not carry that source today, so the run metadata gains it when the turn starts.
Calls to the `knowledge_graph` tool and their results are never ingested and never become leads, or extracted claims would return as fresh tool evidence.

Every fact is an attributed claim, not a verified truth.
Graphiti records which episodes support a fact, so provenance is per episode, and each episode has one kind:

- `conversation`: the run's user and assistant messages as a Graphiti `message` episode, each line prefixed with its speaker (`person <Matrix ID>` or `agent <name>`), so extraction attributes assertions to whoever made them.
- `tool_results`: the run's tool calls and results as a `text` episode, an observation of the sources the model chose to query.

Each episode's `source_description` starts with its kind and names its origin: the room and thread, and for tool results the tool, function, and an argument digest for each call, plus the time of the run.
Search results return the kinds and origins of each fact's episodes, so the agent can tell an observation from something it or a person said.

Rejected: a `record_evidence` tool the agent calls during exploration.
It lets a model paraphrase, omit, or invent evidence, and its own summaries would then return as fresh evidence on later runs.

### Exploration runs as a visible automation turn

Exploration reuses the automation runner: the check in code decides whether to run, and the run is a normal turn in a new thread, so tool execution keeps worker routing, credentials, file access, tool hooks, usage tracking, and the stop control.
Operators point it at a dedicated room; private agents post in the requester's personal room, as other automations do.

Rejected: a silent out-of-band agent run, like the skill-learning reviewer.
Building an agent's full live toolset outside a turn would duplicate worker routing, credentials, and hooks; the delegation-child machinery is bound to a parent tool call.
Rejected: silent scheduled delivery.
Approval cards and the stop control need a visible thread, and the thread is the audit trail.

### Code enforces what exploration may call

An exploration turn is marked by its trusted automation hook source, and its agent build:

- keeps only the functions named in the entry's required `tools` allowlist, plus the `knowledge_graph` tool, through the same pruning that subagent personas use;
- hides every approval-gated function, as minimal delegation children do, so an unattended run never waits on a card;
- caps tool calls at the entry's `max_tool_calls` for the whole exploration thread.

The existing per-turn cap counts each Agno response loop afresh, so a continuation would reset it; exploration instead counts executed calls per exploration thread and declines calls past the cap, and the restrictions apply to every continuation and deferred tool in that thread.

Observed tool calls from conversations become leads only when their function is on the allowlist.
Prompt text is never the boundary: a document the agent reads can contain instructions, and the allowlist plus hidden gated functions bound what such an injection can do.

Rejected: defaulting the allowlist to every function seen in conversations.
A function a person drove while watching, such as sending mail, is not one an unattended run should call.

### One database, one graph per scope, scope chosen in code

FalkorDB keeps each Graphiti `group_id` in its own graph.
The scope is derived from the agent name, or for a private agent from the requester's private instance, hashed so no Matrix ID appears in a graph name.
No tool function takes a `group_id`; the model can never name another scope.
The embedded database file lives in primary-only storage at `<storage_root>/knowledge_graph/`, outside every workspace.
FalkorDB Lite's server opens no TCP port and listens on a Unix socket in its own `0700` temporary directory, which keeps the socket path under the 108-byte limit that a long storage path could exceed; worker containers cannot reach it.
An external FalkorDB server is the operator's to keep off worker networks; graph names alone do not isolate a connection that a worker could open.
The graph name also carries the effective embedder signature that names Mem0 collections, so changing the embedder starts a new graph that ingestion rebuilds from stored sessions, as Mem0 switches collections.

Rejected: one embedded database file per agent or private instance.
FalkorDB Lite starts one Redis server process per file, and per-scope graphs in one server already isolate data that code addresses.

### Graphiti's models come from MindRoom config

`knowledge_graph.model` names a `models` entry.
Graphiti's `LLMClient` and `EmbedderClient` are thin adapters over MindRoom's own model and embedder construction (`get_model_instance` with a structured-output Agno run, and `create_configured_embedder` for `memory.embedder`), so every provider, credential rule, and base URL MindRoom supports works without a second mapping.
Each extraction call is an Agno run, so its tokens are recorded through the same helper-usage path as auto-flush.

Rejected: mapping MindRoom providers onto Graphiti's own provider clients.
It duplicates key precedence and base-URL rules per provider, covers fewer providers, and gives no usage accounting.
Search uses Graphiti's hybrid search with reciprocal-rank fusion and no cross-encoder, so a query costs no LLM call.
Graphiti builds an OpenAI reranker when none is passed, which needs an OpenAI key, so MindRoom passes a reranker that is never called by the search recipes it uses.
Extraction tokens are recorded as helper usage of kind `knowledge_graph`, attributed to the agent and the conversation's requester.
`GRAPHITI_TELEMETRY_ENABLED=false` joins the vendor telemetry opt-outs.

Graphiti needs structured output; small local models may fail extraction, which the ingestion worker reports and retries rather than hides.

### Graphiti is optional

`graphiti-core[falkordblite]` is an optional extra installed on first use, imported only inside the store module, and kept out of the slim import surfaces that `tests/test_import_graph.py` pins.

## Configuration

```yaml
knowledge_graph:
  backend: falkordb_lite        # or falkordb
  url: null                     # falkordb only, e.g. redis://graph:6379
  model: luna                   # a models key; required when any agent enables the graph
  ingest:
    interval_seconds: 600
    idle_seconds: 120
    max_sessions_per_cycle: 10
    max_episode_chars: 24000
    max_tool_result_chars: 4000

agents:
  mind:
    knowledge_graph: true       # passive ingestion and the knowledge_graph tool
    automations:
      - name: exploration
        cron: "0 */6 * * *"
        room: "#mind-exploration"
        tools: [mcp_github, mcp_linear.list_issues, web_search]
        max_tool_calls: 40
        max_leads: 20
        revisit_days: 7
        model: luna
```

- `agents.<name>.knowledge_graph` (bool, default `false`, inheritable from `defaults`) turns on ingestion and gives the agent the `knowledge_graph` tool.
- `exploration` requires `knowledge_graph: true`; `tools` entries are toolkit names or `toolkit.function`, must be on the agent's own tool list, and must be non-empty.
- `max_tool_calls` (default `40`, min 1) caps one run, and the agent's own `max_tool_calls_per_turn` still applies when lower.
- `max_leads` (default `20`) caps observed tool calls listed in one prompt; `revisit_days` (default `7`) runs a pass without new leads once that long has passed since the last run.

## Data flow

### Ingestion worker

1. Every `interval_seconds`, for each graph-enabled agent and private instance, list sessions updated since that scope's cursor.
2. Skip sessions active within `idle_seconds`, and threads that automations other than `exploration` started.
3. For each session, up to `max_sessions_per_cycle`, read the completed runs after the session's ingested-run marker from both the live session and the compaction archive, because compaction can move runs before ingestion reaches them.
4. Render them with a dedicated episode renderer, not the skill-learning digest, which shortens older messages and drops older tool results: every message in full with its speaker, tool calls with arguments, each tool result clipped to `max_tool_result_chars`, credential-like values redacted with the shared `redact_sensitive_text`, and consecutive runs merged into `conversation` and `tool_results` episodes of at most `max_episode_chars`, split only at run boundaries; a single run larger than the cap is clipped.
5. Call `add_episode` for each episode with `reference_time` from its last run and the scope's `group_id`, then advance the marker past that episode's runs.
6. The marker is the only record of completion; Graphiti's `add_episode(uuid=...)` loads an existing episode and extracts again rather than skipping it, so no UUID is passed, and a crash between the write and the marker update re-extracts one episode, whose entities and facts Graphiti's resolution merges with the first copy.
7. A failure keeps the marker, logs, and retries the session next cycle with a growing cooldown.

Ingestion spend is bounded by `max_sessions_per_cycle` sessions of at most `max_episode_chars` per episode each `interval_seconds`, independent of exploration's `max_tool_calls`.

The exploration automation's final step wakes the worker so an exploration thread is ingested without waiting a full interval.

### Exploration automation

1. The check, in code, collects leads: distinct allowlisted `(function, arguments)` calls in the agent's conversations since the last run, with counts and last-seen times, newest first, capped at `max_leads`.
2. With no leads and `revisit_days` not elapsed, it returns `None` and costs nothing.
3. Otherwise it asks the agent, in a new thread, to: check what the graph knows with `knowledge_graph`, re-read the sources behind the leads for changes, follow references into containers not yet seen, and look for owners, dates, and links the graph lacks, using only read operations, then end with a short report of what it checked and what remains unknown.
4. When the run ends, the step wakes ingestion, posts a notice with the tool calls made, and resolves the thread.

`exploration` does not require file memory, unlike the other built-ins.

### Queries

`knowledge_graph` functions, all scoped to the caller's graph:

- `search_graph(query, limit=10)` returns facts with `valid_at`, `invalid_at` when superseded, and their source episodes' provenance, descriptions, and times.
- `graph_entity(name)` returns the best-matching entity's summary and its current and superseded facts.

The tool runs in the primary process and touches no files.

## Security

- The graph is a primary-side store fed by worker-produced text (tool results); it is data, never executed, and extraction prompt injection can at worst add wrong facts, which carry their source.
- A shared agent's graph holds what it saw in every requester's conversations, including tool results, and any requester of that agent can query it; this matches shared-agent memory and must be stated on the user page and in `security-posture.md`.
- Private agents get one graph per requester instance and explore as that requester, with that requester's credentials, as automations already do.
- Exploration authority is the allowlist and hidden gated functions, enforced in the agent build, not the prompt.
- Credential-like values are redacted best-effort before ingestion with the shared `redact_sensitive_text`, the single redaction implementation.

## Error handling

- Missing optional dependency: install on first use, as other extras do; failure disables ingestion and the tool for that runtime with one logged error and a `/api/health` entry.
- Extraction failure or timeout: the session keeps its marker and retries later; repeated failure appears in health, not in rooms.
- Database unavailable: tool calls return a readable error; ingestion pauses and retries.
- Exploration check that cannot read sessions posts the standard automation warning.

## Testing

- Unit: lead collection from a seeded session DB (allowlist filtering, `knowledge_graph` exclusion, dedup, caps), episode rendering (episode kinds and speakers, conversation vs exploration threads, archived runs, splitting at run boundaries, redaction), marker advance and retry, scope derivation, graph-name hashing, and the embedder fingerprint.
- Seam: exploration turns build with only allowlisted functions and no gated functions, and ordinary turns of the same agent are unchanged; the per-thread cap holds across a continuation.
- Integration: a fake Graphiti client behind the store protocol for the worker loop; one opt-in test against real FalkorDB Lite and a local OpenAI-compatible model.
- Live: the success scenario above with two real sources, checked for correct facts and source links.

## Out of scope

- Teams, and team sessions as an ingestion source.
- Injecting graph facts into every prompt automatically; the agent queries when it needs to.
- Neo4j and Neptune backends; Graphiti supports them, so adding one is a driver choice later.
- A monthly USD cap for exploration; `max_tool_calls`, cron, and model choice bound spend, and `knowledge_graph` usage shows extraction cost.
- Custom entity and edge types; Graphiti's default extraction first, a prescribed ontology only if evaluation shows it is needed.
- Cognee, which the user did not ask for and was not evaluated.

## Open questions

- Whether default Graphiti extraction on a cheap model is accurate enough on real transcripts; the live evaluation decides the default `knowledge_graph.model` and whether custom entity types are needed.
