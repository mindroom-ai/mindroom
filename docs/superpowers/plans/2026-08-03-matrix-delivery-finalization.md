# Matrix Delivery Finalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> Native review agents remain read-only because the main thread owns every repository mutation for this PR.

**Goal:** Make every Matrix application send share one room delivery boundary that validates encryption readiness, uses an authoritative joined-only crypto roster, and rotates stale Megolm sessions.

**Architecture:** `mindroom.matrix.client_delivery` owns the complete preflight, content preparation, final hydration, and send sequence.
Encrypted hydration compares exact joined membership before and after `joined_members`, retires every existing outbound session immediately when that membership is unknown or changed, and fences sending until device-key queries complete.
Every raw event and message send acquires the same per-client, per-room lock, while the runtime client rejects stale readiness and invitees at nio's final preparation boundary.
Request-owned membership generations reject older joined-members responses, membership and device-list generations reject superseded key queries, and prepared uploads retain their proven encryption mode.
Recipient and membership generations also bind the final transport attempt to the exact hydrated room state.
Restart recovery remains a consumer of this boundary and does not acquire crypto-specific branches.

**Tech Stack:** Python 3.13, matrix-nio, asyncio, pytest, pytest-asyncio, Ruff, ty, Vulture, Tach, GitHub CLI.

## Global Constraints

Use the existing PR worktree at `/work/worktrees/mindroom-pr1759` on `fix/restart-recovery-coordinator`.
Keep imports at module scope unless an established optional-dependency rule requires otherwise.
Do not add compatibility fallbacks or dynamic attribute probing.
Write one sentence per Markdown source line.
Write each behavior regression before production code and observe the expected failure.
Keep recovery retry and deterministic transaction-ID behavior unchanged.
Keep all repository mutations in the main thread.

---

### Task 1: Rotate Encrypted Sessions When Authoritative Membership Changes

**Files:**
- Modify: `tests/test_matrix_delivery.py`
- Modify: `src/mindroom/matrix/client_delivery.py:250-375`

**Interfaces:**
- Consumes: `hydrate_joined_room_for_delivery(client: nio.AsyncClient, room_id: str) -> RoomDeliveryHydrationProof | None`.
- Produces: `_joined_room_user_ids(room: nio.MatrixRoom) -> frozenset[str]` and membership-aware encrypted hydration.
- Preserves: `RoomDeliveryHydrationProof(encrypted=True, joined_user_ids=<authoritative exact set>)`.

- [ ] **Step 1: Add a real-nio test fixture for an encrypted cached room with one shared outbound session**

Add `SimpleNamespace` to the module imports and add this helper beside `_mock_client`:

```python
def _encrypted_client_with_shared_session(
    *,
    room_id: str,
    user_ids: frozenset[str],
) -> tuple[nio.AsyncClient, nio.MatrixRoom]:
    bot_user_id = "@bot:localhost"
    client = nio.AsyncClient("https://localhost", bot_user_id)
    room = nio.MatrixRoom(room_id, bot_user_id, encrypted=True)
    room.members_synced = True
    for user_id in user_ids:
        room.add_member(user_id, user_id, None)
    client.rooms[room_id] = room
    client.encrypted_rooms.add(room_id)
    client.store = MagicMock()
    client.olm = MagicMock()
    client.olm.outbound_group_sessions = {
        room_id: SimpleNamespace(shared=True),
    }
    client.olm.users_for_key_query = set()
    client.olm.should_query_keys = False
    return client, room
```

- [ ] **Step 2: Add the failing membership-rotation regression**

Add a parameterized test whose two cases remove `@departed:localhost` and add `@joined:localhost`:

```python
@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            frozenset({"@bot:localhost", "@departed:localhost"}),
            frozenset({"@bot:localhost"}),
        ),
        (
            frozenset({"@bot:localhost"}),
            frozenset({"@bot:localhost", "@joined:localhost"}),
        ),
    ],
)
@pytest.mark.asyncio
async def test_encrypted_hydration_retires_outbound_session_when_membership_changes(
    before: frozenset[str],
    after: frozenset[str],
) -> None:
    room_id = "!room:localhost"
    client, room = _encrypted_client_with_shared_session(room_id=room_id, user_ids=before)
    response = nio.JoinedMembersResponse(
        [nio.RoomMember(user_id, user_id, "") for user_id in sorted(after)],
        room_id,
    )

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        client._handle_joined_members(response)
        return response

    client.joined_members = AsyncMock(side_effect=joined_members)

    proof = await hydrate_joined_room_for_delivery(client, room_id)

    assert proof == RoomDeliveryHydrationProof(encrypted=True, joined_user_ids=after)
    assert frozenset(room.users) == after
    assert not room.invited_users
    assert client.olm is not None
    assert room_id not in client.olm.outbound_group_sessions
```

- [ ] **Step 3: Run the new test and verify RED**

Run:

```bash
nix-shell shell.nix --run 'uv run pytest -n 0 --no-cov tests/test_matrix_delivery.py::test_encrypted_hydration_retires_outbound_session_when_membership_changes -vv'
```

Expected: both cases fail because the shared session remains in `outbound_group_sessions`.

- [ ] **Step 4: Implement exact joined-member validation and rotation**

Add the exact joined-user helper and use it in `_room_covers_joined_members` and `delivery_hydration_is_current`:

```python
def _joined_room_user_ids(room: nio.MatrixRoom) -> frozenset[str]:
    """Return the exact nio roster used for encrypted session sharing."""
    return frozenset(room.users)
```

Replace cached membership with the authoritative joined response and require exact equality with `_joined_room_user_ids(room)` plus an empty invite map.
At the start of `_hydrate_encrypted_joined_room`, snapshot an invite-free recipient roster from a cached encrypted, member-synchronized room or use `None` when no trustworthy prior snapshot exists.
Immediately after `joined_members` returns, retire any existing outbound session when the prior set is unknown or differs from the authoritative response so no later state or key await exposes the old session:

```python
joined_user_ids = frozenset(member.user_id for member in members.members)
membership_changed = previous_joined_user_ids != joined_user_ids
if (
    client.olm is not None
    and membership_changed
    and room_id in client.olm.outbound_group_sessions
):
    client.olm.outbound_group_sessions.pop(room_id)
```

Discard partially distributed sessions as well as sessions marked fully shared because a departed device may already possess either key.
When device keys require a query, mark the room membership-unsynchronized before awaiting the query and restore it only after no joined member remains pending.
Keep tracked-user updates, key queries, cache publication, and proof construction in the same hydration owner.

- [ ] **Step 5: Run the new test and existing hydration tests and verify GREEN**

Run:

```bash
nix-shell shell.nix --run 'uv run pytest -n 0 --no-cov tests/test_matrix_delivery.py tests/test_restart_recovery.py -k "hydration or encrypted_room" -vv'
```

Expected: all selected tests pass.

---

### Task 2: Move Proof Validation Ahead Of Uploads And Refresh Encryption At Send

**Files:**
- Modify: `tests/test_matrix_delivery.py`
- Modify: `src/mindroom/matrix/client_delivery.py:482-545`

**Interfaces:**
- Consumes: `RoomDeliveryHydrationProof`, `prepare_large_message`, and `hydrate_joined_room_for_delivery`.
- Produces: `_delivery_hydration_is_current_before_preparation(...) -> Awaitable[bool]` and `_refresh_delivery_hydration_at_send(...) -> Awaitable[bool]`.
- Preserves: `send_message_result(...) -> DeliveredMatrixEvent | None` and its stable transaction-ID forwarding.

- [ ] **Step 1: Add the failing no-upload regression for stale plaintext proof**

Add this test with a complete Matrix upload response double:

```python
@pytest.mark.asyncio
async def test_stale_plaintext_proof_rejects_before_large_message_upload() -> None:
    client = AsyncMock(spec=nio.AsyncClient)
    room_id = "!room:localhost"
    client.rooms = {}
    client.access_token = TEST_ACCESS_TOKEN
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventResponse(
            content={"algorithm": "m.megolm.v1.aes-sha2"},
            event_type="m.room.encryption",
            state_key="",
            room_id=room_id,
        ),
    )
    upload = AsyncMock(
        return_value=(nio.UploadResponse("mxc://localhost/orphan"), None),
    )

    with patch("mindroom.matrix.large_messages.upload_media_bytes", upload):
        delivered = await send_message_result(
            client,
            room_id,
            {"body": "private recovery payload " * 5000, "msgtype": "m.text"},
            delivery_proof=RoomDeliveryHydrationProof(encrypted=False),
        )

    assert delivered is None
    upload.assert_not_awaited()
    client._send.assert_not_awaited()
```

- [ ] **Step 2: Run the no-upload test and verify RED**

Run:

```bash
nix-shell shell.nix --run 'uv run pytest -n 0 --no-cov tests/test_matrix_delivery.py::test_stale_plaintext_proof_rejects_before_large_message_upload -vv'
```

Expected: the upload assertion fails with one awaited plaintext upload.

- [ ] **Step 3: Add the failing final encrypted-refresh regression**

Add this test so preparation, membership refresh, rotation, and send have one observable order:

```python
@pytest.mark.asyncio
async def test_encrypted_proof_refreshes_membership_after_preparation() -> None:
    room_id = "!room:localhost"
    before = frozenset({"@bot:localhost", "@departed:localhost"})
    after = frozenset({"@bot:localhost"})
    client, _room = _encrypted_client_with_shared_session(room_id=room_id, user_ids=before)
    proof = RoomDeliveryHydrationProof(encrypted=True, joined_user_ids=before)
    response = nio.JoinedMembersResponse(
        [nio.RoomMember("@bot:localhost", "Bot", "")],
        room_id,
    )
    observed: list[str] = []

    async def prepare(
        _client: nio.AsyncClient,
        _room_id: str,
        content: dict[str, object],
    ) -> dict[str, object]:
        observed.append("prepare")
        return content

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        assert observed == ["prepare"]
        observed.append("members")
        client._handle_joined_members(response)
        return response

    async def send_prepared(*_args: object, **_kwargs: object) -> nio.RoomSendResponse:
        assert client.olm is not None
        assert room_id not in client.olm.outbound_group_sessions
        observed.append("send")
        return nio.RoomSendResponse(event_id="$event:localhost", room_id=room_id)

    client.joined_members = AsyncMock(side_effect=joined_members)
    with (
        patch(
            "mindroom.matrix.client_delivery.prepare_large_message",
            new=AsyncMock(side_effect=prepare),
        ),
        patch(
            "mindroom.matrix.client_delivery._send_prepared_room_message",
            new=AsyncMock(side_effect=send_prepared),
        ),
    ):
        delivered = await send_message_result(
            client,
            room_id,
            {"body": "resume", "msgtype": "m.text"},
            delivery_proof=proof,
        )

    assert delivered is not None
    assert delivered.event_id == "$event:localhost"
    assert observed == ["prepare", "members", "send"]
```

- [ ] **Step 4: Run the final-refresh test and verify RED**

Run:

```bash
nix-shell shell.nix --run 'uv run pytest -n 0 --no-cov tests/test_matrix_delivery.py::test_encrypted_proof_refreshes_membership_after_preparation -vv'
```

Expected: the patched sender observes the still-present outbound session because current code performs only a local proof check.

- [ ] **Step 5: Implement the two-stage proof boundary**

Replace `_delivery_hydration_is_current_at_send` with these responsibilities:

```python
async def _delivery_hydration_is_current_before_preparation(
    client: nio.AsyncClient,
    room_id: str,
    proof: RoomDeliveryHydrationProof,
) -> bool:
    if proof.encrypted:
        return delivery_hydration_is_current(client, room_id, proof)
    if await _remote_room_encrypted(client, room_id) is not False:
        return False
    return delivery_hydration_is_current(client, room_id, proof)


async def _refresh_delivery_hydration_at_send(
    client: nio.AsyncClient,
    room_id: str,
    proof: RoomDeliveryHydrationProof,
) -> bool:
    if not proof.encrypted:
        return await _delivery_hydration_is_current_before_preparation(client, room_id, proof)
    refreshed = await hydrate_joined_room_for_delivery(client, room_id)
    return refreshed is not None and refreshed.encrypted
```

In `send_message_result`, call the preflight helper before emitting `prepare_start` or awaiting `prepare_large_message`.
Use the existing stale-proof log and return `None` when preflight fails.
After preparation, call `_refresh_delivery_hydration_at_send` and use the same failure result.
Call `_send_prepared_room_message` immediately after final refresh and timing emission without another application-level read or preparation await.

- [ ] **Step 6: Run both new tests and all Matrix delivery tests and verify GREEN**

Run:

```bash
nix-shell shell.nix --run 'uv run pytest -n 0 --no-cov tests/test_matrix_delivery.py -vv'
```

Expected: all tests pass.

---

### Task 3: Centralize Room Retention Policy And Correct Runtime Documentation

**Files:**
- Modify: `src/mindroom/bot_room_lifecycle.py:285-301`
- Modify: `docs/architecture/bot-runtime.md:167-215`
- Test: `tests/test_room_invites.py`

**Interfaces:**
- Consumes: `BotRoomLifecycle.desired_room_ids`.
- Produces: one configured-plus-invited room-retention source of truth for both join and leave behavior.
- Documents: proof preflight, final encrypted membership refresh, and Megolm rotation.

- [ ] **Step 1: Run the existing invited-room preservation tests as characterization tests**

Run:

```bash
nix-shell shell.nix --run 'uv run pytest -n 0 --no-cov tests/test_room_invites.py -k "leave_unconfigured_rooms_preserves" -vv'
```

Expected: the existing behavior passes before the refactor.

- [ ] **Step 2: Replace the duplicated retention calculation**

In `_rooms_to_leave`, replace the configured-room and invited-room reconstruction with:

```python
configured_rooms = set(self.desired_room_ids)
```

Keep the router root-space addition and current-room difference unchanged.

- [ ] **Step 3: Update the runtime architecture documentation**

Add these one-sentence-per-line statements in the restart-recovery scope and delivery sections:

```markdown
Encrypted hidden-room hydration publishes full non-membership state plus exact authoritative joined membership into nio's shared cache, while plaintext hidden rooms stay uncached so normal sync remains their sole cache owner.
Hydration intentionally omits invite membership from the encryption roster and retires every existing outbound Megolm session before later awaits whenever the prior roster is unknown, invite-polluted, or changed.
Pending device-key work keeps the room send-fenced through nio's membership-synchronization state.
Proof-bound delivery validates encryption before large-message preparation can upload content.
After preparation, encrypted delivery refreshes joined membership and device-key readiness, rotates a stale outbound session, and retains its application delivery lock through nio's send path.
All application room events use the same per-client, per-room lock, and the runtime client rejects stale readiness at nio's final preparation boundary.
This client-side sequence narrows the out-of-window membership race but does not claim transactional ordering against concurrent homeserver membership writes.
```

- [ ] **Step 4: Run characterization and documentation checks**

Run:

```bash
nix-shell shell.nix --run 'uv run pytest -n 0 --no-cov tests/test_room_invites.py -k "leave_unconfigured_rooms_preserves" -vv'
uv run pre-commit run generate-skill-references --files docs/architecture/bot-runtime.md
git diff --check
```

Expected: tests and checks pass without generated drift outside the architecture references owned by the docs hook.

- [ ] **Step 5: Commit the implemented boundary and cleanup**

Stage only the implementation, tests, and runtime documentation:

```bash
git add src/mindroom/matrix/client_delivery.py src/mindroom/bot_room_lifecycle.py tests/test_matrix_delivery.py docs/architecture/bot-runtime.md
git commit -m "fix: finalize encrypted recovery delivery safely"
```

If the documentation generator modifies tracked files under `skills/mindroom-docs/references/`, inspect those changes and add only the generated files attributable to `docs/architecture/bot-runtime.md` before committing.

---

### Task 3B: Close The Application And Crypto Boundaries Found In Review

**Files:**
- Modify: `src/mindroom/matrix/client_delivery.py`
- Modify: `src/mindroom/matrix/client_session.py`
- Modify: every production module that calls `AsyncClient.room_send`
- Modify: the corresponding Matrix transport tests

**Interfaces:**
- Produces: `send_room_event_result(...) -> nio.RoomSendResponse | nio.RoomSendError | None`.
- Produces: one per-client, per-room delivery lock shared by hydration, messages, reactions, and custom events.
- Preserves: raw `RoomSendError` details and deterministic transaction IDs for config-confirmation reactions.

- [ ] **Step 1: Reproduce the failed-key-query race with real nio**

Start hydration with a blocked key query, queue a normal application send for the same room, fail both readiness attempts, and assert that nio encryption and the wire transport are never reached.
Add the successful counterpart and assert that exactly one queued send reaches the wire after key readiness succeeds.

- [ ] **Step 2: Reproduce invitee key-sharing exposure**

Hydrate a real encrypted `MatrixRoom` containing an invitee and assert that the resulting `room.users` exactly equals `/joined_members`, the invite map is empty, and both fully and partially distributed sessions are retired.
Cover invite-to-join promotion and an invite added during the key-query await.

- [ ] **Step 3: Add the centralized raw-event boundary**

Add a typed raw-result helper that acquires the room delivery lock, establishes encrypted readiness or authoritative plaintext cache bypass, and retains the lock through the private transport primitive.
Migrate reactions, approval events, low-level tool events, stop buttons, and call notices to this helper.
Add a static source guard that permits `.room_send(` only inside `client_delivery.py`.

- [ ] **Step 4: Add the runtime nio fail-closed boundary**

Make `_MindRoomAsyncClient` remove invitees from encrypted rosters after joined-member and sync processing.
Override nio send preparation to raise `SendRetryError` when encrypted membership is unsynchronized, a joined member still needs a key query, or invitees remain in the crypto roster.
This prevents nio from using its unchecked member-and-key refresh fallback after application validation.

- [ ] **Step 5: Verify caller compatibility**

Run the focused transport suites for messages, approvals, config confirmations, interactive reactions, stop buttons, Matrix API tools, and MatrixRTC notices.
Use proper cached-room and crypto-state test doubles instead of weakening production interfaces for old mocks.

- [ ] **Step 6: Fence stale hydration responses**

Preapply membership events, encryption events, joined-count mismatches, and device-list changes at the ordered Classic Sync and Sliding Sync handlers before recovery work can await.
Construct endpoint-specific typed errors for non-2xx Matrix responses before success-payload parsing, while preserving nio's deliberate HTTP 401 interactive-auth responses.
Reject non-2xx sync responses before rate-limit callbacks or ordered ingestion, and reject non-2xx full-state responses before hidden-room event parsing or publication.
Give every runtime `joined_members` request an ordering token and reject its response before cache mutation when a newer request or membership update won, or when its HTTP transport was unsuccessful.
Retain the accepted membership generation through hidden-room full-state validation, device-key readiness, and candidate publication.
Globally sequence every runtime `keys_query`, including pinned-device discovery, and reject an older or success-typed non-2xx response at the client response boundary before nio mutates its device store.
Require an actual HTTP 404 before any transported `M_NOT_FOUND` response can prove resource absence across delivery, scheduling, config, room, tool, and strict thread-history reads.
Track final key-claim and per-device share failures across nio's aggregate Megolm sharing call, retire the outbound session, clean only the current invocation's sharing event, and abort before encryption.
Capture device-list and membership generations across delivery-owned `keys_query`, and requeue the queried and current room users before rejecting a superseded response.
Do not use the recipient generation for key-query supersession because a successful key query legitimately retires its own obsolete outbound session.
Require hidden-room full state and joined-members results to agree on their joined-only roster before cache publication.

- [ ] **Step 7: Preserve stable sessions and payload confidentiality**

Compare raw joined-only rosters during authoritative response handling so the temporary send fence does not rotate an unchanged shared session.
Recheck cached room identity and encryption after each encryption-state network await.
Treat `encrypted_rooms` as monotonic proof that overrides any later negative state-event response.
Pass the proven encryption mode into large-message, file, audio, and approval upload preparation, and reject mode changes before the event references the upload.
Read local file bytes before obtaining the proof so encryption changes during file I/O precede upload-mode selection.
Hold the room delivery lock across local encryption state reads and writes, then route local and sync-discovered encryption through the same monotonic membership fence and outbound-session retirement.

- [ ] **Step 8: Guard transport retries**

Bind the exact room identity, mode, roster, pending-key state, and recipient generation to nio's room-send request.
Validate that guard after dynamic header preparation and before every HTTP attempt.
Release the application room lock during recovery backoff and rehydrate after reacquiring it.
Generate one transaction ID per logical message delivery and reuse it across every application retry.

---

### Task 4: Verify The Complete PR-Owned Change

**Files:**
- Verify: every Python and documentation file changed from `origin/main` through `HEAD`.

**Interfaces:**
- Consumes: the complete PR-owned diff.
- Produces: fresh same-SHA test and static-check evidence.

- [ ] **Step 1: Run focused recovery verification**

Run:

```bash
nix-shell shell.nix --run 'uv run pytest -n auto --no-cov tests/test_matrix_delivery.py tests/test_restart_recovery.py tests/test_stale_stream_cleanup.py tests/test_room_invites.py'
```

Expected: all selected tests pass.

- [ ] **Step 2: Run the broader advertised PR suite**

Run:

```bash
nix-shell shell.nix --run 'uv run pytest -n auto --no-cov tests/test_restart_recovery.py tests/test_matrix_delivery.py tests/test_stale_stream_cleanup.py tests/test_sync_task_cancellation.py tests/test_orchestrator_runtime.py tests/test_dynamic_config_update.py tests/test_startup_maintenance.py tests/test_matrix_client_session.py tests/test_nio_recovery_consumer_contract.py tests/test_sync_cache_trust.py tests/test_sync_certification.py tests/test_matrix_sync_continuity.py tests/test_dispatch_obligations.py tests/test_cold_history_fence.py tests/test_room_member_hooks.py tests/test_bot_sync_event_cache.py tests/test_matrix_cache_interaction_contract.py tests/test_matrix_event_cache_fuzz.py tests/test_import_graph.py tests/test_external_trigger_runtime_binding.py tests/test_hook_sender.py tests/test_ingress_validation.py tests/test_large_messages_integration.py tests/test_mcp_orchestrator.py tests/test_turn_policy.py tests/test_room_invites.py'
```

Expected: all selected tests pass, with only their established skips.

- [ ] **Step 3: Run static and architecture gates**

Run:

```bash
git diff --check origin/main..HEAD
git diff --name-only -z origin/main..HEAD -- '*.py' | xargs -0 uv run ruff check
git diff --name-only -z origin/main..HEAD -- '*.py' | xargs -0 uv run ruff format --check
git diff --name-only -z origin/main..HEAD -- 'src/**/*.py' | xargs -0 -r uv run ty check
uv run vulture
uv run tach check --dependencies --interfaces
uv run privata --methods .
```

Expected: every command exits zero.

- [ ] **Step 4: Run the full backend suite**

Run:

```bash
nix-shell shell.nix --run 'uv run pytest -n auto --no-cov'
```

Expected on this NixOS host: all product tests pass, while the six untouched `tests/test_ci_release_dispatch.py` cases may fail because their isolated PATH contains only `/usr/bin:/bin` and cannot resolve Nix's `bash`.
Record the exact totals and do not describe the suite as green if those environment failures remain.

- [ ] **Step 5: Confirm repository state**

Run:

```bash
git status --short --branch
git log -3 --oneline --decorate
git diff origin/main..HEAD --stat
```

Expected: the worktree is clean and only intended commits are ahead of the remote branch.

---

### Task 5: Push, Update PR Evidence, And Run Same-SHA Review Loops

**Files:**
- External: Git branch `fix/restart-recovery-coordinator`.
- External: GitHub PR `mindroom-ai/mindroom#1759`.

**Interfaces:**
- Consumes: verified local commits and exact verification output.
- Produces: updated remote head, accurate PR description, and two independent approvals on the same SHA.

- [ ] **Step 1: Push the rewritten branch with lease protection**

Run:

```bash
git push --force-with-lease origin fix/restart-recovery-coordinator
```

Expected: the remote branch is replaced only if it still points at the previously inspected head.

- [ ] **Step 2: Update the pull-request description**

Use `gh pr edit 1759` to target `main` and replace the stale current-head SHA, owned diff totals, verification totals, and review state with facts from the pushed head.
State that the restart-recovery coordinator now lands independently of the abandoned stacked implementation.

- [ ] **Step 3: Launch two fresh read-only native reviewers**

Give each reviewer the repo path, PR number, exact base ref, branch, head SHA, `origin/main..HEAD` diff, and the `pr-review` skill.
Do not mention prior findings or desired outcomes.
Tell each reviewer not to edit, commit, push, or inspect CI.

- [ ] **Step 4: Verify every reviewer claim in the main thread**

If either reviewer requests changes, classify each claim and fix only real in-scope issues using a new red-green cycle.
Commit and push verified fixes, close the old review round, and launch two new fresh reviewers on the new SHA.
After three rounds with many findings or a new major bug class, stop and reconsider the design before patching again.

- [ ] **Step 5: Finish only after same-SHA approval**

Require both fresh reviewers to return `APPROVE` on the same pushed SHA.
Confirm `git status --short --branch` is clean and `git rev-parse HEAD` equals `git rev-parse origin/fix/restart-recovery-coordinator`.
Report the pushed SHA, commits, verification, review outcomes, and any environment-limited checks.
