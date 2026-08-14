# Legacy Approval Card Removal

## Goal

Remove the one-deployment compatibility path for approval cards created before native Agno continuation identity became mandatory.

The deployment gate in issue #1830 has passed on every live deployment.

## Scope

Every stored approval card must identify one persisted continuation generation and exact tool call.

New `approval_cards` tables declare `continuation_id`, `continuation_generation`, and `tool_call_id` as non-null.

Application writes validate the same invariant before inserting a card, including on databases whose existing physical columns remain nullable.

Existing databases are not rebuilt because they contain no legacy rows and a table rewrite would add migration risk without changing application behavior.

## Runtime Behavior

Card decisions continue to commit the card resolution and exact-call continuation decision atomically.

Startup continues to recover attempted native card sends, redeliver recorded native decisions, and arm expiry for unanswered native cards.

Malformed or manually corrupted cards fail closed and never authorize a continuation.

Unknown Matrix cards are not reinterpreted as current approvals.

## Deletions

Remove the additive approval-card schema probes from the SQLite and PostgreSQL startup paths.

Remove nullable continuation identity from `StoredApprovalCard` and the approval-card storage API.

Remove the generic card-only resolution transaction and its public store method.

Remove Matrix-only startup settlement and click handling from the approval manager.

Remove legacy Dynamic Workflow identity fields from approval-card parsing and rendering.

Remove tests that exist only to preserve legacy schema upgrades or Matrix-only settlement.

## Preserved Invariants

No live `Future` waits for a human decision.

Agno remains the only tool-execution approval boundary.

Approval decisions remain exact-call, deadline-checked, first-decision-wins, and crash safe.

Card publication remains claim-before-send and bind-after-send.

Recorded decisions remain recoverable until their Matrix edit is durably delivered.

No continuation, response-outbox, source-journal, STOP, restart, reload, or unavailable-owner behavior changes.

## Verification

Add a regression proving that a card without complete native continuation identity is rejected before storage.

Keep the native approval store, restart, human-decision, expiry, duplicate-decision, and publication crash-window suites green on SQLite and PostgreSQL.

Run the full test suite with `-n auto`, all pre-commit hooks, and `git diff --check` before opening the pull request.

