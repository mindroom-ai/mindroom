# Docker Worker Stale-Image Recovery Design

## Goal

Make Docker worker protocol-mismatch recovery preserve accurate lifecycle metadata, progress reporting, operator guidance, and bounded retry behavior.

## Design

`DockerWorkerBackend.ensure_worker` remains the owner of worker lifecycle transitions, persistence, timestamps, and progress events.
When the first readiness check raises `_WorkerImageIncompatibleError`, `ensure_worker` records another startup attempt with `prepare_worker_ensure_lifecycle(..., should_restart=True)` and persists the updated metadata before pulling or replacing the container.
The recovery refreshes `last_started_at` and increments `startup_count` for both newly created and previously ready workers.
`ensure_worker` emits `cold_start` at recovery time only when the current ensure attempt has not already emitted that phase.

`_relaunch_after_stale_image` remains responsible only for Docker operations.
It pulls the configured image once, recomputes the launch-config hash, removes the incompatible container, and creates its replacement.
The replacement readiness check stays outside the typed recovery catch so a second protocol mismatch follows the ordinary failure path without another pull.

## Error Behavior

If the pull fails, the raised `WorkerBackendError` retains the original protocol-compatibility guidance and appends the pull failure detail.
If the replacement image still reports an incompatible protocol, startup fails after exactly one pull and one replacement attempt.

## Tests

Regression coverage will verify lifecycle counts, timestamps, and progress for fresh and reused worker recovery.
Negative-path coverage will assert the pull-error suffix and prove that a second mismatch does not trigger another pull.
The existing Docker, shared lifecycle, and Kubernetes worker suites will provide broader regression coverage.

## Documentation

The dedicated Docker worker deployment guide will explain the automatic pull-and-relaunch attempt.
It will also explain that operators must rebuild or select a compatible image when the retry still fails, and that pull failures preserve the compatibility guidance.
