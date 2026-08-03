# Turn Pipeline Refactor — Implementation Status (Handoff)

Status of the implementation of `2026-08-02-turn-pipeline-lifecycle-refactor.md` (the plan, PR #1782).
Written at branch `turn-pipeline-lifecycle-refactor`, 13 commits ahead of `origin/main` (`a66c8bec`).
Every commit on the branch is atomic, tested, and lint-clean; the branch is green end to end.

## Verification state

- Full focused matrix (21 files, ingress + response + durability + delivery seams): **800 passed**.
- `uv run tach check --dependencies --interfaces`: pass.
- `uv run pytest tests/test_import_graph.py`: pass.
- `uv run ruff check src/mindroom tests`: pass.
- Pipeline boundary LOC (`uv run python scripts/testing/turn_pipeline_loc.py` or `just pipeline-loc`): started at 24,092 (34 core + 4 adjacent), currently 24,132 (core 22,329).
  Net reduction arrives in Phases 5-8, which delete outer settlement duplication.
- Known pre-existing environment issue: the `ty` pre-commit hook fails with ~47 diagnostics in macOS-only code (Quartz imports unresolved on this Linux host).
  It fails identically on `origin/main`; none of the diagnostics touch refactored files.
- `REPORT.md` at repo root is an untracked security review predating this work; leave it alone.

## Completed stages (plan execution sequence 1-4)

Commits, oldest first:

1. `4fb93b91` Phase 0 docs/measurements: plan doc, checked 38-file LOC manifest (`docs/architecture/turn-pipeline-manifest.txt`) + report command (`scripts/testing/turn_pipeline_loc.py`) + pinning test, terminal-writer inventory, method-ownership ledger, invariant test map, glossary + ordinary-turn and multi-purpose-callback sequence diagrams in `docs/architecture/bot-runtime.md`.
2. `16b85c6e` **Bug fix**: lifecycle-lock eviction no longer drops live queued signals (`ResponseLifecycleCoordinator` skipped only locked candidates; a live `_QueuedMessageState` could be evicted and its queued human input silently lost). Pinned by `tests/test_lifecycle_lock_eviction.py`.
3. `8e456982` Phase 0 invariant tests: cancellation-note markers vs `stale_stream_cleanup` (production builders incl. user_stop), `settle_pending_from_turn_store` never compacts never-run pending rows.
4. `b50a6120` Phase 0 invariant tests: pending turn claim is held before text/media normalization and released exactly once on every non-admission path.
5. `97a43ec5` Phase 0 invariant tests: persisted replay text byte-identical to live model-facing prompt (ledger + run-metadata projections); dynamic-tool continuation never calls final delivery between attempts (blocking + streaming).
6. `8737d005` Phase 0 invariant tests: live user-stop converges visible cancellation edit and durable terminal record; stop-before-placeholder is a coherent no-op.
7. `ba85686d` Phase 1a: `ReceiptLaneKey(room_id, physical_sender_id)` replaces the private lane tuple.
8. `4266ad28` Phase 1b: `CoalescingOwner = RequesterCoalescingOwner | ActiveFollowUpCoalescingOwner` union replaces the synthetic requester-string prefix; all keys via `derive_coalescing_key`. A requester ID resembling the old reserved prefix can never classify as a follow-up owner. Requester-less primary in a follow-up batch falls back to the event sender (module's existing per-message fallback).
9. `e5d02b75` Phase 1c: `PreparedTextEvent` renamed to the canonical `PreparedIngress` everywhere.
10. `95d8d8bf` Phase 2a: ordinary text carries the prepared event into the coalescing gate (no raw retention).
11. `4cdb07ca` Phase 2b: second dispatch-side normalization deleted; `TextDispatchEvent` collapsed into `PreparedIngress`; payload re-hydration/merge at dispatch deleted (identity against batch-carried metadata); `trust_hydrated_internal_metadata` plumbing deleted; stale preview-prompt refresh branch deleted (prompts hydrated at batch build).
12. `47e37844` Phase 2c: per-source evidence (effective requester, source/policy/hook kinds, trust, discovery alias, callback settlement kind, recovery flag) moved onto frozen `PreparedIngress`; `PendingEvent` is now queue-local (event, room, enqueue_time, dispatch_metadata). Raw media wrapped at enqueue via `prepare_media_ingress` (caption as body, raw nio retained on `raw_event` for attachment registration and plan_turn). Busy-reroute policy stamp is a `dataclasses.replace` on the frozen ingress. Merge-built batch events stamp `source_kind` so `is_voice_event` protocol resolution is unchanged.
13. `7992d437` Phase 3: `ResponseLifecycleKey(room_id, thread_id)` derived via `MessageTarget.lifecycle_key` keys lifecycle locks, queued signals, and queued-notice metadata; lane APIs accept `ReceiptLaneKey` with `ReceiptLaneKey.for_coalescing_owner` as the single derivation for the gate-to-lane readiness query; requester keys built only via `requester_coalescing_key`.

All Phase 0-3 exit gates pass: no parallel old/new type families, per-source evidence available after coalescing, single normalization, claim-before-normalization preserved, tach + import-graph green.

## Remaining stages (plan execution sequence 5-9)

Work from the plan doc sections directly; expected deltas and exit gates are listed there.

1. **Phase 4 — Typed Matrix delivery failures** (plan line ~343).
   Replace the internal `None` collapse in `src/mindroom/matrix/client_delivery.py` with typed failure reasons (encryption guards, sync prerequisites, unknown encryption state, send exceptions, unexpected response types); translate once at `delivery_gateway.py` into the existing final-delivery vocabulary.
   Keep the public Matrix-client surface stable.
   Expected delta: +100/-100.
2. **Phase 5 — Gateway owns final delivery** (plan line ~365).
   Exactly five non-gateway `FinalDeliveryOutcome` constructions exist, all in `src/mindroom/response_runner.py`: `:1404` (`_finalize_pre_delivery_terminal`), `:1442` (`_settle_missing_delivery_outcome`), `:1476` (`_finalize_locked_outcome` late-cancel), `:2128` (team blocking cancellation pre-event), `:2656` (agent blocking cancellation pre-event).
   Relocate them into narrow named gateway methods in `delivery_gateway.py`; share one final event-ID precedence function across streaming/blocking/cancellation/adoption/pre-delivery paths; preserve the branch matrix in the plan (suppression, failure, cleanup, cancellation, retry, adoption never substitute rendered Matrix text for replayable assistant text).
   Primary files: `delivery_gateway.py`, `final_delivery.py`, `response_terminal.py`, `streaming.py`, `matrix/stale_stream_cleanup.py`, `response_runner.py`.
   Expected delta: -250 to -400.
3. **Phase 6 — Deduplicate outer response settlement** (plan line ~405).
   Extract duplicated outer helpers in `response_runner.py` (stream-delivery failure, cancellation-note settlement, streamed finalization, timing marks, interrupted persistence, post-response outcome, session-watch setup) without replacing the shared `run_blocking_response_turn`/`stream_response_turn` drivers and without growing `BlockingTurnAdapter` (10 callback fields) / `StreamingTurnAdapter` (11) beyond those baselines.
   The method-ownership ledger (`2026-08-02-turn-pipeline-method-ownership-ledger.md`) maps every method to its candidate owner.
   Expected delta: -300 to -550.
4. **Phase 7 — Durable settlement clarity** (plan line ~441).
   Terminology table for the two deferred result enums + persisted deferred state; corrupt obligation rows become operator-visible quarantined state (currently logged and skipped, retained for repair — see `dispatch_obligations/runner.py` recovery and `bot-runtime.md` "Durable Dispatch Boundary"); preserve live deferred-row settlement vs replay-time unconditional tombstoning asymmetry.
   The terminal-writer inventory (`2026-08-02-turn-pipeline-terminal-writer-inventory.md`) lists every writer; extract shared `TurnRecord` construction only where it proves duplication (candidates: user-stop write closures, the 13 open-coded `record_responded_turn` sites, pending->deliver->terminal triples; dead `HandledTurnLedger.record_handled_turn` has no production callers).
   Expected delta: +100/-150.
5. **Phase 8 — Finish the boundary** (plan line ~483).
   Delete remaining dead plumbing, move domain decisions out of `turn_controller.py` (2777 lines) and `response_runner.py` (3081 lines) per the ownership ledger (only four new modules proposed: `router_relay.py`, `voice_ingress.py`, `response_action_executor.py`, `inbox_response_tracker.py`), update `docs/architecture/bot-runtime.md` to match the final code, duplication audit on the touched pipeline.
   Targets: turn_controller ~1800-2000 lines; pipeline boundary 1000-2500 lines smaller than the 24,092 start.

## How to resume

1. `git checkout turn-pipeline-lifecycle-refactor && uv sync --all-extras`.
2. Read the plan doc (`docs/superpowers/plans/2026-08-02-turn-pipeline-lifecycle-refactor.md`) sections for the current phase, plus this status file.
3. Keep the commit-per-stage discipline: every commit passes its focused test matrix; run the full matrix after stages 6 and 9.
4. Verification commands:
   - Focused: `uv run pytest <files> -n 0 --no-cov`
   - Full matrix: the 21-file list used in recent commits (see shell history or run `uv run pytest tests/ -n auto --no-cov -x` for the full suite before merging).
   - `uv run tach check --dependencies --interfaces`, `uv run pytest tests/test_import_graph.py`, `uv run ruff check src/mindroom tests`.
   - If pytest fails with `module mindroom has no attribute bot` (libstdc++ on NixOS): `nix-shell -I nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos shell.nix --run "uv run pytest ..."`.
5. `docs/superpowers/` is gitignored on purpose; force-add companion docs with `git add -f` (the plan PR #1782 does the same).
6. Commit style on the branch: short imperative subject, body explaining the architectural transition; never `git add .` (targeted adds only).
