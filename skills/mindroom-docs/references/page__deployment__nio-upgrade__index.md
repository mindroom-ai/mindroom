# Upgrading to Nio 1.0

The cutover from the previous Nio integration requires a fresh MindRoom event journal.
MindRoom rejects pre-durable journals instead of converting pending requests, deliveries, approvals, or membership ownership.
This procedure applies to deployments that have not yet opened a Nio 1.0 durable session.
An existing Nio 1.0 session is bound to its journal consumer; resetting that journal alone is not a supported restart procedure.

## What stays silent

Nio classifies the first room snapshot without a known membership baseline as history.
MindRoom stores those messages as context only, so they do not trigger replies or commands even when the fresh journal has no handled-event records.
Encrypted events first seen as unreadable history remain context only when their keys arrive, including after a restart.
Startup auto-resume also requires an attempted delivery owned by the current journal and room membership, so interrupted responses abandoned at cutover stay abandoned across later restarts.
Later live messages become actionable normally, and subsequent restarts preserve pending work and duplicate protection.
Messages sent during downtime or before the first room baseline can therefore remain unanswered; resend any request you still want handled after startup completes.
Room-member onboarding markers now live in the event journal; the old `tracking/room_member_joins.json` file is ignored.
Initial historical membership baselines prevent later profile updates from onboarding existing members again.

## One-time cutover

1. Finish or abandon pending responses and approvals, then stop every MindRoom process using the deployment's storage or journal.
2. Back up the configuration and persistent storage, including the event journal and encryption keys.
3. For SQLite, archive `tracking/event_journal.db` and any `event_journal.db-wal` and `event_journal.db-shm` companions together from the storage root while all processes are stopped.
   For PostgreSQL, configure `event_journal` to use a fresh database or an empty schema dedicated to this deployment, preserving the previous database for backup.
4. Archive the old `sync_continuity` directory from the storage root; its checkpoint formats are no longer read.
5. Using the new release, run `mindroom journal adopt --config config.yaml --storage-path mindroom_data` with your actual config and storage paths to bind the deployment to the fresh journal.
6. Start MindRoom and wait until startup completes before sending a new request.

Keep the same storage root, Matrix account credentials, device IDs, and `encryption_keys` directory.
Nio owns adoption of the existing crypto store and may require outstanding transport recovery to be drained or settled using the prior release before it can proceed.
Do not delete crypto keys to bypass that refusal.

The cutover abandons the old journal's unfinished work and local conversation projection.
Configuration, credentials, workspaces, memories, knowledge stores, and separate agent sessions remain in their existing stores.
Matrix room messages remain on the homeserver, and accessible history can be fetched again when the required encryption keys are available.

If you customized `MINDROOM_MATRIX_SYNC_CACHE_WRITE_GRACE_SECONDS`, replace it with `MINDROOM_MATRIX_INGESTION_GRACE_SECONDS`; the watchdog now bounds Nio ingestion progress instead of MindRoom's retired callback cache writes.

## Device recovery after the cutover

Normal restarts reuse the existing Matrix device and durable stream.
If the homeserver reports a soft logout, MindRoom renews credentials for that same device using the configured authentication method.
Pending transport input, application work, encryption keys, and delivery identities remain intact.

Automatic device replacement is unsupported.
If the device store is missing, restore the deployment's matching storage backup before restarting.
Hard logout, a deleted server device, or a changed returned identity stops startup and requires operator recovery of the bound device; clearing the journal or crypto directory is not a supported repair.
Preserve the failed deployment's state when recovering it, because retained input and attempted deliveries may still need reconciliation.
