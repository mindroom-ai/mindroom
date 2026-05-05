## Summary

Top duplication candidates for `src/mindroom/runtime_resolution.py` are concentrated around path/scope resolution that is already split across focused modules.
The strongest related overlap is with `tool_system/worker_routing.py`, which resolves execution identities and worker keys that `runtime_resolution.py` wraps with agent policy validation.
Workspace root materialization in `workspaces.py`, memory storage-root resolution in `memory/_policy.py`, and knowledge target key construction in `knowledge/registry.py` are related consumers rather than independent duplicate implementations.
No meaningful duplication found that warrants a refactor.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ResolvedAgentExecution	class	lines 37-49	related-only	ResolvedAgentExecution dataclass execution scope policy worker_key	src/mindroom/tool_system/worker_routing.py:52; src/mindroom/tool_system/worker_routing.py:210; src/mindroom/tool_system/worker_routing.py:240
ResolvedAgentExecution.is_private	method	lines 47-49	related-only	is_private policy.is_private private agent runtime	src/mindroom/agent_policy.py:35; src/mindroom/runtime_resolution.py:67; src/mindroom/agents.py:1174
ResolvedAgentRuntime	class	lines 53-69	related-only	ResolvedAgentRuntime state_root workspace tool_base_dir file_memory_root	src/mindroom/workspaces.py:387; src/mindroom/memory/_policy.py:79; src/mindroom/agents.py:984
ResolvedAgentRuntime.is_private	method	lines 67-69	related-only	is_private policy.is_private runtime private	src/mindroom/runtime_resolution.py:47; src/mindroom/agents.py:1174
ResolvedKnowledgeBinding	class	lines 73-80	related-only	ResolvedKnowledgeBinding storage_root knowledge_path incremental_sync_on_access	src/mindroom/knowledge/registry.py:141; src/mindroom/knowledge/registry.py:162; src/mindroom/knowledge/manager.py:817
_knowledge_refresh_enabled	function	lines 83-89	related-only	watch git refresh enabled incremental sync on access	src/mindroom/knowledge/watch.py:58; src/mindroom/knowledge/watch.py:83; src/mindroom/knowledge/refresh_scheduler.py:50
_resolve_private_scope_root	function	lines 92-105	related-only	private_instance_scope_root_path resolve_relative_path_within_root private scope root	src/mindroom/tool_system/worker_routing.py:403; src/mindroom/workspaces.py:347
resolve_private_requester_scope_root	function	lines 108-125	related-only	resolve_private_requester_scope_root requester_worker_key user_agent resolve_worker_key	src/mindroom/tool_system/worker_routing.py:210; src/mindroom/tool_system/worker_routing.py:383; src/mindroom/agents.py:1174
_resolved_private_state_root	function	lines 128-140	related-only	private state root private scope root agent_name resolve_private_scope_root	src/mindroom/workspaces.py:347; src/mindroom/tool_system/worker_routing.py:403
resolve_agent_execution	function	lines 143-174	related-only	resolve_agent_policy_from_data resolve_worker_execution_scope private requires identity	src/mindroom/agent_policy.py:183; src/mindroom/tool_system/worker_routing.py:210; src/mindroom/workspaces.py:343
resolve_agent_runtime	function	lines 177-256	related-only	agent runtime state_root workspace knowledge links file_memory_root	src/mindroom/workspaces.py:387; src/mindroom/memory/_policy.py:79; src/mindroom/memory/_policy.py:202; src/mindroom/agents.py:984
resolve_knowledge_binding	function	lines 259-316	related-only	knowledge binding private knowledge storage_root knowledge_path refresh target	src/mindroom/knowledge/registry.py:162; src/mindroom/knowledge/registry.py:219; src/mindroom/knowledge/watch.py:58; src/mindroom/knowledge/manager.py:817
```

## Findings

No real duplication found.

`resolve_agent_execution()` overlaps conceptually with `resolve_worker_execution_scope()` in `src/mindroom/tool_system/worker_routing.py:210`, but it adds agent-specific policy derivation from `src/mindroom/agent_policy.py:183` and private-agent validation before returning a runtime-resolution-specific dataclass.
The worker module is the lower-level primitive; `runtime_resolution.py` is the agent materialization facade.
The differences to preserve are the private-agent error messages and the policy-derived execution scope.

`resolve_agent_runtime()` overlaps with `resolve_agent_workspace_from_state_path()` in `src/mindroom/workspaces.py:387` and `_effective_storage_path_for_agent()` in `src/mindroom/memory/_policy.py:79`.
These are related call chains, not duplicate implementations: runtime resolution computes state roots and workspace knowledge links, workspace resolution materializes private/file workspace paths, and memory policy consumes the resulting `state_root`.
The differences to preserve are workspace link maintenance in `runtime_resolution.py:215` and memory-specific file resolution in `src/mindroom/memory/_policy.py:202`.

`resolve_knowledge_binding()` overlaps with knowledge registry target construction in `src/mindroom/knowledge/registry.py:162` and `src/mindroom/knowledge/registry.py:219`.
The registry delegates binding resolution back to `runtime_resolution.py`, then converts the binding into stable published-index and refresh keys.
The differences to preserve are binding-level decisions about private-agent storage and `incremental_sync_on_access`, versus registry-level key construction and metadata paths.

`_knowledge_refresh_enabled()` duplicates no active helper.
Watcher setup in `src/mindroom/knowledge/watch.py:58` and `src/mindroom/knowledge/watch.py:83` repeats local conditions around `watch` and `git`, but those paths choose watcher or poller tasks, while `_knowledge_refresh_enabled()` only answers whether any refresh mechanism exists for access scheduling.

## Proposed Generalization

No refactor recommended.
The code already centralizes runtime materialization in `runtime_resolution.py` and delegates lower-level policy, worker, workspace, memory, and knowledge registry behavior to focused modules.

## Risk/Tests

The main risks are regressions in private-agent scope boundaries, requester-scoped culture roots, workspace knowledge symlink/link maintenance, and private knowledge refresh scheduling.
If this area changes, tests should cover shared, `user`, and `user_agent` scopes; missing requester identities; private workspace paths; private and shared knowledge bases; Git-backed knowledge bases; and file-memory agents that rely on `file_memory_root`.
