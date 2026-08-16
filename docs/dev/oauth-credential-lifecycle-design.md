# OAuth Credential Lifecycle Design

## Goal

MindRoom-managed OAuth credentials must remain correct when refresh grants rotate or fail, callbacks race resets, callers are cancelled, or the process stops unexpectedly.

## Why one store owns the lifecycle

OAuth state used to be split across credential JSON, generation metadata, reset intents, provider wrappers, and MCP sessions.
Each extra persistence boundary required recovery rules for partial commits.
The lifecycle now stores each canonical OAuth scope in one private SQLite database.
One SQLite transaction contains the encrypted credential payload, lease revision, connection generation, and completed browser-reset receipts.
This makes the local state transition atomic instead of reconstructing it after a partial multi-file write.

## Invariants

1. One `OAuthCredentialContext` identifies exactly one provider credential scope.
2. SQLite scope metadata binds a database to its provider, credential service, worker scope, worker key, and relevant routing agent.
3. A copied database fails closed when opened for a different scope.
4. Every credential read or mutation enters the same per-scope SQLite write transaction.
5. `BEGIN IMMEDIATE` is the cross-process operation lock.
6. Provider refresh and authorization-code exchange remain inside that transaction until the matching local commit succeeds.
7. Different credential scopes use different databases and may run concurrently.
8. Every publication advances a lease revision used by materialized clients and MCP sessions.
9. Callback publication, reset, and terminal refresh rejection also advance a connection generation used to reject stale callbacks and confirmed resets.
10. A stable browser reset receipt is checked before generation comparison or deletion.
11. Replaying a completed browser reset returns its original result and cannot delete a later connection.
12. Resetting unreadable credentials never requires decoding their encrypted payload.
13. Credential payloads use the existing `CredentialsManager` encryption codec before entering SQLite.
14. Encrypted legacy ciphertext remains recoverable when the correct key returns.
15. Plaintext legacy bytes are never copied into SQLite while credential encryption is enabled.
16. Request actor identity remains raw for room and membership checks.
17. Credential identity is canonicalized only while resolving `OAuthCredentialContext`.
18. Every provider token service ends with `_oauth`, which keeps OAuth tokens out of worker credential mirrors.
19. Provider adapters classify terminal refresh failures from structured error codes and never expose provider-controlled descriptions.
20. All consumers build reconnect responses through the shared OAuth service factory.
21. The reset tool is non-destructive and only issues a requester-bound browser confirmation URL.
22. The authenticated browser POST is the reset execution boundary.
23. MCP retirement completes before the credential transaction commits a reset.
24. A same-generation HTTP bearer rejection retires the exact MCP requester session without replaying the remote call.

## Ownership

### SQLite credential store

`src/mindroom/oauth/credential_store.py` owns the database schema, scope binding, legacy adoption, transaction admission, payload encoding, revision updates, reset receipts, file permissions, and SQLite durability settings.
The store uses rollback-journal mode, `synchronous=EXTRA`, a zero SQLite busy timeout, and cancellable asynchronous retry around lock admission.
Commit retry remains inside the same transaction, so a reader-blocked commit never repeats provider I/O.
The database and its directory are private to the runtime user.

### Credential lifecycle

`src/mindroom/oauth/credential_lifecycle.py` owns the canonical context and the semantic operations performed inside a store transaction.
It owns reads, refresh, callback exchange, claim validation, refresh-token preservation, terminal invalidation, reset compare-and-swap, and stable reset replay.
One lazy process-lifetime event loop lets asynchronous and synchronous provider adapters share the same transaction implementation.
Synchronous provider work runs in a worker thread while the owner loop retains the SQLite transaction.

### Connection and provider adapters

`src/mindroom/oauth/service.py` owns connect URLs, scope-upgrade instructions, and reconnect payloads.
`src/mindroom/oauth/providers.py` owns asynchronous provider exchange and refresh contracts plus structured error classification.
`src/mindroom/oauth/client.py` adapts synchronous Google refresh and revalidates cached clients against the canonical lease and connection generation.
`src/mindroom/custom_tools/github.py` reloads authoritative credentials for each managed call and keeps each thread's token and PyGithub client together.

### MCP sessions

`src/mindroom/mcp/manager.py` treats the credential revision and token hash as an authorization lease.
It revalidates that lease before publishing a session catalog and before admitting a remote tool call.
Requester sessions are fenced during reset so captured stale state cannot reconnect before the SQLite reset commits.

### Browser reset

`src/mindroom/oauth/reset.py` freezes provider, service, agent, canonical requester, scope, worker key, connection generation, and a random operation ID into a short-lived authenticated browser action.
`src/mindroom/api/oauth.py` revalidates that target on GET and POST, while GET remains non-mutating.
`src/mindroom/oauth/reset_execution.py` returns completed operations before transport work, otherwise retires MCP state and asks the lifecycle to atomically reset the credential.

## Transactions

### Refresh

1. Resolve the canonical context.
2. Wait cancellably for `BEGIN IMMEDIATE`.
3. Read and validate the encrypted credential snapshot.
4. Return without provider I/O when the credential is missing, unusable, or already current.
5. Call the provider while retaining the transaction.
6. Publish a rotation or atomically clear a terminally rejected credential.
7. Commit once.

Later same-scope callers observe the committed rotation and do not consume the same refresh grant again.

### OAuth callback

1. Authenticate the browser user and consume the opaque pending state.
2. Enter a cancellation-safe lifecycle operation.
3. Acquire the same SQLite transaction used by refresh.
4. Compare the pending connection generation with the current generation.
5. Exchange the authorization code and validate claims.
6. Preserve an existing refresh token only for the same verified external identity and OAuth client.
7. Publish the credential and advance both revisions.
8. Commit before propagating cancellation.

### Reset and disconnect

1. Revalidate the requester, provider, agent, scope, worker key, and confirmed connection generation.
2. Return a completed stable operation before MCP retirement.
3. Fence and retire every cached MCP requester session for the credential key.
4. Enter the SQLite transaction.
5. Recheck the stable receipt and connection generation.
6. Clear the payload, advance both revisions, and insert the permanent receipt in one commit.
7. Release the MCP fence and continue to provider authorization when requested.

If MCP teardown fails or is cancelled, no credential transaction begins.
If SQLite commit fails, the whole reset rolls back.
If the process stops after commit, the stable receipt and deletion recover together.

## Cancellation and crash behavior

Snapshot, refresh, and reset waits are cancellable before transaction admission.
Refresh becomes cancellation-safe after admission because the remote provider may rotate its grant.
Callback admission, exchange, and commit are cancellation-safe after pending browser state is consumed.
Reset remains cancellable through MCP teardown and SQLite lock admission.
Cancellation during reset commit rolls the transaction back unless commit already succeeded.
After a successful reset commit, cancellation returns the committed result.
SQLite provides the crash boundary for payload, revisions, and reset receipt together.

## Legacy adoption

The first transaction for a scope adopts its existing OAuth credential JSON into SQLite.
Readable credentials are normalized and encoded with the active credential encryption policy.
Unreadable encrypted ciphertext is retained as an unreadable payload so restoring the key can recover it.
Unreadable plaintext is represented as present but without storing the secret bytes when encryption is enabled.
Generations and reset do not decode the payload, so a corrupt credential remains resettable.
Legacy credential and sidecar files are removed only after the SQLite adoption commit and cleanup is retried on later opens.

## Verification

Tests cover same-scope serialization, different-scope concurrency, cross-process lock admission, reader-blocked commit retry, cancellation at each boundary, callback and reset compare-and-swap, stable reset replay, copied-database rejection, corrupt credential reset, wrong-key recovery, private file modes, Google and GitHub client invalidation, and MCP lease retirement.

## Non-goals

This design does not revoke grants at external providers.
It does not redesign OAuth discovery or dynamic client registration.
It does not globally canonicalize Matrix identities.
It does not exempt the reset tool from normal operator-configured tool approval policy.
