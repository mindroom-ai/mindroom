# Turn Pipeline Refactor — Implementation Status (Handoff)

Status of the implementation of `2026-08-02-turn-pipeline-lifecycle-refactor.md` (the plan, PR #1782).
Written at branch `turn-pipeline-lifecycle-refactor`, 21 commits ahead of `origin/main` (`a66c8bec`).
Every commit on the branch is atomic, tested, and lint-clean; the branch is green end to end.

## Verification state

- Focused matrices (ingress, response, durability, delivery): green after every stage (see per-stage commit messages).
- Full suite `uv run pytest tests/ --no-cov`: green except `tests/test_ci_release_dispatch.py` (6 failures, `FileNotFoundError: 'bash'` in the test's subprocess environment — untouched by this branch and fails identically on `origin/main`).
- `uv run tach check --dependencies --interfaces`: pass.
- `uv run pytest tests/test_import_graph.py`: pass.
- `uv run ruff check src/mindroom tests`: pass.
- Pipeline boundary LOC (`uv run python scripts/testing/turn_pipeline_loc.py` or `just pipeline-loc`): started at 24,092 (34 core + 4 adjacent), currently 24,355 (35 core incl. new `router_relay.py` at 22,522 + 4 adjacent at 1,833).
  Absolute file lengths are tracking metrics, not merge gates (plan line ~102).
- Known pre-existing environment issue: the `ty` pre-commit hook fails with ~47 diagnostics in macOS-only code (Quartz imports unresolved on this Linux host), identically on `origin/main`.
- `REPORT.md` at repo root is an untracked security review predating this work; leave it alone.

## Completed stages (plan execution sequence 1-9, all landed)

Commits, oldest first:

1. `4fb93b91` Phase 0 docs/measurements: plan doc, checked 38-file LOC manifest + report command + pinning test, terminal-writer inventory, method-ownership ledger, invariant test map, glossary + durable-sequence diagrams in `docs/architecture/bot-runtime.md`.
2. `16b85c6e` **Bug fix**: lifecycle-lock eviction no longer drops live queued signals (real data-loss bug; pinned by `tests/test_lifecycle_lock_eviction.py`).
3. `8e456982` Phase 0 invariant tests: cancellation-note markers vs `stale_stream_cleanup` (production builders incl. user_stop), `settle_pending_from_turn_store` never compacts never-run pending rows.
4. `b50a6120` Phase 0 invariant tests: pending turn claim held before text/media normalization, released exactly once on every non-admission path.
5. `97a43ec5` Phase 0 invariant tests: persisted replay text byte-identical to live prompt; dynamic-tool continuation never calls final delivery between attempts.
6. `8737d005` Phase 0 invariant tests: live user-stop converges visible cancellation edit and durable terminal record; stop-before-placeholder is a coherent no-op.
7. `ba85686d` Phase 1a: `ReceiptLaneKey(room_id, physical_sender_id)` replaces the private lane tuple.
8. `4266ad28` Phase 1b: `CoalescingOwner` union replaces the synthetic requester-string prefix; all keys via `derive_coalescing_key`.
9. `e5d02b75` Phase 1c: `PreparedTextEvent` renamed to the canonical `PreparedIngress` everywhere.
10. `95d8d8bf` Phase 2a: ordinary text carries the prepared event into the coalescing gate (no raw retention).
11. `4cdb07ca` Phase 2b: second dispatch-side normalization deleted; `TextDispatchEvent` collapsed into `PreparedIngress`; payload re-hydration/merge and `trust_hydrated_internal_metadata` plumbing deleted.
12. `47e37844` Phase 2c: per-source evidence moved onto frozen `PreparedIngress`; `PendingEvent` is queue-local; media wrapped via `prepare_media_ingress` with `raw_event` retained for registration/planning; busy-reroute stamp via `dataclasses.replace`; merge-built batch events stamp `source_kind` so `is_voice_event` resolution is unchanged.
13. `7992d437` Phase 3: `ResponseLifecycleKey` via `MessageTarget.lifecycle_key` keys locks/signals/notices; lane APIs accept `ReceiptLaneKey` (`for_coalescing_owner` for the readiness query); requester keys only via `requester_coalescing_key`.
14. `2904f20c` Phase 4: `send_message_outcome`/`edit_message_outcome` return typed `MatrixDeliveryFailure` (encryption guard, sync prerequisite, unknown encryption state, send exception, unexpected response); public `*_result` surface keeps its None-collapse; gateway translates once into its failure vocabulary; tach interface updated.
15. `208639d0` Phase 5a: the gateway is the sole `FinalDeliveryOutcome` constructor; the five non-gateway sites in `response_runner.py` relocated into `terminal_outcome_without_visible_event` and `cancelled_terminal_outcome`.
16. `8986f853` Phase 5b: cancellation provenance centralized as `resolved_cancel_source` on both delivery types (explicit provenance wins, incl. on error outcomes; then persisted reason; interrupted fallback); streaming/blocking event-ID precedence equivalence pinned.
17. `207f19c3` Phase 6: duplicated outer settlement extracted — `_settle_blocking_cancellation`, `_persist_failed_turn`, `_note_final_delivery_timing`, `_finalize_streamed_turn`; adapter baselines held (Blocking 10 / Streaming 11 fields) and pinned structurally.
18. `c5a463e0` Phase 7: deferred-enum terminology table; corrupt obligation rows are operator-visible quarantined state (`DispatchObligationStorage.quarantined()` + `dispatch_obligation_quarantined` summary) and still protect cleanup ownership; shared `record_deferred_outcome_response`/`record_user_stop_terminal` on turn_store; dead `HandledTurnLedger.record_handled_turn` deleted; tach updated.
19. `e25d96f0` Phase 8a: router relay cluster extracted to `router_relay.py` (`RouterRelayDeps`, `execute_router_relay`); `turn_controller.py` 2777->2480 lines; pipeline manifest now 35 core files.
20. `0273e42e` Phase 8b: `is_media_dispatch_event` sees through the media `PreparedIngress` wrapper; `bot-runtime.md` documents the code that exists.

## Remaining work (not part of the plan's merge gates)

The plan's exit gates for every phase pass.
What remains is the optional tail of the composition-root migration (tracking targets, not gates):

- `turn_controller.py` is 2,480 lines (target range 1,800-2,000): the ownership ledger's remaining extraction candidates are the voice readiness cluster (~360 lines), interactive-selection execution (~170), `_execute_response_action` (~180), and `_prepare_dispatch` (~180).
- `response_runner.py` is 3,114 lines: remaining candidates per the ledger — team turn driver into `response_turn.py`, interrupted-recorder cluster into `history/interrupted_replay.py`, inbox cluster into a new `inbox_response_tracker.py`, enrichment/history helpers into `execution_preparation.py`.
- Deeper Phase 6 consolidation the plan allowed but did not require: streamed-delivery-error settle extraction (agent vs team) and session-watch setup sharing; both were evaluated and deliberately skipped (parameter-bag risk the plan warns against).

To resume those, work from `docs/superpowers/plans/2026-08-02-turn-pipeline-method-ownership-ledger.md` (every method mapped to a candidate owner) and keep the commit-per-stage discipline.

## How to resume

1. `git checkout turn-pipeline-lifecycle-refactor && uv sync --all-extras`.
2. Read the plan doc (`docs/superpowers/plans/2026-08-02-turn-pipeline-lifecycle-refactor.md`) sections for the current phase, plus this status file.
3. Verification commands:
   - Focused: `uv run pytest <files> -n 0 --no-cov`
   - Full suite: `uv run pytest tests/ --no-cov`
   - `uv run tach check --dependencies --interfaces`, `uv run pytest tests/test_import_graph.py`, `uv run ruff check src/mindroom tests`.
   - If pytest fails with `module mindroom has no attribute bot` (libstdc++ on NixOS): `nix-shell -I nixpkgs=/nix/var/nix/profiles/per-user/root/channels/nixos shell.nix --run "uv run pytest ..."`.
4. `docs/superpowers/` is gitignored on purpose; force-add companion docs with `git add -f` (the plan PR #1782 does the same).
5. Commit style on the branch: short imperative subject, body explaining the architectural transition; never `git add .` (targeted adds only).
