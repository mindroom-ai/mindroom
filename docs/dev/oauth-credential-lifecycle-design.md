# OAuth Credential Lifecycle Design

## Goal

MindRoom-managed OAuth credentials must remain recoverable when refresh grants are revoked, while reconnect, reset, callback, and refresh operations remain correct under concurrency and cancellation.

## Problem

The current implementation distributes one credential lifecycle across API routes, provider wrappers, toolkit mixins, MCP session management, reset approval code, and generic OAuth helpers.
Each component is locally reasonable, but no component owns the complete provider-operation-to-persistence transaction.
That gap has produced stale refresh-token preservation, lost rotated tokens, reconnect races, inconsistent failure messages, and destructive resets that can be cancelled after deletion but before returning a recovery link.

Two different per-scope locks and optimistic snapshot comparisons attempt to reconcile concurrent mutations after provider I/O.
Those mechanisms create a retry state machine whose branches grow whenever another writer or cancellation point is found.
OAuth operations are rare enough that this concurrency is not worth the correctness cost.

## Invariants

1. One `OAuthCredentialContext` identifies exactly one provider credential scope.
2. The context contains the provider, runtime paths, credential manager, resolved worker target, and allowed shared services.
3. Every credential mutation uses one cross-process operation lock derived from that context.
4. Refresh, authorization-code exchange, reset, disconnect, and provider-driven lazy refresh serialize on that same lock.
5. A provider operation that can consume or rotate remote state completes its matching local commit before cancellation propagates.
6. A destructive reset performs all fallible asynchronous cleanup before deleting local credentials.
7. After deletion, reset performs only infallible in-memory retirement release before returning.
8. Request actor identity remains raw for room and membership checks.
9. Credential identity is canonicalized only when resolving `OAuthCredentialContext`.
10. Provider adapters classify structured terminal refresh errors as `OAuthRefreshRejectedError` without exposing provider-controlled text.
11. All consumers build connection and reconnection responses through one factory.
12. Approval continuations persist and revalidate the exact credential target descriptor before execution.
13. Every browser callback carries the credential generation observed at authorization and cannot publish after that generation is reset.

## Architecture

### Credential lifecycle owner

`src/mindroom/oauth/credential_lifecycle.py` owns scoped credential reads, validation, locking, refresh, callback publication, conditional invalidation, and reset.

`OAuthCredentialContext` is an immutable runtime value object.
Consumers construct it once and pass it to lifecycle operations instead of repeatedly passing a service name, manager, and worker target.

The module owns one lazy process-lifetime transaction event loop used by both asynchronous callers and synchronous provider adapters.
Every transaction acquires the same asynchronous cross-process operation lock on that owner loop.
Synchronous provider work runs in a worker thread while the transaction loop retains the operation lock, so a synchronous tool cannot deadlock behind work that needs its caller event loop.
Lock contexts remain private so callers cannot perform mutations outside lifecycle operations.
There is no separate refresh lock and singleflight lock.

### Connection flow owner

`src/mindroom/oauth/service.py` continues to own connect tokens, authorization links, success redirects, and user-facing connection instructions.
It exposes one `oauth_connection_required()` factory that chooses connect versus reconnect wording and structured reason metadata.
Google, GitHub, MCP, API, and configuration callers do not build those exceptions independently.

### Provider adapters

`src/mindroom/oauth/providers.py` remains the canonical provider contract for asynchronous code exchange and refresh.
It translates structured OAuth error codes into `OAuthRefreshRejectedError` for terminal grant rejection and `OAuthProviderError` for other failures.

`src/mindroom/oauth/client.py` adapts synchronous Google credential refresh to the same lifecycle transaction.
Google-specific `RefreshError` parsing remains in the adapter, but persistence, locking, invalidation, and reconnect classification do not.

### Reset authorization

`src/mindroom/oauth/reset.py` owns only authorization, provider availability, credential-scope resolution, and approval binding validation.
It returns the canonical credential context plus the invoking agent name.
It does not delete credentials or build links.

`src/mindroom/mcp/manager.py` owns requester-session generations and rejects refresh, reconnect, or calls through a retired generation.

`src/mindroom/custom_tools/oauth_connections.py` issues a reconnect link, enters MCP retirement, asks the lifecycle owner to delete the credential, and renders the receipt.
If teardown is cancelled or fails, deletion does not occur.

## Data Flow

### Refresh

1. Resolve one `OAuthCredentialContext`.
2. Submit the transaction to the process OAuth transaction loop.
3. Acquire its operation lock.
4. Load the current credential snapshot.
5. Return immediately when credentials are missing, unusable, or do not need refresh.
6. Call the provider refresh adapter while retaining the operation lock.
7. Save a returned rotation or delete the rejected current grant.
8. Release the lock.

Later same-scope refresh callers observe the committed snapshot and do not repeat an unnecessary rotation.
Different credential scopes continue concurrently because their lock paths differ.

### OAuth callback

1. Authenticate the browser user and validate the opaque pending state, target binding, and credential generation.
2. Start a cancellation-safe lifecycle operation.
3. Acquire the same operation lock used by refresh.
4. Reject the callback if reset advanced the durable credential generation after authorization.
5. Exchange the authorization code.
6. Validate and sanitize claims.
7. Preserve an existing refresh token only when the current locked snapshot has the same verified external identity and OAuth client.
8. Save the new credential snapshot and release the lock.
9. Propagate cancellation only after the local commit completes.

The callback cannot preserve a refresh token that another operation rotated concurrently because exchange and refresh cannot overlap for one scope.

### Reset and disconnect

1. Resolve and revalidate the exact approved credential context.
2. Create the requester-bound reconnect link before any destructive mutation.
3. Mark the matching MCP requester-session generation retired so captured callers cannot reconnect it.
4. Close the retired session and keep its key fenced against new sessions.
5. If retirement fails or is cancelled, retain credentials and restore the tracked generation when possible.
6. Submit credential deletion to the transaction loop and wait cancellably for the operation lock.
7. Advance the durable credential generation and delete under the operation lock without another suspension point.
8. Release the in-memory MCP retirement fence and return the receipt even if cancellation arrived after commit.

Dashboard disconnect uses the same transaction and fails without deleting credentials if MCP teardown cannot complete.

### Provider call failure

1. Provider adapters convert terminal structured codes into `OAuthRefreshRejectedError`.
2. The lifecycle transaction deletes only the credential held under its operation lock.
3. Consumers convert the canonical error through `oauth_connection_required(reason="refresh_rejected")`.
4. Logs contain only allowlisted error codes and bounded metadata.

No consumer infers terminal rejection from free-form error text.

## Cancellation Semantics

Waiting for an operation lock is cancellable unless pending OAuth state has already been consumed.
Refresh becomes cancellation-safe after its operation lock is acquired because a remote provider may rotate a token during the call.
Callback operation-lock wait, exchange, and save are cancellation-safe after pending state is consumed because the one-time state and authorization code cannot be replayed safely.
Reset teardown remains cancellable because credentials are still intact at that point.
Operation-lock waiting remains cancellable and does not mutate credentials.
Generation advancement plus deletion is the reset commit point, and the only following work releases the in-memory retirement fence without provider or transport I/O.
Cancellation after that commit is consumed so the approved destructive tool can return its reconnect receipt.

## Testing

Focused tests cover observable lifecycle behavior rather than private lock choreography.

- Concurrent same-scope refresh calls perform one necessary rotation and both observe the committed result.
- Callback waits behind refresh and preserves the latest rotated refresh token when the callback omits one.
- Callback cancellation after state consumption still commits exchanged credentials before cancellation propagates.
- Reset waits behind refresh, closes MCP state, deletes once, and cannot resurrect credentials.
- Cancellation or failure during MCP teardown leaves credentials intact.
- Different credential scopes refresh concurrently.
- A synchronous refresh invoked from an event-loop thread cannot deadlock behind an asynchronous same-scope transaction.
- Retired MCP generations cannot reconnect with captured stale authorization headers or create a replacement session before deletion commits.
- Callback state issued before a reset cannot republish credentials after the durable generation advances.
- Reset cancellation before operation-lock ownership preserves credentials, while cancellation after deletion still returns the committed receipt.
- Google lazy refresh, GitHub refresh, MCP refresh, API status refresh, and dashboard callback all use the same operation-lock path.
- Bridge aliases canonicalize only for OAuth credential targets, reset links authorize for the alias, and callbacks store in the canonical scope.
- Terminal refresh rejection returns the same structured reconnect reason and instruction from every consumer.
- Logs never include refresh tokens, access tokens, or unrecognized provider-controlled error text.
- Approval continuation fails closed when provider, service, scope, key, or routing agent changes.

Existing tests that require reconnect writes to bypass an in-flight refresh or assert stale-retry branches are removed because those behaviors violate the serialized transaction model.

## Migration

`oauth.service` initially re-exports lifecycle names only where doing so avoids unrelated call-site churn.
Production callers migrate to `OAuthCredentialContext` in this change.
The obsolete singleflight lock path, stale-retry result field, retry reconciliation helpers, and duplicated prompt builders are removed.
Generated documentation is refreshed after source documentation changes.

## Non-goals

This change does not revoke grants at external providers.
This change does not redesign OAuth discovery or dynamic client registration beyond keeping it independent from credential transactions.
This change does not change room membership identity or globally canonicalize Matrix requester IDs.
This change does not alter tool approval policy beyond binding resets to the exact credential context.
