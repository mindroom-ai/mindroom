# Compaction unification and mid-turn compaction

Design spec, October 10, 2026, revised after three GPT-6 Astra design review rounds the same day.
Branch base: `origin/main` at `d649e5657` (#2776 merged).

## Goal

Compaction behaves the same way for every agent, team, subagent, workflow participant, and agent mode: after a compaction the summary replaces all older messages, and the conversation builds on from there.
Compaction also runs inside a turn, between two model requests, so a long tool loop can compact and keep going.
After a compaction the request settles on a new stable prefix that later requests reuse, so prompt caching survives.

Success criteria:

- Every request that replays persisted history has the shape `[system, tools, summary message, remaining history, current input, current-turn messages]`, with the system prompt and tools byte-identical across compactions.
- A turn whose next request would exceed the context window compacts before that request and continues, on every route a MindRoom agent or team runs on as its primary model, including minimal agents and single-turn subagents.
- An approval resume replays a summary consistent with the history it replays, the archive's current state, so a resumed request never repeats or contradicts its summary.
- Redacting an event that compacted history depends on still rolls compaction back to just before the run that consumed, answered, or wrote it.

## Current behavior (verified on `main`)

- Text compaction folds every visible run into a cumulative summary (`history/compaction.py` `compact_scope_history`), moves the runs into the archive (`history/archive.py`), and caches the latest generation summary as `session.summary` (`history/storage.py`).
  Between turns this already matches the user's rule: after a compaction nothing older than the summary replays.
- It runs only before a reply (`history/runtime.py` `prepare_scope_history`), or after a `compact_context` request.
- Agno renders `session.summary` inside the system prompt when `add_session_summary_to_context` is on (`agents.py` sets it to `persist_runtime_state`, `teams.py` to `True`), so every compaction rewrites the system prompt and loses the whole cached prefix.
- A string `system_message` bypasses that builder, so minimal agents never see the summary in their prompt; it only exists in the on-demand `agent-context` document.
  Authored subagents got it through the interim `with_session_summary` code from #2776, which #2794 deleted when it moved them onto the normal prompt builder.
- Inside a turn, portable text compaction never runs; native routes compact provider-side, Vertex AI Claude drops the oldest replayed turns per request (`vertex_claude_compat.py` `_fit_request_messages`), which breaks the cache each time, and falls back from a native checkpoint to canonical history when the projected request does not fit; every other text route fails with a context-window error.
- MindRoom never stores history copies in runs (`store_history_messages=False` for agents and teams), so an approval resume of a stored run re-fetches history from the session (`_build_continue_run_messages`, `input_has_history` is false), while a continuation handed the in-memory run (team delegation) keeps the history it was given, and MindRoom's builder patch (`history/agno_compat_message_builder.py`) already wraps both the new-run and the continuation builders.
  A paused run does keep its system message, which Agno reuses on resume.
- Agno loads a run's session at its start and writes that object's row at its end; `cache_session` is off and `RunContext` holds no session, so no public API returns the object a running loop will write.
- Native compaction routes compact inside each provider request and replay the provider checkpoint (`native_compaction.py` `native_replay_messages`); the route identity hashes `session.summary`.
- `model_usage.context_input_tokens_from_counts` already turns a response's provider usage counters into the full request-context size, accounting for providers that report cache tokens outside `input_tokens`.

## Approaches considered

1. **Derived summary message, plus a pre-request hook that folds the whole conversation except the current input (chosen).**
   The summary stays derived from the archive's latest generation and is inserted as the first history message by MindRoom's existing message-builder patch; mid-turn compaction reuses the scope compactor from a hook that runs before every model request.
   Folding every model and tool message of the turn, keeping only the user's input, is what Codex CLI's inline auto-compaction and Claude Code's auto-compaction do, and it leaves no tool-call pairs, signed reasoning, or provider response chains for the rewritten request to keep valid.
2. **Persisted summary run.**
   Store each summary as a synthetic live run at the start of the runs table so Agno replays it natively.
   Rejected: it counts against `num_history_runs` and `num_history_messages`, disappears when that run is redacted, discarded as empty, or falls out of the replay window, and every runs reader would need to special-case it.
3. **Request projection.**
   Keep the full turn in the run record and project each outbound request, marking folded messages so replay skips them.
   Rejected: every reader of run messages (Agno's replay, MindRoom's estimators, summary input, continuation) would have to honor the fold marker, and a missed reader replays folded content twice.
4. **Keep the last tool exchange verbatim after a mid-turn compaction.**
   Rejected: the kept assistant message's signed thinking is bound to the replaced prefix on Claude models with adaptive thinking, Gemini and OpenAI carry their own reasoning and response-chain rules, and a provider-specific keep-or-strip contract is more code than the fidelity it buys.

## Design

### 1. The summary is the first history message

- `add_session_summary_to_context=False` for every Agent and Team, so Agno never renders the summary.
- `history/replay.py` owns one rendering function, `compaction_summary_message(summary, *, from_history)`, returning a `user` message whose `provider_data` carries the marker `mindroom_compaction_summary`:

  ```text
  The earlier part of this conversation was compacted into this summary:

  <compacted_history>
  {summary}
  </compacted_history>

  The conversation continues after this summary. If it records progress on the request that follows, continue that work from its next steps instead of starting over.
  ```

- The builder patch inserts it into the run messages of every Agent and Team request whose target replays persisted history (the `add_history_to_context` value the builder resolves for that request), new runs and continuations alike, directly after the leading system and developer messages and before replayed history.
  It reads the summary from the `session` the builder received and skips insertion when the list already holds a marked summary with `from_history=True`, which keeps an in-memory continuation's summary paired with the history it carries; a stored run-local summary (section 6) never suppresses it.
- Replay plans that replay only the summary keep `add_history_to_context=True` with `num_history_runs=0`, instead of turning history off, so the summary still replays; a scheduled task with `history_limit=0` turns history off and therefore no longer sees the summary, matching "`0` for none" in `docs/scheduling.md`.
- The inserted message is `from_history=True`, so Agno drops it when it stores the run (`store_history_messages=False`) and never replays it from a run.
- Minimal agents and authored subagents get the summary through this path with no special case; their system prompts stay byte for byte, and `agent-context` no longer contains a summary.

### 2. Persistence and approval resume (question 1)

- The summary is never stored as a message.
  It is derived from the archive's latest generation, cached in `session.summary`, at each request build.
- A paused run stores only its non-history messages, so its resume re-fetches history from the session and the builder re-inserts the summary with it; both come from the same session state.
  When the conversation changed in between (another turn, a compaction, a redaction), the resume sees the new state consistently, as it already does for replayed runs.
- Later turns cannot replay the summary twice, because no stored run contains it.
- No session migration is needed (question 8): existing sessions already cache the latest generation summary.
- A run paused before this release stored a system message that embeds Agno's summary block; resuming it inserts no summary message, so that request keeps the single summary it was paused with (a `LEGACY_COMPAT` rule in `history/legacy_summary_system_prompt.py`, keyed on Agno's `<summary_of_previous_interactions>` wrapper).
  Leaving the paused prefix unchanged keeps the paused tool call's signed reasoning valid; if the conversation compacted between pause and resume, that one resumed request pairs the old summary with the newer history.
  A mid-turn compaction of such a resumed request folds that tool call anyway, so its rewrite also removes the legacy block from the system message and leaves one summary.
- Native checkpoint routes hash the rendered summary message instead of the raw summary text, so a checkpoint recorded while the summary lived in the system prompt is not reused with a request that now carries it as a message; affected conversations rebuild from their stored history once, as after a model switch.

### 3. Mid-turn compaction hook (question 2)

- `agno_compat_model_hooks.py` gains `install_request_preparation(model, *, marker, prepare)`.
  It wraps the model instance's `_aprocess_model_response` and `aprocess_response_stream`, which Agno calls once per provider request with the loop's live `messages` list, the formatted `tools`, and `run_response`, and awaits `prepare(messages, tools=..., run_response=...)` before delegating.
  This is the seam the tool-call cap already uses; it gets its own `AGNO_COMPAT` marker (no awaitable pre-request hook with mutable messages; Agno's `CompressionManager` is a partial extension point limited to tool results and tied to `compress_tool_results`).
- A new module, `history/mid_turn_compaction.py`, owns the policy.
  `create_agent` and team creation call `install_mid_turn_compaction(target, config=..., runtime_paths=..., entity_name=..., model_name=...)` on the target's model once the Agent or Team exists, and install the tool-call cap after it, so the cap's wrapper is outermost and refuses a request before compaction spends a summary call.
  Response preparation (`ai.py`, `teams.py`) attaches the turn's `CompactionLifecycle` to that binding; paths without one (approval continuations, delegated children, team members, workflow participants) compact without a notice.
- The builder patch records on the same binding the session object Agno built the request from, because that is the object Agno writes at the end of the run (section 5).
- Before each request the hook:
  1. skips when compaction is disabled or text compaction is unavailable for the entity, or when this run already failed a mid-turn compaction;
  2. sizes the request: when this response loop has already received a response, the provider-reported input context of the latest one (`context_input_tokens_from_counts` over its usage counters, or the native final-iteration `context_usage` when present) plus an estimate of that assistant message and every message after it; otherwise the replay planner's estimate of the whole list plus the tool definitions.
     A response loaded with a resumed run never anchors the size, because the resume rebuilt the prefix it was billed against, and a response whose counters are all zero (a server that reports no usage) never anchors it either; native routes always size their projected request (checkpoint plus tail) by estimate, because a response that created a checkpoint is billed for the transcript it replaced;
  3. compacts when that size exceeds the replay window minus `reserve_tokens`, the same limit the pre-reply trigger applies; `reserve_tokens` stays the one headroom setting and must cover the model's maximum output, including any thinking budget, because no provider-neutral output setting exists.
- On a native route the provider compacts inside the request, so the hook only acts when the projected request still exceeds the limit; it then turns MindRoom's native checkpoint replay off for the rest of the run and compacts the canonical messages as a text route does, replacing Vertex AI Claude's per-request fallback.
- Vertex AI Claude's per-request fitting (trimming, exact counting, and native fallback) is deleted.

### 4. What stays verbatim (question 3)

The rewritten request is `[leading system and developer messages, summary message, current prompt, transient messages, queued-message notices]`, in their original relative order:

- **Current prompt**: the last message of this turn's input, which MindRoom always builds as the user's current message with its attachments; identified by the last message id in `run_response.input.input_content` when the input is a message list (MindRoom's agents and teams), otherwise Agno's single user message.
  Agno stores `run_response.input` with message ids, so a continuation finds the same prompt.
- **Transient messages**: messages with `add_to_agent_memory=False`, such as the per-turn transient context and the approval receipt; they are never stored, so they are never summarized either.
- **Queued-message notices**: the hidden notices that a newer message is waiting, recognized by their existing marker.
- **Folded**: replayed history, an earlier summary, the rest of the turn's input (unseen thread messages and other context), and every other current-turn message (assistant, tool, tool media follow-ups).
  The summarizer sees every folded current-turn tool call and result: `max_tool_calls_from_history` limits replayed history only, so it is not applied to the in-progress snapshot.
  The run's tool trace (`run_response.tools`) is untouched, so the visible reply still lists every tool call.
- With no assistant message left, Agno has no `response_id` to chain from, so OpenAI Responses cannot restore folded items through `previous_response_id`, whether or not portable replay is on.
- With nothing to fold, compaction is skipped.

### 5. Scoped compaction and the archive (question 4)

A request is scoped when its target replays persisted history (`add_history_to_context`) and has session storage.

- The hook runs the existing scope compactor (`compact_scope_history` through `history/runtime.py`) on the session object Agno loaded for this run, which the builder patch recorded, after reconciling it; the compactor already adopts the freshest stored row into that object after every committed chunk, so Agno's end-of-run row write carries the new summary even when a later chunk fails or the run is cancelled.
  The one addition is an in-progress snapshot appended as the last compactable run.
- The snapshot is a `RunOutput` or `TeamRunOutput` with the scope's agent or team id, status completed, a copy of the live run's metadata (every Matrix event id the run has consumed so far), and the folded current-turn messages plus the current input as context for the summarizer.
  Its run id is unique per snapshot and starts with the live run's id (`archive.snapshot_run_id(origin_run_id)` appends a fresh suffix), so the archive can find every snapshot of a run, two snapshots of one run never collide, and none collides with the live run, which keeps its own id and stays live.
- Redaction treats a run and its snapshots as one: when redaction removes a live run, or hits a run archived later, compaction rolls back to the earliest archived row of that run or any of its snapshots, using the existing `roll_back_to`.
  Redaction therefore keeps the ids of the runs it removes (`remove_run_by_event_id` returns them) and looks up their snapshots before rolling back.
  This covers events the run consumed after its snapshot was taken and the reply event attached to the live run after it finished.
  Snapshots are always the last run of their generation and are never restored as live runs.
- The in-flight list is rewritten only when the snapshot was archived, which means every earlier run was archived too; that includes a cancellation that arrives after the snapshot's chunk committed, where the hook rewrites the list before the cancellation propagates.
  A compaction that commits earlier chunks and then fails leaves the request unchanged and marks the run as failed for mid-turn compaction; storage and the live session stay consistent, and the next reply's preparation continues from the committed chunks.
- After the rewrite the hook re-sizes the request; when the system prompt, tools, current prompt, and new summary still exceed the limit, it raises the error replay planning already uses, `Saved conversation summary exceeds the available history budget`, instead of sending an oversized request.
- The force-compaction flag is left untouched, so a `compact_context` request made during the turn still applies before the next reply.
- Folded assistant messages leave the run record, so their per-request usage is appended to `run_response.metadata` under a dedicated key that `usage_storage.project_usage` merges into the run's request list in order; run totals and the request breakdown stay complete across repeated compactions, approval resumes, and cancellation.
  The summary input omits that metadata key.

### 6. Run-local compaction (question 5)

A request without persisted replay (team members, `room_agent` workflow participants, a scheduled task with `history_limit=0`) compacts only its own turn:

- The folded messages and any earlier run-local summary are summarized with the same summary model, prompt, budgets, and fallback, without touching the archive.
- The summary call's usage is recorded as `compaction_summary` usage in the target's session storage when it has one, else under the reply's helper usage owner, else as system usage, so no summary call goes unaccounted.
- The summary message is an ordinary message (`from_history=False`), so it is stored with the run and a member's approval resume replays it.
- Teams: the leader's request is scoped to the team scope, with the team summary first and its in-progress snapshot a `TeamRunOutput`; member runs stay attached to the live team run.
  Members keep no history or summary of their own, and each member's tool loop compacts run-locally.

### 7. Counting the summary once (question 6)

- The replay planner estimates the rendered summary message through the same rendering function.
- The static-prompt estimator builds the system message from an empty session, which no longer contains a summary in any case.
- The mid-turn trigger sizes the actual request list, which holds the summary once.

### 8. What the user sees (question 7)

- Mid-turn compaction posts the same `Compacting history...` notice in the thread, edits it with progress and the result, and the streamed reply continues after the summary call.
- The failure notice becomes `Compaction failed; continuing without compaction.` for both paths, since a mid-turn failure continues with the uncompacted request.
- Compaction hooks (`compaction:before`, `compaction:after`) fire for scoped mid-turn compaction exactly as for pre-reply compaction; run-local compaction has no scope and emits none.

### 9. Minimal authored subagents' context documents (question 9)

With the summary in every agent's history, `agent-context` is no longer any agent's route to it.
#2794 owns whether minimal authored subagents keep the configured agent's `role`, `instructions`, and `agent-context` documents; this design recommends dropping them and needs nothing from that choice.

## Ownership and boundaries

| Module | Change |
| --- | --- |
| `history/replay.py` | Summary message rendering and estimate; summary-only plans as zero runs; request sizing helpers for the mid-turn trigger. |
| `history/agno_compat_message_builder.py` | Inserts the summary message in Agent and Team new-run and continuation builders and records the request's session (new `AGNO_COMPAT` marker). |
| `history/legacy_summary_system_prompt.py` (new) | Recognizes a paused system message that already embeds Agno's summary block. |
| `agno_compat_model_hooks.py` | `install_request_preparation` (new `AGNO_COMPAT` marker). |
| `history/mid_turn_compaction.py` (new) | Binding, trigger, in-flight rewrite, snapshot construction, scoped and run-local compaction, native fallback, usage carry-over. |
| `history/compaction.py`, `history/runtime.py` | Accept the in-progress snapshot; expose a run-local summary call that reuses chunk generation without archive writes. |
| `history/archive.py`, `history/storage.py` | Snapshot run ids; redaction rolls back to a run's earliest snapshot; restore skips snapshots. |
| `history/native.py` | Route identity from the rendered summary message. |
| `model_usage.py` | Request-context size of one assistant message, shared with run metadata. |
| `agents.py`, `teams.py`, `ai.py` | Flag off, hook installation order, lifecycle binding. |
| `vertex_claude_compat.py` | Per-request fitting deleted. |
| `usage_storage.py`, `history/summary_input.py` | Carried-over request usage merged into projections and omitted from summary input. |
| `delivery_gateway.py` | Failure notice wording. |

`tach.toml` gains the new modules; `docs/architecture/compaction.md`, `docs/architecture/agno-compatibility.md`, `docs/architecture/migrations.md`, and `docs/architecture/code-map.md` follow the code.

## User docs

`docs/configuration/history.md` changes once:

- Context Window: replace the Vertex AI Claude paragraph with mid-turn compaction (every request is checked; one that would exceed the window compacts first; one whose current input alone cannot fit fails with a provider error).
- Agent Compaction Settings: text compaction runs before a reply and between model requests of a reply; after a mid-turn compaction the agent keeps the user's request and a summary of its work so far; the notice can appear mid-reply; the new failure wording.
- Native compaction: native routes fall back to text compaction for the rest of a reply whose request no longer fits.
- Team History and Compaction: members compact their own long tool loops.

## Testing

Failing-first tests at the owning seams (history modules, the builder patch, the hook, `ResponseRunner` only where wiring matters):

- Summary message first and system prompt byte-identical across a compaction, for a configured agent, a team, a minimal agent, an authored subagent, a `run_subagent` child, and a summary-only replay plan.
- Approval resume after a pre-reply compaction and after a mid-turn compaction replays a summary consistent with its replayed history (agent and team), and a resumed run paused before this release keeps its single embedded summary.
- One long single-turn run on a scripted model compacts mid-turn, continues, and finishes; the next turn replays `[summary, current input, post-compaction messages, final answer]` behind an unchanged prefix, and the session row written at the run's end already holds the new summary.
- The first request of a loop, before any current-turn assistant message, folds history and unseen thread context and keeps the current prompt and transient context once.
- A large unseen thread context with a short current prompt is folded, not kept.
- A resumed run sizes its first request by estimate, not by the paused response's usage.
- A rewritten request that still exceeds the limit raises `Saved conversation summary exceeds the available history budget`.
- A cancellation after the snapshot's chunk committed still leaves the request rewritten and the live session holding the new summary.
- A team member's long tool loop compacts run-locally and its approval resume keeps the run-local summary.
- Redacting each of: an event the run consumed before its snapshot, an event it consumed after its snapshot, and its reply event, rolls back to before the run, after one and after two mid-turn compactions, including team member runs.
- A compaction that commits an earlier chunk and then fails leaves the request unchanged and storage consistent.
- Request usage of folded requests stays in the run's usage projection, in order and exactly once, and reconciles with run totals across two compactions, an approval resume, and a cancellation.
- Request sizing anchors on provider usage for providers that report cache tokens inside and outside `input_tokens`, and falls back to estimates when usage is missing.
- An oversized native request turns native compaction off and compacts as text; a native request under the limit never text-compacts.
- `tests/test_compaction_fuzz.py` extended with mid-turn snapshots and their redaction in generated histories.

Live tests over Matrix on a text route (Gemini or a local OpenAI-compatible model) and a native route, recording provider cache-read tokens for the requests before and after a pre-reply and a mid-turn compaction, and checking that after a mid-turn compaction the agent continues its task instead of restarting it.
A Claude text route (for example with an explicit `compaction.model`) also runs one mid-turn compaction when credentials allow, since Claude validates reasoning most strictly.

## Out of scope

- An approval resume of a scheduled task with `history_limit` rebuilds its agent with the configured history settings instead of the task's limit; this predates the design and is unchanged by it.
- Turning native replay off does not remove an authored `compact_20260112` policy from the request, as in today's pre-reply fallback.

- Fallback models (`FallbackConfig`) do not get the mid-turn hook; a fallback serves only after the primary fails.
- Synchronous Agno response loops; MindRoom runs every agent and team asynchronously.
- The prompt-builder and skills changes of #2794.
