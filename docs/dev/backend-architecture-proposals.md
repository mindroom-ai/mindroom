# Backend Architecture Proposals

This document turns the audit in [backend-architecture-audit.md](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/docs/dev/backend-architecture-audit.md) into explicit follow-up proposals.

The proposals are ordered by expected maintenance payoff.

The goal is not churn.

The goal is to make the backend harder to regress and easier for future AI-generated edits to keep correct.

Internal backward compatibility is not a constraint for these proposals.

If a proposal replaces an internal representation, helper, or call path with a cleaner source of truth, the old internal shape should be deleted in the same change instead of preserved behind wrappers or aliases.

## Priority 1: Replace TeamFormationDecision Booleans With One Explicit Outcome Type

Current problem:

- [teams.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/teams.py) represents team formation with `should_form_team` plus `rejected_request`.
- That is logically an enum encoded as two fields.
- The meaning is easy to misunderstand at future call sites.

Why this matters:

- The branch already had one real bug from collapsing "explicit team request rejected" into "no team".
- The current fix works, but the representation still invites the same class of mistake.

Proposal:

- Replace `TeamFormationDecision` with a tagged outcome.
- Example shape:
  - `kind: "team" | "reject" | "none"`
  - `agents`
  - `mode`
- Make `agents` and `mode` meaningful only for `kind == "team"`.
- Keep DM fallback partial degradation represented as `kind == "none"`, not `reject`.

Expected simplification:

- Fewer implicit invariants.
- Easier call flow in [bot.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/bot.py).
- Lower chance of reintroducing rejected-request fallthrough bugs.

Change size:

- Small to medium.

Risk:

- Low if covered by the existing team-collaboration and bot-dispatch tests.

## Priority 2: Introduce One Explicit Resolved Worker Target For Worker-Scoped Consumers

Current problem:

- Worker-aware consumers still pass around overlapping pieces of routing context.
- The common set is:
  - `worker_scope`
  - `routing_agent_name`
  - `execution_identity`
  - `routing_agent_is_private`
- [sandbox_proxy.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/tool_system/sandbox_proxy.py) and [credentials.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/credentials.py) both resolve worker context, but not through the same carried object.

Why this matters:

- This is one of the last places where correctness depends on multiple related parameters staying in sync.
- The code is correct now, but it is still verbose and easier than necessary to misuse.

Proposal:

- Add a small `ResolvedWorkerTarget` dataclass in [runtime_resolution.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/runtime_resolution.py) or a nearby worker-resolution module.
- Suggested fields:
  - `worker_scope`
  - `worker_key`
  - `execution_identity`
  - `routing_agent_name`
  - `private_agent_names`
- Build it once for worker-scoped tool execution.
- Make sandbox routing and scoped credential lookup consume it directly.

Expected simplification:

- Fewer repeated parameter bundles.
- Easier tracing of worker-scoped decisions.
- Less risk of forgetting one of the worker-target inputs later.

Change size:

- Medium.

Risk:

- Medium, because it touches sandbox and credential plumbing.

Backward-compatibility rule:

- Do not keep the current loose parameter bundle API once the resolved worker target lands.
- Move the callers and delete the old parallel shapes in the same slice.

## Priority 3: Separate Knowledge Binding Policy From Knowledge Manager Lifecycle

Current problem:

- [knowledge/manager.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/knowledge/manager.py) still owns too many concerns at once.
- It currently combines:
  - effective binding lookup
  - key construction
  - manager creation
  - reuse and replacement rules
  - watcher startup
  - incremental refresh
  - static and request-scoped coexistence
  - scoped LRU cleanup

Why this matters:

- This is now the densest backend seam.
- The logic is correct, but future edits are still more likely to create subtle regressions here than in other subsystems.

Proposal:

- Keep `resolve_knowledge_binding(...)` as the authoritative binding policy.
- Extract a narrower manager-lifecycle layer with explicit operations like:
  - `lookup_manager(key)`
  - `ensure_manager(binding, reindex_on_create=...)`
  - `replace_stale_managers(base_id, current_key, request_scoped=...)`
- Make the current `_knowledge_manager_key(...)` helper return a single richer object if useful, instead of a tuple that is unpacked repeatedly.

Expected simplification:

- Better separation between "where should this knowledge live" and "how do we manage the process-global manager cache".
- Easier reasoning about which behavior change belongs to which layer.

Change size:

- Medium to large.

Risk:

- Medium-high.
- Only worth doing if the implementation is kept incremental and test-led.

Backward-compatibility rule:

- Do not keep mixed old/new entry points alive after each migration step.
- Delete replaced manager helpers and compatibility branches as soon as the callers move.

## Priority 4: Introduce An Explicit Culture Scope Key

Current problem:

- Private culture is now correct, but [agents.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/agents.py) still expresses it through:
  - `cache_private`
  - one normal cache
  - one weak private cache
  - implicit storage-path-based cache keys

Why this matters:

- The actual concept is now stable.
- It is "culture manager scope equals shared storage root or requester scope root, plus culture name and culture signature".
- The code does not name that concept directly.

Proposal:

- Add a small helper or dataclass such as `CultureScopeKey`.
- Resolve it once from:
  - shared agent runtime
  - or requester private scope root
- Make `_resolve_agent_culture(...)` operate on the explicit scope key instead of a boolean mode flag.

Expected simplification:

- Cleaner cache semantics.
- Easier to verify that private cultures are requester-scoped and shared cultures are globally shared.

Change size:

- Small to medium.

Risk:

- Low.

## Priority 5: Consider A Structured Team Eligibility API Response

Current problem:

- Team eligibility is centrally derived on the backend, which is good.
- The frontend still receives editor-facing strings rather than structured reason codes.

Why this matters:

- Today this is acceptable.
- If more UI behavior depends on eligibility reasons, string-based coupling will become awkward.

Proposal:

- If this seam grows, change `/api/config/team-eligibility` to return a richer typed payload.
- Example shape:
  - `eligible: bool`
  - `reason_code: "private" | "delegates_to_private" | "unknown" | null`
  - `message`

Expected simplification:

- Better frontend behavior without recreating backend logic.
- Less coupling to exact backend wording.

Change size:

- Small.

Risk:

- Low.

Priority note:

- This is not urgent today.
- It becomes worthwhile only if the frontend needs more policy-aware behavior than it already has.

## Explicit Non-Proposals

These areas should not be refactored further right now because they already do their job clearly enough:

- `resolve_agent_runtime(...)` itself
- path-containment helpers in [workspaces.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/workspaces.py)
- backend team/private closure logic in [config/main.py](/home/basnijholt/Work/dev/mindroom-worktrees/feat-user-scoped-shared-mind-workspaces/src/mindroom/config/main.py)
- execution-identity ingress construction via `build_tool_execution_identity(...)`

Further churn there is more likely to add motion than to reduce real complexity.

## Recommended Order

If more cleanup is pursued, do it in this order:

1. Replace team outcome booleans with one explicit outcome type.
2. Introduce one resolved worker target object for worker-scoped consumers.
3. Refactor knowledge-manager lifecycle into a clearer layering.
4. Clean up private culture caching with an explicit culture scope key.
5. Only then consider structured team-eligibility API responses if the frontend needs them.

## Completion Bar

This follow-up work should be considered done only when:

- the representation is simpler, not just moved
- the number of caller-owned invariants goes down
- tests cover the seam directly
- future AI-generated edits are more likely to use the shared helper than to open-code the policy again
- no internal compatibility alias, wrapper, or alternate path remains for the replaced seam
