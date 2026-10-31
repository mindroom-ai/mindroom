# Script runs: one supervised-process adapter, one wire, one owner per run rule

This record keeps the decisions behind the October 2026 script-run refactor that the code cannot show: why each owner sits where it does, which caller policies stay separate on purpose, and the alternatives review rejected.

## Callers start their own supervisor

Unsafe-local runs and worker routes share one adapter for launching, polling, and signalling a script, but each caller starts its supervisor itself.
A supervisor that fails to start means something different to each caller: an unsafe-local launch has definitely not spawned and fails the run at once, a worker route answers with a structured failure, and the recovery integration test drives its own supervisor instance.
Starting the supervisor inside the adapter would have moved the unsafe-local start failure into the ambiguous-launch window, where the run waits for reconciliation instead of failing.

The worker routes keep checking the snapshot files, the source digest, and the gateway URL although the shim checks the files again inside the process: the route answers the primary before anything runs, and the shim guards against what can change in the workspace in between.

## Launch facts the script process reads

The environment names, the size limits, and the snapshot layout live in a module that imports only the standard library, because the shim and the script SDK import it inside the script's own process.

## Upgrade compatibility of the worker wire

Kubernetes script workers can outlive a primary upgrade, so the script protocol keeps its own version, which also feeds every run's recovery signature, requests reject unknown fields, and responses accept fields a newer worker adds.
Dropping the never-read `failure_kind` response field is compatible in both directions: an older primary reads its absence as the worker failure it always assumed, and a newer primary ignores it from an older worker.
A rejected message becomes a worker error that names each field but never its value, because a launch request carries the primary's config snapshot.

## Where request failures become the manager's error

Start, status, cancel, and durable reconciliation report worker transport, worker backend, and local supervisor startup failures as the manager's own error, so the script tool answers with its JSON error instead of raising and the lifecycle's maintenance and recovery passes catch one error type.
A lifecycle catch that named the specific types missed a primary supervisor that cannot start, which failed every maintenance pass instead of leaving that run for the next one.
The translation sits at those public methods, not at every raise site, which would spread one rule over the worker client, the supervisor adapter, and each backend call.
Process-only reconciliation of a revoked run keeps the raw failure, because its one caller treats every exception as retryable.
Rejected: a base exception shared by the manager, the wire, and backend errors, which needed a new module below all three; and translating in the tool, which would make the tool the owner of the manager's failure set.

## Why the claim rechecks the run first

A call claim checks that its run still accepts calls before it looks up a retried call of the same ID.
A cancellation can land while the owner's live authority is checked; checking the run inside the claim's transaction refuses both a new call and a retry for that run, so the broker verifies the capability only once.

## Rejected alternatives

- Collapsing the cancellation receipt's three flags into "signalled or raised", although the manager only checks that a receipt is not empty: older primaries read the three flags from newer workers.
- Making a run's execution identity the only owner of its agent, requester, room, and thread: rows the resource-snapshot migration preserved hold identities without a channel, so the identity cannot rebuild those fields.
- Folding the script protocol version into the worker protocol version: a worker-protocol bump would then stop Kubernetes runs that survive primary upgrades from being adopted.
- Merging tool jobs and script runs into one engine: scripts are host processes that outlive replies and primary restarts, which tool jobs never do.
- Collapsing the lifecycle's phased interruption into the manager's terminate: the phases are the reload contract recorded in [Worker plumbing](2026-10-28-worker-plumbing.md).
