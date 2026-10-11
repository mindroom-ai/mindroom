# Compaction Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with the skill named in **Execution**. Steps use checkbox (`- [ ]`) syntax.

**Execution:** baspowers:executing-plans — tasks share one evolving module and many private interfaces, so one implementer keeps them consistent; a fresh-context reviewer checks each task before the next, and three models review the whole branch at the end.

**Goal:** Every agent, team, subagent, workflow participant, and mode replays the compaction summary as its first history message, and any request that would exceed the context window compacts first, including inside a turn.

**Architecture:** The summary stays derived from the archive's latest generation and is inserted by MindRoom's existing Agno message-builder patch; Agno's system-prompt summary is off.
A pre-request hook on every model folds replayed history and the turn's model and tool messages into a new generation (scoped) or a run-local summary (unscoped), keeping only the system prompt, the current input, and queued-message notices.

**Tech Stack:** Python 3.13, Agno 3.0.9 (pinned), SQLite session storage, pytest, Tach.

**Spec:** `docs/dev/2026-10-10-compaction-unification-design.md`

## Global Constraints

- Follow `AGENTS.md`: simplest correct change, no backward-compatibility shims beyond the one `LEGACY_COMPAT` rule the spec names, imports at module top, no `getattr`/`hasattr` probing of typed interfaces.
- Every new Agno workaround carries an `AGNO_COMPAT:` marker with `Reason`, `Upstream issue`, `Upstream PR`, `Remove when`, `Coverage`; search Agno issues before writing a tracking gap.
- The legacy rule carries `LEGACY_COMPAT:` with `Legacy format`, `Last legacy release` (latest tag at merge time, currently `v2026.10.231`), `Handling`, `Coverage`.
- Summary message wording is exactly the text block in spec section 1; marker key `mindroom_compaction_summary`; failure notice `Compaction failed; continuing without compaction.`
- Mid-turn limit: `context_budget_after_reserve(plan.replay_window_tokens, plan.reserve_tokens)`.
- Tests run inside `nix-shell shell.nix` on this host: `uv run pytest tests/<file>.py -x -n 0 --no-cov -v`.
- `tach.toml` updated in the task that adds an import edge; `uv run tach check --dependencies --interfaces` passes after every task.
- Docs: one sentence per line; user docs change only in Task 8.
- Never touch persona code (`_apply_persona`, `persona_hint`, `with_session_summary`); the persona PR (#2794) owns it.

## Review Focus

- Tool results that carry images or files inside folded exchanges: the summary input and request sizing must handle them without serializing image bytes as text (Task 4 test `test_folded_tool_media_is_summarized_not_kept`).
- A batch of parallel tool calls is folded whole, never split (Task 4 test `test_parallel_tool_batch_is_folded_whole`).
- Streaming and non-streaming loops behave identically (every hook and mid-turn test is parametrized over `stream`).
- A reusable agent across CLI follow-up turns rebinds its lifecycle and starts each run with a clean failure state (Task 4 test `test_failed_mid_turn_compaction_does_not_carry_into_the_next_run`).
- Cancellation during the mid-turn summary call keeps committed chunks, edits the notice to failure, and propagates the cancel (Task 5 test `test_cancelled_mid_turn_compaction_keeps_committed_chunks`).

---

### Task 1: Summary as the first history message

**Files:**
- Modify: `src/mindroom/history/replay.py` (rendering, marker, estimate, summary-only plan)
- Modify: `src/mindroom/history/agno_compat_message_builder.py` (insertion in all six builders, session record)
- Modify: `src/mindroom/agents.py:2164`, `src/mindroom/teams.py:2328` (`add_session_summary_to_context=False`)
- Modify: `src/mindroom/execution_preparation.py` (`_prepared_history_with_scheduled_limit` keeps summary-only plans)
- Modify: `tach.toml`
- Test: `tests/test_agno_compat_message_builder.py`, `tests/test_history_replay_planning.py`, `tests/test_agent_mode_history.py`

**Interfaces:**
- Produces: `COMPACTION_SUMMARY_MARKER = "mindroom_compaction_summary"`, `compaction_summary_message(summary: str, *, from_history: bool) -> Message`, `is_compaction_summary(message: Message) -> bool` in `history/replay.py`.
- Produces: `built_request_session(target: Agent | Team) -> AgentSession | TeamSession | None` in `history/agno_compat_message_builder.py`, backed by an `id(target)`-keyed dict of `(weakref, session)` (Agno's Agent and Team dataclasses are unhashable) filled by every wrapped builder.
- Produces: `plan_replay_that_fits` returns `ResolvedReplayPlan(mode="disabled", add_history_to_context=True, num_history_runs=0, ...)` for summary-only replay; `has_effective_persisted_replay` treats `num_history_runs == 0` as no raw runs.

- [ ] **Step 1: Write failing tests**
  - `test_summary_is_the_first_history_message_for_new_runs[agent|team]`: session with summary `"S1"` and two runs; built `run_messages.messages[1]` is the marked summary with `from_history=True` and content containing `<compacted_history>\nS1\n</compacted_history>`; the system message contains neither `S1` nor `summary_of_previous_interactions`.
  - `test_summary_is_reinserted_on_continuation[agent|team]`: pause, resume through the existing `_paused_and_resumed_requests` helper with a session summary; the resumed request's first non-system message is the marked summary, exactly once.
  - `test_summary_not_inserted_without_history_replay`: `add_history_to_context=False` builds no summary message.
  - `test_stored_run_local_summary_does_not_suppress_the_scope_summary`: a continuation input holding a marked summary with `from_history=False` still gets the scope summary inserted.
  - `test_summary_only_plan_replays_the_summary_without_runs` in `test_history_replay_planning.py`: oversized history whose summary fits yields `add_history_to_context=True, num_history_runs=0`, and the built request holds the summary and no history runs.
  - `test_scheduled_history_limit_zero_replays_no_summary`.
  - `test_minimal_agent_request_starts_history_with_the_summary` in `test_agent_mode_history.py`: the minimal bootstrap system message is unchanged byte for byte and the summary message follows it.
  - `test_system_prompt_is_identical_before_and_after_compaction`: build twice around a compaction; system message content equal.
  - `test_replay_estimate_counts_the_rendered_summary_once`: the planner's estimate grows by exactly the rendered summary message's estimate when a summary is added.
- [ ] **Step 2: Run them; expect failures (summary inside system prompt, no marked message).**
- [ ] **Step 3: Implement.** `_estimate_session_summary_tokens` estimates `compaction_summary_message(...).content`; the builder wrappers resolve `add_history_to_context` as Agno does (kwarg, else `target.add_history_to_context`) and insert after the leading `system`/`developer` messages when `session.summary` is non-empty and no marked message exists; agents and teams set the flag off; `apply_replay_plan` already copies the plan fields.
- [ ] **Step 4: Run the task's tests plus `tests/test_agno_compat_message_builder.py tests/test_history_*.py tests/test_compaction*.py`; expect PASS.**
- [ ] **Step 5: Commit** `feat(history): replay the compaction summary as the first history message`.

### Task 2: Native route identity and paused legacy prompts

**Files:**
- Modify: `src/mindroom/history/native.py` (both `configure_native_history` and `restore_native_history`)
- Create: `src/mindroom/history/legacy_summary_system_prompt.py`
- Modify: `src/mindroom/history/agno_compat_message_builder.py` (continuation builders consult the legacy rule)
- Modify: `tach.toml`, `docs/architecture/migrations.md`
- Test: `tests/test_native_compaction_history.py`, `tests/test_agno_compat_message_builder.py`

**Interfaces:**
- Consumes: `compaction_summary_message` (Task 1).
- Produces: `system_message_embeds_summary(messages: Sequence[Message]) -> bool` (true when a leading system or developer message contains `<summary_of_previous_interactions>`).

- [ ] **Step 1: Write failing tests**
  - `test_native_route_identity_hashes_the_rendered_summary`: the route for summary `"S1"` differs from the route computed from the raw text `"S1"` (current behavior) and from the route for `"S2"`; an empty summary keeps the identity used today.
  - `test_resuming_a_pre_release_pause_keeps_its_single_summary[agent|team]`: a paused run whose stored system message embeds Agno's block resumes with no marked summary message.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement** with the `LEGACY_COMPAT:` marker in the new module and a `docs/architecture/migrations.md` row.
- [ ] **Step 4: Run the native and builder test files; expect PASS.**
- [ ] **Step 5: Commit** `feat(history): bind native routes to the rendered summary and keep paused legacy prompts`.

### Task 3: Pre-request hook and request sizing

**Files:**
- Modify: `src/mindroom/agno_compat_model_hooks.py`
- Modify: `src/mindroom/model_usage.py`
- Modify: `src/mindroom/history/replay.py`
- Test: `tests/test_agno_compat_model_hooks.py` (create), `tests/test_model_usage.py` (create or extend existing usage tests)

**Interfaces:**
- Produces: `install_request_preparation(model: Model, *, marker: str, prepare: Callable[[list[Message], list[dict[str, Any]] | None, RunOutput | TeamRunOutput | None], Awaitable[None]]) -> None`; `prepare` runs before each `_aprocess_model_response` and `aprocess_response_stream` call with the loop's own list.
- Produces: `response_context_tokens(message: Message, *, provider: str | None, configured_provider: str | None, model_id: str | None) -> int | None` in `model_usage.py`, preferring `message.metrics.provider_metrics["context_usage"]`.
- Produces: `estimate_request_messages_tokens(messages: Sequence[Message], *, replay_model: NativeCompactionModel | None) -> int` in `history/replay.py`, factored out of `estimate_prompt_visible_history_tokens` (canonical estimate, provider estimate, image fallback).

- [ ] **Step 1: Write failing tests**
  - `test_request_preparation_runs_before_every_provider_request[stream]`: a `SyntheticModel` tool loop with two tool batches calls `prepare` three times, each time with the same list object Agno appends to, the formatted tool dicts, and the run's `run_response`; a mutation made in `prepare` is what the provider receives.
  - `test_tool_call_cap_installed_later_refuses_before_preparation`: with the cap installed after the hook, a refused request never reaches `prepare`.
  - `test_response_context_tokens_adds_cache_tokens_only_where_reported_outside_input`: Anthropic-style counters `input=10, cache_read=90, cache_write=5` give `105`; OpenAI-style `input=100, cache_read=90` gives `100`; native `context_usage` wins over billed counters; a default `MessageMetrics()` (all zero) gives `None`.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement** with an `AGNO_COMPAT:` marker on the installer; the hook awaits `prepare` before delegating and leaves Agno's arguments unchanged.
- [ ] **Step 4: Run; expect PASS, plus `tests/test_tool_call_budget.py`.**
- [ ] **Step 5: Commit** `feat(models): add an awaitable pre-request hook and provider-anchored request sizing`.

### Task 4: Mid-turn compaction binding, trigger, rewrite, and run-local compaction

**Files:**
- Create: `src/mindroom/history/mid_turn_compaction.py`
- Modify: `src/mindroom/history/runtime.py` (public `resolve_entity_preparation_inputs`, `summarize_run_locally`)
- Modify: `src/mindroom/history/compaction.py` (`summarize_runs`)
- Modify: `src/mindroom/agents.py`, `src/mindroom/teams.py` (install hook, then the cap), `src/mindroom/ai.py` and the team preparation in `teams.py` (lifecycle binding)
- Modify: `src/mindroom/constants.py` (`COMPACTED_REQUESTS_METADATA_KEY = "mindroom_compacted_requests"`), `src/mindroom/usage_storage.py`, `src/mindroom/history/summary_input.py`
- Modify: `tach.toml`
- Test: `tests/test_mid_turn_compaction.py` (create), `tests/test_usage_storage.py`

**Interfaces:**
- Consumes: Tasks 1 and 3.
- Produces: `install_mid_turn_compaction(target: Agent | Team, *, config: Config, runtime_paths: RuntimePaths, entity_name: str | None, model_name: str) -> None` and `bind_compaction_lifecycle(target: Agent | Team, lifecycle: CompactionLifecycle | None) -> None`.
- Produces: `summarize_runs(*, summary_model: SummaryModel, fallback_summary_model: SummaryModel | None, previous_summary: str | None, runs: Sequence[RunOutput | TeamRunOutput], history_settings: ResolvedHistorySettings, summary_prompt: str, timeout_seconds: float) -> str` in `compaction.py`, one chunk through `_generate_compaction_summary_with_retry`.
- Produces: `project_requests(messages: Sequence[dict[str, object]]) -> list[dict[str, object]] | None` in `usage_storage.py` (today's `_project_requests`); `project_usage` prepends `metadata[COMPACTED_REQUESTS_METADATA_KEY]`.
- Produces: `summarize_run_locally(*, resolved_inputs: _HistoryPreparationInputs, previous_summary: str | None, snapshot: RunOutput | TeamRunOutput, config: Config, runtime_paths: RuntimePaths) -> str` in `runtime.py`, loading the summary and fallback models like `_run_scope_compaction` and calling `summarize_runs`.

- [ ] **Step 1: Write failing tests** (scripted `SyntheticModel` whose tool results grow the request past a small `context_window`):
  - `test_run_local_tool_loop_compacts_and_finishes[stream]`: an agent without history replay compacts once, the next provider request is `[system, run-local summary, input]`, and the run completes with the final answer.
  - `test_first_request_without_turn_messages_folds_nothing_when_unscoped`.
  - `test_queued_notice_survives_mid_turn_compaction`.
  - `test_rewritten_request_carries_no_response_chain`: after compaction no message in the list has `provider_data["response_id"]`, so an OpenAI Responses request built from it has no `previous_response_id`.
  - `test_parallel_tool_batch_is_folded_whole`.
  - `test_folded_tool_media_is_summarized_not_kept`.
  - `test_mid_turn_compaction_skips_when_disabled_or_unavailable` (`enabled: false`, no `context_window`).
  - `test_failed_mid_turn_compaction_does_not_retry_in_the_same_run` and `test_failed_mid_turn_compaction_does_not_carry_into_the_next_run`.
  - `test_team_member_loop_compacts_run_locally_and_resumes_with_its_summary`.
  - `test_folded_request_usage_stays_in_the_usage_projection`: two compactions, one approval resume, one cancellation; the projected request list is chronological, has one entry per provider request, and sums to the run totals.
  - `test_request_sizing_uses_the_latest_response_usage`: a response reporting usage above the limit triggers compaction even when the canonical estimate is below it.
  - `test_resumed_run_does_not_anchor_on_its_paused_response`: the first request of a continuation is sized by estimate even when the loaded assistant message reports usage above the limit.
  - `test_unseen_thread_context_is_folded_and_transient_context_kept`.
  - `test_snapshot_summary_keeps_current_turn_tool_results_despite_history_limits[0|1]`: with `max_tool_calls_from_history` 0 and 1, a fact present only in an early current-turn tool result reaches the summary input.
  - `test_run_local_summary_usage_is_recorded`: the summary call's usage lands as `compaction_summary` usage (target storage, else helper owner), including a refused primary and a successful fallback.
  - `test_usage_less_server_never_anchors_sizing`: a provider leaving `MessageMetrics()` at zero across several tool batches is sized by full estimate.
  - `test_rewritten_request_over_the_limit_raises_the_summary_budget_error`.
- [ ] **Step 2: Run; expect FAIL (module missing).**
- [ ] **Step 3: Implement.** The hook skips a request whose run id is in the binding's failed set, resolves the plan through `resolve_entity_preparation_inputs(static_prompt_tokens=0)`, sizes the request (Task 3), and for unscoped runs builds an in-memory snapshot run of the folded messages, calls `summarize_run_locally`, rewrites the list in place to `[leading system/developer, compaction_summary_message(summary, from_history=False), current prompt, transient messages, queued notices]` in their original relative order, and appends the folded assistant requests to `run_response.metadata[COMPACTED_REQUESTS_METADATA_KEY]`.
  The current prompt is the last message id in `run_response.input.input_content`, else the first non-history non-system message; transient messages are those with `add_to_agent_memory=False` and are never summarized; queued notices are recognized by the `mindroom_queued_message_notice` marker shared with `ai_runtime` (move the key to `constants.py` and use it in both existing readers).
  The failed set is cleared when a run id is not the current one.
  The sizing anchor is the latest assistant message appended during this response loop: the binding records the message ids present at the first `prepare` call for each `run_response` object and never anchors on those.
- [ ] **Step 4: Run the new test file plus `tests/test_usage_storage.py tests/test_usage_stats.py tests/test_request_usage.py tests/test_mid_turn.py tests/test_queued_message_notify.py`; expect PASS.**
- [ ] **Step 5: Commit** `feat(history): compact long turns run-locally between model requests`.

### Task 5: Scoped mid-turn compaction

**Files:**
- Modify: `src/mindroom/history/mid_turn_compaction.py`
- Modify: `src/mindroom/history/compaction.py` (`compact_scope_history(..., in_progress: RunOutput | TeamRunOutput | None = None)`)
- Modify: `src/mindroom/history/runtime.py` (`compact_scope_mid_turn`)
- Modify: `src/mindroom/history/archive.py` (`snapshot_run_id`, `is_snapshot_run_id`)
- Test: `tests/test_mid_turn_compaction.py`, `tests/test_history_compaction_rewrite.py`

**Interfaces:**
- Consumes: Tasks 1, 3, 4; `built_request_session` (Task 1).
- Produces: `snapshot_run_id(origin_run_id: str) -> str` (`f"{origin_run_id}:compaction-snapshot:{uuid4().hex}"`), `is_snapshot_run_id(run_id: str) -> bool`, `snapshot_origin(run_id: str) -> str | None`.
- Produces: `compact_scope_mid_turn(*, storage: BaseDb, session: AgentSession | TeamSession, scope: HistoryScope, resolved_inputs: _HistoryPreparationInputs, snapshot: RunOutput | TeamRunOutput, before_tokens: int, config: Config, runtime_paths: RuntimePaths, compaction_lifecycle: CompactionLifecycle | None) -> CompactionOutcome | None`.

- [ ] **Step 1: Write failing tests**
  - `test_long_single_turn_compacts_mid_turn_and_continues[agent|team][stream]`: two prior runs plus a growing tool loop; after compaction the provider sees `[system, summary, input]`, the prior runs and the snapshot are archived in one generation, the live run is not, and the run finishes.
  - `test_next_turn_reuses_the_post_compaction_prefix`: the next turn's request starts with exactly the messages the last mid-turn request started with.
  - `test_end_of_run_session_write_keeps_the_new_summary`: after the run, the stored session row's summary equals the latest generation and reconciliation logs no `Restored the replayed summary` warning.
  - `test_approval_resume_after_mid_turn_compaction[agent|team]`: the resumed request is `[system, summary, input, approved tool results...]` with the summary matching the archive.
  - `test_partial_commit_then_failure_leaves_the_request_unchanged`.
  - `test_cancelled_mid_turn_compaction_keeps_committed_chunks` and `test_cancellation_after_the_snapshot_commit_still_rewrites_the_request`.
  - `test_failed_second_chunk_keeps_the_live_session_fresh`: after the terminal session write, the stored summary equals the latest generation and the scope's metadata seen ids are intact.
  - `test_force_flag_survives_mid_turn_compaction`.
  - `test_legacy_resume_then_mid_turn_compaction_leaves_one_summary`: a resumed pre-release pause, after another tool result, compacts and its system message no longer embeds the legacy block.
  - `test_in_memory_team_continuation_keeps_one_summary`: a delegation continuation handed the in-memory run carries exactly one summary.
  - `test_mid_turn_compaction_emits_compaction_hooks_and_notices`.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement.** Scoped iff the target replays history and has `db`; take `built_request_session(target)` (Agno's live session for this run), reconcile it in place, build the snapshot (scope id, `status=completed`, deep-copied live metadata, folded messages plus input, `run_id=snapshot_run_id(run_response.run_id)`), run `compact_scope_mid_turn` on that same session object with a no-force `HistoryScopeState()` (chunk persistence adopts the fresh row into it after every chunk), check `archive.archived_run_ids` for the snapshot, then rewrite with `compaction_summary_message(new_summary, from_history=True)` and carry usage as in Task 4; on `CancelledError`, perform the same check and rewrite before re-raising.
  `compact_scope_history` excludes the live run id (`snapshot_origin(in_progress.run_id)`) from visible runs and appends the snapshot last.
- [ ] **Step 4: Run the task's files plus `tests/test_compaction*.py tests/test_history_*.py`; expect PASS.**
- [ ] **Step 5: Commit** `feat(history): compact scoped history and the current turn between model requests`.

### Task 6: Redaction through snapshots

**Files:**
- Modify: `src/mindroom/history/archive.py` (`earliest_archived_run`, `roll_back_to` skips snapshots)
- Modify: `src/mindroom/history/storage.py` (`remove_run_by_event_id` returns removed run ids; `_remove_redacted_event_from_compaction` rolls back to the earliest row of the hit or removed runs and their snapshots), `src/mindroom/turn_store.py` (caller of `remove_run_by_event_id`)
- Test: `tests/test_compaction_redaction.py`, `tests/test_compaction_fuzz.py`, `docs/architecture/compaction.md` invariants

**Interfaces:**
- Produces: `earliest_archived_run(storage: BaseDb, *, session_id: str, scope_key: str, run_ids: Collection[str]) -> _ArchiveHit | None` (earliest row whose run id is in `run_ids` or is a snapshot of one).
- Produces: `remove_run_by_event_id(...) -> list[str]`.

- [ ] **Step 1: Write failing tests** for one and two mid-turn compactions, agent and team (with member runs), including two snapshots with an event first consumed between them that is redacted after the origin run itself was archived (archive hits normalize through `snapshot_origin`): redacting an event consumed before the snapshot, an event added to the live run's metadata after it, and the reply event attached after the run, each rolls back to before the run, restores the earlier runs, and replays the previous generation's summary; redacting an earlier archived run's event still rolls back to that run; snapshots never return to the live table; fuzz generator adds mid-turn snapshots and checks the existing invariants.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run `tests/test_compaction_redaction.py tests/test_compaction_fuzz.py tests/test_history_archive.py tests/test_turn_store.py`; expect PASS.**
- [ ] **Step 5: Commit** `fix(history): roll back compaction through mid-turn snapshots on redaction`.

### Task 7: Native fallback and Vertex fitting removal

**Files:**
- Modify: `src/mindroom/history/mid_turn_compaction.py` (native projection sizing, native off, canonical compaction)
- Modify: `src/mindroom/vertex_claude_compat.py` (delete `_fit_request_messages`, counting helpers, trimming; `ainvoke`/`ainvoke_stream` keep `_messages_with_replay_safe_reasoning(self.native_replay_messages(messages))`)
- Delete or rewrite: `tests/test_vertex_claude_context_guard.py`, counting cases in `tests/test_vertex_native_compaction_count.py`, `tests/test_claude_native_compaction.py:529`, `tests/test_claude_authored_compaction.py:75`, `tests/test_extra_kwargs.py:896`
- Test: `tests/test_mid_turn_compaction.py`

- [ ] **Step 1: Write failing tests**: `test_native_request_under_the_limit_never_text_compacts`; `test_openai_checkpoint_with_large_billed_input_keeps_native_replay` (large billed input, no `context_usage`, small projected request); `test_oversized_native_request_turns_native_off_and_compacts_as_text[openai|claude]` (native projection sized from the latest `context_usage`; afterwards `model.native_compaction is None` for the rest of the run and the request is canonical `[system, summary, input]`); `test_vertex_requests_are_sent_without_trimming`.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the native, Vertex, and Claude test files; expect PASS.**
- [ ] **Step 5: Commit** `feat(history): fall back from native to text compaction mid-turn; drop Vertex request trimming`.

### Task 8: Notices and documentation

**Files:**
- Modify: `src/mindroom/delivery_gateway.py` (failure text), `src/mindroom/claude_prompt_cache.py` (module docstring no longer mentions summaries in the system prompt)
- Modify: `docs/configuration/history.md` (spec "User docs"), `docs/architecture/compaction.md`, `docs/architecture/agno-compatibility.md`, `docs/architecture/code-map.md`
- Test: existing notice tests in `tests/test_history_prepare_lifecycle.py`

- [ ] **Step 1: Update the failure-notice test to the new text; run; expect FAIL.**
- [ ] **Step 2: Implement the text and docs; run `uv run pre-commit run --all-files` (regenerates the `mindroom-docs` skill references).**
- [ ] **Step 3: Commit** `docs(history): document mid-turn compaction and the summary's position`.

### Task 9: Verification, live tests, and PR

- [ ] **Step 1:** `nix-shell shell.nix` then `uv run pytest -n auto` and `uv run pre-commit run --all-files` and `uv run tach check --dependencies --interfaces`; all green.
- [ ] **Step 2:** If the persona PR has not landed, delete the now-dead `with_session_summary` path or rebase onto the persona PR, whichever exists first; rerun Step 1.
- [ ] **Step 3:** Live tests (live-test skill): a text route (Gemini or a local OpenAI-compatible server) and a native route; record cache-read tokens per request before and after a pre-reply and a mid-turn compaction; confirm the agent continues its task after a mid-turn compaction on two real models; one Claude text-route mid-turn compaction if credentials allow.
- [ ] **Step 4:** Push, open the PR, link it in the thread, then three-model sign-off (Opus 5.5, GPT-6.1 Sol, GPT-6 Astra) on the full diff and GitHub bot triage before offering to merge.
