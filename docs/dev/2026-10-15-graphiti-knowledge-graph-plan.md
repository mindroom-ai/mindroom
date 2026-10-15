# Graphiti Knowledge Graph and Exploration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with the skill named in **Execution**. Steps use checkbox (`- [ ]`) syntax.

**Execution:** baspowers:executing-plans — the user prefers inline implementation with a fresh-context reviewer per phase and a final three-model review, and later tasks depend tightly on earlier interfaces.

**Goal:** Per-agent Graphiti knowledge graph fed from conversations and tool results, queried through a read-only tool, and grown by an unattended `exploration` automation restricted in code.

**Architecture:** A runtime-wide store owns one Graphiti client over embedded FalkorDB Lite, with one graph per agent or private instance chosen in code.
A background worker turns completed Matrix runs (live and archived) into `conversation` and `tool_results` episodes.
The `exploration` automation posts a trusted prompt whose turn is built with a persona-style tool allowlist, gated functions hidden, and a per-thread call cap.

**Tech Stack:** Python 3.12+, `graphiti-core==0.31.1` with the `falkordblite` extra, Agno 3.0.9, pydantic, SQLite session storage.

**Spec:** `docs/dev/2026-10-15-graphiti-knowledge-graph-design.md`

## Global Constraints

- Optional dependency: extra `knowledge_graph = ["graphiti-core[falkordblite]==0.31.1"]`; import `graphiti_core`, `falkordb`, and `redislite` only inside functions with `# noqa: PLC0415`, and add `graphiti_core` and `falkordb` to `_HEAVY_OPTIONAL_RUNTIME_ROOTS` in `tests/test_import_graph.py`.
- `GRAPHITI_TELEMETRY_ENABLED=false` in `VENDOR_TELEMETRY_ENV_VALUES`.
- No model-callable function writes to the graph and no function accepts a `group_id`.
- Never ingest `knowledge_graph` tool calls or results; exploration threads contribute only `tool_results` episodes; other automation threads contribute nothing.
- Only top-level, completed Matrix runs (with `run.metadata["room_id"]`) are ingested.
- Redaction uses `redact_sensitive_text` / `redact_sensitive_data` from `mindroom.redaction`; clip text before redacting so input stays under its 64 KiB limit.
- Usage kind `knowledge_graph`.
- New package `src/mindroom/knowledge_graph/` with a docstring-only `__init__.py`; update `tach.toml` and run `uv run tach check --dependencies --interfaces`.
- Docs: one sentence per line; only sentences that answer a user question; owning page `docs/knowledge-graph.md`.
- Tests: `uv run pytest tests/<file>.py -x -n 0 --no-cov -v` inside `nix-shell shell.nix`, with `TMPDIR=/work/tmp`.

## Review Focus

- A tool result far larger than 64 KiB (for example a fetched web page): it is clipped to `max_tool_result_chars` before redaction, never replaced wholesale by `[redaction failed]` (Task 4).
- Graphiti extraction fails for one session (a small model returns invalid structured output): that session backs off and the worker still ingests the other sessions in the cycle (Task 6).
- The operator changes `memory.embedder`: a new graph name is used, and ingestion restarts within `backfill_days` instead of reusing the old graph's markers (Tasks 3 and 6).
- A person replies inside an exploration thread: their turn runs with the agent's normal tools, because the policy follows the turn's own hook source (Task 9).
- Compaction moves runs into the archive between two cycles: they are neither skipped nor ingested twice (Task 5).

---

## Phase 1: Graph, ingestion, and tool

### Task 1: Configuration

**Files:**
- Create: `src/mindroom/config/knowledge_graph.py`
- Modify: `src/mindroom/config/main.py` (root field near `memory` at :446, validator beside `validate_agent_helper_models` at :1990, `_agent_knowledge_graph` beside `_agent_memory_backend` at :1850)
- Modify: `src/mindroom/config/agent.py:291` (agent field), `src/mindroom/config/models.py:436` (defaults field), `src/mindroom/config/entity_view.py` (property)
- Regenerate: `frontend/src/test/fixtures/config-schema.json` (pre-commit `generate-config-schema`)
- Test: `tests/test_knowledge_graph_config.py`

**Interfaces:**
- Produces: `KnowledgeGraphIngestConfig(interval_seconds: int = 600 [ge 30], idle_seconds: int = 120 [ge 0], max_sessions_per_cycle: int = 10 [ge 1], max_episode_chars: int = 24000 [ge 1000, le 60000], max_tool_result_chars: int = 4000 [ge 100], backfill_days: int = 7 [ge 0])`; `KnowledgeGraphConfig(backend: Literal["falkordb_lite", "falkordb"] = "falkordb_lite", url: str | None = None, model: str | None = None, ingest: KnowledgeGraphIngestConfig)`, both `extra="forbid"` with field descriptions; `Config.knowledge_graph: KnowledgeGraphConfig`; `AgentConfig.knowledge_graph: bool | None = None`; `DefaultsConfig.knowledge_graph: bool = False`; `Config._agent_knowledge_graph(agent_name: str) -> bool`; `Config.knowledge_graph_agents() -> list[str]`; `ResolvedEntityView.knowledge_graph: bool`.

- [ ] **Step 1: Write failing tests**

```python
def test_agent_inherits_knowledge_graph_from_defaults(): ...  # defaults True, agent unset -> _agent_knowledge_graph("a") is True; agent False overrides
def test_enabled_graph_requires_known_model(): ...  # agent enables, knowledge_graph.model None -> ValidationError "knowledge_graph.model"; model "nope" -> "Unknown knowledge_graph model"
def test_falkordb_backend_requires_url_and_lite_rejects_url(): ...
def test_unknown_knowledge_graph_field_rejected(): ...
def test_knowledge_graph_agents_lists_enabled_agents_only(): ...
```

- [ ] **Step 2: Run, expect FAIL** — `uv run pytest tests/test_knowledge_graph_config.py -x -n 0 --no-cov -v`
- [ ] **Step 3: Implement** the models and `Config` validator `validate_knowledge_graph(self) -> Config` (only when `knowledge_graph_agents()` is non-empty: `model` required and in `self.models`; `falkordb` requires `url`; `falkordb_lite` rejects `url`).
- [ ] **Step 4: Run tests, expect PASS; regenerate the schema snapshot; run `tests/test_config_schema.py`.**
- [ ] **Step 5: Commit** `Add knowledge graph configuration`

### Task 2: Run metadata carries the hook source, and the archive becomes readable

**Files:**
- Modify: `src/mindroom/response_turn.py:167-213` (`build_matrix_run_metadata` gains `hook_source`), `src/mindroom/response_turn.py:282` (`ResponseTurnContext.hook_source: str | None = None`), `src/mindroom/response_runner.py:3886` (`_agent_turn_context` sets it from `request.response_envelope.hook_source`), `src/mindroom/ai.py:1368-1381` (passes `ctx.hook_source`)
- Modify: `src/mindroom/history/archive.py` (reader), `src/mindroom/history/storage.py` (facade)
- Test: `tests/test_run_hook_source_metadata.py`, `tests/test_history_archive_reader.py`

**Interfaces:**
- Produces: run metadata key `"hook_source"` (omitted when `None`); `history.storage.load_archived_runs(storage: BaseDb, session_id: str) -> list[RunOutput | TeamRunOutput]` in archive order, skipping tombstones (`run_data IS NULL`).

- [ ] **Step 1: Failing tests**

```python
def test_matrix_run_metadata_records_hook_source(): ...  # build_matrix_run_metadata(..., hook_source="automation/exploration")["hook_source"] == "automation/exploration"; None -> key absent
def test_agent_turn_context_carries_envelope_hook_source(): ...  # ResponseRunner seam
def test_load_archived_runs_returns_compacted_runs_in_order(): ...  # compact a seeded session, then load_archived_runs -> run_ids in archive order
def test_load_archived_runs_skips_redacted_tombstones(): ...
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement**; the reader reuses the `SELECT run_type, run_data ... ORDER BY id` shape from `roll_back_to` (archive.py:318) filtered by `session_id`.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `Persist the turn's hook source on runs and read archived runs`

### Task 3: Scopes and the Graphiti store

**Files:**
- Create: `src/mindroom/knowledge_graph/__init__.py`, `scope.py`, `clients.py`, `store.py`
- Modify: `pyproject.toml` (extra), `src/mindroom/runtime_env_policy.py:128` (telemetry key), `tests/test_import_graph.py:41-60`, `tach.toml`
- Test: `tests/test_knowledge_graph_scope.py`, `tests/test_knowledge_graph_store.py`

**Interfaces:**
- Produces in `scope.py`: `@dataclass(frozen=True) GraphScope(agent_name: str, execution_identity: ToolExecutionIdentity | None, requester_id: str | None, graph_name: str)`; `graph_scope(agent_name: str, config: Config, runtime_paths: RuntimePaths, execution_identity: ToolExecutionIdentity | None) -> GraphScope`; `graph_scopes(config: Config, runtime_paths: RuntimePaths) -> list[GraphScope]` (shared graph agents, plus each `private_instances_for_agent` requester with `build_requester_execution_identity`, skipping `None` and aliased requesters as `automations/runner.py:105-131` does); `graph_name(scope_key: str, embedder_signature: tuple[str, ...]) -> str` returning `"kg_" + sha256(scope_key + "|" + "|".join(sig)).hexdigest()[:24]`, where `scope_key` is the agent name or `resolve_agent_execution(...).worker_key` and `sig` is `effective_mem0_embedder_signature(...)`.
- Produces in `store.py`: dataclasses `FactSource(kind: Literal["conversation", "tool_results"], description: str, observed_at: datetime)`, `GraphFact(fact: str, valid_at: datetime | None, invalid_at: datetime | None, sources: list[FactSource])`, `GraphEntity(name: str, summary: str, facts: list[GraphFact])`, `EpisodeInput(kind: Literal["conversation", "tool_results"], body: str, source_description: str, reference_time: datetime)`; `class GraphStore(Protocol)` with `async add_episode(scope: GraphScope, episode: EpisodeInput, usage: UsageTarget) -> None`, `async search_facts(scope: GraphScope, query: str, limit: int) -> list[GraphFact]`, `async entity(scope: GraphScope, name: str) -> GraphEntity | None`, `async close() -> None`; `GraphitiStore(config_provider: Callable[[], Config], runtime_paths: RuntimePaths)` implementing it; `knowledge_graph_failure() -> str | None` and `record_knowledge_graph_failure(detail: str | None) -> None` (module state for `/api/health`).
- Produces in `clients.py`: `@dataclass(frozen=True) UsageTarget(agent_name: str, session_id: str, execution_identity: ToolExecutionIdentity | None)`; `MindRoomLLMClient(LLMClient)` whose `_generate_response` runs `Agent(model=get_model_instance(config, runtime_paths, config.knowledge_graph.model, execution_identity), output_schema=response_model, telemetry=False)` and calls `record_helper_usage(..., kind="knowledge_graph")` for the `UsageTarget` held in a `ContextVar` set by `add_episode`; `MindRoomEmbedder(EmbedderClient)` over `create_configured_embedder`; `UnusedCrossEncoder(CrossEncoderClient)` whose `rank` raises `RuntimeError`, passed because Graphiti otherwise builds an OpenAI reranker.

- [ ] **Step 1: Failing tests**

```python
def test_graph_name_is_stable_and_hides_scope(): ...  # same inputs -> same name; "@alice:example.org" not in name; matches ^kg_[0-9a-f]{24}$
def test_embedder_change_changes_graph_name(): ...
def test_private_instances_get_distinct_graphs(): ...  # two seeded private instances -> two GraphScopes with different graph_name
def test_llm_client_returns_structured_output_and_records_usage(): ...  # fake Agno model returns schema instance; dict equals model_dump(); record_helper_usage called with kind="knowledge_graph"
def test_search_facts_maps_edges_and_episode_sources(): ...  # fake driver results -> GraphFact with sources kind parsed from source_description prefix
def test_unused_cross_encoder_raises(): ...
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** `GraphitiStore` opens lazily under an `asyncio.Lock`: `ensure_tool_deps(["graphiti-core"], "knowledge_graph", runtime_paths)`, then `AsyncFalkorDB(dbfilename=str(storage_root / "knowledge_graph" / "graph.db"))` or `FalkorDriver(host, port, username, password)` parsed from `url`; builds Graphiti with the three adapters; calls `build_indices_and_constraints()` once per graph name; searches with `COMBINED_HYBRID_SEARCH_RRF` scoped by `group_ids=[scope.graph_name]`; `source_description` starts with `"conversation:"` or `"tool_results:"` so `FactSource.kind` is parsed back. Reload: rebuild the client when `knowledge_graph` or `memory.embedder` config changes.
- [ ] **Step 4: Run, expect PASS; run `tests/test_import_graph.py` and `tach check`.**
- [ ] **Step 5: Opt-in integration test** `tests/test_knowledge_graph_falkordb_lite.py` marked to skip without `MINDROOM_KG_LIVE=1`: add one episode against a local OpenAI-compatible model and find its fact.
- [ ] **Step 6: Commit** `Add the Graphiti knowledge graph store`

### Task 4: Episode rendering

**Files:**
- Create: `src/mindroom/knowledge_graph/episodes.py`
- Test: `tests/test_knowledge_graph_episodes.py`

**Interfaces:**
- Consumes: `EpisodeInput` (Task 3), run metadata `hook_source` (Task 2).
- Produces: `ThreadKind = Literal["conversation", "exploration", "automation"]`; `classify_thread(runs: Sequence[RunOutput], automation_thread_ids: set[str]) -> ThreadKind` (first run's `hook_source`: `"automation/exploration"` is exploration, other `automation/` is automation; no hook source and `metadata["thread_id"]` in `automation_thread_ids` is automation; else conversation); `@dataclass(frozen=True) RenderedEpisode(episode: EpisodeInput, last_run: RunKey)` with `RunKey = tuple[int, str]` (`created_at`, `run_id`); `render_episodes(runs: Sequence[RunOutput], *, kind: ThreadKind, agent_name: str, settings: KnowledgeGraphIngestConfig) -> list[RenderedEpisode]`; constant `KNOWLEDGE_GRAPH_FUNCTIONS = frozenset({"search_graph", "graph_entity"})`.

- [ ] **Step 1: Failing tests**

```python
def test_conversation_runs_render_speaker_lines_and_separate_tool_episode(): ...  # body lines "person @alice:x: ..." / "agent mind: ..."; second episode kind "tool_results" with "mcp_github.get_issue {...}" and result
def test_exploration_thread_renders_only_tool_results(): ...
def test_automation_thread_renders_nothing(): ...
def test_knowledge_graph_calls_are_never_rendered(): ...
def test_oversized_tool_result_is_clipped_before_redaction(): ...  # 200_000-char result -> clipped to max_tool_result_chars, no "[redaction failed]"
def test_credentials_are_redacted(): ...  # "sk-..." style token assembled at runtime -> absent from body
def test_episodes_split_only_at_run_boundaries(): ...  # three runs whose total exceeds max_episode_chars -> two episodes; last_run of first equals key of second run
def test_single_run_over_cap_is_clipped(): ...
def test_classify_thread_uses_root_hook_source_then_automation_threads(): ...
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement**; prompts are `<msg event_id=.. from=..>` tags (`prompt_message_tags.py:13`), so take the speaker from `from=` and strip the tag; `source_description` is `f"{kind}: room {room_id} thread {thread_id}"` plus, for tool results, `"; calls: " + ", ".join(f"{tool}.{function} {arg_digest}")` with `arg_digest` a 12-char sha256 of the canonical JSON arguments.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `Render conversations and tool results as graph episodes`

### Task 5: Session run reader

**Files:**
- Create: `src/mindroom/knowledge_graph/session_runs.py`
- Test: `tests/test_knowledge_graph_session_runs.py`

**Interfaces:**
- Consumes: `load_archived_runs` (Task 2), `RunKey` (Task 4), `create_session_storage`, `get_agent_session`.
- Produces: `updated_sessions(storage: BaseDb, agent_name: str, *, since: int) -> list[tuple[str, int]]` (`get_sessions(session_type=SessionType.AGENT, component_id=agent_name, sort_by="updated_at", sort_order="asc", include_runs=False)` filtered to `updated_at > since`, skipping ids containing `":dynamic_workflow:"`); `completed_runs(storage: BaseDb, session_id: str, *, after: RunKey | None) -> list[RunOutput]` merging live and archived runs, deduplicated by `run_id`, kept only when `is_model_history_visible_run`, `status == RunStatus.completed`, and `metadata.get("room_id")`, sorted by `(created_at, run_id)`, strictly after `after`.

- [ ] **Step 1: Failing tests**

```python
def test_completed_runs_merge_live_and_archive_without_duplicates(): ...
def test_runs_moved_to_archive_between_reads_are_not_skipped(): ...  # read after=None, compact, read after=marker -> only newer runs
def test_non_matrix_and_member_runs_are_excluded(): ...  # OpenAI-compat session, parent_run_id set, status error
def test_updated_sessions_filters_by_updated_at(): ...
```

- [ ] **Step 2–4: FAIL, implement, PASS.**
- [ ] **Step 5: Commit** `Read completed Matrix runs for graph ingestion`

### Task 6: Ingestion worker and runtime wiring

**Files:**
- Create: `src/mindroom/knowledge_graph/ingestion.py`
- Modify: `src/mindroom/orchestrator.py` (fields beside `_memory_auto_flush_*` at :413, sync in `_sync_runtime_support_services` at :1071, shutdown step at :2501), `src/mindroom/usage/storage.py:10-25` (`"knowledge_graph"` kind), `src/mindroom/api/main.py:810` (health block)
- Test: `tests/test_knowledge_graph_ingestion.py`

**Interfaces:**
- Consumes: Tasks 3–5.
- Produces: `@dataclass KnowledgeGraphIngestionWorker(runtime_paths: RuntimePaths, config_provider: Callable[[], Config | None], store: GraphStore)` with `async run() -> None`, `stop() -> None`, `async run_cycle(config: Config) -> None`; `wake_knowledge_graph_ingestion() -> None`; state file `<storage_root>/knowledge_graph/ingestion/<graph_name>.json` holding per session `{"updated_at": int, "marker": [created_at, run_id] | null, "failures": int, "retry_at": int}` and per graph `"started_at"`, written with `mindroom.durable_write`; orchestrator `self._knowledge_graph_store: GraphitiStore | None` shared with the tool (Task 7) through `knowledge_graph_store() -> GraphStore | None` set at startup.

- [ ] **Step 1: Failing tests** (fake `GraphStore` recording calls; seeded session DBs)

```python
def test_cycle_ingests_new_runs_and_advances_marker(): ...
def test_marker_survives_restart_and_runs_are_not_reingested(): ...
def test_failing_session_backs_off_and_others_still_ingest(): ...  # store raises for session A -> B ingested; A retry_at grows; failure in knowledge_graph_failure()
def test_active_sessions_wait_for_idle_seconds(): ...
def test_max_sessions_per_cycle_is_respected(): ...
def test_new_graph_backfills_only_recent_sessions(): ...  # sessions older than backfill_days skipped on a graph's first cycle
def test_embedder_change_starts_new_graph_state(): ...
def test_wake_runs_a_cycle_before_the_interval(): ...
```

- [ ] **Step 2–4: FAIL, implement, PASS.** Per scope: open storage with `create_session_storage(agent, config, runtime_paths, execution_identity=scope.execution_identity)` in `asyncio.to_thread`; `classify_thread` with `automation_threads(runtime_paths)`; for each `RenderedEpisode` call `store.add_episode`, then persist the marker; exponential cooldown 60 s doubling to 3600 s.
- [ ] **Step 5: Commit** `Ingest conversations into the knowledge graph in the background`

### Task 7: `knowledge_graph` tool and Phase 1 docs

**Files:**
- Create: `src/mindroom/tools/knowledge_graph.py` (metadata: `file_access=ToolFileAccess.NONE`, `dependencies=["graphiti-core"]`, `function_names=("search_graph", "graph_entity")`), `src/mindroom/custom_tools/knowledge_graph.py`
- Modify: `src/mindroom/tools/__init__.py:20-29`, `src/mindroom/agents.py:790` (direct-toolkit branch beside `memory`), `src/mindroom/tool_system/dynamic_toolkits.py:170` (`_special_tool_names` appends `"knowledge_graph"` when `config._agent_knowledge_graph(agent_name)`, as `skill_learning` implies `skill_manage`), `pyproject.toml` (extra group sync), `src/mindroom/tools_metadata.json` (regenerate per `tests/test_tools_metadata.py:163`)
- Create: `docs/knowledge-graph.md`; Modify: `zensical.toml` nav, `docs/configuration/index.md`, `docs/configuration/agents.md`, `docs/usage.md:45-54`, `docs/architecture/code-map.md`, `docs/architecture/security-posture.md`
- Test: `tests/test_knowledge_graph_tool.py`

**Interfaces:**
- Consumes: `graph_scope`, `GraphStore`, `knowledge_graph_store()`.
- Produces: `KnowledgeGraphTools(agent_name: str, config: Config, runtime_paths: RuntimePaths, execution_identity: ToolExecutionIdentity | None)` with `async search_graph(query: str, limit: int = 10) -> str` and `async graph_entity(name: str) -> str`; each fact rendered with validity and `kind: description (observed_at)` sources; readable error strings, never raised.

- [ ] **Step 1: Failing tests**

```python
def test_search_graph_uses_callers_scope_only(): ...  # private requester A's tool queries only A's graph_name
def test_search_graph_renders_validity_and_sources(): ...
def test_tool_absent_when_agent_graph_disabled(): ...
def test_store_unavailable_returns_readable_error(): ...
def test_limit_is_clamped(): ...  # limit 0 -> 1, 500 -> 50
```

- [ ] **Step 2–4: FAIL, implement, PASS; regenerate `tools_metadata.json`.**
- [ ] **Step 5: Docs.** `docs/knowledge-graph.md`: what it is, enable (`knowledge_graph:` section, `agents.<name>.knowledge_graph`), each field once with type/default, what is ingested and what is not, the tool, cost (`knowledge_graph` usage kind), privacy (shared vs private agents), changing the embedder. Security posture: new primary-side store fed by worker output, exploration authority.
- [ ] **Step 6: Run `uv run pre-commit run --all-files` and the touched test files.**
- [ ] **Step 7: Commit** `Add the knowledge_graph tool and document the knowledge graph`
- [ ] **Step 8: Phase 1 review** by a fresh-context reviewer on the Phase 1 diff; fix confirmed findings.

## Phase 2: Exploration

### Task 8: `exploration` automation config

**Files:**
- Modify: `src/mindroom/config/automations.py` (class, `BUILTIN_AUTOMATION_NAMES`, `_automation_kind` tuple at :158, union at :164), `src/mindroom/config/main.py` (`_automation_block_reason` at :1868, `validate_agent_automations` at :1894, `_describe_automation_union` at :2108)
- Test: `tests/test_exploration_config.py`

**Interfaces:**
- Produces: `ExplorationAutomation(_ScheduledAutomation)` with `requires_file_memory: ClassVar[bool] = False`, `name: Literal["exploration"]`, `cron: str = "30 2 * * *"`, `tools: list[str]` (min length 1), `max_tool_calls: int = 40 [ge 1, le 1000]`, `max_leads: int = 20 [ge 0, le 200]`, `revisit_days: int = 7 [ge 1]`; `EXPLORATION_RESTRICTED_TOOLKITS = frozenset({"delegate", "dynamic_tools", "dynamic_workflow", "scheduler", "matrix_message", "invite_router", "self_config", "skill_manage", "memory", "compact_context", "thread_model", "script"})`; `Config._agent_exploration(agent_name: str) -> ExplorationAutomation | None`.

- [ ] **Step 1: Failing tests**

```python
def test_exploration_requires_knowledge_graph(): ...  # block reason names knowledge_graph; inherited default skipped
def test_exploration_tools_must_be_agent_tools(): ...  # "mcp_github" not in agent tools -> error; "mcp_github.get_issue" with mcp_github present -> ok; "knowledge_graph" always ok
def test_exploration_rejects_restricted_toolkits(): ...  # "scheduler", "delegate.delegate_task"
def test_exploration_parses_from_name_mapping(): ...
```

- [ ] **Step 2–4: FAIL, implement, PASS; regenerate the schema snapshot.** Toolkit part is `entry.partition(".")[0]`, checked against `self._agent_available_tools(agent_name)` plus `"knowledge_graph"`.
- [ ] **Step 5: Commit** `Add the exploration automation configuration`

### Task 9: Exploration turn policy

**Files:**
- Create: `src/mindroom/automations/exploration_policy.py`
- Modify: `src/mindroom/response_turn.py:319` (`ResponseTurnContext.exploration`), `src/mindroom/response_runner.py:3886` (derive), `src/mindroom/ai.py:1128-1148` (pass to `create_agent`), `src/mindroom/agents.py` (`create_agent` param beside `persona` at :2027; persona block at :2107-2133; `build_entry` at :1601-1640; `tool_call_limit` at :2314), `src/mindroom/tool_system/runtime_context.py:147` (`ToolRuntimeContext.exploration`), `src/mindroom/tool_system/tool_hooks.py:752-791` (cap check)
- Test: `tests/test_exploration_policy.py`

**Interfaces:**
- Consumes: `Config._agent_exploration` (Task 8).
- Produces: `@dataclass(frozen=True) ExplorationPolicy(tools: tuple[str, ...], max_tool_calls: int)`; `exploration_policy_for_turn(config: Config, agent_name: str, hook_source: str | None) -> ExplorationPolicy | None` (only for `"automation/exploration"`; `tools` gains `"knowledge_graph"`); `claim_exploration_call(agent_name: str, thread_key: str, cap: int) -> bool` over an in-memory bounded counter (LRU of 256 threads); `create_agent(..., exploration: ExplorationPolicy | None = None)`.

- [ ] **Step 1: Failing tests** (at the `create_agent` / `_assemble_agent_toolkits` seam and the bridge)

```python
def test_exploration_build_keeps_only_allowlisted_functions(): ...  # toolkit "file" listed as "file.read_file" -> only read_file survives; unnamed toolkits absent
def test_exploration_build_hides_gated_functions(): ...  # approval rule on a listed function -> function absent, not requires_confirmation
def test_ordinary_turn_of_same_agent_is_unrestricted(): ...
def test_human_reply_in_exploration_thread_is_unrestricted(): ...  # hook_source None -> policy None
def test_thread_cap_holds_across_continuations(): ...  # claim 40 times True, 41st False; bridge declines with the declined-result text
def test_tool_call_limit_is_min_of_turn_and_exploration_cap(): ...
```

- [ ] **Step 2–4: FAIL, implement, PASS.** In `create_agent`, treat `exploration.tools` exactly as `persona.tools` for `persona_tool_policy` and `_keep_persona_functions` (one code path for both; a persona and an exploration policy never coexist, assert it); in `build_entry`, pass `supports_native_tool_approval=supports_native_tool_approval and exploration is None` and `approvals_as_jobs=approvals_as_jobs and exploration is None`; the bridge check runs before plugin before-hooks and keys on `(agent_name, resolved_thread_id or session_id)`.
- [ ] **Step 5: Commit** `Restrict exploration turns to their allowlist and call cap`

### Task 10: `exploration` check, prompt, and follow-up

**Files:**
- Create: `src/mindroom/automations/exploration.py`
- Modify: `src/mindroom/automations/registry.py:95-102` (`_builtin_definitions`), `tach.toml`
- Modify: `docs/scheduling.md` (new `### exploration` section; built-ins list), `docs/knowledge-graph.md` (link), `docs/architecture/code-map.md`
- Test: `tests/test_exploration_automation.py`

**Interfaces:**
- Consumes: `ExplorationAutomation`, `updated_sessions`, `completed_runs`, `classify_thread`, `KNOWLEDGE_GRAPH_FUNCTIONS`, `wake_knowledge_graph_ingestion`.
- Produces: `@automation("exploration") check_exploration(ctx: AutomationContext) -> Ask | None`; `@dataclass(frozen=True) Lead(function: str, arguments: str, count: int, last_seen: int)`; `collect_leads(ctx: AutomationContext, *, since: int, limit: int) -> list[Lead]`; `EXPLORATION_PROMPT_TEMPLATE` (overridable like `DREAMING_PROMPT_TEMPLATE`); state `ctx.state_dir / "exploration.json"` with `last_run_at`.

- [ ] **Step 1: Failing tests**

```python
def test_no_leads_and_recent_run_posts_nothing(): ...  # returns None
def test_revisit_days_elapsed_runs_without_leads(): ...
def test_leads_are_distinct_counted_newest_first_and_capped(): ...
def test_leads_exclude_graph_calls_and_exploration_threads(): ...
def test_lead_arguments_are_redacted(): ...
def test_prompt_names_max_tool_calls_and_read_only_rule(): ...
def test_follow_up_wakes_ingestion_and_resolves_thread(): ...  # then(config, thread_id, False) -> Done(resolve=(thread_id,)), on_loop wakes worker
```

- [ ] **Step 2–4: FAIL, implement, PASS.** Prompt: what the graph is for, the lead list (`function(arguments) — N times, last <date>`), "check what `knowledge_graph` already knows first", "only read; never create, change, send, or delete anything", "you can make at most {max_tool_calls} tool calls", "end with what you checked and what is still unknown".
- [ ] **Step 6: Docs**: `### exploration` in `docs/scheduling.md` with its field table, needs, room choice and visibility of leads, and what enters the graph.
- [ ] **Step 7: Run `uv run pre-commit run --all-files` and the full test suite** (`just test-backend`).
- [ ] **Step 8: Commit** `Add the exploration automation`
- [ ] **Step 9: Phase 2 review** by a fresh-context reviewer on the whole branch; fix confirmed findings.

## Phase 3: Live verification

### Task 11: Live run with real sources

- [ ] **Step 1:** Use the `live-test` skill to boot the local stack with an agent that has `knowledge_graph: true`, two sources (one MCP server and one ordinary tool), and `exploration` on a one-minute cron in a dedicated room.
- [ ] **Step 2:** Hold a short conversation that uses both tools; wait for ingestion by polling the ingestion state file for the session's marker.
- [ ] **Step 3:** Let one exploration run fire; confirm in the thread that only allowlisted functions were called and the cap held.
- [ ] **Step 4:** Ask the agent the success question; check each fact against its listed source.
- [ ] **Step 5:** Record evidence (no private data) for the PR description; shred live-test credentials per the cleanup routine.
