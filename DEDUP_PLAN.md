# Remaining Timeout Resilience Dedupe Work

This note tracks only the timeout and runtime-resilience dedupe work that is still not done on the `timeouts` branch.
Completed items were removed so this file stays short and current.

## Remaining Items

### 1. Startup and hot-reload reconciliation

`src/mindroom/orchestrator.py:_start_runtime` and `src/mindroom/orchestrator.py:update_config` still perform the same general reconciliation flow in different shapes.
They both prepare accounts, start bots, reconcile rooms, refresh support services, and schedule recovery for transient failures.
The remaining opportunity is to extract a small shared housekeeping core without hiding router-first startup ordering or readiness transitions.
This is medium risk because ordering is important and the current tests are tightly coupled to today’s behavior.

### 2. Retry and supervision internals

Retry and supervision policy is still split across `src/mindroom/orchestrator.py:_run_with_retry`, `src/mindroom/orchestrator.py:_run_bot_start_retry`, `src/mindroom/orchestrator.py:_run_auxiliary_task_forever`, and `src/mindroom/bot.py:AgentBot.try_start`.
These paths share the same shape of classify failure, back off, log, and either retry or stop.
The remaining opportunity is a small shared backoff or retry helper that reduces duplication without introducing a retry framework or obscuring the call flow.
This is medium risk because cancellation, runtime-state updates, and permanent-error handling are easy to regress.

### 3. Resilience test setup duplication

`tests/test_multi_agent_bot.py` still repeats the same setup for startup versus hot-reload and transient versus permanent failure cases.
The remaining opportunity is a small local test builder for those resilience scenarios once the production code is stable.
This is low risk and mostly a readability cleanup.

## Recommendation

None of the remaining items are required for the current PR to be mergeable.
If more cleanup is desired later, start with the startup versus hot-reload reconciliation because that has the best payoff.
