# Upgrading to Nio 1.0

Existing deployments upgrade automatically during normal MindRoom startup.
Stop the previous backend, install the new release, and start it with the same configuration and storage root.
No journal archival, database reset, or `mindroom journal adopt` command is required.
As with any storage upgrade, keep a backup of configuration and persistent storage before replacing the running version.
Do not run old and new backend versions against the same storage concurrently.

## What the automatic migration preserves

MindRoom upgrades a pre-Nio-1 event journal in place on both SQLite and PostgreSQL.
The journal generation and installation binding remain unchanged, along with event identities, projected message history, redactions, and handled-turn records.
Configuration, credentials, workspaces, memories, knowledge stores, and separate agent sessions stay in their existing stores.
Matrix accounts, device IDs, encryption keys, and device trust remain intact.

The journal migration retires unfinished old requests, deliveries, approvals, and membership/hydration bookkeeping in the same transaction that creates the new ingestion consumer table.
An interrupted transaction rolls back; the next startup retries the upgrade.
After that transaction commits, subsequent startups preserve new pending work normally.
Recognized older `sync_continuity` records are converted atomically, retaining pending join/decrypt fences while discarding checkpoints now owned by Nio.
Before Nio first adopts an existing crypto store, MindRoom also retires obsolete transport recovery rows under the store's exclusive lease without changing crypto keys.
An already durable Nio store is never reset by this migration.

## What does not continue

Unfinished replies and tool approvals from before the upgrade are abandoned.
An old approval card may remain visible, but it cannot resume the retired continuation.
A tool action that already happened is not undone; check its result before manually retrying it.
Old interrupted responses do not auto-resume because startup recovery requires delivery ownership in the current journal and room membership.

Nio establishes a new room baseline without importing the previous sync checkpoint.
Initial historical messages are context only: they do not trigger replies or commands.
Encrypted events first seen as unreadable history remain context only when their keys arrive, including after a restart.
Messages sent during downtime or before the first room baseline can therefore remain unanswered; resend any request you still want handled after startup completes.
Later live messages become actionable normally, and subsequent restarts preserve pending work and duplicate protection.
Room-member onboarding markers now live in the event journal; the old `tracking/room_member_joins.json` file is ignored.
Initial historical membership baselines prevent later profile updates from onboarding existing members again.

If you customized `MINDROOM_MATRIX_SYNC_CACHE_WRITE_GRACE_SECONDS`, replace it with `MINDROOM_MATRIX_INGESTION_GRACE_SECONDS`; the watchdog now bounds Nio ingestion progress instead of MindRoom's retired callback cache writes.

## Device recovery after the upgrade

Normal restarts reuse the existing Matrix device and durable stream.
If the homeserver reports a soft logout, MindRoom renews credentials for that same device using the configured authentication method.
Pending transport input, application work, encryption keys, and delivery identities remain intact.

Automatic device replacement is unsupported.
If the device store is missing, restore the deployment's matching storage backup before restarting.
Hard logout, a deleted server device, a changed returned identity, corrupt storage, or an unknown storage format still requires operator recovery.
An existing Nio 1.0 session is bound to its journal consumer; clearing the journal or crypto directory is not a supported repair.
Preserve the failed deployment's state when recovering it, because retained input and attempted deliveries may still need reconciliation.
