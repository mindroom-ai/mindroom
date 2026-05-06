Summary: The main duplication candidates are compaction lifecycle notice serialization split between `history/types.py` and `delivery_gateway.py`, repeated `HistoryScope` validation/construction across history and turn-state code, and repeated `ResolvedHistorySettings` construction from history limits.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
HistoryScope	class	lines 19-28	duplicate-found	HistoryScope construction/normalization/key usage	src/mindroom/conversation_state_writer.py:42; src/mindroom/history/runtime.py:326; src/mindroom/history/storage.py:449; src/mindroom/handled_turns.py:725; src/mindroom/handled_turns.py:1014
HistoryScope.key	method	lines 26-28	related-only	scope.key serialized storage keys	src/mindroom/history/storage.py:31; src/mindroom/history/storage.py:54; src/mindroom/history/runtime.py:1265
HistoryPolicy	class	lines 32-36	duplicate-found	HistoryPolicy mode/limit construction from num_history limits	src/mindroom/config/main.py:1008; src/mindroom/history/runtime.py:1294; src/mindroom/history/runtime.py:1592
ResolvedHistorySettings	class	lines 40-46	duplicate-found	ResolvedHistorySettings construction and copy-with-limit	src/mindroom/config/main.py:1008; src/mindroom/config/main.py:1020; src/mindroom/history/runtime.py:1294; src/mindroom/history/runtime.py:1592
HistoryScopeState	class	lines 50-56	related-only	state metadata parse/serialize/is-empty helpers	src/mindroom/history/storage.py:31; src/mindroom/history/storage.py:323; src/mindroom/history/storage.py:336; src/mindroom/history/storage.py:349
ResolvedHistoryExecutionPlan	class	lines 60-76	related-only	execution plan creation/consumption fields	src/mindroom/history/policy.py:1; src/mindroom/history/runtime.py:684
ResolvedReplayPlan	class	lines 80-89	related-only	replay plan creation and Matrix metadata subset	src/mindroom/history/runtime.py:710; src/mindroom/history/runtime.py:740; src/mindroom/ai_run_metadata.py:188
CompactionDecision	class	lines 93-101	related-only	compaction decision fields serialized to AI run metadata	src/mindroom/history/runtime.py:722; src/mindroom/history/runtime.py:767; src/mindroom/ai_run_metadata.py:170
CompactionLifecycleStart	class	lines 105-115	duplicate-found	start lifecycle metadata construction	src/mindroom/delivery_gateway.py:901
CompactionLifecycleSuccess	class	lines 119-124	related-only	success lifecycle wrapper consumed by gateway	src/mindroom/delivery_gateway.py:962; src/mindroom/history/runtime.py:606
CompactionLifecycleProgress	class	lines 128-178	related-only	progress lifecycle construction/consumption	src/mindroom/history/compaction.py:633; src/mindroom/delivery_gateway.py:946
CompactionLifecycleProgress.to_notice_metadata	method	lines 145-166	duplicate-found	compaction notice metadata dict optional fields	src/mindroom/delivery_gateway.py:909; src/mindroom/delivery_gateway.py:993; src/mindroom/history/types.py:259
CompactionLifecycleProgress.format_notice	method	lines 168-178	duplicate-found	compaction notice body token formatting and history budget suffix	src/mindroom/delivery_gateway.py:909; src/mindroom/history/types.py:293
CompactionLifecycleFailure	class	lines 182-193	duplicate-found	failure lifecycle metadata/body construction	src/mindroom/delivery_gateway.py:979
CompactionLifecycle	class	lines 196-209	related-only	protocol implemented by MatrixCompactionLifecycle	src/mindroom/delivery_gateway.py:251
CompactionLifecycle.start	async_method	lines 199-200	related-only	protocol method implementation delegates to gateway start	src/mindroom/delivery_gateway.py:258; src/mindroom/history/runtime.py:217
CompactionLifecycle.complete_success	async_method	lines 202-203	related-only	protocol method implementation delegates to gateway success	src/mindroom/delivery_gateway.py:273; src/mindroom/history/runtime.py:234
CompactionLifecycle.progress	async_method	lines 205-206	related-only	protocol method implementation delegates to gateway progress	src/mindroom/delivery_gateway.py:266; src/mindroom/history/runtime.py:250
CompactionLifecycle.complete_failure	async_method	lines 208-209	related-only	protocol method implementation delegates to gateway failure	src/mindroom/delivery_gateway.py:280; src/mindroom/history/runtime.py:283
_to_k	function	lines 212-220	none-found	K token abbreviation and floor thousands search	none
_format_exact_tokens	function	lines 223-225	related-only	thousands-separated token formatting in notices	src/mindroom/history/types.py:168; src/mindroom/history/types.py:293
_should_render_overhead_tokens	function	lines 228-230	none-found	nonzero optional overhead token guard	none
CompactionOutcome	class	lines 234-310	related-only	outcome construction/metadata formatting consumption	src/mindroom/history/compaction.py:399; src/mindroom/delivery_gateway.py:962; src/mindroom/api/openai_compat.py:1468
CompactionOutcome.to_notice_metadata	method	lines 259-291	duplicate-found	compaction notice metadata dict optional fields	src/mindroom/history/types.py:145; src/mindroom/delivery_gateway.py:909; src/mindroom/delivery_gateway.py:993
CompactionOutcome.format_notice	method	lines 293-310	duplicate-found	compaction notice body token formatting and history budget suffix	src/mindroom/history/types.py:168; src/mindroom/delivery_gateway.py:909
PreparedHistoryState	class	lines 314-325	related-only	prepared history state creation and metadata serialization	src/mindroom/history/runtime.py:676; src/mindroom/execution_preparation.py:116; src/mindroom/ai.py:819; src/mindroom/ai_run_metadata.py:196
```

## Findings

1. Compaction lifecycle notice metadata is split across event dataclasses and the delivery gateway.
   `CompactionLifecycleProgress.to_notice_metadata()` in `src/mindroom/history/types.py:145` and `CompactionOutcome.to_notice_metadata()` in `src/mindroom/history/types.py:259` both build versioned compaction notice payloads with shared fields such as `status`, `mode`, `session_id`, `scope`, `summary_model`, and optional token fields.
   `src/mindroom/delivery_gateway.py:909` builds the start notice metadata inline, and `src/mindroom/delivery_gateway.py:993` builds failure metadata inline with the same content-key contract.
   Differences to preserve: start notices include `before_tokens`, `history_budget_tokens`, `runs_before`, and optional threshold; failure notices include `duration_ms`, `failure_reason`, and may include `history_budget_tokens`; progress uses `version: 3`, while completed outcomes use `version: 1` or `2` depending on `history_budget_tokens`.

2. Compaction lifecycle notice text formatting repeats the same user-facing concepts in separate places.
   `CompactionLifecycleProgress.format_notice()` in `src/mindroom/history/types.py:168` and `CompactionOutcome.format_notice()` in `src/mindroom/history/types.py:293` both format before/after token counts, append a history-budget suffix, and use the shared exact-token formatter.
   `src/mindroom/delivery_gateway.py:909` and `src/mindroom/delivery_gateway.py:988` still own start/failure body strings directly.
   Differences to preserve: progress uses ASCII `->` and running text; success uses the package emoji and Unicode arrow; failure includes the failure reason and trimmed-history fallback message.

3. History scope creation and validation is repeated.
   `HistoryScope` is created from runtime agent identity in `src/mindroom/history/runtime.py:326`, bot/config identity in `src/mindroom/conversation_state_writer.py:42`, run output identity in `src/mindroom/history/storage.py:449`, and persisted handled-turn JSON in `src/mindroom/handled_turns.py:1014`.
   `src/mindroom/handled_turns.py:725` also normalizes a `HistoryScope` by rechecking `kind` and `scope_id`, and `src/mindroom/conversation_state_writer.py:70` recreates an already-typed scope.
   Differences to preserve: `conversation_state_writer` treats configured teams by config name, runtime prefers `agent.team_id`, and storage must return `None` for runs without stable IDs.

4. History policy/settings construction from limits is repeated.
   `src/mindroom/config/main.py:1008` and `src/mindroom/config/main.py:1020` construct `ResolvedHistorySettings` from `num_history_runs`, `num_history_messages`, and tool-call defaults.
   `src/mindroom/history/runtime.py:1294` performs the same policy selection from an Agno `Agent`, and `src/mindroom/history/runtime.py:1592` copies the same settings while replacing only mode/limit.
   Differences to preserve: config-level helpers apply defaults for configured entities; runtime reads already-resolved Agno agent attributes; the copy-with-limit helper preserves role and tool-call settings.

## Proposed Generalization

1. Add `to_notice_metadata()` and `format_notice()` methods to `CompactionLifecycleStart` and `CompactionLifecycleFailure`, mirroring progress/outcome ownership.
2. Keep the gateway as transport-only by calling those event methods in `send_compaction_lifecycle_start()` and `edit_compaction_lifecycle_failure()`.
3. Add a tiny `HistoryScope.from_parts(kind, scope_id)` helper or module-level `valid_history_scope(kind, scope_id)` factory only if the repeated validation continues to grow; for now, the call-site differences make this lower priority.
4. Add a small `ResolvedHistorySettings.with_policy(mode, limit)` or module helper only if more settings-copy sites appear; current duplication is real but not high-risk enough to justify immediate churn.

## Risk/tests

Refactoring lifecycle notice serialization would need tests asserting exact Matrix metadata for start, progress, success, and failure notices, including optional omitted fields and version values.
Scope factory extraction would need tests for invalid kind/scope IDs in handled-turn records and run-output scope derivation.
Settings helper extraction would need config and runtime tests that verify `num_history_messages` still takes precedence over `num_history_runs` and defaults remain unchanged.
