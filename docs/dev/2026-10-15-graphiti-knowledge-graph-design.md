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
Each episode's `source_description` names its origin: the room and thread for a conversation, or the tool, function, and argument digest for a tool result, plus the time it was observed.

Rejected: a `record_evidence` tool the agent calls during exploration.
It lets a model paraphrase, omit, or invent evidence, and the other reviewer's concern about re-ingesting summaries applies directly.

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
- caps tool calls at the entry's `max_tool_calls`, through the existing per-turn tool-call cap.

Observed tool calls from conversations become leads only when their function is on the allowlist.
Prompt text is never the boundary: a document the agent reads can contain instructions, and the allowlist plus hidden gated functions bound what such an injection can do.

Rejected: defaulting the allowlist to every function seen in conversations.
A function a person drove while watching, such as sending mail, is not one an unattended run should call.

### One database, one graph per scope, scope chosen in code

FalkorDB keeps each Graphiti `group_id` in its own graph.
The scope is derived from the agent name, or for a private agent from the requester's private instance, hashed so no Matrix ID appears in a graph name.
No tool function takes a `group_id`; the model can never name another scope.
The embedded database lives in primary-only storage at `<storage_root>/knowledge_graph/`, outside every workspace, so worker code cannot touch it.

Rejected: one embedded database file per agent or private instance.
FalkorDB Lite starts one Redis server process per file, and per-scope graphs in one server already isolate data that code addresses.

### Graphiti's models come from MindRoom config

`knowledge_graph.model` names a `models` entry; MindRoom maps its provider to Graphiti's client (`openai`, `anthropic`, `google`, and `OpenAIGenericClient` for OpenAI-compatible hosts such as OpenRouter and Ollama) with that model's credentials.
The embedder reuses `memory.embedder`, limited to providers with an OpenAI-compatible embeddings API (`openai`, and `ollama` through its `/v1` endpoint); other providers fail config validation when the graph is enabled.
Search uses Graphiti's hybrid search with reciprocal-rank fusion and no cross-encoder, so a query costs no LLM call.
Graphiti's extraction tokens are recorded as helper usage of kind `knowledge_graph`, attributed to the agent and the conversation's requester.
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
3. For each session, up to `max_sessions_per_cycle`, render the completed runs after the session's ingested-run marker with the redacting transcript renderer: speakers labeled, tool calls with arguments, each tool result clipped to `max_tool_result_chars`, the episode clipped to `max_episode_chars` at a run boundary.
4. Call `add_episode` with a deterministic UUID from scope, session, and last run ID, `reference_time` from that run, and the scope's `group_id`; an episode whose UUID already exists is skipped, so a restart between the write and the marker update adds no duplicate.
5. Advance the marker after each episode; a failure keeps the marker, logs, and retries the session next cycle with a growing cooldown.

The exploration automation's final step wakes the worker so an exploration thread is ingested without waiting a full interval.

### Exploration automation

1. The check, in code, collects leads: distinct allowlisted `(function, arguments)` calls in the agent's conversations since the last run, with counts and last-seen times, newest first, capped at `max_leads`.
2. With no leads and `revisit_days` not elapsed, it returns `None` and costs nothing.
3. Otherwise it asks the agent, in a new thread, to: check what the graph knows with `knowledge_graph`, re-read the sources behind the leads for changes, follow references into containers not yet seen, and look for owners, dates, and links the graph lacks, using only read operations, then end with a short report of what it checked and what remains unknown.
4. When the run ends, the step wakes ingestion, posts a notice with the tool calls made, and resolves the thread.

`exploration` does not require file memory, unlike the other built-ins.

### Queries

`knowledge_graph` functions, all scoped to the caller's graph:

- `search_graph(query, limit=10)` returns facts with `valid_at`, `invalid_at` when superseded, and their source episodes' descriptions and times.
- `graph_entity(name)` returns the best-matching entity's summary and its current and superseded facts.

The tool runs in the primary process and touches no files.

## Security

- The graph is a primary-side store fed by worker-produced text (tool results); it is data, never executed, and extraction prompt injection can at worst add wrong facts, which carry their source.
- A shared agent's graph holds what it saw in every requester's conversations, including tool results, and any requester of that agent can query it; this matches shared-agent memory and must be stated on the user page and in `security-posture.md`.
- Private agents get one graph per requester instance and explore as that requester, with that requester's credentials, as automations already do.
- Exploration authority is the allowlist and hidden gated functions, enforced in the agent build, not the prompt.
- Credential-like values are redacted best-effort before ingestion; the transcript renderer's existing redaction is the single implementation.

## Error handling

- Missing optional dependency: install on first use, as other extras do; failure disables ingestion and the tool for that runtime with one logged error and a `/api/health` entry.
- Extraction failure or timeout: the session keeps its marker and retries later; repeated failure appears in health, not in rooms.
- Database unavailable: tool calls return a readable error; ingestion pauses and retries.
- Exploration check that cannot read sessions posts the standard automation warning.

## Testing

- Unit: lead collection from a seeded session DB (allowlist filtering, dedup, caps), episode rendering (role handling for conversation vs exploration threads, clipping at run boundaries, redaction), deterministic UUID and skip-existing on restart, scope derivation and graph-name hashing.
- Seam: exploration turns build with only allowlisted functions and no gated functions, and ordinary turns of the same agent are unchanged; the tool-call cap applies.
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

- Whether stored runs already carry the root prompt's hook source; if not, the run metadata gains it, because classification must not depend on a later record.

- Whether default Graphiti extraction on a cheap model is accurate enough on real transcripts; the live evaluation decides the default `knowledge_graph.model` and whether custom entity types are needed.
- Whether FalkorDB Lite's bundled server starts on NixOS development hosts without `nix-ld`; production images are unaffected.
