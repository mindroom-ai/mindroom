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
It should resolve explicit thread identity, history, mentions, and normalized ingress envelopes.

`DeliveryGateway` owns Matrix transport.
It should send, edit, redact, and finalize already-generated responses.

`EditRegenerator` owns the edited-message replay workflow.
It is still coupled to the current persistence split, but its workflow boundary is real.

## Current Problems

`TurnController` is the real turn owner now, but it is still too large.
`TurnPolicy` is pure now, but `ResponseRunner` still owns too much execution detail.
`IngressHookRunner` is a thin hook adapter with a vague name.
`TurnStore` gives the runtime one durable turn boundary, but it still has to reconcile ledger state with persisted run metadata under the hood.
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

## Completed Simplifications

`TurnController` is now the only normal-turn owner.
It sequences `precheck -> normalize -> resolve -> decide -> execute -> record`.

`TurnPolicy` is now pure.
It no longer sends messages, runs AI, or writes persistence state.

`TurnStore` is now the main durable turn boundary for the extracted runtime flows.
`TurnController` and `EditRegenerator` read and write through `TurnStore` instead of owning their own persistence helpers.
Command handling now records terminal outcomes through `TurnStore` as well.

`AgentBot` is closer to a runtime shell again.
It still needs more cleanup, but normal turn control, edit regeneration, and interactive selection execution no longer live in the bot class itself.

Interactive reactions and numeric text selections now share the same controller-owned execution path.
That path sends the acknowledgment, runs response generation, and records the handled turn once.

## Next Simplification Work

Shrink `ResponseRunner`.
It should keep placeholder, locking, streaming, cancellation, AI or team execution, and post-response effects, but it should stop accumulating unrelated side paths.

Revisit `IngressHookRunner`.
It may stay as a helper, but it should not grow into another top-level orchestration object.

Only after those steps should we revisit `MessageTarget`.
That follow-up can split conversation identity from delivery placement if the runtime still needs it.

## Review Questions

When reviewing either PR, ask these questions.

Does each abstraction own a concrete thing rather than a vague place in the pipeline.
Did the change delete an old owner instead of adding a second one.
Can one inbound turn be traced without jumping between multiple coordinators.
Is the durable turn truth singular.
