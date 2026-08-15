# OAuth Credential Lifecycle Design

## Goal

MindRoom-managed OAuth credentials must remain recoverable when refresh grants are revoked, while reconnect, reset, callback, and refresh operations remain correct under concurrency and cancellation.

## Problem

Before this change, one credential lifecycle was distributed across API routes, provider wrappers, toolkit mixins, MCP session management, reset approval code, and generic OAuth helpers.
Each component was locally reasonable, but no component owned the complete provider-operation-to-persistence transaction.
That gap produced stale refresh-token preservation, lost rotated tokens, reconnect races, inconsistent failure messages, and destructive resets that could be cancelled after deletion but before returning a recovery link.

Two different per-scope locks and optimistic snapshot comparisons attempted to reconcile concurrent mutations after provider I/O.
Those mechanisms created a retry state machine whose branches grew whenever another writer or cancellation point was found.
OAuth operations are rare enough that this concurrency was not worth the correctness cost.

## Invariants

1. One `OAuthCredentialContext` identifies exactly one provider credential scope.
2. The context contains the provider, runtime paths, credential manager, resolved worker target, and allowed shared services.
3. Every credential mutation uses one cross-process operation lock derived from that context.
4. Refresh, authorization-code exchange, reset, disconnect, and provider-driven lazy refresh serialize on that same lock.
5. A provider operation that can consume or rotate remote state completes its matching local commit before cancellation propagates.
6. Credential save and delete commits fsync their payload and parent-directory updates before reporting success.
7. A destructive reset performs all fallible asynchronous cleanup before deleting local credentials.
8. After deletion, the durable pending intent remains the reset owner until completed-state publication succeeds, while the in-memory MCP fence may release during exceptional unwinding because every later credential transaction must finish that pending intent before use.
9. Request actor identity remains raw for room and membership checks.
10. Credential identity is canonicalized only when resolving `OAuthCredentialContext`.
11. Provider adapters classify structured terminal refresh errors as `OAuthRefreshRejectedError` without exposing provider-controlled text.
12. All consumers build connection and reconnection responses through one factory.
13. Approval continuations persist and revalidate the exact credential target descriptor before execution.
14. Every credential publication advances a durable lease revision used by materialized clients and MCP sessions, while callback replacement, reset, and terminal invalidation also advance a separate connection generation used to fence browser callbacks and approved resets.
15. Agent approval continuations install their persisted requester context before reconstructing OAuth-backed toolkits.
16. Every provider token service ends with `_oauth`, making primary-runtime placement and worker-grant rejection structural.
17. OAuth-backed toolkit construction receives authorization explicitly, and every managed call revalidates its canonical credential scope and durable revision before reusing cached credentials.
18. Managed Google credentials and services are owned together by one worker thread and keyed by the full canonical credential context plus durable revision.
19. Caller-supplied Google credentials are accepted only as the exact supported concrete type, copied into private blocking credentials, and serialized for the whole provider call by one reentrant toolkit lock.
20. Google Drive applies quota configuration before lifecycle refresh wrapping and never clones a managed tracked credential while building a service.
21. GitHub access tokens and PyGithub clients are owned together by one worker thread, while every managed call reloads authoritative credentials on its execution thread.
22. OAuth-backed MCP connections revalidate their durable generation and token hash immediately before session publication and each admitted remote use.
23. Every approval freezes the exact call ID, tool name, canonical arguments, invoking agent, and any sensitive nested target before execution.
24. An OAuth reset is the sole observed call in its paused run and uses `approval_id:generation:tool_call_id` as its stable lifecycle operation ID.
25. Reset generation metadata durably records every pending intent, retains permanent completed tombstones only for replayable approval operations, and prunes completed direct or provider-driven intents.
26. Every credential-use and publication transaction finishes pending reset deletion before reading, refreshing, or storing a credential.
27. Claimed reset recovery re-enters only a durable pending or completed operation by its stable ID, never starts a missing reset, and never resumes the stale paused Agno run.
28. Credential documents carry a scope-bound self-describing publication record, are durably saved before their counters are published, and repair an interrupted state-file commit under the operation lock.
29. Pending reset deletion always takes precedence over credential-publication recovery.
30. Reset receipt recovery rechecks current authorization before publishing reconnect material, while revoked recovery emits only a link-free completion receipt.

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

`src/mindroom/oauth/service.py` continues to own connect tokens, authorization links, success redirects, scope-upgrade instructions, and user-facing connection instructions.
It exposes one `oauth_connection_required()` factory that chooses connect, reconnect, or scope-upgrade wording and structured reason metadata.
Google, GitHub, MCP, API, and configuration callers do not build those exceptions independently.

### Provider adapters

`src/mindroom/oauth/providers.py` remains the canonical provider contract for asynchronous code exchange and refresh.
It translates structured OAuth error codes into `OAuthRefreshRejectedError` for terminal grant rejection and `OAuthProviderError` for other failures.

`src/mindroom/oauth/client.py` adapts synchronous Google credential refresh to the same lifecycle transaction.
Google-specific `RefreshError` parsing remains in the adapter, but persistence, locking, invalidation, and reconnect classification do not.
Managed Google credentials and googleapiclient services remain in one thread-local state because Agno executes provider calls in worker threads.
The state key contains the full canonical credential context and durable revision, so equal revision strings from different requesters cannot share credentials.
Supplied credentials cross a stricter boundary: MindRoom copies the exact concrete Google credential into private blocking state and holds one reentrant lock from credential installation through the complete nested provider call.
Google Drive constructs managed credentials with quota configuration already present and passes the exact tracked object to googleapiclient instead of cloning away its lifecycle refresh hook.

`src/mindroom/custom_tools/github.py` reloads managed credentials for every call and stores each worker thread's token and PyGithub client together.
An older call may finish on its own thread, but it cannot overwrite another worker's newer client.

`src/mindroom/mcp/manager.py` treats the durable credential revision plus OAuth token hash as an authorization lease.
It tracks both the desired lease and the lease that built the connected session, validates both immediately before catalog publication and tool use, and reacquires authoritative credentials when either changes.

### Reset authorization

`src/mindroom/oauth/reset.py` owns only authorization, provider availability, credential-scope resolution, and approval binding validation.
It returns the canonical credential context plus the invoking agent name.
It does not delete credentials or build links.

`src/mindroom/mcp/manager.py` owns requester sessions and performs short durable credential-revision checks immediately before connection publication and admitted remote calls.

`src/mindroom/oauth/reset_execution.py` enters MCP retirement, issues a reconnect link immediately before deletion, asks the lifecycle owner to commit the reset, and renders the receipt.
`src/mindroom/custom_tools/oauth_connections.py` owns only live-request authorization and error translation around that executor.
If teardown is cancelled or fails, deletion does not occur.

`src/mindroom/approval_bindings.py` freezes and validates every paused call descriptor.
OAuth reset bindings add the exact canonical credential target under that generic descriptor and reject a reset mixed with any other call.

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

1. Authenticate the browser user and validate the opaque pending state, target binding, and durable connection generation.
2. Start a cancellation-safe lifecycle operation.
3. Acquire the same operation lock used by refresh.
4. Reject the callback if reset, terminal invalidation, or another successful callback advanced the durable connection generation after authorization; a same-lineage refresh may advance only the lease revision while the callback waits.
5. Exchange the authorization code.
6. Validate and sanitize claims.
7. Preserve an existing refresh token only when the current locked snapshot has the same verified external identity and OAuth client.
8. Durably save the scope-bound credential publication record, then publish its new lease revision and connection generation while retaining the lock.
9. Release the lock and propagate cancellation only after the local commit completes.

The callback cannot preserve a refresh token that another operation rotated concurrently because exchange and refresh cannot overlap for one scope.

### Reset and disconnect

1. Resolve and revalidate the exact approved credential context.
2. Return a completed stable operation without entering MCP retirement or touching credentials.
3. Fence the requester-session key, then load its authoritative credential revision and connection generation.
4. Skip retirement when the authoritative connection generation differs from the approved connection generation.
5. Otherwise mark every cached requester session for that key retired, including sessions carrying an older lease revision, so captured callers cannot reconnect it.
6. Close the retired session and keep its key fenced against new sessions.
7. If retirement fails or is cancelled, retain credentials and restore the tracked generation when possible.
8. Mint the requester-bound reconnect link after teardown and immediately before reset submission.
9. Submit credential deletion to the transaction loop and wait cancellably for the operation lock.
10. Recheck the approved connection generation and write the stable reset operation as pending together with new durable lease and connection generations.
11. Durably delete the exact scoped credential file without first decoding it.
12. Mark a replayable approval operation completed with its original file-existed result, retain that tombstone permanently, and prune non-replayable completion state.
13. If completed-state publication fails after deletion, retain the claimed continuation and retry the durable pending operation by its stable ID.
14. Release the in-memory MCP retirement fence and return the receipt even if cancellation arrived after full durable commit.

All credential transactions finish any pending delete before using or publishing the scope.
A retry of a completed operation returns the stored result without advancing the revision or deleting a credential created by a later callback.
If a process dies while a stable operation is pending, claimed-continuation recovery re-enters that exact operation to finish deletion and completed-state publication.
If a process dies after the reset commit but before FINAL delivery, claimed-continuation recovery proves the completed operation read-only, skips MCP retirement and deletion, and publishes the receipt directly without calling Agno continuation APIs.

Dashboard disconnect uses the same transaction and fails without deleting credentials if MCP teardown cannot complete.

### Provider call failure

1. Provider adapters convert terminal structured codes into `OAuthRefreshRejectedError`.
2. The lifecycle transaction advances the durable credential revision and deletes only the credential held under its operation lock.
3. Other materialized clients observe the new revision before their next managed call and discard cached credentials and services.
4. Consumers convert the canonical error through `oauth_connection_required(reason="refresh_rejected")`.
5. Logs contain only allowlisted error codes and bounded metadata.

No consumer infers terminal rejection from free-form error text.

## Cancellation Semantics

Waiting for an operation lock is cancellable unless pending OAuth state has already been consumed.
Refresh becomes cancellation-safe after its operation lock is acquired because a remote provider may rotate a token during the call.
Callback operation-lock wait, exchange, and save are cancellation-safe after pending state is consumed because the one-time state and authorization code cannot be replayed safely.
Reset teardown remains cancellable because credentials are still intact at that point.
Operation-lock waiting remains cancellable and does not mutate credentials.
Pending-intent publication, durable deletion, and completed-tombstone publication form the reset commit sequence.
Cancellation after that commit is consumed so the approved destructive tool can return its reconnect receipt.

## Testing

Focused tests cover observable lifecycle behavior rather than private lock choreography.

- Concurrent same-scope refresh calls perform one necessary rotation and both observe the committed result.
- Rotated credentials and callback results repair an interrupted state-file commit from their durable scope-bound publication record.
- Callback waits behind refresh, accepts same-lineage lease-revision drift, and preserves the latest rotated refresh token when the callback omits one.
- Callback cancellation after state consumption still commits exchanged credentials before cancellation propagates.
- Reset waits behind refresh, closes MCP state, deletes once, and cannot resurrect credentials.
- Cancellation or failure during MCP teardown leaves credentials intact.
- Different credential scopes refresh concurrently.
- A synchronous refresh invoked from an event-loop thread cannot deadlock behind an asynchronous same-scope transaction.
- Retired MCP sessions cannot reconnect with captured stale authorization headers or create a replacement session before deletion commits, even when their cached lease revision predates the approved reset.
- Callback state issued before a reset cannot republish credentials after the connection generation advances.
- Callback success advances both counters, and a second callback bound to the consumed connection generation is rejected.
- Reset cancellation before operation-lock ownership preserves credentials, while cancellation after deletion still returns the committed receipt.
- Reset success is reported only after the credential unlink and parent-directory update are flushed.
- Refresh cancellation before operation-lock ownership never calls the provider, while cancellation after ownership waits for local publication.
- Google lazy refresh, GitHub refresh, MCP refresh, API status refresh, and dashboard callback all use the same operation-lock path.
- Concurrent lazy refresh calls on one Google client publish one in-memory snapshot atomically and reuse its outcome.
- Bridge aliases canonicalize only for OAuth credential targets, reset links authorize for the alias, and callbacks store in the canonical scope.
- Terminal refresh rejection returns the same structured reconnect reason and instruction from every consumer.
- Logs never include refresh tokens, access tokens, or unrecognized provider-controlled error text.
- Approval continuation fails closed when provider, service, scope, key, or routing agent changes.
- Approval continuation fails closed when the persisted call reuses an ID with a changed name, canonical arguments, or invoking member.
- OAuth reset is refused when another call is observed in its paused run.
- Reset deletes corrupt plaintext or encrypted credential files by exact scoped path and permits reconnect afterward.
- A crash after pending intent hides the credential until deletion finishes, while completed-operation replay cannot delete a later callback credential.
- Claimed reset recovery requires an existing pending or completed stable operation and never resumes Agno.
- Pending recovery re-enters only that operation to finish deletion and completed-state publication, while completed recovery is read-only and publishes FINAL directly.
- Missing-operation recovery never starts deletion.
- Agent approval continuation reconstructs OAuth-backed toolkits inside the same runtime and execution-identity context used for resumed calls.
- Agent and voice toolkit construction receive authorization explicitly, so alias canonicalization does not depend on an ambient call context.
- Long-lived Google clients discard valid cached access tokens after reset, disconnect, terminal invalidation, or canonical scope change.
- Persistent Google workers independently discard their own credentials and services after revision change, and requester changes invalidate the cache even when both scopes use the initial revision string.
- A successful callback replacing account A with account B invalidates already-materialized account A services before their next managed call.
- Supplied Google credentials are privately copied, reject caller-controlled refresh hooks, reauth modes, subclasses, and arbitrary objects, and serialize nested provider calls with a reentrant lock.
- Google Drive quota configuration preserves the exact lifecycle-tracked credential and concurrent lazy refresh still publishes one rotation.
- GitHub workers keep tokens and PyGithub clients thread-local across old-call and new-token interleavings.
- MCP sessions built with stale authorization headers close before publication or use, while calls admitted before a later credential mutation may finish normally.
- Plugin providers without the `_oauth` token-service suffix fail registration before any credential can be stored.

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
