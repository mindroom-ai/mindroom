# OAuth Credential Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use baspowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace distributed OAuth mutation logic with one serialized credential lifecycle owner.

**Architecture:** An immutable `OAuthCredentialContext` identifies one provider and credential scope.
One lifecycle module submits asynchronous callers and synchronous provider adapters to a process-lifetime transaction loop, serializes every provider operation and local mutation on one cross-process lock, and leaves `oauth.service` ownership of links and connection prompts.

**Tech Stack:** Python 3.13, asyncio, dataclasses, advisory file locks, FastAPI, Agno toolkits, Google Auth, Authlib, pytest test specifications, Ruff, ty, Vulture, pre-commit.

**Spec:** `docs/dev/oauth-credential-lifecycle-design.md`

## Global Constraints

- Keep request actor identity raw and canonicalize only credential identity.
- Keep all OAuth credential mutations in `src/mindroom/oauth/credential_lifecycle.py`.
- Use one operation lock per provider credential scope.
- Hold the operation lock across provider I/O and its matching local commit.
- Run synchronous provider adapters off the caller event loop through the same transaction owner.
- Complete remote-token-producing operations locally before propagating cancellation.
- Perform fallible MCP teardown before destructive credential deletion.
- Fence retired MCP requester-session generations until credential deletion commits.
- Bind managed consumers to a durable lease revision advanced by every credential publication, and bind browser callbacks and resets to a separate connection generation advanced only by replacement, reset, and terminal invalidation.
- Save a scope-bound self-describing credential publication record before publishing its counters, recover an interrupted state commit under the operation lock, and give pending reset deletion absolute recovery precedence.
- Pass authorization explicitly into OAuth-backed toolkit construction and revalidate the full canonical context plus durable revision before every managed call.
- Keep managed Google credentials and services together in worker-thread-local state.
- Copy supported supplied Google credentials into private blocking state and serialize their complete provider calls with one reentrant toolkit lock.
- Keep GitHub tokens and PyGithub clients together in worker-thread-local state.
- Revalidate every authenticated MCP connection and call against authoritative cross-process credential revision and token hash immediately before publication or remote use.
- Use structured provider error codes and never log provider-controlled descriptions or token values.
- Keep reset approval bound to the exact provider, service, scope, worker key, routing agent, and connection generation while retaining credential revision as audit metadata.
- Freeze every approved call's exact ID, name, canonical arguments, and invoking agent.
- Require reset to be the sole observed call in its paused run.
- Key approved reset commits by `approval_id:generation:tool_call_id`, retain those completed tombstones permanently, and prune non-replayable direct or provider-driven completion state.
- Finish pending reset deletion before every credential read, refresh, callback publication, or later reset.
- Recover a claimed reset receipt directly without resuming Agno.
- Require every provider token credential service to end with `_oauth` so storage policy cannot misclassify plugin tokens.
- Do not run tests or CI, per user instruction.
- Run static checks and repository pre-commit hooks on every changed file.
- Do not amend commits, force-push, merge the PR, or create a `codex/` branch.

---

### Task 1: Create the credential lifecycle owner

**Files:**

- Create: `src/mindroom/oauth/credential_lifecycle.py`
- Modify: `src/mindroom/oauth/service.py`
- Modify: `tests/test_oauth_service.py`

**Interfaces:**

- Produces: `OAuthCredentialContext(provider, runtime_paths, credentials_manager, worker_target, allowed_shared_services=None)`.
- Produces: `OAuthCredentialsRefreshResult(credentials, refreshed, generation, connection_generation)`.
- Produces: `load_oauth_credentials_snapshot_sync(context)` for lock-consistent eager toolkit construction.
- Produces: `oauth_credentials_worker_target(provider, worker_target, execution_identity=None, authorization=None)`.
- Produces: `resolve_oauth_credential_context(...)` as the OAuth-only alias-canonicalization boundary.
- Produces: `load_oauth_credentials(context)`.
- Produces: authoritative credential snapshots carrying both the consumer lease revision and callback/reset connection generation.
- Produces: `refresh_oauth_credentials(context)` and `refresh_oauth_credentials_with_result(context)`.
- Produces: `refresh_oauth_credentials_blocking(context)` for synchronous callers of the asynchronous provider contract.
- Produces: `refresh_oauth_credentials_sync(context, refresh)` for synchronous provider adapters.
- Produces: `exchange_and_store_oauth_credentials(context, code, code_verifier, expected_connection_generation=...)`.
- Produces: `reset_oauth_credentials(context, operation_id=None)`.
- Produces: credential validation and sanitization helpers currently housed in `oauth.service`.
- Consumes: `OAuthProvider`, `RuntimePaths`, `CredentialsManager`, `ResolvedWorkerTarget`, `AuthorizationConfig`, `async_exclusive_file_lock`, and scoped credential storage functions.

- [ ] **Step 1: Replace optimistic-race test specifications with serialized lifecycle specifications**

Remove tests that expect reconnect writes to bypass active provider refresh or assert stale-retry branches.
Keep terminal rejection, bounded logging, scope isolation, and cancellation assertions.
Add specifications equivalent to:

```python
@pytest.mark.asyncio
async def test_same_scope_refresh_serializes_provider_rotation(tmp_path: Path) -> None:
    context = _credential_context(tmp_path, _credentials(ACCESS_0, CHAIN_0, expires_at=1.0))
    first_started = threading.Event()
    release_first = threading.Event()
    seen: list[str] = []

    async def refresh(credentials: Mapping[str, Any]) -> dict[str, Any] | None:
        seen.append(str(credentials["refresh_token"]))
        if credentials["refresh_token"] == CHAIN_0:
            first_started.set()
            await asyncio.to_thread(release_first.wait)
            return _credentials("access-1", CHAIN_1, expires_at=FUTURE_EXPIRES_AT)
        return None

    first = asyncio.create_task(refresh_oauth_credentials_with_result(replace(context, provider=_provider(refresh))))
    await asyncio.to_thread(first_started.wait)
    second = asyncio.create_task(refresh_oauth_credentials_with_result(replace(context, provider=_provider(refresh))))
    release_first.set()

    first_result, second_result = await asyncio.gather(first, second)
    assert first_result.refreshed is True
    assert second_result.credentials == first_result.credentials
    assert seen == [CHAIN_0, CHAIN_1]
```

The production mutation that must fail this test is releasing the per-scope operation lock before provider refresh and publication finish.

- [ ] **Step 2: Define the immutable context and private operation lock**

Create the runtime value object and derive exactly one lock from its credential path:

```python
@dataclass(frozen=True, slots=True)
class OAuthCredentialContext:
    provider: OAuthProvider
    runtime_paths: RuntimePaths
    credentials_manager: CredentialsManager
    worker_target: ResolvedWorkerTarget | None
    allowed_shared_services: frozenset[str] | None = None


def _operation_lock_path(context: OAuthCredentialContext) -> Path:
    credentials_path = scoped_credentials_path(
        context.provider.credential_service,
        credentials_manager=context.credentials_manager,
        worker_target=context.worker_target,
    )
    return credentials_path.with_name(f"{credentials_path.name}.oauth-operation.lock")
```

Do not export a mutation-capable lock context.

- [ ] **Step 3: Implement asynchronous serialized refresh**

Submit the complete transaction to the process-lifetime OAuth transaction loop.
Acquire the operation lock there before loading credentials and retain it through provider refresh, terminal invalidation, or publication.
Keep transaction submission and operation-lock admission cancellable.
After lock acquisition, use `run_coroutine_until_complete()` around provider refresh and publication so an accepted remote mutation reaches local commit before caller cancellation propagates.
On `OAuthRefreshRejectedError`, attach safe pre-invalidation metadata, delete the locked snapshot, emit one bounded warning, and re-raise.
On a returned rotation, save it before releasing the lock.
Remove stale retry, snapshot reconciliation, and the second singleflight lock.

- [ ] **Step 4: Implement synchronous serialized refresh**

Add:

```python
def refresh_oauth_credentials_sync(
    context: OAuthCredentialContext,
    refresh: Callable[[Mapping[str, Any]], dict[str, Any] | None],
) -> OAuthCredentialsRefreshResult:
    ...
```

Submit the synchronous adapter to the same transaction loop and run its blocking provider callback in a worker thread while retaining the asynchronous operation lock.
Apply the same missing, unusable, no-refresh-needed, success, terminal rejection, and bounded logging semantics as the asynchronous operation.

- [ ] **Step 5: Move callback publication and reset into the lifecycle module**

`exchange_and_store_oauth_credentials()` must acquire the operation lock, verify the pending callback connection generation, and retain the lock through exchange, claim validation, refresh-token preservation, and publication.
The API route will wrap the complete lock-wait-through-save coroutine in `run_coroutine_until_complete()` after consuming state.

`reset_oauth_credentials()` must wait cancellably for the operation lock, reject an uncompleted operation whose approved connection generation changed, durably record its pending intent with advanced lease and connection generations, delete the exact scoped file, and durably complete or prune the operation without owning MCP transport cleanup.
A completed replayable operation stores the original file-existed result and remains as a permanent tombstone so a stale retry cannot delete credentials from a later callback.
Every locked credential transaction must finish a pending reset delete before it reads or publishes credentials.
Cancellation before lock ownership must abort reset, while cancellation after commit must return the deletion result so the caller can publish its receipt.
MCP reset callers must enter requester-session retirement, mint the reconnect link after teardown, invoke the credential transaction, and release the in-memory fence afterward.
If completed-state publication fails after deletion, durable pending state remains authoritative even when exceptional unwinding releases the in-memory fence.

- [ ] **Step 6: Reduce `oauth.service` to connection-flow ownership**

Move credential lifecycle code and validation helpers out of `oauth.service`.
Temporarily re-export lifecycle public names from `oauth.service` only for unaffected callers and tests.
Delete `scoped_oauth_credentials_singleflight_lock_path`, stale-retry helpers, and the `stale_retry_used` result field.

### Task 2: Centralize connection-required translation

**Files:**

- Modify: `src/mindroom/oauth/service.py`
- Modify: `src/mindroom/oauth/client.py`
- Modify: `src/mindroom/custom_tools/github.py`
- Modify: `src/mindroom/mcp/manager.py`
- Modify: `tests/test_google_oauth_providers.py`
- Modify: `tests/test_github_oauth_tool.py`
- Modify: `tests/test_mcp_manager.py`

**Interfaces:**

- Consumes: `OAuthCredentialContext` from Task 1.
- Produces: `oauth_connection_required(context, reason=None) -> OAuthConnectionRequired`.

- [ ] **Step 1: Add consumer-visible prompt specifications**

Keep one literal expected reconnect instruction and structured payload contract.
Assert Google, GitHub, and MCP surface `reason == "refresh_rejected"` after canonical terminal rejection.
Do not assert on internal helper calls or mocks.

- [ ] **Step 2: Add one connection-required factory**

Implement:

```python
def oauth_connection_required(
    context: OAuthCredentialContext,
    *,
    reason: str | None = None,
) -> OAuthConnectionRequired:
    connect_url = oauth_connect_url(
        context.provider,
        context.runtime_paths,
        worker_target=context.worker_target,
    )
    instruction = (
        build_oauth_reconnect_instruction(context.provider, connect_url)
        if reason == "refresh_rejected"
        else build_oauth_connect_instruction(context.provider, connect_url)
    )
    return OAuthConnectionRequired(
        instruction,
        provider_id=context.provider.id,
        connect_url=connect_url,
        reason=reason,
    )
```

- [ ] **Step 3: Migrate GitHub and MCP consumers**

Construct one context per request scope.
Call lifecycle refresh functions with that context.
Replace local prompt builders with `oauth_connection_required()`.
Preserve safe logs and MCP refreshed-result observability.

- [ ] **Step 4: Migrate Google synchronous and lazy refresh**

Remove direct advisory-lock acquisition, direct credential deletion, the second lock, and lock-held state tracking from `ScopedOAuthClientMixin`.
Use `refresh_oauth_credentials_sync()` for eager and provider-driven lazy refresh.
Pass `AuthorizationConfig` through managed tool construction instead of depending on an ambient runtime context for alias resolution.
Read eager credentials and their durable revision under the operation lock, then compare canonical context plus revision before every managed call that could reuse cached credentials.
Advance revision before terminal credential invalidation so all materialized clients discard the rejected grant.
Serialize provider-driven refresh per materialized Google client and publish its snapshot, token, expiry, refresh token, and outcome before releasing concurrent callers.
Keep Google-specific structured `RefreshError` classification in the adapter.
Translate terminal Google rejection to `OAuthRefreshRejectedError`, let lifecycle deletion occur under the operation lock, and return the shared reconnect payload.
Continue raising a fixed sanitized `RefreshError` to upstream Google code when transport integration requires that type.

### Task 3: Bind API callback and reset to the lifecycle owner

**Files:**

- Modify: `src/mindroom/api/oauth.py`
- Modify: `src/mindroom/oauth/reset.py`
- Modify: `src/mindroom/custom_tools/oauth_connections.py`
- Modify: `src/mindroom/approval_response.py`
- Modify: `src/mindroom/response_runner.py`
- Modify: `tests/api/test_oauth_api.py`
- Modify: `tests/test_oauth_connection_tools.py`

**Interfaces:**

- Consumes: lifecycle context, callback publication, reset transaction, and shared prompt factory from Tasks 1 and 2.
- Produces: `ResolvedOAuthResetTarget(provider, agent_name, credential_context)` with exact approval-binding serialization.

- [ ] **Step 1: Add callback-versus-refresh rotation specification**

Create an integration specification where refresh rotates `CHAIN_0` to `CHAIN_1`, callback exchange omits a refresh token, and both target the same context.
Start refresh first, start callback while refresh owns the operation, release refresh, and assert callback stores its access token with `CHAIN_1`.
The production mutation that must fail this test is allowing callback exchange or refresh-token preservation outside the shared operation lock.

- [ ] **Step 2: Keep callback cancellation durable**

Retain the route-level specification proving cancellation after pending-state consumption does not complete until exchanged credentials are saved.
Patch only the private operation lock in the lifecycle module when deterministic blocking is required.

- [ ] **Step 3: Delegate API callback and status to lifecycle operations**

Build `OAuthCredentialContext` once from `RequestCredentialsTarget`.
Delete route-owned exchange, merge, lock, load, and save helpers.
Wrap callback lifecycle execution in `run_coroutine_until_complete()` after pending state is consumed.
Use lifecycle load and refresh for status.

- [ ] **Step 4: Make disconnect cleanup precede deletion**

Enter MCP requester-session retirement before calling `reset_oauth_credentials()`.
Propagate teardown failure without deleting credentials.
Return the dashboard disconnect receipt only after successful cleanup and deletion.

- [ ] **Step 5: Return canonical context from reset authorization**

Resolve provider-specific requester credential identity once.
Construct the context with the primary runtime credential manager.
Persist provider ID, credential service, worker scope, worker key, routing agent, invoking agent, credential revision, and connection generation in approval bindings.
Re-resolve and compare the target identity plus connection generation before approved execution, while allowing refresh-only credential revision drift.
Pass authorization into agent reconstruction and install persisted tool runtime and execution-identity contexts before reconstruction and resumed execution.

- [ ] **Step 6: Make agent reset cancellation-safe by ordering**

Return completed stable operations before retirement, fence the requester-session key, and skip retirement only when authoritative storage proves the connection generation changed after approval.
Otherwise retire every cached same-key session regardless of lease-revision mismatch, then issue the requester-bound reconnect link immediately before the lifecycle reset transaction.
If an approved lifecycle reset raises after durable intent publication, propagate the interruption so the claimed continuation retains receipt-delivery ownership.
For failures before durable intent publication, log one bounded lifecycle-wide failure event and tell the caller to verify status before retrying.
If teardown is cancelled, propagate cancellation while credentials remain intact.
After deletion, release only the in-memory retirement fence before building the receipt.

### Task 4: Consolidate materialized client ownership

**Files:**

- Modify: `src/mindroom/oauth/client.py`
- Modify: `src/mindroom/custom_tools/google_service.py`
- Modify: `src/mindroom/custom_tools/google_drive.py`
- Modify: `src/mindroom/custom_tools/github.py`
- Modify: `src/mindroom/mcp/manager.py`
- Modify: `src/mindroom/mcp/types.py`
- Modify: `tests/test_google_tool_wrappers.py`
- Modify: `tests/test_github_oauth_tool.py`
- Modify: `tests/test_mcp_manager.py`

- [ ] **Step 1: Make Google credentials and services thread-local**

Store credentials, googleapiclient service, and the full canonical context plus durable revision in one worker-thread-local state.
Clear both credentials and service whenever that key changes.
Let calls begun before revision mutation finish, but require every later managed entrypoint to load the new authoritative snapshot.

- [ ] **Step 2: Isolate supplied Google credentials**

Accept only the exact pinned `google.oauth2.credentials.Credentials` type.
Reject refresh handlers, reauth mode, subclasses, and arbitrary objects.
Copy supported scalar fields and independent immutable scope tuples into a private credential with nonblocking refresh disabled.
Hold one per-tool `threading.RLock` from private credential installation through the complete provider call so nested Drive calls reenter safely while different workers serialize.

- [ ] **Step 3: Preserve Google Drive lifecycle refresh under quota configuration**

Apply quota project configuration while creating managed or private credentials.
Pass the exact managed credential into googleapiclient service construction instead of calling `with_quota_project()` and losing the instance refresh hook.

- [ ] **Step 4: Make GitHub client ownership thread-local**

Store the access token and PyGithub client together per worker thread.
Reload managed credentials on every execution thread and rebuild only that thread's client when its authoritative token changes.

- [ ] **Step 5: Bind authenticated MCP sessions to token-hash leases**

Track the desired token hash and the token hash that created the connected session separately.
Validate the expected hash before connection, before candidate publication, and under the call read lock immediately before remote use.
Close stale candidates and restart authoritative credential acquisition when a lease changes.
Treat cached catalogs as schema only and never as authorization for a call.

### Task 5: Remove obsolete behavior and align documentation

**Files:**

- Modify: `tests/test_oauth_service.py`
- Modify: `tests/test_google_tool_wrappers.py`
- Modify: `tests/api/test_oauth_api.py`
- Modify: `docs/dev/oauth-credential-lifecycle-design.md`
- Modify: `docs/deployment/sandbox-proxy.md`
- Regenerate: `skills/mindroom-docs/references/llms-full.txt`
- Regenerate: `skills/mindroom-docs/references/page__deployment__sandbox-proxy__index.md`

**Interfaces:**

- Consumes: final lifecycle semantics from Tasks 1 through 4.
- Produces: focused tests and documentation matching serialized ownership.

- [ ] **Step 1: Delete obsolete tests and assertions**

Remove stale-retry counters, retry-stage reconnect races, the second-lock path, and tests that directly write credentials while refresh is active and expect the write to win.
Keep scope isolation, exact error classification, cancellation durability, log redaction, and lock-sharing coverage.

- [ ] **Step 2: Apply mutation check to retained tests**

For each lifecycle invariant, identify a realistic mutation that one retained test catches.
Delete tests that only assert private helper calls, source text, or deliberate constant values.

- [ ] **Step 3: Regenerate documentation references**

Run:

```bash
uv run .github/scripts/generate_skill_references.py
```

Verify generated references contain the same local-only tool lists as `docs/deployment/sandbox-proxy.md`.

### Task 6: Static verification, base reconciliation, and delivery

**Files:**

- Verify every file changed by Tasks 1 through 5.
- Reconcile current `origin/main` into `bas/oauth-reset-recovery` when the worktree is clean.

**Interfaces:**

- Consumes: complete refactor.
- Produces: clean pushed PR head ready for fresh independent review.

- [ ] **Step 1: Run static verification without tests**

Run Ruff check, Ruff format check, ty, `git diff --check`, documentation generation, and repository pre-commit hooks on exact changed files.
Do not run pytest or any test runner.

- [ ] **Step 2: Commit implementation without amend**

Run `git status` before staging.
Stage exact intended paths only.
Inspect `git diff --cached --name-status` and `git diff --cached --check`.
Commit with a normal new commit.

- [ ] **Step 3: Reconcile live main**

Fetch `origin/main`, verify the live PR base SHA, and merge current `origin/main` into the feature branch without rewriting history.
Resolve only actual conflicts and preserve unrelated upstream changes.
Run the same static verification after conflict resolution.

- [ ] **Step 4: Push and pin exact head**

Push `bas/oauth-reset-recovery` normally.
Verify local HEAD, remote branch SHA, and GitHub PR head SHA match and the worktree is clean.

- [ ] **Step 5: Resume native review loop**

Launch two new read-only reviewers with fresh context against the exact pushed SHA and current base.
Fix every validated blocker in the main thread and repeat with a new pair after each push.
Stop only after two reviewers approve the same head.
