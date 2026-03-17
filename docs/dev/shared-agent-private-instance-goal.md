# Shared Agent Private Instance Goal

## Purpose

This note defines the actual goal of the shared-agent private-instance work.

It exists to prevent the implementation from drifting toward low-level path plumbing or the wrong user model.

## Core Goal

The goal is to let one shared visible agent definition behave like a template that materializes a private effective instance for each requester.

That private effective instance must get its own files, context, file memory, and local knowledge.

The shared visible identity must stay shared.

The private state must stay private.

## Intended Mental Model

Users should think of this as:

"There is one shared agent in config, and MindRoom gives each requester their own private copy of that agent's state at runtime."

Users should not need to think in terms of:

- workspace-relative path wiring
- manually linking context files to runtime-owned storage paths
- manually linking knowledge bases to agent workspaces
- deployment-specific hacks

## Primary Use Case

A single shared `mind` agent is configured once.

Many users interact with that same visible `mind` agent.

Each user gets their own private effective `mind` instance at runtime.

That private instance has its own scaffolded files such as:

- `SOUL.md`
- `AGENTS.md`
- `USER.md`
- `IDENTITY.md`
- `TOOLS.md`
- `HEARTBEAT.md`
- `MEMORY.md`
- `memory/`

Those files are created on first use if they do not already exist.

The agent loads its private context from that requester's own scaffolded files.

The agent reads and writes file memory inside that requester's own private state.

The agent indexes and queries local knowledge inside that requester's own private state.

Requester A must never read or mutate requester B's private state through normal runtime behavior.

## What This PR Must Achieve

There is one shared visible agent definition in config.

That shared definition can opt into private per-requester materialization.

The common case should be easy to configure and easy to understand.

The public config should describe intent, not implementation plumbing.

Private template source files should be explicit local files, not hidden built-in content.
Private context files and private knowledge paths should stay explicit in config rather than being inferred from template contents.

Shared/global knowledge should remain distinct from requester-local knowledge.

Existing single-user and unscoped behavior must continue to work.

Existing worker-scoped sessions, learning, credentials, and file memory behavior must continue to work.

`mindroom config init` must keep its current single-user output and behavior.

## What This PR Is Not

This PR is not about making every worker-scoped agent automatically get private copied context files.

This PR is not about changing the single-user `mindroom config init` setup.

This PR is not about `/v1` isolation support.

This PR is not about a private deployment workaround.

This PR is not about baking `mind`-specific special cases into unrelated runtime code paths.

## Design Direction

The public config should expose one explicit agent-local concept for requester-private materialized state.

That concept should be higher-level than the current `workspace.*` and `path_relative_to_agent_workspace` surface.

Private requester-local knowledge should be representable as private instance state.

Top-level shared knowledge definitions should remain available for truly shared/global corpora.

The current low-level workspace machinery can remain as internal implementation if it helps.

## Runtime Resolver Contract

This PR keeps `agents.<name>.private` as the public surface.

It does not rename that surface to `requester_state`.

`src/mindroom/runtime_resolution.py` is the internal source of truth for private versus shared runtime resolution.

It resolves state per `(agent_name, execution_identity)` materialization, not once per request.

After ingress, resolved runtime state should be passed explicitly instead of being recomputed from config plus ambient context.

Worker visibility and knowledge bindings may remain derived helpers, but they must come from the same resolver layer.

Outside the resolver layer and low-level path helpers, no module should make its own scope-to-root decision.

## Runtime Scope

The resolver-based contract now covers direct agent execution.

That includes:

- workspace and template materialization
- private context loading
- file memory placement
- private knowledge binding
- sessions
- learning
- worker execution routing for the touched paths
- credential routing for the touched paths
- auto-flush scope resolution

`/v1` remains shared-only.

Private teams are intentionally rejected.

Mixed private/shared team behavior is out of scope and should stay removed.

## Remaining Non-Goals

This PR does not add private-team support.

This PR does not add `/v1` requester isolation.

This PR does not rename the public config surface.

## Acceptance Check

If a reader finishes the final config and thinks:

"This shared agent will create a canonical private per-user instance of its state."

then the config design is on the right track.

If a reader instead thinks:

"I need to wire a bunch of relative paths together to make worker scoping happen."

then the config design is still wrong.
