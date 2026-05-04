# Cache-Friendly Compaction Plan

This note records a follow-up architecture plan discussed on May 3, 2026.

It does not implement runtime behavior by itself.

It should guide the follow-up PR that makes compaction reuse the normal prepared conversation chain.

## Core Idea

Compaction should be cheap when the provider prompt cache is still warm.

The compaction request should reuse the same stable prefix as the normal agent reply request.

Here, stable prefix means the full normal message chain.

That includes the system prompt, tool descriptions, and all user, assistant, and tool messages in the conversation.

The compaction request should then append one small final instruction asking the model to compact the conversation.

The important point is that the cache is used during compaction.

This is not about reusing a cached prefix after compaction.

## Desired Prompt Shape

When the cache is warm, the compaction request should look like the normal reply request plus one extra final message.

The final message should ask for a durable summary of the conversation above.

The system prompt, tools, and previous messages should remain in the same order and same shape as the normal request.

That makes most of the compaction request identical to a request the model provider recently saw.

If the provider cache is still warm, most of the compaction input can be cache-read.

## Simplicity Goal

The implementation should avoid a special compaction-only history construction path if possible.

The normal prompt preparation path should prepare the full message chain first.

Compaction should then be a small final transform over that prepared chain.

This should make the code easier to reason about than maintaining a separate compaction serializer.

This may also reduce code if it replaces custom compaction serialization with the normal message preparation path.

## Cache Warmth

There should be an option for how long we expect the provider cache to remain warm.

If compaction happens inside that window, MindRoom can send the full normal chain and rely on prompt caching.

If compaction happens outside that window, MindRoom can still start from the full prepared chain and strip it down at the end.

This keeps construction simple because the normal preparation path still runs first.

## Cold Cache Shape

When the cache is probably cold, the final transform can remove prompt parts that do not need summarization.

That can include the system prompt, tool descriptions, and static initial setup messages.

Those removed parts can be replaced with a short note saying that static agent instructions and tool definitions were omitted.

The note should also say that those omitted parts do not need to be summarized.

The remaining content should preserve the conversation and tool activity that matters for future state.

Tool-call and tool-result messages must not be separated in a way that makes the remaining history invalid or confusing.

## Current MindRoom Behavior

Current MindRoom compaction does not use this shape.

It uses a separate compaction prompt and serializes compacted runs into `<previous_summary>` and `<new_conversation>` blocks.

That behavior is useful for correctness, but it does not preserve the normal request prefix for provider cache reuse.

Relevant current code is in `src/mindroom/history/compaction.py`.

The normal reply path also does not expose the whole provider-bound chain as one explicit value.

`src/mindroom/execution_preparation.py` returns `PreparedExecutionContext.messages` for the current request.

Persisted replay is represented separately through a replay plan that is applied to the Agno agent or team.

Static request parts such as the system prompt and tool definitions are represented through the agent or team object, not through the prepared messages tuple.

That means the current compaction path cannot simply append a final summary instruction to the same provider request shape used by a normal reply.

## Target Architecture

Introduce a prepared conversation chain seam.

The seam should describe the provider-bound request shape that a normal reply would send.

The interface should be small enough that callers do not need to know whether history came from persisted replay, raw Matrix fallback context, or current-turn messages.

The implementation can still use Agno sessions and replay plans internally.

The point is to give normal replies and compaction one shared place where conversation history is materialized, measured, and transformed.

This should make the module deeper because callers get one prepared-chain value instead of coordinating replay plans, current messages, rendered text, static token budgets, and fallback context themselves.

## Planned Module Shape

Add a focused module for prepared conversation chain construction.

A likely file name is `src/mindroom/prepared_conversation_chain.py`.

The module should own pure chain transforms and diagnostics.

It should not own durable session mutation, Matrix delivery, lifecycle notices, model loading, or tool execution.

The initial interface should expose a dataclass that carries ordered `Message` objects, rendered text for diagnostics, source run ids, seen Matrix event ids, and token estimates.

The interface should also identify whether the chain was built from persisted replay or fallback Matrix history.

Normal reply preparation should keep returning `PreparedExecutionContext`, but it should obtain its message chain and rendered text through this module.

Compaction should then consume the same prepared chain instead of serializing runs directly inside `history/compaction.py`.

## Warm-Cache Compaction Transform

Warm-cache compaction should append one final user instruction to the prepared normal chain.

That final instruction should ask for a durable summary of the conversation above.

The preceding prefix should stay byte-shape-compatible with the normal reply request as much as the provider and Agno surfaces allow.

The summary instruction must tell the model not to summarize static instructions or tool definitions.

The implementation should verify that tool-call and tool-result message adjacency remains valid after the transform.

If a provider surface cannot prevent tool calls while still keeping tool definitions in the request, the first implementation should keep the transform behind a capability check rather than silently changing tool behavior.

## Cold-Cache Compaction Transform

Cold-cache compaction should still start from the prepared normal chain.

It may then remove static prompt and setup material after preparation.

Removed static material should be replaced by a short note saying that static agent instructions and tool definitions were omitted and do not need summarization.

The transform must preserve conversation turns and tool activity that affect future state.

The transform must never separate a tool result from the tool call it answers.

The transform should be tested independently from model calls.

## Migration Plan

First, extract the existing compaction input construction from `src/mindroom/history/compaction.py` into a small internal module without changing behavior.

That gives the old XML-shaped serializer one owner while the new chain seam is introduced.

Second, introduce the prepared conversation chain module and move normal reply message rendering through it.

This should keep current agent, team, and OpenAI-compatible behavior unchanged.

Third, add tests that compare normal reply preparation and warm-cache compaction preparation for prefix equivalence.

Those tests should cover agent replies, team replies, Matrix fallback replay, persisted replay, and interrupted partial replies.

Fourth, add the warm-cache compaction transform behind an explicit policy decision.

The policy should use a cache-warm duration setting or an equivalent provider capability signal.

Fifth, add the cold-cache transform as a fallback from the same prepared-chain value.

Only after those steps pass should the direct `_serialize_run` compaction path be removed or kept as a legacy adapter.

## Acceptance Criteria

`history/compaction.py` should no longer own the normal conversation-to-message serialization rules.

Normal reply preparation and compaction preparation should share the same prepared-chain module.

The warm-cache compaction request should preserve the normal request prefix before the final summary instruction.

The cold-cache compaction request should be derived from the same prepared chain rather than from a second source of truth.

Metadata propagation should remain unchanged for Matrix seen-event ids, response event ids, prepared-history diagnostics, and compaction lifecycle metadata.

Existing destructive compaction semantics should remain unchanged unless a test explicitly documents the intended behavior change.

## Test Plan

Add unit tests for prepared-chain materialization from persisted replay.

Add unit tests for prepared-chain materialization from fallback Matrix thread history.

Add unit tests that assert the warm-cache compaction prefix matches the normal reply prefix before the final summary instruction.

Add unit tests that assert cold-cache stripping does not break tool-call and tool-result adjacency.

Run targeted history tests with `uv run pytest tests/test_agno_history.py -k compaction -q`.

Run targeted agent and team preparation tests that cover `execution_preparation.py`, `ai.py`, `teams.py`, and `api/openai_compat.py`.

Run `uv run tach check --dependencies --interfaces` if new module imports are added.

Run `uv run pre-commit run --all-files` before merging the implementation PR.

## What We Know About Codex

The current OpenAI Codex client uses a special `/responses/compact` endpoint.

The client sends current input, instructions, tools, and conversation or session headers to that endpoint.

The client source we inspected does not visibly construct a hand-rolled normal prompt plus compact instruction request.

The server may be doing a cache-aware compaction operation internally.

The exact server-side prompt caching behavior is not proven by the client source.

OpenAI describes this compact endpoint in the Codex agent loop article.

## What We Know About PiMono

The PiMono coding agent appears to use a separate compaction prompt.

It serializes the conversation into a wrapper such as `<conversation>...</conversation>`.

That means it does not preserve the normal request prefix in the way described here.

This looks similar in shape to MindRoom's current standalone compaction prompt.

## What We Know About py-pimono

The py-pimono code we inspected did not appear to have a compaction path.

It does replay session messages for normal model calls.

It also uses a prompt cache key for normal Codex requests.

That does not answer the compaction-specific question because no compaction path was found.

## Restrictions

Do not implement runtime behavior in this planning PR.

Do not add a second complex compaction construction path if the normal prompt preparation path can be reused.

Do not optimize for post-compaction prefix reuse when the actual goal is cheap compaction itself.

Do not summarize the system prompt or tool descriptions.

Do not strip system prompts, tool descriptions, or static setup messages before normal prompt preparation.

Prepare the normal chain first, then apply the cache-warm or cache-cold compaction transform at the end.

## Follow-Up Research

Figure out how Claude Code handles compaction.

Figure out how OpenCode handles compaction.

Compare both specifically on whether their compaction request preserves the normal request prefix.

Compare both specifically on whether they rely on prompt caching during compaction.

Compare both specifically on whether they use a special provider endpoint or a normal model call.
