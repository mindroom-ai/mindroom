## Summary

One small duplication candidate was found: missing knowledge-base warnings are emitted with the same event text and payload shape in multiple runtime entry points.
No broad refactor is recommended for the knowledge-resolution, refresh-scheduling, notice-formatting, or multi-vector-search logic in `src/mindroom/knowledge/utils.py`; those behaviors are already centralized here and other matches are call sites or adjacent lifecycle paths.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
KnowledgeAvailabilityDetail	class	lines 47-51	related-only	KnowledgeAvailabilityDetail unavailable_bases search_available	src/mindroom/agent_run_context.py:13; src/mindroom/teams.py:57; src/mindroom/api/openai_compat.py:59
KnowledgeResolution	class	lines 55-60	related-only	KnowledgeResolution missing unavailable knowledge_resolution	src/mindroom/teams.py:1221; src/mindroom/custom_tools/delegate.py:102; src/mindroom/api/openai_compat.py:996
_KnowledgeVectorDb	class	lines 63-72	related-only	vector_db search Protocol filters limit	src/mindroom/memory/_shared.py:63; src/mindroom/knowledge/registry.py:108
_KnowledgeVectorDb.search	method	lines 66-72	related-only	def search query limit filters vector_db	src/mindroom/memory/_shared.py:63; src/mindroom/custom_tools/matrix_api.py:1317
_AsyncKnowledgeVectorDb	class	lines 76-85	related-only	async_search Protocol vector_db	src/mindroom/memory/_shared.py:63; src/mindroom/knowledge/utils.py:553
_AsyncKnowledgeVectorDb.async_search	async_method	lines 79-85	related-only	async_search query limit filters	src/mindroom/memory/_shared.py:63; src/mindroom/knowledge/utils.py:567
_lookup_knowledge_for_base	function	lines 88-105	none-found	get_published_index ValueError Published knowledge index lookup failed	src/mindroom/knowledge/registry.py:649; src/mindroom/api/knowledge.py:700
_refresh_schedule_due	function	lines 108-122	none-found	_refresh_scheduled_at refresh_schedule_due cooldown_seconds monotonic	src/mindroom/knowledge/refresh_scheduler.py:46; src/mindroom/knowledge/watch.py:222
_prune_refresh_schedule_bookkeeping	function	lines 125-131	related-only	prune bookkeeping max entries sorted pop oldest	src/mindroom/knowledge/registry.py:800; src/mindroom/approval_manager.py:82; src/mindroom/matrix/message_content.py:351
_published_index_age_seconds	function	lines 134-143	related-only	fromisoformat UTC total_seconds last_published_at	src/mindroom/approval_manager.py:102; src/mindroom/thread_tags.py:107; src/mindroom/scheduling.py:183
_git_poll_interval_seconds	function	lines 146-150	related-only	poll_interval_seconds git_config max float	src/mindroom/knowledge/watch.py:104; src/mindroom/runtime_resolution.py:289
_git_poll_due	function	lines 153-162	related-only	git poll due last_refresh_at last_published_at	src/mindroom/knowledge/watch.py:214; src/mindroom/knowledge/refresh_runner.py:810
_ready_index_effective_availability	function	lines 165-173	none-found	READY STALE git_poll_due effective_availability	src/mindroom/knowledge/registry.py:567; src/mindroom/knowledge/status.py:74
_refresh_cooldown_seconds	function	lines 176-186	none-found	refresh cooldown stale poll_interval retry cooldown	src/mindroom/knowledge/watch.py:230; src/mindroom/knowledge/refresh_scheduler.py:46
_failed_refresh_retry_fingerprint	function	lines 189-222	none-found	credentials_mtime_ns credentials_size git-refresh fingerprint	src/mindroom/knowledge/registry.py:567; src/mindroom/credentials.py
_embedded_userinfo_fingerprint	function	lines 225-231	none-found	embedded_http_userinfo hmac sha256 repo_url	src/mindroom/knowledge/redaction.py; src/mindroom/knowledge/utils.py:203
_refresh_retry_settings	function	lines 234-244	none-found	CONFIG_MISMATCH REFRESH_FAILED indexing_settings retry settings	src/mindroom/knowledge/registry.py:582; src/mindroom/knowledge/refresh_runner.py:636
_schedule_refresh_on_access_cooldown_seconds	function	lines 247-252	none-found	schedule_refresh_on_access cooldown git poll interval	src/mindroom/runtime_resolution.py:313; src/mindroom/knowledge/watch.py:222
_schedule_refresh_on_access_due	function	lines 255-259	none-found	schedule_refresh_on_access_due git_poll_due	src/mindroom/runtime_resolution.py:313; src/mindroom/knowledge/watch.py:222
_schedule_refresh_for_availability	function	lines 262-340	related-only	is_refreshing schedule_refresh availability READY INITIALIZING STALE	src/mindroom/api/knowledge.py:147; src/mindroom/knowledge/watch.py:222; src/mindroom/knowledge/refresh_scheduler.py:46
resolve_agent_knowledge_access	function	lines 343-403	related-only	resolve_agent_knowledge_access get_agent_knowledge_base_ids missing unavailable	src/mindroom/teams.py:1221; src/mindroom/custom_tools/delegate.py:102; src/mindroom/api/openai_compat.py:996
resolve_agent_knowledge_access.<locals>._resolve	nested_function	lines 353-378	none-found	resolved_knowledge lookup availability knowledge cache	src/mindroom/knowledge/registry.py:649; src/mindroom/config/main.py:1225
_stale_availability_notice	function	lines 406-415	none-found	stale availability notice Do not claim searched latest	src/mindroom/agent_run_context.py:21; src/mindroom/api/openai_compat.py:1607
format_knowledge_availability_notice	function	lines 418-459	related-only	format_knowledge_availability_notice Do not claim to have searched	src/mindroom/agent_run_context.py:16; src/mindroom/api/openai_compat.py:1607; src/mindroom/teams.py:1578
KnowledgeAccessSupport	class	lines 463-502	duplicate-found	Knowledge bases not available for agent logger warning resolution missing	src/mindroom/api/openai_compat.py:829; src/mindroom/teams.py:1228
KnowledgeAccessSupport.for_agent	method	lines 470-477	none-found	for_agent resolve_for_agent knowledge property	src/mindroom/bot.py:358; src/mindroom/response_runner.py:356
KnowledgeAccessSupport.resolve_for_agent	method	lines 479-502	duplicate-found	resolve_for_agent missing logger.warning Knowledge bases not available	src/mindroom/api/openai_compat.py:829; src/mindroom/teams.py:1228
MultiKnowledgeVectorDb	class	lines 506-582	none-found	MultiKnowledgeVectorDb multiple vector_db interleave agno Knowledge	src/mindroom/knowledge/registry.py:497; src/mindroom/knowledge/manager.py:1336
MultiKnowledgeVectorDb._resolved_vector_dbs	method	lines 519-521	none-found	resolved_vector_dbs vector_dbs copy	none
MultiKnowledgeVectorDb.exists	method	lines 523-525	related-only	exists create vector_db protocol initialized	src/mindroom/knowledge/registry.py:108; src/mindroom/knowledge/registry.py:612
MultiKnowledgeVectorDb.create	method	lines 527-529	none-found	create no-op vector_db initialized	none
MultiKnowledgeVectorDb.search	method	lines 531-551	none-found	vector database search failed interleave results_by_db	src/mindroom/memory/_mem0_backend.py:80; src/mindroom/custom_tools/matrix_api.py:1317
MultiKnowledgeVectorDb.async_search	async_method	lines 553-582	none-found	async_search asyncio.gather NotImplementedError vector_db	src/mindroom/knowledge/manager.py:1632; src/mindroom/hooks/execution.py:356
MultiKnowledgeVectorDb.async_search.<locals>._search_one	nested_async_function	lines 562-579	none-found	async_search fallback NotImplementedError warning vector_db_type	src/mindroom/knowledge/manager.py:1632; src/mindroom/custom_tools/coding.py:503
_interleave_documents	function	lines 585-603	none-found	interleave documents results_by_db while len merged limit	src/mindroom/api/openai_compat.py:1873; src/mindroom/tool_system/events.py:265
_merge_knowledge	function	lines 606-621	none-found	merge Knowledge vector_db max_results MultiKnowledgeVectorDb	src/mindroom/knowledge/registry.py:497; src/mindroom/knowledge/manager.py:1336
```

## Findings

### 1. Missing knowledge-base warnings are duplicated

`KnowledgeAccessSupport.resolve_for_agent` logs `"Knowledge bases not available for agent"` when `KnowledgeResolution.missing` is non-empty at `src/mindroom/knowledge/utils.py:496`.
The OpenAI-compatible path builds the same warning through `_log_missing_knowledge_bases` at `src/mindroom/api/openai_compat.py:829` and calls it after `resolve_agent_knowledge_access` at `src/mindroom/api/openai_compat.py:1008`.
Team materialization emits the same warning shape for team agents at `src/mindroom/teams.py:1228`, with only the event text changed to `"Knowledge bases not available for team agent"`.

The shared behavior is "after resolving knowledge, warn once with agent name and missing base IDs".
The differences to preserve are the logger field name (`agent_name` versus `agent` in OpenAI compatibility) and the team-specific event text.

### Non-findings

The availability notice formatting is centralized in `format_knowledge_availability_notice` and reused by `agent_run_context`, Matrix/team paths, delegation, and OpenAI-compatible paths.
The refresh-on-access logic in `utils.py` is related to watcher/API scheduling, but it has distinct trigger semantics: request-path cooldowns and stale fallback versus explicit mutation/watch scheduling.
The multi-vector search adapter has no equivalent implementation elsewhere under `src`; other search methods operate on memory or Matrix APIs, not merged Agno knowledge vector DBs.

## Proposed Generalization

Introduce a tiny helper near `KnowledgeResolution`, for example `log_missing_knowledge_bases(logger, agent_name, missing, *, event="Knowledge bases not available for agent", agent_field="agent_name")`, and use it from `KnowledgeAccessSupport`, `api/openai_compat.py`, and `teams.py`.
This would remove repeated warning construction without changing resolution behavior.

No refactor is recommended for scheduling, availability notice text, vector DB protocol wrappers, or knowledge merging.

## Risk/tests

The logging helper would be low risk, but tests or log assertions that expect exact field names need attention.
Coverage should include `KnowledgeAccessSupport.resolve_for_agent`, OpenAI-compatible agent completions with missing bases, and team member materialization with missing bases.
No production code was edited for this audit.
