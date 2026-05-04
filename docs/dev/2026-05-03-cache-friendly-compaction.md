# Cache-Friendly Compaction

This note records the prepared conversation-chain architecture implemented on May 3, 2026.

The goal is to make destructive compaction reuse the same conversation-chain construction as normal reply preparation.

## Core Idea

Compaction should be cheap when the provider prompt cache is still warm and the compaction model is the active reply model.

With the default `compaction.model: null`, the compaction request is prepared through the same live Agent or Team request assembly used by normal replies.

That keeps the provider-visible system prompt, session summary, selected persisted history, and tool schemas aligned with the normal reply request prefix.

Warm-cache compaction preserves that prefix and appends one final user instruction asking the model to update the durable summary.

The important point is that compaction and normal reply preparation now share one owner for message materialization.

An explicit `compaction.model` remains supported as a configurable summary-model override, but a different model or provider should not be expected to reuse the active reply prompt cache.

This is not about reusing a cached prefix after compaction.

## Implemented Shape

The shared chain code lives in `src/mindroom/prepared_conversation_chain.py`.

It owns Matrix visible-message conversion, unseen-context preparation, Matrix fallback replay preparation, persisted-run replay materialization, replay token estimates, media snapshots, stale Anthropic replay-field stripping, and compaction summary transforms.

`src/mindroom/execution_preparation.py` still owns request-scoped orchestration, history policy application, compaction lifecycle wiring, and the `PreparedExecutionContext` returned to callers.

Execution preparation calls the prepared-chain module directly instead of re-exporting compatibility helpers.

`src/mindroom/history/compaction.py` still owns durable session mutation, chunk selection, lifecycle metadata, model calls, retries, hook emission, and progress persistence.
`src/mindroom/history/agno_compaction_request.py` owns the Agno-specific adapter that asks Agno to prepare compaction as a normal Agent or Team request without running a full Agent or Team turn.

It now builds summary requests through the prepared-chain module and, when the active model is used, through Agno's live Agent or Team request builder instead of materializing replay messages independently.

## Warm-Cache Transform

Warm-cache compaction appends one summary instruction to the prepared persisted-run chain.

The preceding messages are copied without changing their role order or content shape.

When the active model is used, Agno receives a synthetic session containing only the selected chain so its normal request builder can place the same system prompt, summary context, tool schemas, and history prefix before the final summary instruction.

The final instruction tells the model not to summarize static instructions or tool definitions and not to call tools.

Tool schemas are sent as provider schemas, not executable `Function` objects, with `tool_choice: none` when schemas are present.

The transform validates that tool-call and tool-result adjacency remains intact.

The runtime compaction path uses this warm-cache transform for summary chunk requests.

## Boundary Decisions

Prepared-chain construction is intentionally pure and does not write durable history.

Durable compaction state remains in `history/compaction.py` and `history/storage.py`.

Matrix delivery, lifecycle notices, model loading, and tool execution stay outside the prepared-chain module.

The new module is the source of truth for how visible Matrix context and persisted Agno runs become provider-message chains.

This removes the duplicated execution-preparation and compaction ownership that made previous review rounds hard to reason about.

## Preserved Behavior

Destructive compaction semantics are unchanged.

Manual compaction semantics are unchanged.

Matrix seen-event metadata propagation is unchanged.

Response event ids, prepared-history diagnostics, and compaction lifecycle metadata are unchanged.

The legacy XML summary-input helper was removed, so compaction has one serialization path for provider-visible history.

## Test Coverage

Unit tests cover prepared-chain materialization from persisted replay.

Unit tests assert that warm-cache compaction preserves the prepared-chain prefix before the final summary instruction.

Unit tests assert that `compact_scope_history` sends the chain-shaped summary request to the summary model.

The relevant targeted checks are `uv run pytest tests/test_agno_history.py -k compaction -q`, `uv run pytest tests/test_execution_preparation.py -q`, `uv run pytest tests/test_partial_reply_context.py -q`, and `uv run tach check --dependencies --interfaces`.
