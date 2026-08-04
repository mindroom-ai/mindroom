# OpenRouter Reasoning-Details Coalescing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent OpenRouter streaming reasoning deltas from inflating persisted sessions and replay payloads, then safely normalize Mom's existing polluted history.

**Architecture:** Add a conservative adjacent-fragment coalescer and use it at the two OpenRouter boundaries: streamed provider-data aggregation and persisted-message wire formatting. Deploy the tested source files to Mom, stop the service, back up and recursively normalize the SQLite `runs` JSON, verify it, and restart once.

**Tech Stack:** Python 3.13, Agno 2.6.12, pytest, SQLite, systemd/Incus.

---

### Task 1: Define conservative coalescing behavior

**Files:**
- Modify: `tests/test_openai_models.py`
- Modify: `src/mindroom/openai_models.py`

- [ ] **Step 1: Write failing pure-helper tests**

Add table-driven tests for adjacent `reasoning.text` dictionaries. Require string text, exact equality of every other field, and either a numeric `index` or non-empty string `id`. Verify conflicting/missing IDs, signatures, malformed entries, non-string text, non-adjacent matches, and non-text details remain separate.

Also deep-copy the test input before calling the helper and assert the original list and dictionaries are byte-for-byte unchanged afterward.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest -q tests/test_openai_models.py -k reasoning_details`

Expected: collection/import failure because the helper does not exist.

- [ ] **Step 3: Implement the minimal helper**

Add `_coalesced_openrouter_reasoning_details(details: object) -> object` to `openai_models.py`. Iterate once, copy the list, and merge only with the immediately preceding compatible detail by concatenating `text`. Return malformed/non-list inputs unchanged.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `uv run pytest -q tests/test_openai_models.py -k reasoning_details`

Expected: all new helper tests pass.

### Task 2: Normalize streaming storage and replay

**Files:**
- Modify: `tests/test_openai_models.py`
- Modify: `src/mindroom/openai_models.py`

- [ ] **Step 1: Write failing integration tests**

Test `MindRoomOpenRouter._populate_stream_data` with two same-index deltas and assert one concatenated stored detail. Test `_format_message` with polluted history and assert the wire message is coalesced while the original `Message.provider_data` is unchanged. Verify unrelated provider data survives both paths.

Include a streaming delta containing multiple reasoning-detail entries so ordering and within-delta adjacent coalescing are covered, in addition to cross-delta coalescing.

Because `_populate_stream_data` is a generator, consume its output and assert the override preserves the superclass's yielded response deltas and their content in addition to the accumulated `stream_data` state.

- [ ] **Step 2: Run the integration tests and verify RED**

Run: `uv run pytest -q tests/test_openai_models.py -k reasoning_details`

Expected: streamed details remain separate and replay emits the polluted list.

- [ ] **Step 3: Implement streaming aggregation**

Override `MindRoomOpenRouter._populate_stream_data`. Merge incoming `reasoning_details` into the existing stream list in place using the same compatibility rule, then pass a dataclass copy of the delta without that key to Agno's generic merger. This keeps aggregation linear rather than repeatedly rebuilding the accumulated list.

- [ ] **Step 4: Implement non-mutating replay normalization**

Override `MindRoomOpenRouter._format_message`. When an assistant message contains a changed reasoning-details list, create a Pydantic copy with copied provider data and pass that to Agno. Never mutate the persisted `Message`.

- [ ] **Step 5: Run focused and module tests**

Run: `uv run pytest -q tests/test_openai_models.py`

Expected: all tests pass.

- [ ] **Step 6: Run static checks**

Run: `uv run ruff check src/mindroom/openai_models.py tests/test_openai_models.py && uv run ruff format --check src/mindroom/openai_models.py tests/test_openai_models.py && uv run ty check src/mindroom/openai_models.py`

Expected: zero errors.

### Task 3: Build and test the database normalizer

**Files:**
- Add: `scripts/normalize_openrouter_reasoning_details.py`
- Add: `tests/test_normalize_openrouter_reasoning_details.py`

- [ ] **Step 1: Write failing migration tests**

Create a temporary SQLite database with Mom's exact `mind_sessions(session_id, runs)` table/column shape. Include `reasoning_details` beneath messages, run-level `model_provider_data`, nested events, unrelated JSON, malformed details, and multiple session rows. Test dry-run leaves the database byte-identical, apply mode produces the exact recursively normalized JSON expected for each row, and all unrelated values remain identical.

- [ ] **Step 2: Run migration tests and verify RED**

Run: `uv run pytest -q tests/test_normalize_openrouter_reasoning_details.py`

Expected: failure because the normalizer does not exist.

- [ ] **Step 3: Implement the normalizer**

Implement a CLI with required database path, required `--table` argument, and mutually exclusive `--dry-run`/`--apply` modes. Validate that the table is an exact identifier present in `sqlite_master` and has the required `session_id` and `runs` columns; reject unsafe identifiers or schema mismatches. Production will pass `--table mind_sessions`. Recursively locate every `reasoning_details` key and use the production helper. Before applying, checkpoint WAL, create a timestamped sibling backup with SQLite's backup API, and require `PRAGMA integrity_check = ok` on both source and backup. Update all changed rows in one `BEGIN IMMEDIATE` transaction.

For each row, retain the decoded pre-image and compute the exact expected post-image using only the recursive coalescer. Re-read every row after commit and require equality with its expected post-image; require unchanged session IDs, row count, run count, and concatenated reasoning text. On any mismatch, roll back before commit or exit nonzero with the verified backup path and exact restore command.

- [ ] **Step 4: Run migration tests and verify GREEN**

Run: `uv run pytest -q tests/test_normalize_openrouter_reasoning_details.py`

Expected: all tests pass, including backup creation and restoration of the fixture from that backup.

- [ ] **Step 5: Exercise the exact CLI against a disposable copy**

Copy the fixture database, run both `--dry-run` and `--apply`, inspect the emitted counts and backup path, restore the disposable database from its backup, and prove it matches the original checksum.

### Task 4: Commit the implementation

**Files:**
- Modify: `src/mindroom/openai_models.py`
- Modify: `tests/test_openai_models.py`
- Add: `scripts/normalize_openrouter_reasoning_details.py`
- Add: `tests/test_normalize_openrouter_reasoning_details.py`
- Add: `docs/superpowers/plans/2026-08-04-openrouter-reasoning-details-coalescing.md`

- [ ] **Step 1: Review the diff**

Run: `git diff --check && git diff -- src/mindroom/openai_models.py tests/test_openai_models.py scripts/normalize_openrouter_reasoning_details.py tests/test_normalize_openrouter_reasoning_details.py`

- [ ] **Step 2: Run the broader relevant suite**

Run: `uv run pytest -q tests/test_openai_models.py tests/test_normalize_openrouter_reasoning_details.py && uv run ruff check src/mindroom/openai_models.py tests/test_openai_models.py scripts/normalize_openrouter_reasoning_details.py tests/test_normalize_openrouter_reasoning_details.py && uv run ruff format --check src/mindroom/openai_models.py tests/test_openai_models.py scripts/normalize_openrouter_reasoning_details.py tests/test_normalize_openrouter_reasoning_details.py && uv run ty check src/mindroom/openai_models.py scripts/normalize_openrouter_reasoning_details.py`

- [ ] **Step 3: Commit locally**

Run: `git add src/mindroom/openai_models.py tests/test_openai_models.py scripts/normalize_openrouter_reasoning_details.py tests/test_normalize_openrouter_reasoning_details.py && git add -f docs/superpowers/plans/2026-08-04-openrouter-reasoning-details-coalescing.md && git commit -m "fix: coalesce OpenRouter reasoning stream details"`

Do not push without separate authorization.

### Task 5: Preflight Mom's exact runtime target

**Remote target:** NAS host alias `nas`, Incus instance `mindroom-mom`

- [ ] **Step 1: Resolve the remote instance and project**

Run `ssh nas 'incus list --all-projects --format csv -c e,n,s'` and require exactly one running `mindroom-mom` entry. Record its project with `incus project show <resolved-project>`. Do not use the local Incus daemon. Pass `--project <resolved-project>` explicitly to every subsequent `incus` command, for example `incus exec --project <resolved-project> mindroom-mom -- ...` and `incus file push --project <resolved-project> ... mindroom-mom/...`.

- [ ] **Step 2: Resolve service configuration and identity**

Run `ssh nas 'incus exec --project <resolved-project> mindroom-mom -- systemctl cat mindroom'` and record `User`, `Environment=MINDROOM_CONFIG_PATH`, `EnvironmentFile`, `WorkingDirectory`, and `ExecStart`. Require the expected checkout `/srv/mindroom` and live config `/home/basnijholt/.mindroom/config.yaml` before continuing.

- [ ] **Step 3: Resolve and validate the live database**

Read the effective config to resolve the exact sessions database, then inspect its SQLite schema, table/column names, `PRAGMA integrity_check`, session/run counts, WAL mode, file/WAL sizes, owner/mode, and parent filesystem free space. Require enough free space for the backup plus a separately gated `VACUUM` copy.

- [ ] **Step 4: Record runtime baseline and fresh-log cursor**

Record checkout SHA/status, service state, PID, `NRestarts`, current/peak memory, configured model ID, and `journalctl -u mindroom --show-cursor -n 1`. This cursor is the lower bound for post-start log inspection.

### Task 6: Deploy the committed revision and normalize Mom's history

**Files:**
- Deploy from the committed revision: `src/mindroom/openai_models.py`
- Deploy from the committed revision: `tests/test_openai_models.py`
- Deploy from the committed revision: `scripts/normalize_openrouter_reasoning_details.py`
- Deploy from the committed revision: `tests/test_normalize_openrouter_reasoning_details.py`
- Data: resolved live database, expected `/home/basnijholt/.mindroom/mindroom_data/agents/mind/sessions/mind.db`

- [ ] **Step 1: Deploy the exact committed files and test them**

Export/copy the four files from `git show <commit>:<path>`, verify their SHA-256 checksums in the container, and run the focused OpenAI-model and normalizer tests in `/srv/mindroom`. The checkout will intentionally be dirty until the fix is published upstream.

- [ ] **Step 2: Dry-run against a disposable live-data copy while service remains running**

Use SQLite's backup API to create a disposable consistent copy, run the committed normalizer with `--dry-run` and then `--apply` on that copy, and verify exact per-row expected JSON, integrity, counts, and restoration from its generated backup. Do not touch the live database in this step.

- [ ] **Step 3: Stop once, back up, and apply**

Capture a new journal cursor, stop `mindroom.service`, verify it is inactive, and invoke the committed normalizer with `--apply` on the resolved live database. It must checkpoint WAL, create and integrity-check a timestamped sibling backup before mutation, then enforce its per-row post-image invariants. If it exits nonzero, leave the service stopped, restore atomically from the verified backup using the emitted exact command, integrity-check the restored database, and then start the service.

- [ ] **Step 4: Gate optional physical compaction**

Report logical size savings first. Run `VACUUM` only if meaningful physical size reduction is desired and free space exceeds twice the live database size plus safety margin. If run, integrity-check again and preserve the verified pre-migration backup.

- [ ] **Step 5: Start and verify runtime behavior**

Start `mindroom.service` once. Require active state, expected model ID, expected checkout status, no new `NRestarts`, and no startup errors after the captured journal cursor. Record current/peak memory and the verified backup path.

- [ ] **Step 6: Verify a fresh reasoning-enabled turn**

Trigger one normal Mom agent turn through the existing Matrix path (without changing configuration), trace its event in fresh logs, and inspect the newly persisted run plus replay formatting. Require adjacent compatible `reasoning_details` to be coalesced and confirm memory settles after the turn. If sending a user-visible Matrix message requires new authority, ask the user to send one and complete the persisted-run check immediately afterward.
