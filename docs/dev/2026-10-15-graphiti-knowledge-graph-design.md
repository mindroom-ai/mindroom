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

### Evidence enters only from transcripts and tool results

Episodes come from stored sessions, never from a model-callable write function.
Ordinary conversations ingest user, assistant, and tool messages with speaker labels, because a person confirming or correcting the agent is evidence.
A background job follow-up's prompt is MindRoom's instruction to collect the results, not its requester's words, so only its reply is ingested; stored runs record their turn intent so ingestion can tell.
Exploration threads and delegated tasks ingest only tool calls and their results, because their prompts come from an agent, not a person, and their answers are the agent's own synthesis.
Other automation threads (`dreaming`, `prompt_curation`, plugin automations) are not ingested.
An automation turn inside an ordinary conversation thread adds no conversation lines, because its prompt is an automation's and not a person's, but its tool results are still ingested like any turn's.
A thread's kind is derived from the trusted hook source of its root prompt (`automation/exploration`, other `automation/` sources, or none), not from a record written after the run, so ingestion classifies a thread correctly however early it reaches it.
Calls to the `knowledge_graph` tool and their results, including lookups through a shell, are never ingested and never become leads, or extracted claims would return as fresh tool evidence; the `reasoning` tool's `think` and `analyze`, subagents', workflow participants', and Claude Code's answers, and results collected from background jobs, which no longer name the tool that produced them, are left out because they are or can be a model's prose.
Each message is joined onto one line, because Graphiti reads every dialogue line as a new speaker's, so a line break in someone's message cannot put words in another person's mouth.

Every fact is an attributed claim, not a verified truth.
Graphiti records which episodes support a fact, so provenance is per episode, and each episode has one kind:

- `conversation`: the run's user and assistant messages as a Graphiti `message` episode, each line prefixed with its speaker, so extraction attributes assertions to whoever made them.
- `tool_results`: the run's tool calls and results as a `text` episode, an observation of the sources the model chose to query.

Search results return the kinds and origins of each fact's episodes, so the agent can tell an observation from something it or a person said.

Rejected: a `record_evidence` tool the agent calls during exploration.
It lets a model paraphrase, omit, or invent evidence, and its own summaries would then return as fresh evidence on later runs.

### Exploration runs as a visible automation turn

Exploration reuses the automation runner: the check in code decides whether to run, and the run is a normal turn in a new thread, so tool execution keeps worker routing, credentials, file access, tool hooks, usage tracking, and the stop control.

Rejected: a silent out-of-band agent run, like the skill-learning reviewer.
Building an agent's full live toolset outside a turn would duplicate worker routing, credentials, and hooks; the delegation-child machinery is bound to a parent tool call.
Rejected: silent scheduled delivery.
Approval cards and the stop control need a visible thread, and the thread is the audit trail.

### Exploration is a prompt, with the agent's own tools and approval rules

An exploration turn is an ordinary turn started by a trusted automation prompt: the agent has its usual tools, and approval rules apply as in any turn, so a call that needs approval waits in the exploration thread for a private agent's requester, and a shared agent's is denied because no person asked for its run, which a live run on 2026-10-16 confirmed.
The prompt asks the agent to only read and to stay within `max_tool_calls`; the agent's `max_tool_calls_per_turn` is the enforced limit, and approval rules are what stop writes, as for any turn that reads untrusted content.

Rejected: restricting exploration turns in code with an operator allowlist, hidden approval-gated functions, and a per-thread call cap.
Every capability that runs or defers work beside a turn's own tool list became a way around the allowlist: background jobs, skills, knowledge search, delegation, and the OAuth MCP `call_tool` dispatcher, which reaches any remote tool by name.
Hiding the dispatcher also left exploration unable to reach an OAuth MCP server after a restart, because such a server lists its concrete functions only once its catalog is cached.
Five review rounds each found another escape, and the boundary duplicated what approval rules already decide.

### One database, one graph per scope, scope chosen in code

FalkorDB keeps each Graphiti `group_id` in its own graph.
The scope is derived from the agent name, or for a private agent from the requester's private instance, hashed so no Matrix ID appears in a graph name.
No tool function takes a `group_id`; the model can never name another scope.
The embedded database file lives in primary-only storage at `<storage_root>/knowledge_graph/`, outside every workspace.
The server's socket sits in its own temporary directory because a long storage path could push it past the 108-byte socket-path limit.
The graph name also carries the effective embedder signature that names Mem0 collections, so changing the embedder starts a new graph that ingestion rebuilds from stored sessions, as Mem0 switches collections.

Rejected: an external FalkorDB server.
Review found that it needs a namespace per installation, because installations sharing a server would read each other's graphs, plus credential and TLS handling in the URL and a separate reset procedure; nothing needs it yet, so the embedded server is the only backend.
Rejected: one embedded database file per agent or private instance.
FalkorDB Lite starts one Redis server process per file, and per-scope graphs in one server already isolate data that code addresses.

MindRoom starts falkordblite 0.10.0's bundled `redis-server` as its own child process instead of using falkordblite's clients.
Evidence: its async client's `close()` skips the shutdown it delegates to the async wrapper, so the server outlives the Python process; its sync client waits up to ten seconds per close and did not always stop the server; and a server whose final save fails, for example after its directory is removed, ignores SIGTERM.
A restart that leaves the old server running would put two servers on one database file, so a start first stops a server left in the same directory, matched by process name and working directory rather than binary path, because an upgrade moves the binary while the old server keeps running.
The server persists with an append-only file, so a kill loses at most a second of writes.

### Graphiti's models come from MindRoom config

Graphiti's `LLMClient` and `EmbedderClient` are thin adapters over MindRoom's own model and embedder construction (`get_model_instance` with a structured-output Agno run, and `create_configured_embedder` for `memory.embedder`), so every provider, credential rule, and base URL MindRoom supports works without a second mapping.

Rejected: mapping MindRoom providers onto Graphiti's own provider clients.
It duplicates key precedence and base-URL rules per provider, covers fewer providers, and gives no usage accounting.
Search uses Graphiti's hybrid search with reciprocal-rank fusion and no cross-encoder, so a query costs no LLM call.
Graphiti builds an OpenAI reranker when none is passed, which needs an OpenAI key, so MindRoom passes a reranker that is never called by the search recipes it uses.
An entity lookup tries a case-insensitive exact name first (the entity with the most facts when several match) and falls back to the hybrid search, because the search can rank an entity that merely mentions a name above the entity named exactly that: `graph_entity("Marcella")` returned her investment account although a `Marcella` entity with 293 facts existed.
A fuzzy match returns the best hit and names the next four, so the agent can look up the one it meant.

Graphiti needs structured output; small local models may fail extraction, which the ingestion worker reports and retries rather than hides.

## Ingestion invariants

- Only finished turns are ingested, and a turn waiting for approval holds back every later turn of its conversation, because it finishes in place with its original, earlier creation time and a marker past it would skip it.
- Runs are read from the whole run log rather than the loaded session, because a loaded session holds only the runs after the latest compaction summary.
- Episodes are rendered from full messages and tool results, not the skill-learning digest, which shortens older messages and drops older tool results.
- Each episode kind has its own marker, so a kind that keeps failing never repeats the other kind's episodes.
- The marker is the only record of completion: Graphiti's `add_episode(uuid=...)` loads an existing episode and extracts again rather than skipping it, so a crash between the write and the marker re-extracts one episode, which Graphiti's entity and fact resolution merges.
- An episode is dropped after five unusable model outputs in a row, which only the extraction adapter reports, so a setup error such as an unsupported embedder waits like an outage, or after three days of failing, so a request the provider always refuses stops costing calls while a shorter outage loses nothing.
- A run larger than `max_episode_chars` is split into several episodes, and the marker records each piece, added or dropped, so a retry never repeats a piece and no tool result is cut away; a tool result is at most half an episode, so a split never cuts one.
- Failure counts belong to the failing episode, named by its kind and where that kind stopped, so new turns of the other kind that extract fine never keep a failing one from being dropped.
- Turns share an episode only with turns of the same day and requester, so relative dates resolve against the day they were said and each episode's extraction is charged to one person.
- A newly seen conversation starts at the backfill cutoff, and one conversation adds at most twenty completed episodes per pass, so a long history spreads over passes instead of holding up every other conversation.
- A paused turn stops holding back its conversation once the approval it waits for has expired, counted from its newest message, which is the call waiting for approval; the conversation is looked at again then even without new messages.
- A new graph's start is saved when it is first seen, so the backfill window stays anchored even with `backfill_days: 0`.

## Exploration invariants

- Leads come only from completed conversation turns created after the last run, so a reply in an old thread does not bring back that thread's earlier calls.
- Every `@` in lead arguments becomes `＠`, which no mention parser reads, because the posted prompt is parsed for mentions and a mentioned agent would answer the trusted automation prompt.
- The last-run time is saved only when a run ends, so a prompt that never posted offers the same leads again.

## Security

- The graph is a primary-side store fed by worker-produced text (tool results); it is data, never executed, and extraction prompt injection can at worst add wrong facts, which carry their source.
- The intentional behaviors a reviewer must not "fix", such as a shared agent's graph answering every requester, are listed in `docs/architecture/security-posture.md`.
- Exploration prompts list redacted arguments of recent tool calls from the agent's conversations, so everyone who can read the exploration room sees them; a shared agent's operator must choose that room, and private agents post in the requester's own personal room.
- Ingestion skips turns derived from events the agent's journal saw redacted, because stored history drops a redacted turn only when the agent next replies there; it waits while the agent's bot is not running, since it cannot tell then.
  Exploration's lead check runs in a thread without the journal, so a redacted turn's calls can be listed until that reply.

## Error handling

- Graphiti calls `load_dotenv()` when it is imported, which would copy the first `.env` above its install into `os.environ`; MindRoom imports it with `PYTHON_DOTENV_DISABLED` set, because it reads `.env` files only deliberately.
- A failed install, extraction, or database call is retried on a later pass, with its kind in `/api/health` and its cause in the log, never in a room; one agent, private instance, or conversation that fails does not stop the others.

## Out of scope

- Teams, and team sessions as an ingestion source.
- Injecting graph facts into every prompt automatically; the agent queries when it needs to.
- External FalkorDB, Neo4j, and Neptune backends; see the rejected external server above.
- A monthly USD cap for exploration; the agent's `max_tool_calls_per_turn`, cron, and model choice bound spend, and `knowledge_graph` usage shows extraction cost.
- Custom entity and edge types; Graphiti's default extraction first, a prescribed ontology only if evaluation shows it is needed.
- Cognee, which the user did not ask for and was not evaluated.

## Limitations

- Redacting or deleting a message does not remove facts already extracted from it; deleting the `knowledge_graph/` storage directory rebuilds the graph from the remaining conversations of the last `backfill_days`.
- A config reload that changes the embedder during a pass can add that pass's remaining episodes to the old graph.

## Open questions

- Whether default Graphiti extraction on a cheap model is accurate enough on real transcripts; a labeled evaluation decides the default `knowledge_graph.model` and whether custom entity types are needed.
  A live run on 2026-10-16 with GPT-6 Luna, Gemini 3.8 Flash, and GPT-6 Luna through Codex extracted release, pull request, and author facts that matched their GitHub sources and superseded a changed owner, but missed a moved deadline and stored an agent's restatement of graph lookups as new conversation facts; extraction made about 15 model calls per episode.
