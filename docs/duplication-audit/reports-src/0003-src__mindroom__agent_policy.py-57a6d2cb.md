## Summary

Top duplication candidates for `src/mindroom/agent_policy.py`:

- `src/mindroom/api/credentials.py:331` repeats execution-scope string parsing/coercion for dashboard query parameters, overlapping with `_coerce_worker_scope`.
- `src/mindroom/knowledge/registry.py:118` duplicates the private knowledge base ID prefix and private-ID classification that `agent_policy.py` uses to create and resolve synthetic private knowledge base IDs.
- `src/mindroom/config/main.py:1174`, `src/mindroom/runtime_resolution.py:143`, and `src/mindroom/api/main.py:570` are consumers of the canonical policy helpers rather than independent duplicates.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
AgentPolicySeed	class	lines 21-30	related-only	AgentPolicySeed policy seed authored fields delegate_to private worker_scope	src/mindroom/config/main.py:1453; src/mindroom/runtime_resolution.py:274; src/mindroom/api/main.py:570
ResolvedAgentPolicy	class	lines 34-46	related-only	ResolvedAgentPolicy effective_execution_scope scope_label private_workspace_enabled	src/mindroom/runtime_resolution.py:41; src/mindroom/api/credentials.py:379; src/mindroom/config/main.py:1181
ResolvedAgentPolicyIndex	class	lines 50-55	related-only	ResolvedAgentPolicyIndex policies delegation_closures private_targets_by_agent	src/mindroom/api/main.py:570
_coerce_worker_scope	function	lines 58-61	duplicate-found	worker_scope shared user user_agent execution_scope cast	src/mindroom/api/credentials.py:331
_coerce_private_scope	function	lines 64-67	none-found	private.per user user_agent private scope coercion	none
_build_agent_policy_seed	function	lines 70-117	related-only	AgentConfig private knowledge delegate_to worker_scope draft payload model_dump	src/mindroom/api/main.py:567; src/mindroom/config/main.py:1181; src/mindroom/runtime_resolution.py:149
build_agent_policy_seeds	function	lines 120-133	related-only	build_agent_policy_seeds config.agents default_worker_scope	src/mindroom/config/main.py:1216; src/mindroom/config/main.py:1453; src/mindroom/runtime_resolution.py:274; src/mindroom/api/main.py:570
_resolved_scope_and_source	function	lines 136-143	related-only	scope_label private.per worker_scope defaults.worker_scope unscoped	src/mindroom/api/credentials.py:317; src/mindroom/config/main.py:1189
dashboard_credentials_supported_for_scope	function	lines 146-148	related-only	dashboard credential management supports unscoped shared user user_agent	src/mindroom/api/credentials.py:496; src/mindroom/api/tools.py:213; src/mindroom/config/main.py:642
_resolve_agent_policy	function	lines 151-178	related-only	private_knowledge_base_id private_workspace_enabled private_agent_knowledge_enabled effective scope	src/mindroom/config/main.py:1174; src/mindroom/runtime_resolution.py:143; src/mindroom/api/credentials.py:379
resolve_agent_policy_from_data	function	lines 181-198	related-only	resolve_agent_policy_from_data get_agent_execution_scope get_agent_scope_label private knowledge	src/mindroom/config/main.py:1181; src/mindroom/config/main.py:1196; src/mindroom/config/main.py:1206; src/mindroom/runtime_resolution.py:149
get_agent_delegation_closure	function	lines 201-230	related-only	delegation closure transitive delegate_to pending reachable closures	src/mindroom/config/main.py:1446; src/mindroom/api/openai_compat.py:326
get_private_team_targets	function	lines 233-251	related-only	private team targets reachable private delegation	src/mindroom/config/main.py:1462; src/mindroom/teams.py:900
get_unsupported_team_agents	function	lines 254-270	related-only	unsupported team agents unknown private targets team eligibility	src/mindroom/config/main.py:1478; src/mindroom/teams.py:900
_team_eligibility_reason	function	lines 273-290	related-only	team eligibility reason private agents cannot participate delegates to private	src/mindroom/api/main.py:577; src/mindroom/teams.py:1007
unsupported_team_agent_message	function	lines 293-313	related-only	unsupported team agent message private agents cannot participate via delegation	src/mindroom/config/main.py:1500; src/mindroom/config/main.py:1527; src/mindroom/teams.py:1007
resolve_agent_policy_index	function	lines 316-346	related-only	resolve policy index private_targets_by_agent team_eligibility_reason policies	src/mindroom/api/main.py:570
resolve_private_knowledge_base_agent	function	lines 349-368	duplicate-found	private knowledge base prefix startswith removeprefix __agent_private__	src/mindroom/config/main.py:357; src/mindroom/knowledge/registry.py:118; src/mindroom/knowledge/registry.py:789; src/mindroom/runtime_resolution.py:274
```

## Findings

### 1. Execution-scope string coercion is repeated for dashboard query parameters

`src/mindroom/agent_policy.py:58` accepts only `"shared"`, `"user"`, and `"user_agent"` and casts those raw values to `WorkerScope`.
`src/mindroom/api/credentials.py:331` parses the dashboard `execution_scope` query parameter with the same accepted worker-scope string set, plus the query-only `"unscoped"` sentinel and HTTP error handling.

The shared behavior is the raw worker-scope membership check and typed cast.
The differences to preserve are important: dashboard query parsing must distinguish absent or empty values from explicit `"unscoped"` and must raise `HTTPException` for invalid user input, while `_coerce_worker_scope` silently returns `None` for draft payload coercion.

### 2. Private knowledge base ID prefix is duplicated outside the policy source of truth

`src/mindroom/agent_policy.py:17` defines `DEFAULT_PRIVATE_KNOWLEDGE_BASE_ID_PREFIX`, `_resolve_agent_policy` creates IDs with that prefix at `src/mindroom/agent_policy.py:159`, and `resolve_private_knowledge_base_agent` resolves IDs by checking and removing the prefix at `src/mindroom/agent_policy.py:356`.
`src/mindroom/config/main.py:357` independently defines the same string as `PRIVATE_KNOWLEDGE_BASE_ID_PREFIX`.
`src/mindroom/knowledge/registry.py:118` independently defines `_PRIVATE_KNOWLEDGE_BASE_ID_PREFIX` and `_published_index_key_is_private` classifies private published indexes with `startswith()` at `src/mindroom/knowledge/registry.py:789`.

The shared behavior is identifying synthetic private knowledge base IDs by prefix.
The differences to preserve are that `Config.PRIVATE_KNOWLEDGE_BASE_ID_PREFIX` is the active configured prefix passed into policy helpers, while `knowledge/registry.py` only classifies published index cache entries and does not know the owning agent.

## Proposed Generalization

For execution-scope coercion, a small public helper in `mindroom.agent_policy` or `mindroom.tool_system.worker_routing` could expose the allowed worker-scope value set and typed coercion.
`resolve_dashboard_execution_scope_override` should keep its HTTP-level absent, `"unscoped"`, and invalid-input handling.

For private knowledge base IDs, prefer one exported prefix constant or predicate.
The lowest-risk direction is for `knowledge/registry.py` to import the same prefix used by config/policy, or for a small `is_private_knowledge_base_id(base_id: str, *, prefix: str = ...)` helper to live beside `resolve_private_knowledge_base_agent`.
No production refactor is recommended in this audit because the task forbids production edits and both duplications are small.

## Risk/tests

Refactoring execution-scope coercion risks changing API behavior for empty query parameters, `"unscoped"`, or invalid values.
Tests should cover `resolve_dashboard_execution_scope_override`, draft agent policy payload coercion in `/api/config/agent-policies`, and credential/tool availability behavior for `None`, `"shared"`, `"user"`, and `"user_agent"`.

Refactoring private knowledge base prefix handling risks breaking synthetic private knowledge base lookup, cache pruning, and workspace knowledge binding.
Tests should cover `Config.get_agent_private_knowledge_base_id`, `Config.get_private_knowledge_base_agent`, `runtime_resolution.resolve_knowledge_binding`, and `knowledge.registry.prune_private_index_bookkeeping` for private and non-private base IDs.
