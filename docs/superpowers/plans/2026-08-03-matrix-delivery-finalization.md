# Matrix Delivery Finalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> Native review agents remain read-only because the main thread owns every repository mutation for this PR.

**Goal:** Make proof-bound Matrix recovery delivery validate encryption before uploads, refresh exact encrypted membership immediately before send, and rotate stale Megolm sessions.

**Architecture:** `mindroom.matrix.client_delivery` owns the complete preflight, content preparation, final hydration, and send sequence.
Encrypted hydration compares exact joined membership before and after `joined_members`, invalidates an existing outbound session when that membership is unknown or changed, and completes device-key queries before returning a proof.
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
async def test_encrypted_hydration_rotates_shared_session_when_membership_changes(
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
    assert frozenset(user_id for user_id, user in room.users.items() if not user.invited) == after
    assert client.olm is not None
    assert room_id not in client.olm.outbound_group_sessions
```

- [ ] **Step 3: Run the new test and verify RED**

Run:

```bash
nix-shell shell.nix --run 'uv run pytest -n 0 --no-cov tests/test_matrix_delivery.py::test_encrypted_hydration_rotates_shared_session_when_membership_changes -vv'
```

Expected: both cases fail because the shared session remains in `outbound_group_sessions`.

- [ ] **Step 4: Implement exact joined-member validation and rotation**

Add the exact joined-user helper and use it in `_room_covers_joined_members` and `delivery_hydration_is_current`:

```python
def _joined_room_user_ids(room: nio.MatrixRoom) -> frozenset[str]:
    """Return the room users whose current membership is joined."""
    return frozenset(user_id for user_id, user in room.users.items() if not user.invited)
```

Change coverage from subset matching to exact equality with `_joined_room_user_ids(room)`.
At the start of `_hydrate_encrypted_joined_room`, snapshot exact joined membership from a cached encrypted, member-synchronized room or use `None` when no complete prior snapshot exists.
After `_current_encrypted_room_after_hydration` selects the authoritative room, invalidate only an existing outbound session when the prior set is unknown or differs from `hydration.joined_user_ids`:

```python
membership_changed = previous_joined_user_ids != hydration.joined_user_ids
if (
    client.olm is not None
    and membership_changed
    and room_id in client.olm.outbound_group_sessions
):
    client.invalidate_outbound_session(room_id)
```

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
Encrypted hidden-room hydration publishes a full-state `MatrixRoom` with exact authoritative joined membership into nio's shared cache, while plaintext hidden rooms stay uncached so normal sync remains their sole cache owner.
Hydration invalidates an existing outbound Megolm session whenever authoritative joined membership differs from the prior complete snapshot.
Proof-bound delivery validates encryption before large-message preparation can upload content.
After preparation, encrypted delivery refreshes joined membership and device-key readiness, rotates a stale outbound session, and then enters nio's send path without unrelated application awaits.
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

### Task 4: Verify The Complete PR-Owned Change

**Files:**
- Verify: every Python and documentation file changed from `origin/fix/recovery-restart-boundaries` through `HEAD`.

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
nix-shell shell.nix --run 'uv run pytest -n auto --no-cov tests/test_restart_recovery.py tests/test_matrix_delivery.py tests/test_stale_stream_cleanup.py tests/test_sync_task_cancellation.py tests/test_orchestrator_runtime.py tests/test_dynamic_config_update.py tests/test_startup_maintenance.py tests/test_matrix_client_session.py tests/test_nio_recovery_consumer_contract.py tests/test_sync_cache_trust.py tests/test_sync_certification.py tests/test_matrix_sync_continuity.py tests/test_dispatch_obligations.py tests/test_cold_history_fence.py tests/test_room_member_hooks.py tests/test_room_member_hook_lifecycle.py tests/test_bot_sync_event_cache.py tests/test_matrix_cache_interaction_contract.py tests/test_matrix_event_cache_fuzz.py tests/test_import_graph.py tests/test_external_trigger_runtime_binding.py tests/test_hook_sender.py tests/test_ingress_validation.py tests/test_large_messages_integration.py tests/test_mcp_orchestrator.py tests/test_turn_policy.py tests/test_room_invites.py'
```

Expected: all selected tests pass, with only their established skips.

- [ ] **Step 3: Run static and architecture gates**

Run:

```bash
git diff --check origin/fix/recovery-restart-boundaries..HEAD
git diff --name-only -z origin/fix/recovery-restart-boundaries..HEAD -- '*.py' | xargs -0 uv run ruff check
git diff --name-only -z origin/fix/recovery-restart-boundaries..HEAD -- '*.py' | xargs -0 uv run ruff format --check
git diff --name-only -z origin/fix/recovery-restart-boundaries..HEAD -- '*.py' | xargs -0 uv run ty check
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
git diff origin/fix/recovery-restart-boundaries..HEAD --stat
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

- [ ] **Step 1: Push the named branch without force**

Run:

```bash
git push origin fix/restart-recovery-coordinator
```

Expected: the remote branch advances by fast-forward to local `HEAD`.

- [ ] **Step 2: Update the pull-request description**

Use `gh pr edit 1759` to replace the stale current-head SHA, owned diff totals, verification totals, and review state with facts from the pushed head.
Keep the dependency and landing-order warning because PR #1783 remains the stacked base until its merge and restack complete.

- [ ] **Step 3: Launch two fresh read-only native reviewers**

Give each reviewer the repo path, PR number, exact base ref, branch, head SHA, `origin/fix/recovery-restart-boundaries..HEAD` diff, and the `pr-review` skill.
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
