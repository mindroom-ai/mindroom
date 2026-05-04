# Cache-Friendly Compaction Idea

This note records a follow-up idea discussed on May 3, 2026.

It is not part of the current history compaction PR.

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

Do not implement this in the current PR.

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

