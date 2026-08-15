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
- Bind browser callbacks to a durable credential generation advanced by reset.
- Pass authorization explicitly into OAuth-backed toolkit construction and revalidate cached credential revisions before every managed call.
- Use structured provider error codes and never log provider-controlled descriptions or token values.
- Keep reset approval bound to the exact provider, service, scope, worker key, and routing agent.
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
- Produces: `OAuthCredentialsRefreshResult(credentials, refreshed, generation)`.
- Produces: `load_oauth_credentials_snapshot_sync(context)` for lock-consistent eager toolkit construction.
- Produces: `oauth_credentials_worker_target(provider, worker_target, execution_identity=None, authorization=None)`.
- Produces: `resolve_oauth_credential_context(...)` as the OAuth-only alias-canonicalization boundary.
- Produces: `load_oauth_credentials(context)`.
- Produces: `oauth_credential_generation(context)` for callback/reset fencing.
- Produces: `refresh_oauth_credentials(context)` and `refresh_oauth_credentials_with_result(context)`.
- Produces: `refresh_oauth_credentials_blocking(context)` for synchronous callers of the asynchronous provider contract.
- Produces: `refresh_oauth_credentials_sync(context, refresh)` for synchronous provider adapters.
- Produces: `exchange_and_store_oauth_credentials(context, code, code_verifier, expected_generation=...)`.
- Produces: `reset_oauth_credentials(context)`.
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

`exchange_and_store_oauth_credentials()` must acquire the operation lock, verify the pending callback generation, and retain the lock through exchange, claim validation, refresh-token preservation, and save.
The API route will wrap the complete lock-wait-through-save coroutine in `run_coroutine_until_complete()` after consuming state.

`reset_oauth_credentials()` must wait cancellably for the operation lock, advance the durable credential generation, and durably delete the credential snapshot without owning MCP transport cleanup.
Cancellation before lock ownership must abort reset, while cancellation after commit must return the deletion result so the caller can publish its receipt.
MCP reset callers must enter requester-session retirement before invoking the credential transaction and release only the in-memory fence afterward.

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
Read eager credentials and their generation under the operation lock, then compare canonical context plus generation before every managed call that could reuse cached credentials.
Advance generation before terminal credential invalidation so all materialized clients discard the rejected grant.
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
Persist provider ID, credential service, worker scope, worker key, routing agent, and invoking agent in approval bindings.
Re-resolve and compare every field before approved execution.
Pass authorization into agent reconstruction and install persisted tool runtime and execution-identity contexts before reconstruction and resumed execution.

- [ ] **Step 6: Make agent reset cancellation-safe by ordering**

Issue the requester-bound reconnect link first.
Enter MCP requester-session retirement around the lifecycle reset transaction.
If teardown or lifecycle reset raises an ordinary exception, log one bounded lifecycle-wide failure event and tell the caller to verify status before retrying.
If teardown is cancelled, propagate cancellation while credentials remain intact.
After deletion, release only the in-memory retirement fence before building the receipt.

### Task 4: Remove obsolete behavior and align documentation

**Files:**

- Modify: `tests/test_oauth_service.py`
- Modify: `tests/test_google_tool_wrappers.py`
- Modify: `tests/api/test_oauth_api.py`
- Modify: `docs/dev/oauth-credential-lifecycle-design.md`
- Modify: `docs/deployment/sandbox-proxy.md`
- Regenerate: `skills/mindroom-docs/references/llms-full.txt`
- Regenerate: `skills/mindroom-docs/references/page__deployment__sandbox-proxy__index.md`

**Interfaces:**

- Consumes: final lifecycle semantics from Tasks 1 through 3.
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

### Task 5: Static verification, base reconciliation, and delivery

**Files:**

- Verify every file changed by Tasks 1 through 4.
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
