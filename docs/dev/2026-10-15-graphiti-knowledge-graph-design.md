# Graphiti knowledge graph and autonomous exploration

Status: implemented; the extraction-quality question below is still open.

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
The count lives in memory, so a restart during an exploration run grants that thread a fresh cap; persisting it was rejected as more machinery than a rare, bounded overrun is worth.

The allowlist rejects, at config load, toolkits that start other agent runs or change the agent itself (`delegate`, `dynamic_tools`, `dynamic_workflow`, `scheduler`, `matrix_message`, `invite_router`, `self_config`, `skill_manage`, `memory`, `compact_context`, `thread_model`, `script`), because a call to one would act outside the allowlist.
Listing a code-executing tool such as `shell` or `python` is allowed and gives exploration everything that tool can reach, as the trust model states for any agent.
Observed tool calls are leads, not permissions: stored tool executions record only the function name, so the prompt lists every observed call and the agent can follow only those its pruned tools allow.
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

MindRoom starts falkordblite 0.10.0's bundled `redis-server` as its own child process instead of using falkordblite's clients.
Evidence: its async client's `close()` skips the shutdown it delegates to the async wrapper, so the server outlives the Python process; its sync client waits up to ten seconds per close and did not always stop the server; and a server whose final save fails, for example after its directory is removed, ignores SIGTERM.
A restart that leaves the old server running would put two servers on one database file, so a start first stops a server left in the same directory, matched by process name and working directory rather than binary path, because an upgrade moves the binary while the old server keeps running.
The server persists with an append-only file, so a kill loses at most a second of writes.

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

Fields and defaults are documented once, in `docs/knowledge-graph.md` and the `exploration` section of `docs/scheduling.md`.

## Ingestion invariants

- Only finished turns are ingested, and a turn waiting for approval holds back every later turn of its conversation, because it finishes in place with its original, earlier creation time and a marker past it would skip it.
- Runs are read from the live session and the compaction archive, because compaction can move runs before ingestion reaches them.
- Episodes are rendered from full messages and tool results, not the skill-learning digest, which shortens older messages and drops older tool results.
- Each episode kind has its own marker, so a kind that keeps failing never repeats the other kind's episodes.
- The marker is the only record of completion: Graphiti's `add_episode(uuid=...)` loads an existing episode and extracts again rather than skipping it, so a crash between the write and the marker re-extracts one episode, which Graphiti's entity and fact resolution merges.
- An episode is dropped only after five unusable model outputs in a row, which Graphiti and the extraction adapter raise as `ValueError`; an outage of the database or the provider never counts, so evidence waits however long the outage lasts.
- A new graph's start is saved when it is first seen, so the backfill window stays anchored even with `backfill_days: 0`.
- The graph name includes the database destination (backend, host, and port, without credentials) and the embedder signature, so moving to another server or embedder starts a graph that ingestion fills again, while rotating a password does not.
- Ingestion spend per pass is bounded by `max_sessions_per_cycle` conversations, each split into episodes of at most `max_episode_chars`; a long conversation's backlog spreads over passes rather than being capped.

## Exploration invariants

- Leads come only from completed conversation turns created after the last run, so a reply in an old thread does not bring back that thread's earlier calls.
- The `@` that starts a name is removed from lead arguments, because the posted prompt is parsed for mentions and a mentioned agent would answer the trusted automation prompt with its ordinary tools.
- The last-run time is saved only when a run ends, so a prompt that never posted offers the same leads again.
- An exploration build has no skills, no generated knowledge search, no background jobs or job controls, and no MCP `call_tool` dispatcher: skill scripts can run code, a call that became a job would finish in a later turn without the exploration restrictions, and a dispatcher reaches remote tools that approval rules see only by their own names.
- An allowlisted toolkit that fails to load leaves exploration with the others instead of failing the run.

## Security

- The graph is a primary-side store fed by worker-produced text (tool results); it is data, never executed, and extraction prompt injection can at worst add wrong facts, which carry their source.
- A shared agent's graph holds what it saw in every requester's conversations, including tool results, and any requester of that agent can query it; this matches shared-agent memory and must be stated on the user page and in `security-posture.md`.
- Exploration prompts list redacted arguments of recent tool calls from the agent's conversations, so everyone who can read the exploration room sees them; operators choose that room accordingly, and private agents post in the requester's own personal room.
- Only top-level, completed Matrix runs are ingested: runs with a `room_id`, not OpenAI-compatible API sessions, dynamic-workflow participant sessions, or team-member runs.
- Private agents get one graph per requester instance and explore as that requester, with that requester's credentials, as automations already do.
- Exploration authority is the allowlist and hidden gated functions, enforced in the agent build, not the prompt.
- Credential-like values are redacted best-effort before ingestion with the shared `redact_sensitive_text`, the single redaction implementation.

## Error handling

- Graphiti calls `load_dotenv()` when it is imported, which would copy the first `.env` above its install into `os.environ`; MindRoom imports it with `PYTHON_DOTENV_DISABLED` set, because it reads `.env` files only deliberately.
- An extraction call the provider rejects raises with the provider's message, so health shows the cause rather than a JSON parse error.
- An agent or private instance whose conversations cannot be read is reported and skipped, so it does not stop ingestion for the others, and stopping the runtime cancels a pass in progress instead of waiting for its extraction calls.

- Missing optional dependency: installed on first use, as other extras are; a failed install fails that pass or lookup and is retried on the next one, with the cause in `/api/health`.
- Extraction failure, timeout, or unavailable database: the conversation keeps its marker and retries later with a growing cooldown; the cause appears in health, not in rooms, and tool lookups return a readable error.
- Exploration check that cannot read sessions posts the standard automation warning.

## Testing

- Unit: lead collection from a seeded session DB (`knowledge_graph` and exploration-thread exclusion, dedup, caps, cursor, mention removal), episode rendering (episode kinds and speakers, conversation vs exploration threads, archived runs, splitting at run boundaries, redaction), markers, retries, drops, outages, and graph naming.
- Seam: an exploration build's whole callable surface is the allowlist plus `knowledge_graph`, with no gated functions, skills, knowledge search, jobs, or dispatchers, while ordinary turns of the same agent are unchanged; the per-thread cap holds across a continuation.
- Integration: a fake store for the worker loop, and real FalkorDB Lite for the store, server lifecycle, and scope isolation, with seeded nodes and edges instead of model extraction.
- Live: the success scenario above with two real sources, checked for correct facts and source links.

## Out of scope

- Teams, and team sessions as an ingestion source.
- Injecting graph facts into every prompt automatically; the agent queries when it needs to.
- Neo4j and Neptune backends; Graphiti supports them, so adding one is a driver choice later.
- A monthly USD cap for exploration; `max_tool_calls`, cron, and model choice bound spend, and `knowledge_graph` usage shows extraction cost.
- Custom entity and edge types; Graphiti's default extraction first, a prescribed ontology only if evaluation shows it is needed.
- Cognee, which the user did not ask for and was not evaluated.

## Limitations

- Redacting or deleting a message does not remove facts already extracted from it; deleting the `knowledge_graph/` storage directory rebuilds the graph from the remaining conversations of the last `backfill_days`.
- A config reload that changes the embedder during a pass can add that pass's remaining episodes to the old graph.

## Open questions

- Whether default Graphiti extraction on a cheap model is accurate enough on real transcripts; the live evaluation decides the default `knowledge_graph.model` and whether custom entity types are needed.
