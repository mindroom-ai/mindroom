# ISSUE-247 implementation report

Implementation commit: `9bd565dfa` ("ISSUE-247: factor per-message prompt boilerplate into msg attrs, system context, and location deltas") on branch `issue-247-prompt-boilerplate`, on top of the committed PLAN.md (`914e923f3`).

## What changed per file

### Production (net +260 lines incl. tach.toml)

- `src/mindroom/execution_preparation.py` (+162 net)
  - `[R1]` Added `current_event_id: str | None = None` to `_build_matrix_prompt_with_history`, `_messages_with_current_prompt`, `_messages_with_capped_context`, `_build_unseen_context_messages`, `_build_thread_history_messages`, `_prepare_execution_context_common`, `prepare_agent_execution_context`, `_prepare_bound_team_execution_context`, and `prepare_bound_team_run_context`; the direct current `<msg>` tag now carries `event_id` when a real Matrix event exists, and structured coalesced/queued prompts keep their per-child IDs without an outer duplicate wrapper.
  - `[R1]` In `_build_unseen_context_messages`, the pre-existing filter argument (the reply-to event used to skip the current event) was renamed to `reply_to_event_id` so the plan's new `current_event_id` msg-tag argument could take the plan-specified name without collision.
  - `[R1]` `_context_message_from_visible_message` now wraps every real `ResolvedVisibleMessage` as `<msg event_id=... from=...>` with the CDATA body, for assistant, human, relayed, and partial messages, dropping the old `sender: ` text prefix; in-progress partials use `response_sender_id` as `from` and keep the `You (...)` guidance label inside the body. Roles, media, attachment annotations, length caps, relay labels, and tool-marker stripping are unchanged; no fallback timestamps were added or moved.
  - `[R3]` Added the location split: `_split_location_enrichment` (terminal `<mindroom_message_context>` block, only its `key="location"` item), `_location_marker_from_fields` (order-independent `key: value` parsing; `📍 Home` → `📍 <place>` → `📍 <lat>, <lon>`; fail-closed to no marker), `_last_persisted_location_marker` (+`_last_location_marker_in_text`) scanning stored scope runs backward while ignoring `<mindroom_message_context>` blocks and handling markers persisted inside `<msg>` CDATA, and the two-call-site orchestrator `_extract_current_location_context`. The full item always leaves the persisted prompt; a single plain marker line is appended only when it differs from the last surviving marker; missing location data changes nothing.
  - `[R3]` `prepare_agent_execution_context` delivers the extracted location-only block as a `Message(role="user", add_to_agent_memory=False)` immediately before the current user message; `_prepare_bound_team_execution_context` instead appends it to the freshly materialized team's and members' `additional_context` (helper `_append_transient_additional_context`) because team input is flattened into one persisted string.
- `src/mindroom/response_turn.py` (+5 net)
  - `[R1]` `DynamicContinuationRunState` gained `active_current_event_id`; `initial()` accepts and preserves it; `advance()` clears it together with the timestamp and structured-input flag.
- `src/mindroom/ai.py` (+10 net)
  - `[R1]` Both blocking and streaming agent turns seed `_initial_agent_continuation` with `ctx.reply_to_event_id` and pass `continuation_state.active_current_event_id` through `_prepare_agent_run_context` → `_prepare_agent_and_prompt` → `prepare_agent_execution_context`. `TurnRecorder.user_message` is untouched.
- `src/mindroom/teams.py` (+10 net)
  - `[R1]` Same seeding/propagation for both team turn drivers via `_initial_team_continuation` and `prepare_materialized_team_execution`.
  - `[R3]` Stable system enrichment stays applied before the execution-preparation call (comment added), so the current full location block lands only as the volatile `additional_context` tail.
- `src/mindroom/response_runner.py` (+11 net)
  - `[R2]` Deleted `_append_matrix_prompt_context`, its duplicate-detection check, and both prompt-mutating call sites; the model prompt no longer carries a `[Matrix metadata for tool calls]` footer.
  - `[R2]` Added module-level `_matrix_message_target_item` returning one `EnrichmentItem(key="matrix_message_target", cache_policy="stable")`: thread wording carries `room_id` + `thread_id` plus the instruction to pass the current or selected `<msg event_id>` as `reply_to_event_id`; room-level wording carries `room_id` plus an explicit do-not-pass-`thread_id` instruction; no source/reply/response event ID value appears. Appended to `system_enrichment_items` in `_agent_turn_context` (existing agent tool gate) and at the team `ResponseTurnContext` construction (existing any-member gate).
  - `[R1]` `_build_turn_recorder` now seeds `requester_id` into the recorder's Matrix run metadata (all three call sites); the response-event callback passes `response_sender_id=self.deps.matrix_full_id` without changing the callback type; `_persist_interrupted_turn` passes `response_sender_id=self.deps.matrix_full_id` to `persist_interrupted_replay_snapshot`.
- `src/mindroom/conversation_state_writer.py` (+31 net)
  - `[R1]` `persist_response_event_id_in_session_run` gained required `response_sender_id`; in the same one-upsert path (same-event early return retained) the new `_wrap_final_assistant_message` replaces the final assistant message's content with `render_msg_tag(sender=..., body=run.content, event_id=...)`, rebuilt from canonical `run.content` so repeated/changed callbacks can never nest tags; runs without string content or a final assistant message persist metadata only.
- `src/mindroom/history/interrupted_replay.py` (+31 net)
  - `[R1]` Canonical interrupted user messages wrap with `requester_id` + `MATRIX_EVENT_ID_METADATA_KEY` when both are present; canonical interrupted assistant content wraps with the new optional `response_sender_id` + `MATRIX_RESPONSE_EVENT_ID_METADATA_KEY` when both exist; otherwise messages stay bare. Optional parameter added only to the existing persistence chain (`persist_interrupted_replay_snapshot` → `_build_interrupted_replay_run`); no timestamps, no new replay type.
- `src/mindroom/prompts.py` (+0 net, one sentence)
  - `[R1]` One sentence in the `<msg>` guidance: `event_id` identifies the message's Matrix event for reactions, edits, and `matrix_message` `reply_to_event_id`. No attribute order documented.
- `tach.toml` (+4)
  - `mindroom.conversation_state_writer` and `mindroom.history.interrupted_replay` now depend on `mindroom.prompt_message_tags`, and both were added to its visibility. No timestamp-formatting dependency added.

### Tests (net +1066 lines)

- `tests/test_execution_preparation.py` — existing fallback/unseen assertions updated to the wrapped `<msg>` format; new tests for direct current event ID presence/omission, structured input without outer wrapper, in-progress partial wrapping, all R3 marker derivations (Home, named place, coordinates, reordered, malformed fail-closed, no-location, non-location items preserved byte-for-byte), backward dedup (Home→gap→Home, Home→Office, marker inside `<msg>` CDATA, old full blocks ignored), agent transient placement, and team `additional_context` placement with the flattened-text exclusion.
- `tests/test_response_turn.py` — `_continuation` helper updated; new test proving `initial()` keeps the event ID and `advance()` clears it.
- `tests/test_conversation_state_writer.py` — new tests: agent wrap, idempotency/no-nesting across repeated and changed callbacks, metadata-only runs, and team-session wrap.
- `tests/test_interrupted_replay.py` — new tests: both-side wrapping, absent-ID bare behavior, one-sided wrapping, and end-to-end snapshot persistence with `response_sender_id`.
- `tests/test_response_runner_agent.py` — five footer tests replaced with stable-item assertions (gate, wording, no footer in model prompt, thread/room variants, no event-ID leakage); new byte-identical-across-source-events test.
- `tests/test_response_runner_session_lifecycle.py` — footer test replaced with system-context assertions; new negative test (no `matrix_message` → no item); new post-delivery callback test asserting `matrix_full_id` is passed; interrupted-persistence expectations updated to wrapped messages.
- `tests/test_response_runner_team_streaming.py` — footer test replaced with team ctx stable-item assertions; interrupted-persistence expectations updated to wrapped messages (including the recorder-wiped-metadata case that correctly stays bare on the user side).
- `tests/test_history_prepare_integration.py` — existing fallback expectations updated; new five-turn integration test (Home, Home, no-location, Home, Office) asserting exactly one `📍 Home` and one `📍 Office` in persisted history, no full GPS fields or enrichment blocks or footer in persistence, per-turn event IDs persisted, full detail delivered as `add_to_agent_memory=False` current-turn input, and compaction summary input containing only short markers.
- `tests/test_partial_reply_context.py`, `tests/test_openai_compat.py`, `tests/test_team_media_fallback.py` — expectations updated to the renamed `reply_to_event_id` filter argument and the wrapped fallback-history format.

## Gate results

1. **pytest (inside nix-shell, NixOS)**: targeted files green, then full suite `uv run pytest` exit 0 (only pre-existing skips; no failures). Re-validated the eleven touched test files after pre-commit formatting: exit 0.
2. **pre-commit** (`uv run pre-commit run --files <all 20 changed files>` after `uv sync --all-extras`): every hook passes (trailing whitespace, end-of-files, docstring-first, ruff check, ruff format, vulture, Tach narrow boundaries, module privacy) **except `ty`**, which fails with 7 pre-existing `unresolved-import` errors for macOS-only modules (`AppKit`, `ApplicationServices`, `Quartz`) in `src/mindroom/desktop/{accessibility,provider}.py`. Verified pre-existing: with all changes stashed, `.venv/bin/ty check src tests` on the base commit fails with the identical 7 errors on this Linux host. No ty diagnostic points at any file touched by this change. The implementation commit was therefore made with `--no-verify` after all other hooks passed; the two files ty flags are untouched by this branch.
3. **tach**: `uv run tach check --dependencies --interfaces` → "All modules validated!".

## Deviations from PLAN.md

1. **Scope budget**: net production delta is **+260 lines** (excl. tests) vs the plan's ~+140 estimate. No extra mechanism, config, flag, module, or abstraction was added beyond PLAN.md's items; the overage is the mechanical cost of the plan's own list in this codebase's style — one keyword line per pass-through at ~20 existing call/signature sites for `current_event_id`, plus mandatory docstrings and two-line function separation on the seven small R3/R1 helpers the plan itself specifies. The single largest file matches the plan's shape (`execution_preparation.py`, +162 vs ~+85 estimated).
2. **Parameter rename in `_build_unseen_context_messages`**: the function already had a `current_event_id` parameter (the unseen-filter reply anchor). To give the plan's new msg-tag argument the plan-specified name, the pre-existing filter argument was renamed to `reply_to_event_id` (private function; two production call sites, five test call sites updated). Behavior unchanged.
3. **OpenAI-compat synthetic history**: the OpenAI-compatible team path synthesizes `ResolvedVisibleMessage`s with `$openai-N` event IDs; per the plan's "wrap every real ResolvedVisibleMessage" rule (and no compat branch allowed), these now render wrapped with their synthetic IDs. The *current* turn on non-Matrix channels still omits `event_id` as required by edge case 10.

Everything else follows PLAN.md exactly, including the existing-history policy (no migration or lazy normalization; old footers/blocks age out naturally; old full location blocks are ignored when locating the last compact marker).

## Live test

**Pending** — bespoke live verification will be run in a later phase via the live-test skill (`/live-test`), per the task instructions.

---

# Round 2 — review-fix report

Commit: "fix: address review findings (ISSUE-247)". All forwarded clusters (1-12) were triaged as real and fixed at the owning boundary; nothing was deferred.

## Triage and root-cause fixes

1. **Location trust boundary / spoofing (cluster 1) + duplicate-item leak (cluster 2) + team continuation leak/loss (cluster 6).** Root cause: provenance was inferred by re-parsing the composed prompt string. The reserved `key="location"` item is now split out at the typed hook boundary (`turn_policy.apply_message_enrichment`) before anything is flattened: it never enters `model_prompt`, any prompt, or any persisted text. The first collected item is authoritative; duplicates are dropped with a warning, so a second full block can never leak. The trusted text rides `_PreparedHookedPayload` → `ResponseRequest.location_item_text` → `ResponseTurnContext.location_item_text`, and execution preparation renders the transient block from typed data only. A user-authored terminal `<mindroom_message_context>` block is now plain persisted user content (regression tests for agent and team). Because the item never sits in a prompt, dynamic-tool continuations can neither leak it (B2) nor lose it (D6): every attempt re-reads the per-turn constant from ctx. The regex split machinery was deleted.
2. **Marker provenance + excluded-run/continuation dedup (clusters 1/7/8 and A4/A5).** The accepted `📍 ...` marker is now recorded in trusted run metadata (`MINDROOM_LOCATION_MARKER_METADATA_KEY`), attached to the run via the existing run-metadata channel in both the agent and team preparation paths. Dedup reads only that key, from the session **reloaded from storage** (`ScopeSessionContext.storage` + `session_id`), so same-turn continuation attempts see markers persisted by earlier attempts and user-typed `📍` lines can never forge state. Interrupted turns keep the marker: the canonical interrupted run inherits the metadata from the recorder, and `_build_interrupted_replay_run` re-renders the `📍` line inside the persisted user turn (the recorder's `user_message` remains unsynchronized).
3. **Current-message identity (cluster 3).** `reply_to_event_id` is a delivery anchor, not a message identity. Added `current_event_id` to `ResponseRequest` and `ResponseTurnContext`, populated only where the prompt is literally one Matrix event's body: the direct dispatch path in `turn_controller` (None for structured batches) and non-coalesced edit regeneration (the original edited event). Interactive selections, scheduled/detached synthetic turns, and OpenAI-compat turns default to None. Agent and team continuations seed from `ctx.current_event_id`; `TurnRecorder`/`InterruptedReplaySnapshot` carry it as a typed field, and the metadata-based (`matrix_event_id`) user wrap was deleted.
4. **Structured interrupted double-wrap (cluster 4).** Falls out of the identity fix: structured batches have `current_event_id=None`, so interrupted persistence keeps the `<messages>`/`<queued_messages>` container byte-for-byte with its per-child event IDs (unit + runner-seam streaming regression).
5. **OpenAI synthetic IDs (cluster 5).** The compat parser no longer mints `$openai-N` event IDs (empty identity), so compat history renders `<msg from="user">` without `event_id`; the prompt contract now states messages without `event_id` are not addressable, and `OPENAI_COMPAT_HISTORY_GUIDANCE` describes the synthetic-history form. Compat tests assert the absence of event IDs.
6. **Persisted run-input leak (B1/G1).** `_PromptSanitizingSqliteDb` — the existing storage sanitization boundary — now also scrubs `add_to_agent_memory=False` messages from `RunOutput.input.input_content` before upsert (this also closes the same gap for PR #1596's transient memory). The location integration test asserts over the fully serialized session (including run input): no `latitude`/`longitude`/`nearby_place`/`at_home`/`mindroom_message_context` anywhere.
7. **Team `additional_context` persistence (D1).** Investigated: Agno renders `additional_context` into the system message, which this storage boundary already strips (`prompt_roles`), and member responses are not persisted (`store_member_responses` defaults to False). No channel change needed; the serialized-session assertion above proves the invariant end-to-end.
8. **Assistant wrap on suppressed/failed deliveries (E3, cluster 3).** `apply_post_response_effects` now passes a `delivered` flag (completed + unsuppressed + run succeeded); the runner's callback maps it to `response_sender_id=None`, and the writer wraps only when a sender is present — visible failure notes and undelivered outcomes keep metadata linkage only.
9. **Content-less runs / tool-call stubs (F4 + H5, clusters 9/10).** `_wrap_final_assistant_message` targets the last assistant message with non-empty string content and falls back to that message's own text when `run.content` is absent, with wrapped-body recovery so changed callbacks still never nest tags.
10. **Model-imitation guidance (H4, cluster 11).** The `<msg>` guidance now says the tags are system-added and must never be written in replies.
11. **Reserved-key contract (C5, cluster 12).** `docs/hooks.md` documents the reserved `location` key: split at the typed boundary, current-turn-only delivery, marker schema and ordering, fail-closed behavior, and the duplicate policy.

Dropped per triage: H6 (magic-string style). H7 (scan cost) is obsolete — dedup now does a metadata key lookup instead of regex text scans.

## Gate results (round 2)

1. pytest: focused files green, then full suite `uv run pytest` exit 0 in nix-shell.
2. `uv run tach check --dependencies --interfaces`: all modules validated (new edges: `execution_preparation` → `hooks`/`agent_storage`, `turn_policy` → `logging_config`, `ai` → `constants`; `agent_storage` visibility extended).
3. pre-commit on all changed files: all hooks pass except the pre-existing `ty` macOS-import failure documented in round 1 (unchanged, reproduced on base).

---

# Round 3 — review-fix report

Commit: "fix: address review findings round 2 (ISSUE-247)". All nine forwarded clusters were real; each is fixed at the boundary owning the violated invariant.

## Triage and fixes

1. **Event-ID binding vs delivery reality (A1, A2, B1, C2, E-partial, G1, G2).** Invariant restored: a `<msg event_id>` content tag may only carry the body actually visible at that event, bound only once delivery is known. Two boundary changes:
   - *Normal path*: `apply_post_response_effects` now passes `FinalDeliveryOutcome.final_visible_body` through the persistence callback when the run succeeded and was not suppressed. The gateway already encodes ownership — `final_visible_body` is set only when the event holds this run's output; unchanged pre-existing edit targets return `is_visible_response=True` with no body. The writer wraps with that delivered body (team headers, before/final response transforms, and interactive formatting included), display chrome stripped exactly as the Matrix-fallback renderer does, keeping `run.content` as the model-native record. Visible replies that survived a late finalization failure (`terminal_status="error"`, body present, run succeeded) now retain their event identity (G1); undelivered/suppressed/unchanged outcomes persist metadata linkage only.
   - *Interrupted path*: the assistant-side wrap is deleted entirely. Interrupted snapshots persist before finalization decides whether the event survives, and the composite partial+summary+tool prose was never any event's body, so interrupted assistant content is always unwrapped (user-side wrapping, bound at ingress and delivery-independent, stays). The `response_sender_id` parameter was removed from the interrupted persistence chain.
2. **Nonexistent tool argument (C1).** `matrix_message` has no `reply_to_event_id`; both the system-context item and the prompts.py guidance now describe the real contract (`room_id`, `thread_id`, and `<msg event_id>` values passed as `target` for reactions/edits). A schema-validation test asserts every backticked argument in the generated instructions exists in `MatrixMessageTools.matrix_message`'s signature.
3. **Conflicting authoritative targets (D2, E1).** `matrix_message_target` is now a reserved runner-owned key: `_with_matrix_target_item` drops hook-provided collisions (with a warning) before appending exactly one canonical item, used by both the agent and team context constructors; documented in `docs/hooks.md`.
4. **Sync SQLite on the event loop (F2, G3).** The trusted-marker lookup now runs through `asyncio.to_thread` (`_extract_current_location_context` is async); a gated-storage heartbeat regression in `test_event_loop_offloading.py` proves the loop stays live while the read blocks.
5. **Marker duplication in compaction (D1).** `MINDROOM_LOCATION_MARKER_METADATA_KEY` joined `_SUMMARY_METADATA_OMIT_KEYS`; the integration test restored exact-count assertions (each delta exactly once, metadata key absent).
6. **Interrupted empty prompt loses baseline (E2).** The canonical user body is built first: an empty prompt with a recorded marker persists the marker alone as the user turn (unwrapped, since there is no event body), keeping the dedup timeline and replay consistent.
7. **`<msg>`-shaped legitimate output corrupted (B2).** Fixed by the cluster-1 redesign: the wrap body always comes from the delivered text, so the unwrap heuristic (`_WRAPPED_MSG_RE`, `_canonical_assistant_body`) is deleted; a literal `<msg>`-example reply is wrapped literally and changed callbacks rebuild without nesting (regression added).
8. **Team durable-storage verification + nested member runs (B3, E3, F1).** The storage sanitizer now recurses into `TeamRunOutput.member_responses` (detection and stripping, nested teams included) for both prompt-role messages and transient run inputs. A new integration test runs a real Agno `Team` (recording team model, fake member models) through real `prepare_materialized_team_execution` and `Team.arun` against the real sanitizing scope storage: the live model sees the full location block in system context, while the serialized `TeamSession` contains no coordinates, place fields, or enrichment markup and exactly one `📍 Home` in replayed content.
9. **Docs drift (C3, D3).** The hook-guide walkthrough now uses a non-reserved key for the persist-as-rendered example, states the `location` exception explicitly in both the walkthrough and policy sections, shows a structured `add_metadata("location", ...)` example that actually produces the documented marker, and documents the reserved `matrix_message_target` key; mirrored skill references regenerate via the pre-commit hook.

## Gate results (round 3)

1. pytest: all focused files green; full suite `uv run pytest` exit 0 in nix-shell.
2. `uv run tach check --dependencies --interfaces`: all modules validated (new edge: `conversation_state_writer` → `streaming`).
3. pre-commit: all hooks pass except the pre-existing `ty` macOS-import failure documented in round 1 (unchanged, reproduced on base).
