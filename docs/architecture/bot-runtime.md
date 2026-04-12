# Bot Runtime Simplification Roadmap

## Purpose

This document is the source of truth for the next runtime simplification.
The goal is to make the remaining abstractions concrete, honest, and easy to trace.

## Good Boundaries To Keep

`AgentBot` is the Matrix runtime shell.
It should own lifecycle, callback registration, sync, room membership, presence, and startup or shutdown.

`InboundTurnNormalizer` owns raw input shaping.
It should turn text, voice, sidecars, and media into canonical turn inputs before policy or execution runs.

`ConversationResolver` owns conversation identity.
It should resolve thread roots, reply chains, history, mentions, and normalized ingress envelopes.

`DeliveryGateway` owns Matrix transport.
It should send, edit, redact, and finalize already-generated responses.

`EditRegenerator` owns the edited-message replay workflow.
It is still coupled to the current persistence split, but its workflow boundary is real.

## Current Problems

`TurnController` is the real turn owner now, but the name is vague.
`TurnPolicy` still mixes policy with command execution, router relay, and response execution.
`ResponseRunner` still mixes lifecycle mechanics with actual response running.
`IngressHookRunner` is a thin hook adapter with a vague name.
`HandledTurnLedger` and persisted run metadata still split durable turn truth.
`MessageTarget` still combines conversation identity and delivery placement.

## Target Runtime Vocabulary

The target runtime should read like this:

```text
Matrix callback
  -> AgentBot
  -> TurnController
       -> InboundTurnNormalizer
       -> ConversationResolver
       -> TurnPolicy
       -> ResponseRunner
       -> TurnStore
       -> DeliveryGateway
```

`AgentBot` owns Matrix lifecycle only.
`TurnController` owns one inbound turn from ingress to recorded outcome.
`TurnPolicy` owns pure decision logic only.
`ResponseRunner` owns response execution and lifecycle only.
`TurnStore` owns durable turn truth.
`DeliveryGateway` owns Matrix transport only.

## Rename-Only PR

The first PR should be rename-only.
It should improve the honesty of the current names without changing behavior.

### Rename Set

Rename `TurnController` to `TurnController`.
Rename `ResponseRunner` to `ResponseRunner`.
Rename `IngressHookRunner` to `IngressHookRunner`.

Do not rename `TurnPolicy` in this PR.
Any better name for it depends on first making it pure.
Renaming it early would either be dishonest or create churn.

### Rules

Do not change behavior.
Do not move logic across modules.
Do not change persistence.
Do not add wrappers or compatibility shims.
Only change names, imports, docstrings, tests, and architecture docs.

### Acceptance Criteria

The diff should be mostly symbol renames and documentation updates.
The full test suite must stay green.
`pre-commit` must stay green.

## Behavioral Simplification PR

The second PR should do the actual simplification work.
It should reduce the number of orchestration objects and remove overlapping truth.

### Scope

Make `TurnPolicy` pure.
Rename the pure result to `TurnPolicy`.
Move command execution, router relay, and response branching out of the planner.

Keep `TurnController` as the only owner of turn sequencing.
It should sequence `precheck -> normalize -> resolve -> decide -> execute -> record`.

Narrow `ResponseRunner` to actual response execution and lifecycle.
It should own placeholders, locking, streaming, cancellation, AI or team runs, and post-response effects.

Introduce `TurnStore` as the single durable turn boundary.
It may wrap current storage first, but it must present one source of truth to the runtime.

Move `EditRegenerator` to read and write through `TurnStore`.
It should stop reconciling `HandledTurnLedger` and persisted run metadata directly.

### Non-Goals

Do not split `MessageTarget` yet unless the rest of the refactor is already stable.
Do not create a new abstraction unless it deletes an old owner immediately.
Do not optimize for line counts.
Optimize for fewer control paths and fewer state representations.

### Acceptance Criteria

A normal text turn can be traced through one controller entrypoint.
The policy layer has no delivery, AI, or persistence side effects.
Edit regeneration reads one durable turn record.
`AgentBot` stays a runtime shell instead of a partial controller.

## Follow-Up After The Behavioral PR

Only after the behavioral refactor lands should we revisit `MessageTarget`.
That follow-up can split conversation identity from delivery placement.

At that point we can decide whether `EditRegenerator` should remain a separate peer or collapse into a `TurnController` path backed by `TurnStore`.

## Review Questions

When reviewing either PR, ask these questions.

Does each abstraction own a concrete thing rather than a vague place in the pipeline.
Did the change delete an old owner instead of adding a second one.
Can one inbound turn be traced without jumping between multiple coordinators.
Is the durable turn truth singular.
