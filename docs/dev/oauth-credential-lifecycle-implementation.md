# OAuth Credential Lifecycle Implementation

## Scope

This implementation consolidates MindRoom-managed OAuth persistence into one SQLite database per canonical credential scope.
The implementation deliberately removes the former JSON generation journal, publication record, pending-reset recovery state, and advisory operation-lock file.

## Modules

### `oauth/credential_store.py`

- Derive the database path from the canonical scoped credential path.
- Create private directories and a `0600` regular database file without following a database symlink.
- Configure rollback-journal mode, `synchronous=EXTRA`, foreign keys, and a zero busy timeout.
- Retry `BEGIN IMMEDIATE` asynchronously so lock waiting remains cancellable.
- Bind the singleton state row to the exact credential scope.
- Encode and decode payloads through `CredentialsManager`.
- Store lease revision, connection generation, payload presence, unreadable state, and permanent reset receipts.
- Retry a busy `COMMIT` in the same active transaction.
- Adopt and then clean up legacy OAuth credential files.

### `oauth/credential_lifecycle.py`

- Resolve the immutable `OAuthCredentialContext`.
- Submit synchronous and asynchronous operations to one process-lifetime owner loop.
- Keep provider refresh and callback exchange inside the admitted SQLite transaction.
- Normalize provider refresh failures from structured error codes.
- Preserve an omitted refresh token only for the same verified identity and client.
- Advance the lease revision on every publication.
- Advance connection generation on callback, reset, and terminal invalidation.
- Return completed reset receipts before compare-and-swap or deletion.

### Consumers

- Google wrappers use lifecycle snapshots and generation checks before reusing cached credentials or services.
- GitHub wrappers reload lifecycle-owned credentials on the executing thread.
- MCP sessions bind to lifecycle revision plus token hash and revalidate before publication and use.
- OAuth API status, callback, disconnect, and browser reset enter the same lifecycle owner.
- Generic credential APIs continue to own non-OAuth configuration and manual fallback credentials.

## Transaction rules

### Snapshot

1. Wait for `BEGIN IMMEDIATE`.
2. Validate scope binding.
3. Decode the payload when present.
4. Commit the read transaction.

### Refresh

1. Wait cancellably for transaction admission.
2. Read the current credential.
3. Retain the transaction across provider I/O.
4. Publish at most one rotation.
5. Commit once.

### Callback

1. Shield the lifecycle call after pending state consumption.
2. Compare connection generation inside the transaction.
3. Exchange and validate inside the transaction.
4. Publish payload and both revisions together.
5. Commit before cancellation propagates.

### Reset

1. Finish fallible MCP retirement before opening the credential transaction.
2. Check a permanent stable receipt before connection-generation comparison.
3. Clear the payload and insert the receipt in one transaction.
4. Return the original result for every replay of the stable operation ID.

## Migration rules

- A missing state row may adopt one legacy OAuth credential file.
- Readable legacy credentials are re-encoded with the active encryption policy.
- Wrong-key ciphertext is retained with an unreadable marker.
- Plaintext bytes are discarded when encryption is enabled, while presence remains recorded for reset.
- The SQLite commit occurs before legacy file cleanup.
- Later opens retry cleanup, so cleanup failure is not permanent.
- SQLite scope metadata is authoritative after adoption.

## Required tests

- Same-scope refresh performs one provider rotation.
- Different scopes refresh concurrently.
- A second process waits for the first scope transaction.
- Cancellation while waiting for the writer lock leaves no transaction behind.
- A reader-blocked commit retries without rerunning publication or provider work.
- Callback cancellation after pending-state consumption still commits once.
- Reset cancellation before commit rolls back.
- Completed reset replay cannot delete a later callback credential.
- Corrupt and wrong-key credentials remain resettable.
- Restoring the correct key recovers retained ciphertext.
- A copied database fails scope binding.
- Database and directory modes remain private.
- Google, GitHub, API, and MCP integration tests read through the lifecycle rather than raw OAuth files.

## Verification commands

Use the repository environment and run focused OAuth tests first.

```bash
uv run pytest tests/test_oauth_credential_store.py tests/test_oauth_service.py tests/api/test_oauth_api.py -q
```

Run the complete repository hooks and suite before pushing.

```bash
uv run pre-commit run --all-files
uv run pytest -q
```
