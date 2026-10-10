# Compaction unification and mid-turn compaction

Design spec, October 10, 2026.
Branch base: `origin/main` at `d649e5657` (#2776 merged).

## Goal

Compaction behaves the same way for every agent, team, subagent, workflow participant, and agent mode: after a compaction the summary replaces all older messages, and the conversation builds on from there.
Compaction also runs inside a turn, between two model requests, so a long tool loop can compact and keep going.
After a compaction the request settles on a new stable prefix that later requests reuse, so prompt caching survives.

Success criteria:

- Every request that replays persisted history has the shape `[system, tools, summary message, remaining history, current input, current-turn exchanges]`, with the system prompt and tools byte-identical across compactions.
- A turn whose next request would exceed the context window compacts before that request and continues, on every text-compaction route, including minimal agents and single-turn subagents.
- An approval resume replays the same summary the paused request saw.
- Redacting an event that compacted history depends on still rolls compaction back to just before the run that consumed it.

## Current behavior (verified on `main`)

- Text compaction folds every visible run into a cumulative summary (`history/compaction.py` `compact_scope_history`), moves the runs into the archive (`history/archive.py`), and caches the latest generation summary as `session.summary` (`history/storage.py`).
  This already matches the user's rule between turns: after a compaction nothing older than the summary replays.
- It runs only before a reply (`history/runtime.py` `prepare_scope_history`), or after a `compact_context` request.
- Agno renders `session.summary` inside the system prompt when `add_session_summary_to_context` is on (`agents.py` sets it to `persist_runtime_state`, `teams.py` to `True`), so every compaction rewrites the system prompt and loses the whole cached prefix.
- A string `system_message` bypasses that builder, so minimal agents never see the summary in their prompt; it only exists in the on-demand `agent-context` document.
  Authored subagents get it through the interim `with_session_summary` code from #2776, which the persona PR (brief `/work/handoffs/persona-prompt-builder-20261010/PROMPT.md`) deletes.
- Inside a turn nothing compacts: Vertex AI Claude drops the oldest replayed turns per request (`vertex_claude_compat.py` `_fit_request_messages`), which breaks the cache each time; every other provider fails with a context-window error.
- MindRoom never stores history copies in runs (`store_history_messages=False` for agents and teams), so an approval resume re-fetches history from the session (`_build_continue_run_messages`, `input_has_history` is false) and MindRoom's builder patch (`history/agno_compat_message_builder.py`) already wraps both the new-run and the continuation builders.
- Native compaction routes compact inside each provider request and replay the provider checkpoint (`native_compaction.py` `native_replay_messages`); the route identity hashes `session.summary`.

## Approaches considered

1. **Derived summary message plus a pre-request mid-turn hook (chosen).**
   The summary stays derived from the archive's latest generation and is inserted as the first history message by MindRoom's existing message-builder patch; mid-turn compaction reuses the scope compactor from a hook that runs before every model request.
2. **Persisted summary run.**
   Store each summary as a synthetic live run at the start of the runs table so Agno replays it natively.
   Rejected: it counts against `num_history_runs` and `num_history_messages`, disappears when that run is redacted, discarded as empty, or falls out of the replay window, and every runs reader would need to special-case it.
3. **Request projection.**
   Keep the full turn in the run record and project each outbound request (summary plus kept tail), marking folded messages so replay skips them.
   Rejected: every reader of run messages (Agno's replay, MindRoom's estimators, summary input, continuation) would have to honor the fold marker, and a missed reader replays folded content twice.

## Design

### 1. The summary is the first history message

- `add_session_summary_to_context=False` for every Agent and Team, so Agno never renders the summary.
- `history/replay.py` owns one rendering function, `compaction_summary_message(summary, *, from_history)`, returning a `user` message whose `provider_data` carries the marker `mindroom_compaction_summary`:

  ```text
  The earlier part of this conversation was compacted into this summary:

  <compacted_history>
  {summary}
  </compacted_history>

  The messages after this one continue from where the summary ends; trust them when they conflict with it.
  ```

- The builder patch inserts it into the run messages of every Agent and Team request whose target replays persisted history (`add_history_to_context` resolved for that request), new runs and continuations alike, directly after the leading system and developer messages and before replayed history.
  It reads the summary from the `session` the builder received and skips insertion when the list already holds a marked summary.
- Replay plans that replay only the summary keep `add_history_to_context=True` with `num_history_runs=0`, instead of turning history off, so the summary still replays; a scheduled task with `history_limit=0` turns history off and therefore no longer sees the summary, matching "`0` for none" in `docs/scheduling.md`.
- The inserted message is `from_history=True`, so Agno drops it when it stores the run (`store_history_messages=False`) and never replays it from a run.
- Minimal agents and authored subagents get the summary through this path with no special case; their system prompts stay byte for byte.

### 2. Persistence and approval resume (question 1)

- The summary is never stored as a message.
  It is derived from the archive's latest generation, cached in `session.summary`, at each request build.
- A paused run stores only its non-history messages, so its resume re-fetches history from the session and the builder re-inserts the summary with it; both come from the same session state, which is exactly what the paused request saw unless the conversation changed in between, the existing contract of `test_resumed_approval_run_replays_the_history_the_paused_request_saw`.
- Later turns cannot replay the summary twice, because no stored run contains it.
- No migration is needed (question 8): existing sessions already cache the latest generation summary.
- Native checkpoint routes hash the rendered summary message instead of the raw summary text, so a checkpoint recorded while the summary lived in the system prompt is not reused with a request that now carries it as a message; affected conversations rebuild from their stored history once, as after a model switch.

### 3. Mid-turn compaction hook (question 2)

- `agno_compat_model_hooks.py` gains `install_request_preparation(model, *, marker, prepare)`.
  It wraps the model instance's `_aprocess_model_response` and `aprocess_response_stream`, which Agno calls once per provider request with the loop's live `messages` list, the formatted `tools`, `response_format`, and `run_response`, and awaits `prepare(...)` before delegating.
  This is the seam the tool-call cap already uses; it gets its own `AGNO_COMPAT` marker (no awaitable pre-request hook with mutable messages; Agno's `CompressionManager` is a partial extension point limited to tool results and tied to `compress_tool_results`).
- A new module, `history/mid_turn.py`, owns the policy.
  `create_agent` and team creation call `install_mid_turn_compaction(target, config=..., runtime_paths=..., entity_name=..., model_name=...)` on the target's model once the Agent or Team exists, and install the tool-call cap after it, so the cap's wrapper is outermost and refuses a request before compaction spends a summary call.
  Response preparation (`ai.py`, `teams.py`) attaches the turn's `CompactionLifecycle` to that binding; paths without one (approval continuations, delegated children, team members, workflow participants) compact without a notice.
- Before each request the hook:
  1. skips when compaction is disabled or unavailable for the entity, when the model has active native compaction, or when this run already failed a mid-turn compaction;
  2. estimates the request with the replay planner's message estimator plus the tool definitions, and, when the estimate passes half the limit, asks the model for an exact count through Agno's public `acount_tokens` (falling back to the estimate when counting fails);
  3. compacts when the count exceeds the replay window minus `reserve_tokens`, the same limit the pre-reply trigger applies (history above `hard_replay_budget_tokens` means exactly that the whole request passes it).
- Vertex AI Claude's per-request trimming is deleted; its exact count, which handles payloads Vertex's count endpoint rejects, becomes that model's `acount_tokens`.

### 4. What stays verbatim (question 3)

The rewritten request is `[leading system and developer messages, summary message, current input, last exchange]`:

- **Current input**: this turn's input messages, identified by the message ids in `run_response.input.input_content` when the input is a message list (MindRoom's agents and teams), otherwise Agno's single user message.
  Input survives continuations, because Agno stores `run_response.input` with message ids.
- **Last exchange**: the last assistant message and everything after it (its tool results, tool media follow-ups, queued-message notices).
  It keeps the signed thinking, thought signatures, and reasoning items providers require for the tool call being answered.
- **Folded**: replayed history, an earlier summary, and every current-turn message between the input and the last exchange.
  With nothing foldable, compaction is skipped.

### 5. Scoped compaction and the archive (question 4)

A request is scoped when its target replays persisted history (`add_history_to_context`) and has session storage.

- The hook loads the scope's latest session, reconciles it, and runs the existing scope compactor (`_run_scope_compaction_with_lifecycle` and `compact_scope_history`) with one addition: an in-progress snapshot run appended as the last compactable run.
- The snapshot is a fresh `RunOutput` or `TeamRunOutput` with a new run id, the scope's agent or team id, status completed, a copy of the live run's metadata (so it carries every Matrix event id the run consumed), and the folded current-turn messages plus the current input as context for the summarizer.
  The live run keeps its own run id and stays live; it never appears among the compactable runs.
- The snapshot is archived in the same transaction as the generation that folded it, so redacting any event the in-progress run consumed finds the snapshot first and rolls back that generation with the existing `roll_back_to`: earlier archived runs return to the live table, and the live run and everything after it are removed.
  Snapshots are always the last run of their generation, so they are never restored as live runs.
- The in-flight list is rewritten only when the snapshot was archived, which means every earlier run was archived too; a compaction that stops early leaves the request unchanged, and the next request retries from the committed chunks.
- After a successful rewrite the hook updates the run's own Agno session (`aget_session`, which returns the session cached for this run) with the new summary and without the archived runs, so the end-of-run session write does not restore a stale summary; reconciliation remains the safety net.
- The force-compaction flag is left untouched, so a `compact_context` request made during the turn still applies before the next reply.
- Folded assistant messages leave the run record, so the per-request usage they carry is projected into `run_response.metadata` under a dedicated key that `usage_storage.project_usage` merges into the run's request list; usage totals and the request breakdown stay complete.
  The summary input omits that metadata key.

### 6. Run-local compaction (question 5)

A request without persisted replay (team members, `room_agent` workflow participants, a scheduled task with `history_limit=0`) compacts only its own turn:

- The folded messages and any earlier run-local summary are summarized with the same summary model, prompt, budgets, and fallback, without touching the archive.
- The summary message is an ordinary message (`from_history=False`), so it is stored with the run and a member's approval resume replays it.
- Teams: the leader's request is scoped to the team scope, with the team summary first and its in-progress snapshot a `TeamRunOutput`; member runs stay attached to the live team run.
  Members keep no history or summary of their own, and each member's tool loop compacts run-locally.

### 7. Counting the summary once (question 6)

- The replay planner estimates the rendered summary message through the same rendering function.
- The static-prompt estimator builds the system message from an empty session, which no longer contains a summary in any case.
- The mid-turn trigger counts the actual request list, which holds the summary once.

### 8. What the user sees (question 7)

- Mid-turn compaction posts the same `Compacting history...` notice in the thread, edits it with progress and the result, and the streamed reply continues after the summary call.
- The failure notice becomes `Compaction failed; continuing without compaction.` for both paths, since a mid-turn failure continues with the uncompacted request; a failed run does not retry mid-turn compaction on every later request.
- Compaction hooks (`compaction:before`, `compaction:after`) fire for scoped mid-turn compaction exactly as for pre-reply compaction; run-local compaction has no scope and emits none.

### 9. Native routes

Native routes keep provider-side compaction, which already compacts inside each request and replaces older content with a checkpoint; the observable shape is the same, and the provider cache-aware path stays cheaper.
Mid-turn text compaction runs only when native compaction is off for the request.

### 10. Minimal authored subagents' context documents (question 9)

With the summary in every agent's history, `agent-context` is no longer any agent's route to it.
The persona PR owns whether minimal authored subagents keep the configured agent's `role`, `instructions`, and `agent-context` documents; this design recommends dropping them and needs nothing from that choice.

## Ownership and boundaries

| Module | Change |
| --- | --- |
| `history/replay.py` | Summary message rendering and estimate; summary-only plans as zero runs; request estimator for the mid-turn trigger. |
| `history/agno_compat_message_builder.py` | Inserts the summary message in Agent and Team new-run and continuation builders (new `AGNO_COMPAT` marker). |
| `agno_compat_model_hooks.py` | `install_request_preparation` (new `AGNO_COMPAT` marker). |
| `history/mid_turn.py` (new) | Trigger, in-flight rewrite, snapshot construction, scoped and run-local compaction, usage carry-over. |
| `history/compaction.py`, `history/runtime.py` | Accept the in-progress snapshot; factor chunk persistence so run-local compaction reuses summary generation. |
| `history/native.py` | Route identity from the rendered summary message. |
| `agents.py`, `teams.py`, `ai.py` | Flag off, hook installation, lifecycle binding. |
| `vertex_claude_compat.py` | Trimming deleted; exact count exposed as `acount_tokens`. |
| `usage_storage.py` | Merges carried-over request usage. |
| `delivery_gateway.py` | Failure notice wording. |

`tach.toml` gains the new module; `docs/architecture/compaction.md`, `docs/architecture/agno-compatibility.md`, and `docs/architecture/code-map.md` follow the code.

## User docs

`docs/configuration/history.md` changes once:

- Context Window: replace the Vertex AI Claude trimming paragraph with mid-turn compaction (every request is checked; a request that would exceed the window compacts first; one that still cannot fit fails with a provider error).
- Agent Compaction Settings: text compaction runs before a reply and between model requests of a reply; the notice appears mid-reply too; the new failure wording.
- Team History and Compaction: members compact their own long tool loops.

## Testing

Failing-first tests at the owning seams (history modules, the builder patch, the hook, `ResponseRunner` only where wiring matters):

- Summary message first and system prompt byte-identical across a compaction, for a configured agent, a team, a minimal agent, an authored subagent, a `run_subagent` child, and a summary-only replay plan.
- Approval resume after a pre-reply compaction and after a mid-turn compaction replays the same summary and messages the paused request saw (agent and team).
- One long single-turn run on a scripted model compacts mid-turn, continues, and finishes; the next turn replays `[summary, current input, last exchange, final answer]` with an unchanged prefix.
- A team member's long tool loop compacts run-locally and its approval resume keeps the run-local summary.
- Redacting an event consumed by a run that compacted mid-turn rolls back that generation and restores the earlier runs.
- Request usage of folded requests stays in the run's usage projection and reconciles with run totals.
- Native routes never trigger mid-turn text compaction; native route identity changes when the summary moves.
- `tests/test_compaction_fuzz.py` extended with mid-turn snapshots in generated histories.

Live tests over Matrix on a text route (Gemini or a local OpenAI-compatible model) and a native route, recording provider cache-read tokens for the requests before and after a pre-reply and a mid-turn compaction.

## Out of scope

- Fallback models (`FallbackConfig`) do not get the mid-turn hook; a fallback serves only after the primary fails.
- Synchronous Agno response loops; MindRoom runs every agent and team asynchronously.
- The persona PR's prompt-builder and skills changes.
