# Private Agent External Triggers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make tool-managed external triggers work for `private.per` agents, with the trigger owner as the requester/private-scope owner.

**Architecture:** Keep the trigger API as a Matrix relay. The trigger record continues to own authentication, replay protection, and delivery target. The private-agent scope is derived at ingress from trusted `ORIGINAL_SENDER_KEY`, so the response runner builds the normal Matrix `ToolExecutionIdentity` for the trigger owner. Do not store reusable serialized execution identities in the trigger store unless a regression test proves the existing Matrix ingress contract cannot express the scope.

**Tech Stack:** Python 3.13, FastAPI trigger API, Matrix event metadata, Pydantic trigger store models, pytest, existing `ToolExecutionIdentity` and private-agent runtime resolution.

---

## Invariants

- Trigger owner is the private-scope owner.
- External trigger API never receives or stores private workspace paths, worker keys, OAuth tokens, or serialized tool execution identities.
- External trigger delivery remains fail-closed when API runtime, router bot, target bot, room membership, signature auth, replay state, or target config is invalid.
- A private target must be configured for the target room, same as shared agents.
- Non-admin trigger creation can only target the current agent and current room.
- Admin trigger creation can target a different configured agent/team and room, but owner remains the admin user who created the trigger.
- Existing shared-agent trigger behavior must not change.
- Runtime deliverability should remain unchanged: `ExternalTriggerRuntimeCoordinator.is_ready(...)` gates delivery from the snapshot and live router/target bots, and it does not inspect private config.

---

## Files

- Modify: `tests/test_external_trigger_manager_tool.py`
  - Add coverage that a private agent can create a trigger for itself through the existing local-only tool context.
- Modify: `tests/test_external_trigger_executor.py`
  - Add coverage that trigger delivery to a private target stamps owner metadata exactly like shared targets.
- Modify: `tests/test_multi_agent_bot.py`
  - Add a policy regression that a mentioned private target in its configured room is eligible to respond.
  - Add an integration-level regression test that a trusted external-trigger Matrix event addressed to a private agent propagates the trigger owner into response generation.
- Modify: `tests/test_ai_user_id.py`
  - Add a response-runner regression that `ResponseRequest.user_id` becomes the private tool execution requester.
- Modify only if tests fail: `src/mindroom/ingress_validation.py`, `src/mindroom/response_runner.py`, or `src/mindroom/turn_policy.py`
  - Fix the owner-to-requester propagation at the owning boundary instead of adding trigger-specific private fallbacks.
- Modify: `docs/external-triggers.md`
  - Document private-agent support and owner-as-private-scope semantics.
- Regenerate generated docs if the repo docs workflow requires it.

---

### Task 1: Manager Tool Allows Private Agents To Create Owner-Scoped Triggers

**Files:**
- Modify: `tests/test_external_trigger_manager_tool.py`

- [ ] **Step 1: Add a private-agent config helper**

Extend the existing `_config(...)` helper so tests can mark `watcher` private without duplicating the whole config.

```python
from mindroom.config.agent import AgentPrivateConfig


def _config(
    *,
    admin_users: list[str] | None = None,
    private_watcher: bool = False,
    private_other: bool = False,
) -> Config:
    config = Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-5.5"}},
            "agents": {
                "watcher": {
                    "display_name": "Watcher",
                    "role": "Watch external systems.",
                    "model": "default",
                    "rooms": ["lobby"],
                },
                "other": {
                    "display_name": "Other",
                    "role": "Other agent.",
                    "model": "default",
                    "rooms": ["other-room"],
                },
            },
            "rooms": {"lobby": {"display_name": "Lobby"}, "other-room": {"display_name": "Other"}},
            "external_trigger_policy": {"admin_users": admin_users or []},
            "authorization": {
                "global_users": ["@owner:example.org", "@other-owner:example.org", "@admin:example.org"],
                "agent_reply_permissions": {
                    "*": ["@owner:example.org", "@other-owner:example.org", "@admin:example.org"],
                },
            },
        },
    )
    if private_watcher:
        config.agents["watcher"].private = AgentPrivateConfig(per="user", root="watcher_data")
    if private_other:
        config.agents["other"].private = AgentPrivateConfig(per="user", root="other_data")
    return config
```

- [ ] **Step 2: Add the private manager regression test**

```python
def test_private_agent_create_trigger_uses_owner_as_scope_owner(tmp_path: Path) -> None:
    """Private agents should create triggers owned by the human requester, not the bot."""
    config = _config(private_watcher=True)
    tool = ExternalTriggerManagerTools()

    with tool_runtime_context(_context(tmp_path, requester_id="@owner:example.org", config=config)):
        payload = _payload(
            tool.create_trigger(
                "private-campground",
                public_key=_PUBLIC_KEY,
                key_id="campground-main",
                allowed_kinds=["campground.availability"],
            ),
        )

    assert payload["status"] == "ok"
    assert payload["trigger"]["owner_user_id"] == "@owner:example.org"
    assert payload["trigger"]["target"] == {
        "agent": "watcher",
        "new_thread": False,
        "room_id": "lobby",
        "thread_id": None,
    }
```

- [ ] **Step 3: Add admin cross-target private regression**

```python
def test_admin_create_trigger_for_private_cross_target_keeps_admin_owner(tmp_path: Path) -> None:
    """Admin cross-target triggers should stay owned by the admin requester."""
    config = _config(admin_users=["@admin:example.org"], private_other=True)
    tool = ExternalTriggerManagerTools()

    with tool_runtime_context(_context(tmp_path, requester_id="@admin:example.org", config=config)):
        payload = _payload(
            tool.create_trigger(
                "admin-private-target",
                public_key=_PUBLIC_KEY,
                target_agent="other",
                target_room_id="other-room",
            ),
        )

    assert payload["status"] == "ok"
    assert payload["trigger"]["owner_user_id"] == "@admin:example.org"
    assert payload["trigger"]["target"] == {
        "agent": "other",
        "new_thread": False,
        "room_id": "other-room",
        "thread_id": None,
    }
```

- [ ] **Step 4: Run the focused tests**

Run:

```bash
uv run pytest tests/test_external_trigger_manager_tool.py::test_private_agent_create_trigger_uses_owner_as_scope_owner tests/test_external_trigger_manager_tool.py::test_admin_create_trigger_for_private_cross_target_keeps_admin_owner -n 0 --no-cov -v
```

Expected:

- If it passes, keep production manager code unchanged.
- If it fails, fix only the manager/store validation path that rejects private targets while preserving non-admin same-agent/same-room limits.

- [ ] **Step 5: Commit if code changes are needed**

If only tests were added and pass, defer commit until the private-trigger support tests land together.

---

### Task 2: Trigger Delivery Keeps Owner Metadata For Private Targets

**Files:**
- Modify: `tests/test_external_trigger_executor.py`

- [ ] **Step 1: Add a private target config helper**

Add a helper next to `_config(...)`.

```python
from mindroom.config.agent import AgentPrivateConfig


def _private_config(tmp_path: Path) -> Config:
    config = _config(tmp_path)
    config.agents["research"].private = AgentPrivateConfig(per="user", root="research_data")
    return config
```

- [ ] **Step 2: Add delivery metadata regression test**

```python
@pytest.mark.asyncio
async def test_execute_external_trigger_private_target_preserves_owner_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private target delivery should still relay the trigger owner as original sender."""
    config = _private_config(tmp_path)
    send_and_track_message = AsyncMock(
        return_value=DeliveredMatrixEvent(event_id="$matrix-event", content_sent={}),
    )
    monkeypatch.setattr("mindroom.external_triggers.executor.send_and_track_message", send_and_track_message)

    event_id = await execute_external_trigger(
        client=AsyncMock(),
        snapshot=_snapshot(),
        payload=_payload(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        conversation_cache=_conversation_cache(),
    )

    assert event_id == "$matrix-event"
    content: dict[str, Any] = send_and_track_message.await_args.args[2]
    assert content[SOURCE_KIND_KEY] == EXTERNAL_TRIGGER_SOURCE_KIND
    assert content[ORIGINAL_SENDER_KEY] == "@owner:localhost"
    assert content["m.mentions"]["user_ids"] == [current_entity_id("research", runtime_paths_for(config)).full_id]
```

- [ ] **Step 3: Run the focused test**

Run:

```bash
uv run pytest tests/test_external_trigger_executor.py::test_execute_external_trigger_private_target_preserves_owner_metadata -n 0 --no-cov -v
```

Expected:

- PASS without production code changes.

---

### Task 3: Private Agent Turn Uses Trigger Owner As Execution Identity

**Files:**
- Modify: `tests/test_multi_agent_bot.py`

- [ ] **Step 1: Add real-policy regression for a mentioned private target**

Use the existing `resolve_response_action` test style near `test_resolve_response_action_keeps_configured_room_boundary_for_explicit_mention`.
This test must not patch `decide_team_formation(...)` or `decide_agent_response(...)`; its purpose is to catch private targets being ignored or routed away by policy.

```python
@pytest.mark.asyncio
async def test_resolve_response_action_allows_explicit_private_agent_mention(
    self,
    mock_agent_user: AgentMatrixUser,
    tmp_path: Path,
) -> None:
    """A private agent explicitly mentioned in its configured room should own the response."""
    config = _runtime_bound_config(
        Config(
            agents={
                "calculator": AgentConfig(
                    display_name="CalculatorAgent",
                    rooms=["!room:localhost"],
                    private=AgentPrivateConfig(per="user", root="calculator_data"),
                ),
            },
            authorization={"default_room_access": True},
        ),
        tmp_path,
    )
    runtime_paths = runtime_paths_for(config)
    bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
    room = _matrix_room(
        own_user_id=bot.matrix_id.full_id,
        user_ids=[entity_ids(config, runtime_paths)["calculator"].full_id],
    )
    context = MessageContext(
        am_i_mentioned=True,
        is_thread=False,
        thread_id=None,
        thread_history=[],
        mentioned_agents=[entity_ids(config, runtime_paths)["calculator"]],
        has_non_agent_mentions=False,
    )

    action = await bot._turn_policy.resolve_response_action(
        _policy_dispatch(
            bot,
            room,
            context,
            "@owner:localhost",
            "@CalculatorAgent campground opened",
            source_kind=EXTERNAL_TRIGGER_SOURCE_KIND,
        ),
        room,
        False,
        has_active_response_for_target=bot._response_runner.has_active_response_for_target,
    )

    assert action.kind == "individual"
```

- [ ] **Step 2: Add ingress regression for owner requester propagation**

Use the existing `TestAgentBot` helpers in `tests/test_multi_agent_bot.py`.
Construct a private `calculator` agent, an external-trigger Matrix text event sent by the router bot, and assert `_on_message(...)` calls `_generate_response(...)` with the trigger owner as `user_id`.
This pins the Matrix ingress contract without mocking a nonexistent higher-level handler.
Add `EXTERNAL_TRIGGER_SOURCE_KIND` to the existing `mindroom.dispatch_source` import list in this file.
Keep the policy functions patched in this test because the policy path is covered by `test_resolve_response_action_allows_explicit_private_agent_mention`; this test should fail only when trusted original-sender metadata stops propagating.
Use the existing `entity_ids(config, runtime_paths)` helper rather than adding another entity-resolution import.

```python
@pytest.mark.asyncio
async def test_external_trigger_to_private_agent_uses_trigger_owner_as_requester(
    self,
    mock_agent_user: AgentMatrixUser,
    tmp_path: Path,
) -> None:
    """Trusted external-trigger relays should enter response generation as the trigger owner."""
    config = _runtime_bound_config(
        Config(
            agents={
                "calculator": AgentConfig(
                    display_name="CalculatorAgent",
                    role="Private calculator.",
                    rooms=["!room:localhost"],
                    private=AgentPrivateConfig(per="user", root="calculator_data"),
                ),
            },
            models={"default": ModelConfig(provider="test", id="test-model")},
            authorization=AuthorizationConfig(
                global_users=["@owner:localhost"],
                agent_reply_permissions={"*": ["@owner:localhost"]},
            ),
        ),
        tmp_path,
    )
    runtime_paths = runtime_paths_for(config)
    ids = entity_ids(config, runtime_paths)
    bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
    _install_runtime_cache_support(bot)
    bot.client = _make_matrix_client_mock()
    tracker = _set_turn_store_tracker(bot, MagicMock())
    tracker.has_responded.return_value = False
    bot._generate_response = AsyncMock(return_value="$response")
    install_generate_response_mock(bot, bot._generate_response)

    room = _matrix_room(
        room_id="!room:localhost",
        own_user_id=mock_agent_user.user_id,
        user_ids=[
            ids[ROUTER_AGENT_NAME].full_id,
            ids["calculator"].full_id,
            "@owner:localhost",
        ],
    )
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$external-trigger",
            "sender": ids[ROUTER_AGENT_NAME].full_id,
            "origin_server_ts": 1234567890,
            "room_id": room.room_id,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.text",
                "body": "@CalculatorAgent Campground opened",
                "m.mentions": {"user_ids": [ids["calculator"].full_id]},
                SOURCE_KIND_KEY: EXTERNAL_TRIGGER_SOURCE_KIND,
                ORIGINAL_SENDER_KEY: "@owner:localhost",
            },
        },
    )

    with (
        patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
        patch("mindroom.text_ingress_dispatch.is_dm_room", new_callable=AsyncMock, return_value=False),
        patch("mindroom.turn_policy.decide_team_formation", return_value=TeamResolution.none()),
        patch("mindroom.turn_policy.decide_agent_response", return_value=AgentResponseDecision(True)),
        patch(
            "mindroom.turn_controller.interactive.handle_text_response",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        await bot._on_message(room, event)
        await drain_coalescing(bot)

    bot._generate_response.assert_awaited_once()
    assert bot._generate_response.await_args.kwargs["user_id"] == "@owner:localhost"
    assert bot._generate_response.await_args.kwargs["response_envelope"].requester_id == "@owner:localhost"
```

- [ ] **Step 3: Add response-runner regression for private execution identity**

**Files:**
- Modify: `tests/test_ai_user_id.py`

Add this near existing `generate_response_locked(...)` tests.
Use this file's real `_build_response_runner(...)`, `_make_bot(...)`, `_runtime_paths(...)`, `_response_request(...)`, and `_set_gateway_method(...)` helpers.
Spy on `ToolRuntimeSupport.build_execution_identity(...)`, because that is the exact boundary where the response runner converts `ResponseRequest.user_id` into private tool scope.

```python
@pytest.mark.asyncio
async def test_private_agent_response_runner_builds_execution_identity_from_requester(
    tmp_path: Path,
) -> None:
    """Private response generation should scope tools to ResponseRequest.user_id."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General", private=AgentPrivateConfig(per="user", root="general_data")),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        runtime_paths,
    )
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths, agent_name="general")
    target = MessageTarget.resolve("!test:localhost", None, "$external-trigger", room_mode=True)
    coordinator = _build_response_runner(
        bot,
        config=config,
        runtime_paths=runtime_paths,
        storage_path=tmp_path,
        requester_id="@owner:localhost",
        message_target=target,
    )
    real_build_execution_identity = coordinator.deps.tool_runtime.build_execution_identity
    build_calls: list[dict[str, object]] = []

    def build_execution_identity_spy(**kwargs: object) -> object:
        build_calls.append(kwargs)
        return real_build_execution_identity(**kwargs)

    with (
        patch.object(
            coordinator.deps.tool_runtime,
            "build_execution_identity",
            side_effect=build_execution_identity_spy,
        ),
        patch("mindroom.response_runner.ai_response", new=AsyncMock(return_value="done")) as mock_ai_response,
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=False)),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock(return_value=None)),
    ):
        response_event_id = await coordinator.generate_response_locked(
            _response_request(prompt="Campground opened", user_id="@owner:localhost"),
            resolved_target=target,
        )

    assert response_event_id == "$thinking"
    assert build_calls[0]["user_id"] == "@owner:localhost"
    execution_identity = mock_ai_response.await_args.kwargs["execution_identity"]
    assert execution_identity.agent_name == "general"
    assert execution_identity.requester_id == "@owner:localhost"
    assert execution_identity.room_id == "!test:localhost"
```

- [ ] **Step 4: Run the focused tests and inspect failures**

Run:

```bash
uv run pytest tests/test_multi_agent_bot.py::TestAgentBot::test_resolve_response_action_allows_explicit_private_agent_mention tests/test_multi_agent_bot.py::TestAgentBot::test_external_trigger_to_private_agent_uses_trigger_owner_as_requester tests/test_ai_user_id.py::test_private_agent_response_runner_builds_execution_identity_from_requester -n 0 --no-cov -v
```

Expected initial result:

- If it passes, no product code is needed for private scope propagation.
- If it fails because `requester_id` is the router or bot user, fix `ingress_validation.py` original-sender trust handling for `EXTERNAL_TRIGGER_SOURCE_KIND`.
- If it fails because private target is ignored or routed away, fix `turn_policy.py` at the explicit mention/private-agent decision boundary.
- If it fails because private agent runtime lacks execution identity, fix `response_runner.py` so external-trigger relays use the same `request.user_id` path as normal trusted Matrix relays.

- [ ] **Step 5: Keep fix at owning boundary**

Do not add trigger-specific private-agent branches inside `create_agent(...)`.
The correct contract is:

```text
trusted external trigger Matrix event
  -> requester_user_id == trigger owner
  -> response runner builds ToolExecutionIdentity from requester_user_id
  -> private agent runtime resolves requester-local state normally
```

---

### Task 4: Trigger API End-To-End Snapshot For Private Target

**Files:**
- Modify: `tests/api/test_external_triggers_api.py`

- [ ] **Step 1: Add API fixture support for private target config**

Extend `_config_payload(...)` and `_write_runtime_config(...)` with `private_research: bool = False`.
When enabled, add a plain YAML-safe private block before writing and validating config.

```python
def _config_payload(
    *,
    max_body_bytes: int = 262144,
    owner_authorized: bool = True,
    private_research: bool = False,
) -> dict[str, object]:
    authorization: dict[str, object] = {"agent_reply_permissions": {"*": [_OWNER]}}
    if owner_authorized:
        authorization["global_users"] = [_OWNER]
    payload: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "gpt-5.5"}},
        "router": {"model": "default"},
        "agents": {"research": {"display_name": "Research", "role": "test", "rooms": ["campground"]}},
        "rooms": {"campground": {"display_name": "Campground"}},
        "external_trigger_policy": {
            "default_max_body_bytes": min(max_body_bytes, 65536),
            "max_body_bytes": max_body_bytes,
        },
        "authorization": authorization,
    }
    if private_research:
        agents = cast("dict[str, dict[str, object]]", payload["agents"])
        agents["research"]["private"] = {"per": "user", "root": "research_data"}
    return payload
```

Then add a small private fixture mirroring `trigger_api`.
Keep the existing shared fixture unchanged so shared-agent tests still cover the old path.

```python
@pytest.fixture
def private_trigger_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TriggerApiContext:
    """Return one initialized API app with a private target trigger record."""
    private_key = Ed25519PrivateKey.generate()
    config_path = tmp_path / "config.yaml"
    config = _write_runtime_config(config_path, private_research=True)
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )
    api_main.initialize_api_app(api_main.app, runtime_paths)
    assert config_lifecycle.load_config_into_app(runtime_paths, api_main.app) is True
    _create_record(runtime_paths, config, _public_key_b64(private_key))
    api_main.unbind_external_trigger_runtime(api_main.app)
    ready_snapshots: list[TriggerDeliverySnapshot] = []
    _bind_runtime(ready_snapshots)
    monkeypatch.setattr("mindroom.api.external_triggers.is_external_trigger_owner_joined_target_room", _owner_joined)

    with TestClient(api_main.app) as client:
        yield TriggerApiContext(
            client=client,
            private_key=private_key,
            runtime_paths=runtime_paths,
            ready_snapshots=ready_snapshots,
        )

    api_main.unbind_external_trigger_runtime(api_main.app)
```

- [ ] **Step 2: Add API regression with private target config**

Copy the existing successful trigger API test and use `private_trigger_api`.
Assert the accepted trigger reaches `execute_external_trigger(...)` with a snapshot whose owner and target are unchanged.

```python
def test_post_external_trigger_accepts_private_agent_target(
    private_trigger_api: TriggerApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Signed trigger requests should deliver to private targets through normal runtime gating."""
    execute_snapshots: list[TriggerDeliverySnapshot] = []

    async def execute_external_trigger(**kwargs: object) -> str:
        snapshot = kwargs["snapshot"]
        assert isinstance(snapshot, TriggerDeliverySnapshot)
        execute_snapshots.append(snapshot)
        return "$delivered"

    monkeypatch.setattr("mindroom.api.external_triggers.execute_external_trigger", execute_external_trigger)

    response = _post_signed(private_trigger_api)

    assert response.status_code == 202
    assert response.json()["matrix_event_id"] == "$delivered"
    assert private_trigger_api.ready_snapshots
    assert execute_snapshots[0] is private_trigger_api.ready_snapshots[0]
    assert execute_snapshots[0].owner_user_id == _OWNER
    assert execute_snapshots[0].target.agent == "research"
```

- [ ] **Step 3: Run focused API test**

Run:

```bash
uv run pytest tests/api/test_external_triggers_api.py::test_post_external_trigger_accepts_private_agent_target -n 0 --no-cov -v
```

Expected:

- PASS after earlier tasks.

---

### Task 5: Documentation

**Files:**
- Modify: `docs/external-triggers.md`
- Regenerate generated docs if pre-commit changes them.

- [ ] **Step 1: Add private-agent semantics**

Add one short section to `docs/external-triggers.md`.
Keep one sentence per line.

```markdown
### Private Agents

External triggers can target agents configured with `private.per`.
The trigger owner is the private-scope requester for the triggered turn.
A trigger created by `@alice:example.org` wakes Alice's private state for that agent.
The API does not store private workspace paths, worker keys, OAuth tokens, or serialized execution identities.
The target agent must still be configured for the target room, and the router plus target transport bot must be joined before delivery.
```

- [ ] **Step 2: Run docs/pre-commit path**

Run:

```bash
uv run pre-commit run --files docs/external-triggers.md
```

If generated reference files change, stage them explicitly with the source doc.

---

### Task 6: Final Verification

**Files:**
- All touched files.

- [ ] **Step 1: Run focused private-trigger suite**

Run:

```bash
uv run pytest tests/test_external_trigger_manager_tool.py tests/test_external_trigger_executor.py tests/api/test_external_triggers_api.py tests/test_ai_user_id.py::test_private_agent_response_runner_builds_execution_identity_from_requester -n auto --no-cov
```

Expected: all pass.
The file-level runs cover the new manager self-target, manager admin-private cross-target, executor metadata, and API private-target tests.

- [ ] **Step 2: Run private-agent regression suite**

Run:

```bash
uv run pytest tests/test_multi_agent_bot.py::TestAgentBot::test_resolve_response_action_allows_explicit_private_agent_mention tests/test_multi_agent_bot.py::TestAgentBot::test_external_trigger_to_private_agent_uses_trigger_owner_as_requester tests/test_team_media_fallback.py -n auto --no-cov
```

Expected: all pass.
The exact named tests cover the new policy and ingress regressions.

- [ ] **Step 3: Run repository checks**

Run:

```bash
uv run pre-commit run --all-files
uv run pytest -n auto --no-cov
```

Expected:

- pre-commit passes.
- full pytest passes.

- [ ] **Step 4: Commit and push**

Stage only touched files.

```bash
git add tests/test_external_trigger_manager_tool.py tests/test_external_trigger_executor.py tests/test_multi_agent_bot.py tests/test_ai_user_id.py tests/api/test_external_triggers_api.py docs/external-triggers.md
git commit -m "Support external triggers for private agents"
git push
```

Do not use `git add .`.

---

## Review Checklist

- Does the plan avoid storing long-lived private execution identity in trigger records?
- Does trigger owner clearly become the private-scope requester?
- Do tests prove shared behavior remains unchanged?
- Does the plan avoid unnecessary readiness changes, relying on existing runtime coordinator tests for router/target bot fail-closed behavior?
- Are private-agent fixes made at ingress/runtime boundaries rather than inside `create_agent(...)`?
- Is documentation explicit enough for users creating watcher scripts?
