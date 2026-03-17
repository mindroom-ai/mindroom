# Backend Architecture Audit

This is a living audit document for the private-runtime and explicit-execution refactor work.

The goal is to identify which backend seams now have a real source of truth, which seams are still merely consistent, and which seams are still too complex or too duplicated to be considered merge-and-forget.

This document is intentionally stricter than a normal review.

The standard here is not "works today".

The standard is "AI-assisted maintenance will keep doing the right thing because the clean path is obvious and the wrong path is hard to reintroduce."

Internal backward compatibility is not a goal of this follow-up work.

There are no library users that justify keeping old internal wrappers, aliases, alternate call paths, or compatibility branches alive.

If a cleaner source of truth replaces an older internal shape, the older shape should be deleted instead of preserved.

## Evaluation Standard

- One source of truth beats repeated local policy.
- Explicit inputs beat ambient state.
- Small shared helpers beat repeated partial reconstruction.
- Clean call flow beats clever reuse.
- Deletion beats internal compatibility shims.
- A seam is only "done" when a future contributor can follow it without rediscovering hidden rules.

## Audit Checklist

- [x] Runtime resolution.
- [x] Execution identity ingress.
- [x] Worker execution and visibility.
- [x] Knowledge binding and manager lookup.
- [x] Team eligibility policy.
- [x] Team dispatch outcome modeling.
- [x] Memory routing.
- [x] Private culture scoping.
- [x] Path containment and workspace safety.

## Summary Verdict

The backend is now materially more consistent than it was before this PR.

The worst bad patterns have been removed from production logic.

The biggest improvements are:

- runtime-sensitive state now resolves through [runtime_resolution.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/runtime_resolution.py)
- ingress identity construction is centralized
- backend team/private eligibility is centralized
- private culture scope is no longer accidentally per-agent
- path containment is enforced through shared helpers instead of ad hoc path math

The backend is not yet perfectly minimal.

There are still a few seams where the logic is correct but the representation is more complicated than necessary.

The main remaining simplification targets are:

- team dispatch outcome modeling
- worker execution plumbing
- knowledge-manager lifecycle shape
- private culture cache shape

## Seam Audit

### 1. Runtime Resolution

Primary files:

- [runtime_resolution.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/runtime_resolution.py)
- [agents.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/agents.py)
- [memory/_policy.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/memory/_policy.py)

Source of truth:

- `resolve_agent_execution(...)`
- `resolve_agent_runtime(...)`
- `resolve_knowledge_binding(...)`

What is good:

- Shared vs private state resolution is explicit and centralized.
- Private agents fail closed without execution identity.
- Workspace, state root, file memory root, and tool base dir come from one runtime object.
- Downstream callers mostly consume resolved runtime instead of recomputing scope.

What is still imperfect:

- The file exposes several adjacent concepts that are related but still separate.
- `ResolvedWorkerExecution`, `ResolvedAgentExecution`, `ResolvedAgentRuntime`, and `ResolvedKnowledgeBinding` are all justified, but the boundary between worker execution and worker target is still a little blurry.
- Some callers still ask only for a worker key instead of carrying a more explicit resolved target object.

Audit verdict:

- Status: strong source of truth.
- Cleanup need: low.
- Recommendation: keep this seam stable and resist adding more ad hoc helpers outside it.

### 2. Execution Identity Ingress

Primary files:

- [tool_system/worker_routing.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/tool_system/worker_routing.py)
- [bot.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/bot.py)
- [api/openai_compat.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/api/openai_compat.py)
- [commands/handler.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/commands/handler.py)

Source of truth:

- `build_tool_execution_identity(...)`

What is good:

- Tenant and account attachment is centralized at ingress.
- The runtime logic no longer depends on ambient identity reads.
- Architecture guardrails now defend the boundary between ingress and feature logic.

What is still imperfect:

- There are still multiple ingress call sites, even though they now call the same builder.
- That duplication is acceptable boundary duplication, but it still means there is more than one place to read when tracing request setup.

Audit verdict:

- Status: strong enough.
- Cleanup need: low.
- Recommendation: do not chase "one callsite" purity here unless a new bug appears.

### 3. Worker Execution And Visibility

Primary files:

- [tool_system/worker_routing.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/tool_system/worker_routing.py)
- [runtime_resolution.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/runtime_resolution.py)
- [sandbox_proxy.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/tool_system/sandbox_proxy.py)
- [credentials.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/credentials.py)
- [api/credentials.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/api/credentials.py)

Source of truth:

- `resolve_worker_execution_scope(...)`
- `require_worker_key_for_scope(...)`
- worker-key path helpers in [worker_routing.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/tool_system/worker_routing.py)

What is good:

- Worker keys are no longer derived independently in many places.
- Shared and isolating scopes now flow through the same resolver layer.
- Worker visibility rules are explicit for `user_agent` private agents.

What is still imperfect:

- The system still passes around `worker_scope`, `routing_agent_name`, `execution_identity`, and sometimes `routing_agent_is_private` as separate fields through [sandbox_proxy.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/tool_system/sandbox_proxy.py).
- Credentials resolution and sandbox routing both resolve the same worker context shape, but each still owns its own "what do I do with the resolved worker key" path.
- The code is consistent, but the call flow is verbose.

Audit verdict:

- Status: consistent but not minimal.
- Cleanup need: medium.
- Recommendation: the next real simplification would be one `ResolvedWorkerTarget` style object for worker-scoped consumers.

### 4. Knowledge Binding And Manager Lookup

Primary files:

- [runtime_resolution.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/runtime_resolution.py)
- [knowledge/utils.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/knowledge/utils.py)
- [knowledge/manager.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/knowledge/manager.py)
- [bot.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/bot.py)
- [teams.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/teams.py)

Source of truth:

- `resolve_knowledge_binding(...)` for storage and refresh policy
- `get_agent_knowledge(...)` for agent-level assembly

What is good:

- Shared and private knowledge now use the same binding rules.
- Request-scoped manager usage is explicit.
- The old ambient request-manager state is gone.
- Shared API knowledge and private request knowledge now use explicit effective keys.

What is still imperfect:

- [knowledge/manager.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/knowledge/manager.py) still combines too many concerns.
- One module currently owns binding derivation, effective keying, cache replacement, watcher lifecycle, incremental sync decisions, LRU eviction, and static-vs-request-scoped coexistence.
- It is coherent, but it is still the densest seam in the backend.

Audit verdict:

- Status: functionally centralized, structurally heavy.
- Cleanup need: medium-high.
- Recommendation: future cleanup should split lifecycle/cache policy from binding derivation more explicitly.

### 5. Team Eligibility Policy

Primary files:

- [config/main.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/config/main.py)
- [teams.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/teams.py)
- [api/main.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/api/main.py)

Source of truth:

- `get_unsupported_team_agents(...)`
- `assert_team_agents_supported(...)`
- `team_eligibility_reasons_for_agents(...)`

What is good:

- The private-team prohibition is now closure-based instead of direct-member-only.
- Config validation, runtime team filtering, and frontend eligibility all use the same backend rule family.
- This is a meaningful source-of-truth success.

What is still imperfect:

- The API exposes only the editor-facing reason map, not a richer typed result.
- That is fine today, but it keeps the frontend coupled to stringly reasons instead of a backend-generated reason code.

Audit verdict:

- Status: strong source of truth.
- Cleanup need: low-medium.
- Recommendation: only revisit if the UI needs richer structured reasons.

### 6. Team Dispatch Outcome Modeling

Primary files:

- [teams.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/teams.py)
- [bot.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/bot.py)

Current shape:

- `TeamFormationDecision(should_form_team, agents, mode, rejected_request)`

What is good:

- The old bug where rejected explicit team requests fell through to individual replies is fixed.
- The bot now distinguishes between "team request rejected" and "no team".

What is still imperfect:

- The representation is still slightly lossy and awkward.
- `should_form_team` plus `rejected_request` is effectively an enum encoded as two booleans.
- The caller has to know that one boolean suppresses the meaning of the other.

Audit verdict:

- Status: correct but not clean.
- Cleanup need: high.
- Recommendation: replace this with a single explicit outcome enum or tagged dataclass.

### 7. Memory Routing

Primary files:

- [memory/_policy.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/memory/_policy.py)
- [memory/auto_flush.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/memory/auto_flush.py)

Source of truth:

- `resolve_agent_runtime(...)` for state roots
- `_resolve_flush_scope(...)` for private auto-flush rehydration

What is good:

- Direct memory routing uses resolved runtime state.
- Auto-flush persists enough execution identity to reopen the same private scope later.
- The previous mixed-team private behavior has been removed.

What is still imperfect:

- The policy layer still includes several string-based scope helpers like `agent_scope_user_id(...)`, `build_team_user_id(...)`, and reverse-parsing helpers.
- These are not wrong, but they are another miniature encoding scheme that callers need to remember.

Audit verdict:

- Status: consistent, moderately clean.
- Cleanup need: low-medium.
- Recommendation: only simplify further if scope IDs become a recurring source of mistakes.

### 8. Private Culture Scoping

Primary files:

- [agents.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/agents.py)
- [runtime_resolution.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/runtime_resolution.py)

Source of truth:

- requester scope root via `resolve_private_scope_root(...)`

What is good:

- Private culture is no longer accidentally per-agent.
- Same-requester private agents in one culture now share one culture manager and one requester-scoped DB.

What is still imperfect:

- The implementation uses two cache paths and a `cache_private` switch.
- The key logic is understandable, but not elegant.
- The concept that really exists here is "culture scope key", and the code does not name that directly yet.

Audit verdict:

- Status: correct but could be clearer.
- Cleanup need: medium.
- Recommendation: introduce an explicit culture-scope key helper if this seam is touched again.

### 9. Path Containment And Workspace Safety

Primary files:

- [workspaces.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/workspaces.py)
- [runtime_resolution.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/runtime_resolution.py)

Source of truth:

- `resolve_relative_path_within_root(...)`
- `resolve_private_scope_root(...)`
- `resolve_agent_workspace_from_state_path(...)`

What is good:

- Symlink escape checks now use shared containment helpers.
- Private template backfill is incremental and non-destructive.
- The private scope root and private state root are both checked under canonical storage.

What is still imperfect:

- Very little.
- This seam is one of the cleaner outcomes of the refactor.

Audit verdict:

- Status: clean.
- Cleanup need: low.

## Overall Back-End Cleanliness Assessment

- Runtime and path safety are in good shape.
- Team eligibility policy is in good shape.
- Execution identity discipline is in good shape.
- Memory routing is in acceptable shape.
- Worker execution plumbing is correct but more verbose than ideal.
- Knowledge manager lifecycle is still the least approachable subsystem.
- Team outcome modeling is still more implicit than it should be.
- Private culture handling is correct but can still be made easier to read.

## Working Conclusion

The backend no longer suffers from the worst form of drift, where multiple modules each define their own version of the same policy.

The main remaining work is not bug fixing.

The main remaining work is reducing representation complexity in a few seams that are already logically centralized.

The proposal document linked next should be treated as the priority order for any further cleanup:

- [backend-architecture-proposals.md](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/docs/dev/backend-architecture-proposals.md)
