# MindRoom Architecture Boundaries

## Status

This document is a living architecture spec for the core MindRoom runtime.

It is intentionally broad and high level.

It records both the current architecture and the target architecture.

It is allowed to change as we learn more about the codebase.

It is not a frozen contract and it is not a promise that the current code already matches the target state.

The immediate enforcement plan is intentionally narrower than the full architecture described here.

## Purpose

MindRoom has grown a large number of useful runtime capabilities.

It now needs a clearer architecture story for humans and AI systems working in the codebase.

The goal of this document is to make ownership, public seams, internal boundaries, and allowed dependencies explicit.

The main outcomes we want are better maintainability, better debuggability, fewer de facto APIs, and fewer places where policy hides in helper layers.

## Core principles

The architecture should optimize for clear ownership over abstract purity.

Each domain should have one primary reason to change.

Each domain should have a small set of public entrypoints that are obvious to consumers.

Internal storage, helper, and wiring modules should not become accidental public APIs.

Higher-level code should depend on stable contracts and facades rather than concrete low-level implementations when a stable boundary already exists.

The target architecture should reduce conceptual hops even when raw line count does not decrease.

## Domain map

The core MindRoom runtime is best understood as several cooperating domains.

### Runtime and orchestration

This domain owns process boot, bot lifecycle, shared runtime resources, response execution, and top-level flow control.

Representative modules include `orchestrator.py`, `bot.py`, `runtime_support.py`, `response_runner.py`, and `turn_controller.py`.

This domain is responsible for bringing the system up, holding long-lived runtime state, and coordinating agent execution.

### Matrix conversation domain

This domain owns Matrix event storage, conversation lookup, thread semantics, room-facing event handling, and message history behavior.

Representative modules include `matrix/client.py`, `matrix/conversation_cache.py`, `matrix/cache/*`, `matrix/thread_membership.py`, and related thread bookkeeping helpers.

This domain is responsible for durable event knowledge and conversation semantics, not for broader orchestration policy.

### Tool execution and dispatch

This domain owns tool runtime context, dispatch contracts, hook bridges, tool registration, and execution-time context propagation.

Representative modules include `tool_system/runtime_context.py`, `tool_system/tool_hooks.py`, `tool_system/dependencies.py`, and surrounding tool-system code.

This domain is responsible for making tool execution explicit and legible.

### Agent and team behavior

This domain owns agent construction, routing, team collaboration, prompt composition, and agent-level decision flow.

Representative modules include `agents.py`, `routing.py`, `teams.py`, and `agent_prompts.py`.

This domain decides who should act and how specialized agents collaborate.

### Memory and knowledge

This domain owns long-term memory, learning, knowledge indexing, and retrieval-backed context surfaces.

Representative modules include `memory/*` and `knowledge/*`.

This domain is responsible for what agents know and remember over time.

### User-facing control surfaces

This domain owns chat commands, scheduling, REST API entrypoints, and dashboard-facing control paths.

Representative modules include `commands/*`, `scheduling.py`, and `api/*`.

This domain is responsible for control-plane interaction, not low-level storage or raw Matrix protocol details.

### Cross-cutting infrastructure

This domain owns attachments, streaming, auth checks, logging, background tasks, and other supporting runtime services that cross multiple features.

Representative modules include `attachments.py`, `streaming.py`, `authorization.py`, `background_tasks.py`, and related support code.

This domain should stay infrastructural and should not absorb domain-specific policy unless that policy is truly shared.

## Domain ownership and intended public seams

### Runtime and orchestration

Runtime and orchestration should expose orchestration entrypoints, bot lifecycle surfaces, and explicit runtime support contracts.

It may construct lower-level implementations when it is the composition root.

It should not leak those concrete implementations upward as the preferred API for unrelated consumers.

This domain is allowed to wire internal services together because that is its job.

It should not quietly redefine lower-level domain policy.

### Matrix conversation domain

The conversation-facing public seams should be the conversation-level cache and related conversation contracts.

Low-level cache internals should remain internal to the cache package unless there is a deliberately exported package-level boundary.

Callers above the cache package should not need to know how storage is decomposed into runtime, event, thread, or write-policy internals.

Thread and conversation policy should have a small number of named decision points.

### Tool execution and dispatch

The public seams should be explicit dispatch contexts and runtime contracts.

Hook bridges and dispatch helpers should consume those contracts directly rather than reconstructing them from partial ambient state.

Compatibility-only wrappers should not survive once the explicit contracts exist.

### Agent and team behavior

Public seams should be agent construction, router decisions, and team collaboration interfaces.

This domain should not reach through lower-level runtime and Matrix internals unless a real orchestration boundary requires it.

### Memory and knowledge

Public seams should be memory and knowledge service interfaces rather than private helper modules.

Storage details and prompt-building helpers should not become de facto public APIs through direct imports.

### User-facing control surfaces

This domain should talk to runtime, conversation, and tool contracts.

It should not own low-level cache policy or storage semantics.

Commands, API routes, and scheduling flows should be consumers of domain contracts, not alternate implementations of them.

### Cross-cutting infrastructure

Cross-cutting support code should stay boring and reusable.

It should help move data across domains without silently becoming a second source of truth for domain policy.

## Current state

The current architecture is stronger than it was before the recent refactor wave, but it is not fully calm yet.

The runtime and dispatch area now has clearer explicit contracts than it had before.

The thread and cache areas have clearer ownership than before, but the cache package in particular still has a relatively high conceptual surface area.

Some private concrete types still leak into runtime wiring and many tests.

Some package internals are now better named and better separated, but separation alone does not guarantee a better mental model.

Much of the recent work improved correctness and testability more than it improved discoverability and navigability.

## Target state

The target architecture is a codebase where each major domain can be explained with a small ownership map.

Higher-level code should mostly depend on stable facades, protocols, and package-level exported boundaries.

Low-level implementation modules should be free to change without becoming hidden APIs that other domains depend on directly.

The conversation domain should present one obvious public boundary for conversation semantics and a limited public boundary for durable cache behavior.

The runtime domain should construct internal implementations where necessary, but that construction should not muddy the preferred interfaces for the rest of the system.

The tool execution domain should expose a small number of explicit dispatch shapes and avoid ambient reconstruction of execution identity and runtime context.

Tests should increasingly prefer real public seams over direct dependency on private implementation types except where a test is intentionally a unit test of an internal module.

## Current state versus target state

### Runtime and orchestration

Current state: runtime support and dispatch contracts are meaningfully better than before, but some wiring still reflects historical implementation details.

Target state: orchestration constructs internal services while the rest of the codebase mostly depends on stable runtime contracts.

### Matrix conversation domain

Current state: the conversation and cache story is more explicit than before, but the cache package still needs a calmer public versus internal boundary.

Target state: callers above the cache package use conversation-level and package-level seams without depending on internal module layout.

### Tool execution and dispatch

Current state: explicit dispatch contexts now exist, but the system still needs continued pressure against compatibility wrappers and ambient context reconstruction.

Target state: tool entrypoints are legible from call sites and invalid runtime shapes are harder to construct.

### Agent and team behavior

Current state: mostly serviceable, but some routing and orchestration flows still carry historical wiring complexity from surrounding domains.

Target state: agent and team behavior depends on clean contracts from runtime, conversation, and tool domains.

### Memory and knowledge

Current state: useful and broad, but not yet part of any narrow enforcement boundary.

Target state: clear service boundaries and fewer private helper imports across domain lines.

### User-facing control surfaces

Current state: commands, scheduling, and API flows already depend on several good typed contracts, but they remain exposed to some lower-level historical details.

Target state: these surfaces are thin consumers of domain contracts and do not need special knowledge of low-level implementations.

## Boundary rules

Higher-level modules should prefer package-level or protocol-level boundaries over direct imports from internal helper and storage modules.

Private implementation modules and private implementation types should not become accidental public APIs through repeated cross-domain imports.

When a stable conversation-facing boundary exists, code above the cache layer should prefer that boundary over direct dependency on low-level cache implementation details.

Runtime composition roots may construct concrete implementations, but those construction sites should not define the preferred public interface for unrelated consumers.

Tool dispatch and hook execution should use explicit runtime and identity contracts rather than ambiently rebuilding them from partial state.

Tests should mirror the intended architecture where practical.

Internal module tests may exercise private types directly.

Broader integration tests should increasingly prefer public seams.

## Enforcement strategy

The architecture described here is broader than what we should enforce immediately.

The first enforcement slice should be narrow and high value.

The initial Tach pilot should focus only on the Matrix cache and conversation boundary cluster.

That first slice should cover:

- `mindroom.matrix.cache`
- `mindroom.matrix.conversation_cache`
- `mindroom.runtime_support`

The purpose of the first slice is to make one important boundary real instead of generating a large repo-wide violation list.

Everything else should remain effectively unchecked or advisory at first.

The existing privacy script can remain in place during the initial Tach rollout.

The goal of the first enforcement phase is not to solve all architecture debt.

The goal is to prove that a small, explicit boundary can be described, enforced, and kept green.

## Why the first enforcement slice is narrow

The recent refactor wave made this boundary the most immediately valuable target.

It is also the place where accidental public surfaces and internal-layout leakage are easiest to reason about right now.

A narrow pilot reduces rollout noise and makes the resulting CI signal meaningful.

A broad first rollout would turn Tach into another large source of generic findings rather than a tool that protects a real architectural seam.

## Possible future enforcement phases

If the narrow cache and conversation pilot succeeds, later phases may include:

- broader runtime contract enforcement
- thread-domain policy boundaries
- memory and knowledge boundary cleanup
- selected private-helper import rules in other domains

Those later phases should only be added once the earlier slice is stable and useful.

## Non-goals

This document is not a full rewrite plan.

It does not imply immediate enforcement of the entire architecture.

It does not claim that every current boundary decision is already final.

It does not require more file splitting.

It does not require fewer files as an end in itself.

It is a guide for converging on clearer ownership and clearer public seams over time.

## Immediate next step

The immediate next step after this document is a narrow Tach adoption plan for the Matrix cache and conversation boundary cluster.

That plan should be evaluated against this architecture document rather than treated as a replacement for it.
