# Runtime Resilience Audit

Audit date: March 7, 2026.

This document records the resilience findings that motivated this branch and the current status after the fixes in this PR.

## Addressed In This Branch

- The top-level runtime supervisor no longer uses `asyncio.FIRST_COMPLETED`.
- Auxiliary tasks now restart in place instead of taking down the whole process when the config watcher, skills watcher, or bundled API server exits.

- Startup no longer blocks on knowledge initialization.
- Knowledge refresh now runs in the background and keeps retrying until it succeeds.
- Agents can answer without knowledge in the meantime, matching the existing OpenAI-compatible API behavior.

- Failed bot startups are no longer dropped after a few attempts.
- Startup now keeps the router mandatory, but failed non-router bots stay registered and are retried in the background until they come up.
- Hot-reloaded bots follow the same pattern instead of being removed from the registry on failure.

- Startup-critical Matrix preparation now retries instead of aborting the process on transient failures.
- The internal MindRoom user account setup retries until it succeeds unless the error is clearly permanent.
- Initial room setup and membership reconciliation also retry instead of crashing the runtime.

- Matrix homeserver waiting is still configurable, but the default remains "wait forever".
- That keeps self-hosted processes alive across long homeserver outages while still allowing CI to set a bounded timeout explicitly.

## Remaining Gaps

- The bundled API server still shares one process with the Matrix runtime.
- It no longer shares the same crash fate, but it still shares deployment, readiness, and shutdown lifecycle.

- `/api/ready` still reflects orchestrator readiness rather than API-only readiness.
- The OpenAI-compatible API can work without Matrix for some configurations, but the readiness endpoint does not currently advertise that degraded mode separately.

- Room creation, invitations, and joins are still part of startup readiness.
- They now retry instead of crashing the process, but readiness can still remain in `starting` for a long time if Matrix room-management operations stay unhealthy.

- Permanent Matrix misconfiguration is still only partially distinguished from transient failure.
- The internal user-account path has explicit permanent-error detection.
- Individual bot startup retries are still conservative and mostly log-driven.

## Practical Outcome

- A temporary outage in Matrix, the vector store, the knowledge watcher path, or the bundled API server should no longer tear down the whole process.
- The runtime now stays alive and keeps retrying until dependencies return.
- The main remaining work is to separate degraded readiness from full readiness and decide whether the bundled API and Matrix runtime should eventually become separate services.
