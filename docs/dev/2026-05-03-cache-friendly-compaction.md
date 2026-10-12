# Cache-Friendly Compaction Idea

This record captures an idea discussed on May 3, 2026 for making the summary request of portable text compaction reuse the provider prompt cache.
[Native compaction](../configuration/history.md#native-compaction) runs inside ordinary provider requests and is outside this idea.

## Why

Compaction should be cheap when the provider prompt cache is still warm.
A compaction request that repeats the normal reply request's stable prefix (the system prompt, tool descriptions, and every user, assistant, and tool message, in the same order and shape) and appends one small final instruction asking for a durable summary of the conversation above is mostly identical to a request the provider recently saw, so most of its input can be cache-read.
The goal is cache reuse during the compaction request itself, not reuse of a cached prefix after compaction.
A standalone summary prompt over a serialized transcript, which portable compaction uses, keeps compaction correct but cannot hit the reply's cache.

## Decisions

- Prepare the full message chain through the normal prompt preparation path first, then apply compaction as a small final transform over that chain; while that path can be reused, avoid a separate compaction-only history construction path or serializer, because one preparation path is easier to reason about and may remove code.
- Never strip the system prompt, tool descriptions, or static setup messages before normal prompt preparation.
- Add an option for how long the provider cache is expected to remain warm.
  Inside that window, send the full normal chain plus the final instruction and rely on prompt caching.
- Outside that window, the final transform removes the parts that need no summary (the system prompt, tool descriptions, and static initial setup messages) and replaces them with a short note saying that static agent instructions and tool definitions were omitted and need not be summarized.
  The remaining content keeps the conversation and tool activity that matter for future state, and must never separate tool-call and tool-result messages in a way that makes the history invalid or confusing.
- Never summarize the system prompt or tool descriptions.
- Do not optimize for post-compaction prefix reuse, because the goal is cheap compaction itself.

## Outcome

The idea was built twice and closed unmerged: PR #1276, then PR #2059, which PR #2069 superseded with native provider compaction and a portable text fallback.
A live canary test on PR #1276 showed that a compaction instruction appended as a user message cannot override the agent's own system instructions: a system-prompt canary token was copied into the persisted summary.
PR #2059 measured the warm request at 14,574 input tokens, 14,336 of them cached, against 8,533 uncached tokens for the standalone request, and the warm request was slower (5.84 s against 4.32 s).

## How other agents compact (inspected May 2026)

- The OpenAI Codex client calls a special `/responses/compact` endpoint with the current input, instructions, tools, and conversation or session headers.
  Its source does not visibly build a normal prompt plus a compact instruction, so the server may compact cache-aware internally, but the client source does not prove the server-side caching behavior.
  OpenAI describes the endpoint in its Codex agent loop article.
- The PiMono coding agent appears to use a separate compaction prompt that serializes the conversation into a wrapper such as `<conversation>...</conversation>`, so it does not preserve the normal request prefix, much like MindRoom's standalone prompt.
- The inspected py-pimono code showed no compaction path; it replays session messages for normal calls and sends a prompt cache key on normal Codex requests, which does not answer the compaction question.

## Open questions

- How do Claude Code and OpenCode compact?
  Compare whether their compaction request preserves the normal request prefix, whether it relies on prompt caching, and whether it uses a special provider endpoint or a normal model call.
