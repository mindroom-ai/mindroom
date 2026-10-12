# Shared Agent Private Instance Goal

This record states the intended user model for requester-private agents (`agents.<name>.private`) and the rules that keep that design from drifting toward low-level path plumbing or a second model of agent-local state.

## Goal

One shared, visible agent definition acts as a template that materializes a private instance for each requester.
Each private instance has its own files, context, file memory, and local knowledge, while the visible agent identity stays shared.
Requester A must never read or mutate requester B's private state through normal runtime behavior.

## User model

Users should think: "There is one shared agent in config, and MindRoom gives each requester their own private copy of that agent's state at runtime."
The motivating case is one `mind` agent configured once and used by many people, each of whom gets their own scaffolded context files and memory.
Users should not have to wire workspace-relative paths, link context files to runtime-owned storage paths, link knowledge bases to agent workspaces, or apply deployment-specific hacks.

## Decisions

- The public config describes intent, not plumbing: one agent-local concept for requester-private state, higher-level than workspace path settings.
  Low-level workspace machinery stays an implementation detail and must not become a second source of truth for runtime scope or state-root decisions.
- The public surface stays named `private`; renaming it to `requester_state` was out of scope.
- `worker_scope` and `private.per` are different concepts: `worker_scope` controls worker runtime reuse for shared agents, `private.per` partitions requester state for private agents, and `private.template_dir` is only the scaffold source for that state.
  A private agent's internal execution scope is derived from `private.per`, but that derived scope is not a public field, so never describe `private.per` as if it were `worker_scope` or infer or restore one field from the other.
- Template sources are explicit local directories rather than hidden built-in content, and private context files and private knowledge paths are stated in config rather than inferred from template contents.
- There is one product model for agent-local state: a shared agent uses the shared workspace root and a private agent uses its private root, with no separate public choice of file-memory location.
- Private-versus-shared and scope-to-root decisions belong to one resolver layer, and after ingress the resolved state and execution identity are passed explicitly instead of being recomputed from config and ambient context.
  A module that makes its own scope decision or adds its own model of agent-local state reintroduces the drift this design exists to prevent.
- The work keeps supported external behavior and protocol compatibility but not internal backward compatibility: no transition helpers, alternate internal paths, or legacy wrappers kept only because older internal code used them.
- No `mind`-specific special cases go into unrelated runtime code, and the feature is not a workaround for one private deployment.
- `mindroom config init` is not the onboarding surface for private instances: its starter config stays a shared single-user example with top-level `context_files` and shared knowledge, and private instances stay opt-in for multi-user or requester-isolated deployments.

## Non-goals and limitations

- A worker-scoped shared agent does not automatically get private copies of its context files.
- This work did not add requester isolation to `/v1`, which served only shared agents.
- Configured teams cannot include private agents, directly or through members that delegate to them.

## Acceptance check

The design is right when a reader of the final config thinks: "This shared agent will create a canonical private per-user instance of its state."
It is wrong when the reader thinks they must wire relative paths together to make worker scoping happen, or that there are two equally valid product models for agent-local state.
