# Pre-Cache-Campaign Experiment

## Goal

Build an experimental MindRoom image whose Matrix cache implementation behaves like the code immediately before PR #1586 while preserving unrelated changes now on `main`.

This branch is experimental and will not be merged.

## Baseline

The rollback baseline is the parent of commit `34fae3688cbd182bc6221e48767ed2ba2c3fafc3`.

The source branch begins at `17bf577081703b9db39b7432707dbfddaa85bc44`, which matched `origin/main` when the experiment began.

## Rollback Boundary

Restore the durable event-cache package, thread-history cache logic, conversation-cache facade internals, sync-cache trust logic, and process-local thread-resolution reuse logic to the rollback baseline.

Remove cache modules that did not exist at the rollback baseline.

Preserve unrelated post-baseline changes.

Adapt current integration callers only where required to make the old cache boundary runnable.

Do not reintroduce cache-campaign behavior through compatibility adapters.

## Validation

Install the complete development environment with `uv sync --all-extras`.

Run cache-focused tests against current-worktree imports.

Run Tach dependency and interface checks.

Run the complete pytest suite.

Run pre-commit across all files and keep unrelated formatter rewrites out of the branch.

Build the production MindRoom Dockerfile.

## Publishing

Push branch `experiment/pre-cache-campaign`.

Do not open a pull request.

Publish only `ghcr.io/mindroom-ai/mindroom:experiment-pre-cache-campaign`.

Publish Linux AMD64 and ARM64 variants behind one multi-architecture manifest.

Do not modify `latest`, release tags, or the minimal image.

Verify the remote manifest and run a container smoke test.

## Experiment Safety

Use fresh isolated cache storage when running the image.

Do not point the experiment at campaign-era SQLite or PostgreSQL cache state because schema and state compatibility are not part of this experiment.

Treat any required non-cache rollback outside the declared boundary as an explicit integration repair and record it in the branch history.
