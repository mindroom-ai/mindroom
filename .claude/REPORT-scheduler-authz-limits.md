# Scheduler Authorization And Limits Report

Scope: scheduling API, scheduling runtime, restore path authorization, Matrix state integrity, and room task quotas.

## Findings And Fixes

SCHED-SCHED-1 was valid.
API schedule edits could keep the prior workflow `created_by` after a different authenticated Matrix user edited the task.
Fix: schedule update now derives the requester from API auth or trusted upstream Matrix identity and rebinds edited workflows to that Matrix user.
Standalone local admin requests without a Matrix identity keep the old creator because there is no stronger local Matrix principal to bind.

SCHED-SCHED-2 was valid on the same API path.
List, edit, and cancel accepted arbitrary room IDs or task rooms without checking the caller against the room authorization policy.
Fix: API schedule list, update, and cancel now authorize the requester against the target room with the loaded runtime config.

SCHED-SCHED-4 was valid.
There was no per-room pending task cap and no minimum cron cadence, so a user could create many tasks or a cron schedule that fires every minute.
Fix: room scheduling now rejects more than 100 pending tasks and rejects cron schedules with cadence below 5 minutes.

SCHED-SCHED-5 was valid.
Restore and running scheduled tasks trusted raw Matrix state, including `created_by`.
Fix: scheduler-owned Matrix state is now HMAC signed under runtime storage, restore and task polling require trusted signed state, and both restore and fire paths revalidate `created_by` room and responder permissions.
Follow-up review fixes: room task caps and schedule list/edit/cancel reads now count only signed state, API auth no longer falls back to owner Matrix identity for authenticated users without Matrix claims, cron tasks are marked failed when creator authorization is revoked, cron cadence errors are not double-wrapped as syntax errors, and signature-key fsync is best effort on unsupported filesystems.

SCHED-SCHED-3 and SCHED-SCHED-6 were reviewed only at the optional boundary.
The nearby interactive and stop handlers are separate reaction flows, not local scheduler state changes, so I did not change them in this scoped scheduler patch.

## Verification

Focused lint and type checks passed:

```bash
PATH="$PWD/.venv/bin:$PATH" uv run ruff check src/mindroom/scheduling.py src/mindroom/api/schedules.py src/mindroom/commands/handler.py src/mindroom/custom_tools/scheduler.py tests/test_scheduling.py tests/test_bot_scheduling.py tests/test_scheduler_tool.py tests/api/test_schedules_api.py
PATH="$PWD/.venv/bin:$PATH" uv run ty check src/mindroom/scheduling.py src/mindroom/api/schedules.py src/mindroom/commands/handler.py src/mindroom/custom_tools/scheduler.py tests/test_scheduling.py tests/test_bot_scheduling.py tests/test_scheduler_tool.py tests/api/test_schedules_api.py
```

Focused scheduler pytest passed:

```bash
PATH="$PWD/.venv/bin:$PATH" uv run pytest tests/api/test_schedules_api.py tests/test_scheduling.py tests/test_scheduler_tool.py tests/test_bot_scheduling.py -x -n 0 --no-cov -v
```

Result: 100 passed.

Full pytest passed:

```bash
PATH="$PWD/.venv/bin:$PATH" uv run pytest -x -n 0 --no-cov -v
```

Result: 7711 passed, 60 skipped.

Pre-commit passed:

```bash
PATH="$PWD/.venv/bin:$HOME/.bun/bin:$PATH" uv run pre-commit run --all-files
```
