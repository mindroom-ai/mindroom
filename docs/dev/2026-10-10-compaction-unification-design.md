# Compaction unification and mid-turn compaction

Design record, October 10, 2026, written for #2798 ("compact the same way everywhere, including inside long replies") and revised after three GPT-6 Astra design review rounds the same day.
Its storage design (the compaction archive, positioned generations, and reconcile) was replaced on October 12, 2026 by the log store in `docs/dev/2026-10-12-compaction-log-store-design.md`.
This record keeps only the decisions that do not depend on how compacted history is stored.

## Goal

Compaction behaves the same way for every agent, team, subagent, workflow participant, and agent mode: after a compaction the summary replaces all older messages, and the conversation builds on from there.
Compaction also runs inside a turn, between two model requests, so a long tool loop can compact and keep going.
After a compaction the request settles on a new stable prefix that later requests reuse, so prompt caching survives.
Every request that replays persisted history has the shape `[system, tools, summary message, remaining history, current input, current-turn messages]`, with the system prompt and tools byte-identical across compactions; after a mid-turn compaction the summary instead follows that turn's input.
A turn whose next request would exceed the context window compacts before that request and continues, on every route a MindRoom agent or team runs on as its primary model, including minimal agents and single-turn subagents.

## Why

Behavior on `main` when this design was written:

- Text compaction ran only before a reply or after a `compact_context` request; between turns it already matched the rule that nothing older than the summary replays.
- Agno rendered `session.summary` inside the system prompt, so every compaction rewrote the system prompt and lost the whole cached prefix.
- A string system message bypassed that builder, so minimal agents never saw the summary in their prompt; it existed only in the on-demand `agent-context` document.
- Inside a turn, portable text compaction never ran: native routes compacted provider-side, and every text route except Vertex AI Claude failed with a context-window error once a long reply outgrew the window.
- Vertex AI Claude dropped the oldest replayed turns on each request, which broke the prompt cache each time, and fell back from a native checkpoint to canonical history when the projected request did not fit.

## Decisions

### The summary is the first history message

- Agno no longer renders the summary; MindRoom inserts it as a user message after the leading system and developer messages and before replayed history, in new runs and continuations alike.
  A compaction therefore never changes the cached prompt prefix, and minimal agents and authored subagents get the summary with no special case while their system prompts stay byte for byte.
- The message tells the model that the earlier conversation was compacted and to continue recorded progress from its next steps instead of starting over, so the agent continues its task after a compaction instead of restarting it.
- A mid-turn compaction places the summary right after the current prompt instead, where the turn's folded work was, and marks that prompt so an approval resume and later turns replay the summary in the same place while that run replays first, which keeps the resumed request's prefix and the next turn's cached prefix; a turn that replays ahead of it (an approval completed after a later turn) was built on the summary, so the summary goes first again, ahead of any native checkpoint that turn recorded.
  A fast model that reads the prompt last can start the request over even when the summary records it finished: on a logged team-member request whose summary recorded all six file reads, gpt-6-luna restarted at the first file 12/12 with the summary before the prompt and 0/12 after it, and in a live run that member looped through about 30 folds until it was stopped.
  gpt-6.1-sol, gpt-6-astra, and gemini-3.8-flash answered in both orders, and with partial progress every model continued at the next file in both orders.
  Rewording the closing line without moving the summary restarted 12/12, and a firmer "do not call them again" line restarted 1/12 and would also discourage a repeat the user asks for in a later turn.
- The summary is never stored as a message: it is marked as history, so Agno drops it when it stores a run (now the agent loop does), later turns cannot replay it twice, and an approval resume rebuilds it together with the history it re-fetches.
- A replay plan that replays only the summary keeps history on with zero runs instead of turning history off, so the summary still replays.
  A scheduled task with `history_limit: 0` turns history off and therefore no longer sees the summary, matching "`0` for none" in the scheduling docs.
- A run paused before this release stored a system message that embeds Agno's summary block, and resuming it inserts no summary message, because leaving the paused prefix unchanged keeps the paused tool call's signed reasoning valid (a `LEGACY_COMPAT` rule).
- Native checkpoint routes bind a checkpoint to the rendered summary message instead of the raw summary text, so a checkpoint recorded while the summary lived in the system prompt is not reused; affected conversations rebuild from their stored history once, as after a model switch.

### A pre-request hook instead of Agno's CompressionManager

- Mid-turn compaction runs from a hook (now one of the agent loop's `before_request` hooks) around the model's per-request response processing, which Agno calls once per provider request with the loop's live messages, the formatted tools, and the run output.
- Agno has no awaitable pre-request hook with mutable messages, and its `CompressionManager` is a partial extension point limited to tool results and tied to `compress_tool_results`, so the hook was an `AGNO_COMPAT` workaround on the seam the tool-call cap already used, until MindRoom [stopped running Agno's agent loop](2026-10-20-replace-agno-design.md).
- The tool-call cap wraps outside the compaction hook, so it refuses a request before compaction spends a summary call.
- The trigger is the limit the pre-reply trigger already applies, the replay window minus `reserve_tokens`.
  `reserve_tokens` stays the one headroom setting and must cover the model's maximum output, including any thinking budget, because no provider-neutral output setting exists.
- A request is sized from the latest response's provider-reported input context plus an estimate of the messages after it when the loop has received a response, and otherwise by estimate.
  A response loaded with a resumed run never anchors the size, because the resume rebuilt the prefix it was billed against, and a response whose counters are all zero never anchors it either.
  Native routes size their projected request by estimate, because a response that created a checkpoint is billed for the transcript it replaced.
- On a native route the provider compacts inside the request, so the hook acts only when the projected request still exceeds the limit; it then turns native checkpoint replay off for the rest of the run and compacts the canonical messages as a text route does.

### What stays verbatim in a compacted request

- A compacted request keeps the leading system and developer messages, the current prompt, transient messages, and queued-message notices in their original order, with the summary right after the prompt, and folds everything else, including every current-turn assistant and tool message.
- Folding every model and tool message of the turn and keeping only the user's input is what Codex CLI's inline auto-compaction and Claude Code's auto-compaction do, and it leaves no tool-call pairs, signed reasoning, or provider response chains for the rewritten request to keep valid.
- Transient messages, such as the per-turn transient context and the approval receipt, stay because they are never stored, so they are never summarized either.
- The rest of the turn's input, such as unseen thread messages, is folded, so a large unseen thread context with a short prompt does not survive the fold.
- The summarizer sees every folded current-turn tool call and result, because `max_tool_calls_from_history` limits replayed history only.
- The run's tool trace is untouched, so the visible reply still lists every tool call.
- With nothing to fold, compaction is skipped.
- When the system prompt, tools, current prompt, and new summary exceed the model's `context_window` minus `reserve_tokens`, the request fails with `Saved conversation summary exceeds the available history budget` instead of being sent oversized.
  When they fit the model window but still exceed the replay window, the run stops compacting, since compacting again would only summarize each new tool result before the model saw it.

### Run-local compaction for requests without persisted replay

- A request without persisted replay (team members, `room_agent` workflow participants, a scheduled task with `history_limit: 0`) compacts only its own turn, with the same summary model, prompt, budgets, and fallback.
- Its summary is an ordinary message of its own run, stored with it, so a member's approval resume replays it.
- A team leader's request is scoped to the team, members keep no history or summary of their own, and each member's tool loop compacts run-locally.

### Counting the summary once

- The replay planner estimates the rendered summary message through the same rendering function that produces it.
- The static-prompt estimator builds the system message from an empty session, which no longer contains a summary in any case.
- The mid-turn trigger sizes the actual request list, which holds the summary once.

### Removing Vertex AI Claude per-request trimming

- Vertex AI Claude's per-request fitting (trimming, exact counting, and native fallback) was deleted, because mid-turn compaction replaces it on every route and the trimming broke the prompt cache each time it dropped turns.

### What the user sees

- Mid-turn compaction posts the same `Compacting history...` notice in the thread, edits it with the result, and the streamed reply continues after the summary call.
- Approval continuations, delegated children, team members, and workflow participants compact without a notice.
- The failure notice became `Compaction failed; continuing without compaction.` for both paths, because a mid-turn failure continues with the uncompacted request.
- The `compaction:before` and `compaction:after` hooks fire for scoped mid-turn compaction as for pre-reply compaction; run-local compaction has no scope and emits none.

## Rejected alternatives

- **A persisted summary run** at the start of the runs table, so Agno replays it natively: it counts against `num_history_runs` and `num_history_messages`, disappears when that run is redacted, discarded as empty, or falls out of the replay window, and every runs reader would need to special-case it.
- **Request projection**, keeping the full turn in the run record and marking folded messages so replay skips them: every reader of run messages (Agno's replay, MindRoom's estimators, summary input, continuation) would have to honor the fold marker, and a missed reader replays folded content twice.
- **Compacting in memory only, for every target**: the next reply finds the older runs still stored raw and compacts again, paying a second summary call for work the first summary already covered, so only requests without persisted replay compact run-locally.
- **Keeping the last tool exchange verbatim**: the kept assistant message's signed thinking is bound to the replaced prefix on Claude models with adaptive thinking, Gemini and OpenAI carry their own reasoning and response-chain rules, and a provider-specific keep-or-strip contract is more code than the fidelity it buys.

## Limits and out of scope

- With no assistant message left after a fold, OpenAI Responses cannot restore folded items through `previous_response_id`.
- Fallback models do not get the mid-turn hook, because a fallback serves only after the primary fails.
- Turning native replay off does not remove an authored `compact_20260112` policy from the request, as in the pre-reply fallback.
- Synchronous Agno response loops are not covered, because MindRoom runs every agent and team asynchronously; the agent loop that replaced them has no synchronous form.
- An approval resume of a scheduled task with `history_limit` rebuilds its agent with the configured history settings instead of the task's limit; this predates the design and is unchanged by it.
- With the summary in every agent's history, `agent-context` is no longer any agent's route to it; whether minimal authored subagents keep the configured agent's `role`, `instructions`, and `agent-context` documents belongs to #2794, as do its prompt-builder and skills changes.
