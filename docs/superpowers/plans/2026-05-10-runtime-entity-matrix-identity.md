# Runtime Entity Matrix Identity Implementation Plan

> **Status:** Implemented in this PR.
> The checkboxes below are historical execution scaffolding, not current TODOs.

**Goal:** Make runtime identity use only `configured entity alias -> actual persisted Matrix ID`, with generated usernames limited to account provisioning and config-time collision checks.

**Architecture:** The orchestrator prepares all managed Matrix accounts before constructing runtime bots.
Provisioning owns proposed usernames for missing accounts.
Runtime modules consume an actual-identity registry and never synthesize `@mindroom_<alias>` as a live responder, sender, mention, or routing identity.

**Tech Stack:** Python 3.13, Matrix via `nio`, Pydantic config, pytest, Tach, pre-commit.

---

## Corrected Design From Review

The product model has only configured entity aliases and actual Matrix IDs.
Generated usernames are proposed account names for provisioning only.
They are not runtime identities.

The previous draft missed five design requirements.

- Fresh startup must provision accounts before any runtime identity registry lookup.
- Config validation must still catch collisions with proposed usernames before accounts exist.
- Alias parsing must not treat generated-looking localparts or bare actual usernames as aliases.
- Sender classification helpers such as `extract_agent_name()` and `active_internal_sender_ids()` must be migrated or deleted, not left as hidden generated-ID paths.
- The affected module list must include routing, teams, conversation history, response execution, config tools, edit regeneration, room joins, tool approval, and interactive flows.

Late review feedback was checked against this design.
Startup ordering, actual provisioned Matrix ID persistence, wrapper identity helpers, missed runtime consumers, root-level agent instruction docs, and stale focused-test selectors are plan-level gates, not optional cleanup.

## Non-Negotiable Invariants

- Runtime code sees one identity model: configured alias to actual persisted Matrix ID.
- Generated usernames are used only by account provisioning and config-time collision validation.
- The orchestrator must prepare router, agent, and team accounts before creating runtime bots that need actual Matrix IDs.
- The runtime identity registry must fail if a required managed account is missing after the account-preparation barrier.
- Friendly alias mentions resolve by exact configured alias, such as `@code` or `@ops`.
- Alias mentions do not resolve generated-looking localparts such as `@mindroom_code` unless the configured alias is literally `mindroom_code`.
- Alias mentions do not resolve bare actual Matrix localparts such as `@actual_code`.
- Full Matrix IDs remain literal unless the full ID exactly matches a current managed entity.
- Duplicate persisted Matrix IDs across router, agents, and teams are invalid.
- Configured-room responder boundaries are alias-first and then converted to actual Matrix IDs.
- Ad-hoc room responder boundaries use present actual managed Matrix IDs and map them back through the same registry.
- Tests must not use generated IDs as runtime identity shortcuts without persisted Matrix state.
- Any remaining generated-ID example must be classified as provisioning-only, actual persisted fixture, or docs explaining initial provisioning.

## Runtime Phases

### Phase 1: Config Validation

Config validation may inspect proposed usernames to catch collisions before accounts are created.
This does not make proposed usernames runtime identities.
The main example is `mindroom_user.username == "mindroom_code"` while agent `code` would provision with `mindroom_code`.

### Phase 2: Account Preparation

The orchestrator prepares managed accounts for router, agents, and teams.
If an account already exists in `matrix_state.yaml`, that account is reused.
If an account is missing, provisioning proposes a username, registers or logs in, and persists the actual returned Matrix ID.

### Phase 3: Runtime Construction

Bots are constructed with real `AgentMatrixUser` objects whose `user_id` is populated.
Team member IDs, prompt identity context, routing, mentions, scheduling, voice, topics, and subagent relays use the actual registry.
Blank temporary users are removed from production startup.

### Phase 4: Runtime Decisions

Runtime decisions only use actual persisted Matrix IDs.
If the registry is missing an account in this phase, that is a startup or reload failure.

## File Structure

### Provisioning

- Modify: `src/mindroom/matrix/users.py`.
- Responsibility: create or load managed Matrix accounts, persist the actual registered or restored identity, and keep proposed username generation private.

### Runtime Identity

- Modify: `src/mindroom/entity_resolution.py`.
- Responsibility: expose `EntityIdentityRegistry`, configured-room alias lookup, actual ID lookup, sender-to-alias lookup, duplicate-ID validation, and internal-sender ID derivation.

### Structural Matrix ID Parsing

- Modify: `src/mindroom/matrix/identity.py`.
- Responsibility: parse and validate Matrix IDs, define managed account keys, and stop owning config-aware runtime identity.

### Startup Lifecycle

- Modify: `src/mindroom/orchestrator.py`.
- Modify: `src/mindroom/orchestration/runtime.py`.
- Modify: `src/mindroom/bot.py`.
- Responsibility: prepare actual Matrix accounts before bot construction, remove blank production bot users, and keep config reload added-entity startup safe.

### Runtime Consumers

- Modify: `src/mindroom/agents.py`.
- Modify: `src/mindroom/authorization.py`.
- Modify: `src/mindroom/thread_utils.py`.
- Modify: `src/mindroom/turn_policy.py`.
- Modify: `src/mindroom/turn_controller.py`.
- Modify: `src/mindroom/commands/handler.py`.
- Modify: `src/mindroom/topic_generator.py`.
- Modify: `src/mindroom/scheduling.py`.
- Modify: `src/mindroom/voice_handler.py`.
- Modify: `src/mindroom/routing.py`.
- Modify: `src/mindroom/teams.py`.
- Modify: `src/mindroom/conversation_resolver.py`.
- Modify: `src/mindroom/conversation_state_writer.py`.
- Modify: `src/mindroom/response_runner.py`.
- Modify: `src/mindroom/edit_regenerator.py`.
- Modify: `src/mindroom/interactive.py`.
- Modify: `src/mindroom/tool_approval.py`.
- Modify: `src/mindroom/custom_tools/subagents.py`.
- Modify: `src/mindroom/custom_tools/config_manager.py`.
- Modify: `src/mindroom/matrix/mentions.py`.
- Modify: `src/mindroom/matrix/room_cleanup.py`.
- Modify: `src/mindroom/matrix/room_member_joins.py`.
- Modify: `src/mindroom/matrix/conversation_cache.py`.
- Modify: `src/mindroom/matrix/client_visible_messages.py`.
- Modify: `src/mindroom/matrix/stale_stream_cleanup.py`.
- Modify: `src/mindroom/execution_preparation.py`.
- Modify: `src/mindroom/api/openai_compat.py`.
- Responsibility: replace generated-ID and sender-classification shortcuts with registry-backed actual identity.

### Tests

- Create: `tests/identity_helpers.py`.
- Modify: tests returned by the final grep gates.
- Responsibility: seed actual persisted Matrix accounts when testing runtime identity.

### Boundaries and Docs

- Modify: `tach.toml`.
- Modify: docs that describe generated `@mindroom_<agent>` names as normal runtime handles.
- Modify: `README.md`, `AGENTS.md`, and `CLAUDE.md` if they contain generated handle examples that would teach future agents the wrong runtime model.
- Regenerate: `skills/mindroom-docs/references/*` when source docs change.

## Task 1: Add Test Helpers And Failing Identity Tests

**Files:**
- Create: `tests/identity_helpers.py`
- Modify: `tests/test_entity_resolution.py`
- Modify: `tests/test_matrix_identity.py`

- [ ] **Step 1: Create persisted account helpers**

Add this file.

```python
# tests/identity_helpers.py
from __future__ import annotations

from collections.abc import Iterable

from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME, RuntimePaths
from mindroom.matrix.identity import managed_account_key
from mindroom.matrix.state import MatrixState


def configured_entity_aliases(config: Config, *, include_router: bool = True) -> Iterable[str]:
    """Return configured managed aliases that need Matrix accounts."""
    if include_router:
        yield ROUTER_AGENT_NAME
    yield from config.agents
    yield from config.teams


def persist_entity_accounts(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    usernames: dict[str, str] | None = None,
    include_router: bool = True,
) -> None:
    """Persist actual Matrix accounts for configured agents, teams, and the router."""
    overrides = usernames or {}
    domain = config.get_domain(runtime_paths)
    state = MatrixState.load(runtime_paths=runtime_paths)
    for alias in configured_entity_aliases(config, include_router=include_router):
        username = overrides.get(alias, f"actual_{alias}")
        state.add_account(managed_account_key(alias), username, "pw", domain=domain)
    state.save(runtime_paths=runtime_paths)
```

- [ ] **Step 2: Add actual runtime identity tests**

Add these tests to `tests/test_entity_resolution.py`.

```python
import pytest

from mindroom.entity_resolution import DuplicateManagedEntityIdentity, MissingManagedEntityAccount, entity_identity_registry
from mindroom.matrix.identity import managed_account_key
from tests.identity_helpers import persist_entity_accounts


def test_entity_identity_registry_uses_persisted_actual_ids(tmp_path: Path) -> None:
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code")},
            teams={"ops": TeamConfig(display_name="Ops", agents=["code"])},
        ),
        runtime_paths=resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "mindroom_data",
            process_env={"MATRIX_HOMESERVER": "http://localhost:8008"},
        ),
    )
    runtime_paths = runtime_paths_for(config)
    persist_entity_accounts(
        config,
        runtime_paths,
        usernames={"router": "actual_router", "code": "actual_code", "ops": "actual_ops"},
    )

    registry = entity_identity_registry(config, runtime_paths)

    assert registry.current_id("code").full_id == "@actual_code:localhost"
    assert registry.current_id("ops").full_id == "@actual_ops:localhost"
    assert registry.alias_for_user_id("@actual_code:localhost") == "code"
    assert registry.alias_for_user_id("@mindroom_code:localhost") is None


def test_entity_identity_registry_requires_persisted_accounts_after_account_prep(tmp_path: Path) -> None:
    config = bind_runtime_paths(
        Config(agents={"code": AgentConfig(display_name="Code")}),
        runtime_paths=resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "mindroom_data",
            process_env={"MATRIX_HOMESERVER": "http://localhost:8008"},
        ),
    )

    with pytest.raises(MissingManagedEntityAccount, match="code"):
        entity_identity_registry(config, runtime_paths_for(config))


def test_entity_identity_registry_rejects_duplicate_actual_ids(tmp_path: Path) -> None:
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code")},
            teams={"ops": TeamConfig(display_name="Ops", agents=["code"])},
        ),
        runtime_paths=resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "mindroom_data",
            process_env={"MATRIX_HOMESERVER": "http://localhost:8008"},
        ),
    )
    runtime_paths = runtime_paths_for(config)
    state = MatrixState.load(runtime_paths=runtime_paths)
    state.add_account(managed_account_key("router"), "actual_router", "pw", domain="localhost")
    state.add_account(managed_account_key("code"), "shared_actual", "pw", domain="localhost")
    state.add_account(managed_account_key("ops"), "shared_actual", "pw", domain="localhost")
    state.save(runtime_paths=runtime_paths)

    with pytest.raises(DuplicateManagedEntityIdentity, match="@shared_actual:localhost"):
        entity_identity_registry(config, runtime_paths)
```

- [ ] **Step 3: Add persisted-only wrapper tests**

Add or update tests in `tests/test_matrix_identity.py`.

```python
def test_extract_agent_name_uses_only_persisted_actual_ids(tmp_path: Path) -> None:
    config = _bind_runtime_paths(self.config, tmp_path)
    runtime_paths = runtime_paths_for(config)
    persist_entity_accounts(config, runtime_paths, usernames={"router": "actual_router", "general": "actual_general", "calculator": "actual_calculator"})

    assert extract_agent_name("@actual_general:localhost", config, runtime_paths) == "general"
    assert extract_agent_name("@mindroom_general:localhost", config, runtime_paths) is None
    assert is_agent_id("@actual_general:localhost", config, runtime_paths)
    assert not is_agent_id("@mindroom_general:localhost", config, runtime_paths)
```

- [ ] **Step 4: Run tests to verify they fail for the intended reason**

Run:

```bash
UV_PYTHON=3.13 uv run pytest -n 0 --no-cov tests/test_entity_resolution.py::test_entity_identity_registry_uses_persisted_actual_ids tests/test_entity_resolution.py::test_entity_identity_registry_requires_persisted_accounts_after_account_prep tests/test_entity_resolution.py::test_entity_identity_registry_rejects_duplicate_actual_ids -q
```

Expected: failure because the new registry and errors do not exist yet.

## Task 2: Implement The Actual Identity Registry

**Files:**
- Modify: `src/mindroom/entity_resolution.py`
- Modify: `src/mindroom/matrix/identity.py`
- Test: `tests/test_entity_resolution.py`
- Test: `tests/test_matrix_identity.py`

- [ ] **Step 1: Add registry errors as plain exception types**

Add this code to `src/mindroom/entity_resolution.py`.

```python
class MissingManagedEntityAccount(RuntimeError):
    """Raised when runtime identity is requested before account preparation."""

    def __init__(self, aliases: list[str]) -> None:
        self.aliases = tuple(aliases)
        super().__init__(f"Missing persisted Matrix account identity for: {', '.join(self.aliases)}")


class DuplicateManagedEntityIdentity(RuntimeError):
    """Raised when two managed aliases point at the same actual Matrix ID."""

    def __init__(self, user_id: str, aliases: list[str]) -> None:
        self.user_id = user_id
        self.aliases = tuple(aliases)
        super().__init__(f"Duplicate managed Matrix identity {user_id} for aliases: {', '.join(self.aliases)}")
```

- [ ] **Step 2: Add the runtime registry**

Add this class to `src/mindroom/entity_resolution.py`.

```python
@dataclass(frozen=True)
class EntityIdentityRegistry:
    """Actual persisted Matrix IDs for configured agents, teams, and the router."""

    by_alias: dict[str, MatrixID]

    def current_id(self, alias: str) -> MatrixID:
        return self.by_alias[alias]

    def alias_for_user_id(self, user_id: str, *, include_router: bool = True) -> str | None:
        for alias, matrix_id in self.by_alias.items():
            if not include_router and alias == ROUTER_AGENT_NAME:
                continue
            if matrix_id.full_id == user_id:
                return alias
        return None

    def internal_sender_ids(self, *, include_mindroom_user: str | None = None) -> frozenset[str]:
        user_ids = {matrix_id.full_id for matrix_id in self.by_alias.values()}
        if include_mindroom_user is not None:
            user_ids.add(include_mindroom_user)
        return frozenset(user_ids)
```

- [ ] **Step 3: Load only persisted actual account IDs**

Add this function to `src/mindroom/entity_resolution.py`.

```python
def entity_identity_registry(config: Config, runtime_paths: RuntimePaths) -> EntityIdentityRegistry:
    """Return actual persisted Matrix identities for configured managed entities."""
    state = matrix_state.matrix_state_for_runtime(runtime_paths)
    aliases = [ROUTER_AGENT_NAME, *config.agents, *config.teams]
    ids: dict[str, MatrixID] = {}
    missing: list[str] = []
    for alias in aliases:
        account = state.accounts.get(managed_account_key(alias))
        if account is None:
            missing.append(alias)
            continue
        ids[alias] = MatrixID.from_username(account.username, account.domain or matrix_domain(runtime_paths))
    if missing:
        raise MissingManagedEntityAccount(missing)
    _raise_for_duplicate_ids(ids)
    return EntityIdentityRegistry(ids)


def _raise_for_duplicate_ids(ids: dict[str, MatrixID]) -> None:
    aliases_by_user_id: dict[str, list[str]] = {}
    for alias, matrix_id in ids.items():
        aliases_by_user_id.setdefault(matrix_id.full_id, []).append(alias)
    for user_id, aliases in aliases_by_user_id.items():
        if len(aliases) > 1:
            raise DuplicateManagedEntityIdentity(user_id, aliases)
```

- [ ] **Step 4: Keep alias parsing out of the registry**

Do not add `alias_for_localpart()` to `EntityIdentityRegistry`.
Mention and voice parsing own text token interpretation.
The registry only knows exact alias keys and exact full Matrix IDs.

- [ ] **Step 5: Replace old generated runtime identity exports**

Delete these runtime concepts from `src/mindroom/entity_resolution.py`.

```text
EntityMatrixIdentity
entity_matrix_identity
_entity_matrix_id_map
_entity_matrix_id
is_stale_localpart
is_stale_user_id
```

- [ ] **Step 6: Migrate or remove wrapper trust helpers**

In `src/mindroom/matrix/identity.py`, remove `MatrixID.from_agent()` and `MatrixID.agent_name()`.
Move config-aware sender helpers to `src/mindroom/entity_resolution.py` or rewrite them to use persisted accounts only.
The preferred new names are:

```python
def entity_alias_for_user_id(user_id: str, config: Config, runtime_paths: RuntimePaths, *, include_router: bool = True) -> str | None:
    return entity_identity_registry(config, runtime_paths).alias_for_user_id(user_id, include_router=include_router)


def is_managed_entity_id(user_id: str, config: Config, runtime_paths: RuntimePaths) -> bool:
    return entity_alias_for_user_id(user_id, config, runtime_paths) is not None


def active_internal_sender_ids(config: Config, runtime_paths: RuntimePaths) -> frozenset[str]:
    return entity_identity_registry(config, runtime_paths).internal_sender_ids(
        include_mindroom_user=mindroom_user_id(config, runtime_paths),
    )
```

Delete or migrate production imports of `extract_agent_name()` and `is_agent_id()`.
If tests keep those old names for compatibility while call sites are migrated, they must assert persisted-only behavior.

- [ ] **Step 7: Run identity tests**

Run:

```bash
UV_PYTHON=3.13 uv run pytest -n 0 --no-cov tests/test_entity_resolution.py tests/test_matrix_identity.py -q
```

Expected: identity tests pass after callers are migrated in later tasks.

## Task 3: Fix Provisioning And Config-Time Collision Checks

**Files:**
- Modify: `src/mindroom/matrix/users.py`
- Modify: `src/mindroom/config/main.py`
- Modify: `src/mindroom/matrix_identifiers.py`
- Modify: `tests/test_matrix_agent_manager.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Keep proposed username generation private to provisioning**

Add this helper in `src/mindroom/matrix/users.py`.

```python
def _proposed_username_for_new_entity(entity_name: str, runtime_paths: RuntimePaths) -> str:
    """Return the username to try when provisioning a missing managed Matrix account."""
    return agent_username_localpart(entity_name, runtime_paths=runtime_paths)
```

Do not export this helper from `__all__`.
Tests may import it only from `mindroom.matrix.users` when testing provisioning.

- [ ] **Step 2: Persist the actual returned Matrix ID**

In `create_agent_user(...)`, use the proposed username only before registration.
After `_register_user(...)` returns, parse the returned actual Matrix ID and persist that username and domain.

```python
if registration_needed:
    registered_user_id = await _register_user(
        homeserver=homeserver,
        username=matrix_username,
        password=password,
        display_name=agent_display_name,
        runtime_paths=runtime_paths,
    )
    registered_matrix_id = MatrixID.parse(registered_user_id)
    matrix_username = registered_matrix_id.username
    server_name = registered_matrix_id.domain
    _save_agent_credentials(agent_name, matrix_username, password, runtime_paths)
    logger.info("agent_credentials_saved_after_registration", agent=agent_name)
else:
    server_name = existing_creds["domain"] or extract_server_name_from_homeserver(homeserver, runtime_paths=runtime_paths)
```

Adjust the exact code to fit the existing credential structure.
The persisted account must match `AgentMatrixUser.user_id`.

- [ ] **Step 3: Return actual IDs from register helpers**

In `_handle_register_response(...)`, return `response.user_id` for successful `nio.RegisterResponse`.
Validate that the returned ID parses as a Matrix ID.
Direct token and no-token paths should propagate the actual returned user ID.

```python
actual_user_id = parse_current_matrix_user_id(response.user_id)
logger.info("matrix_user_registered", user_id=actual_user_id)
return actual_user_id
```

- [ ] **Step 4: Add provisioning mismatch coverage**

Add a test in `tests/test_matrix_agent_manager.py`.

```python
async def test_create_agent_user_persists_actual_returned_user_id(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    with patch("mindroom.matrix.users._register_user", AsyncMock(return_value="@actual_code:localhost")):
        agent_user = await create_agent_user("http://localhost:8008", "code", "Code", runtime_paths)

    state = MatrixState.load(runtime_paths=runtime_paths)

    assert agent_user.user_id == "@actual_code:localhost"
    assert state.accounts["agent_code"].username == "actual_code"
    assert state.accounts["agent_code"].domain == "localhost"
```

- [ ] **Step 5: Preserve pre-provision collision validation**

In `src/mindroom/config/main.py`, keep collision checks against proposed localparts before accounts exist.
Also check persisted account localparts when state exists.
This validator compares localparts and does not require runtime identity registry.

```python
reserved_localparts: dict[str, str] = {}
for alias in (ROUTER_AGENT_NAME, *self.agents, *self.teams):
    label = _managed_entity_label(alias, self)
    reserved_localparts[agent_username_localpart(alias, runtime_paths=runtime_paths)] = label
    account = matrix_state_for_runtime(runtime_paths).accounts.get(managed_account_key(alias))
    if account is not None:
        reserved_localparts[account.username] = label
```

- [ ] **Step 6: Add config validation tests**

Keep `tests/test_cli.py::test_mindroom_user_username_rejects_agent_collision` passing.
Keep `tests/test_cli.py::test_mindroom_user_username_rejects_persisted_agent_username_collision` passing.
Add a team version if one does not exist.

```python
def test_mindroom_user_username_rejects_team_proposed_username_collision(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)

    with pytest.raises(ValueError, match="conflicts with team 'ops'"):
        Config.model_validate(
            {
                "agents": {"code": {"display_name": "Code"}},
                "teams": {"ops": {"display_name": "Ops", "agents": ["code"]}},
                "mindroom_user": {"username": "mindroom_ops", "display_name": "Alice"},
            },
            context={"runtime_paths": runtime_paths},
        )
```

- [ ] **Step 7: Run provisioning and config tests**

Run:

```bash
UV_PYTHON=3.13 uv run pytest -n 0 --no-cov tests/test_matrix_agent_manager.py tests/test_cli.py::test_mindroom_user_username_rejects_agent_collision tests/test_cli.py::test_mindroom_user_username_rejects_persisted_agent_username_collision -q
```

Expected: pass.

## Task 4: Add The Account-Preparation Barrier

**Files:**
- Modify: `src/mindroom/orchestrator.py`
- Modify: `src/mindroom/orchestration/runtime.py`
- Modify: `src/mindroom/bot.py`
- Test: `tests/test_config_reload.py`
- Test: `tests/test_router_rooms.py`
- Test: `tests/test_matrix_agent_manager.py`

- [ ] **Step 1: Add orchestrator account preparation**

Add a method to `src/mindroom/orchestrator.py` that creates or loads account users before bot construction.

```python
async def _prepare_entity_accounts(self, config: Config, entity_names: Iterable[str]) -> dict[str, AgentMatrixUser]:
    """Ensure managed Matrix accounts exist before runtime bot construction."""
    users: dict[str, AgentMatrixUser] = {}
    homeserver = constants.runtime_matrix_homeserver(runtime_paths=self.runtime_paths)
    for entity_name in entity_names:
        display_name = self._entity_display_name(config, entity_name)
        users[entity_name] = await create_agent_user(
            homeserver,
            entity_name,
            display_name,
            runtime_paths=self.runtime_paths,
        )
    return users
```

Add `_entity_display_name(...)` beside `_configured_entity_names(...)`.

```python
def _entity_display_name(self, config: Config, entity_name: str) -> str:
    if entity_name == ROUTER_AGENT_NAME:
        return "RouterAgent"
    if entity_name in config.agents:
        return config.agents[entity_name].display_name
    if entity_name in config.teams:
        return config.teams[entity_name].display_name
    return entity_name
```

- [ ] **Step 2: Use actual users when constructing bots**

Change `_create_managed_bot(...)` to accept an `AgentMatrixUser`.

```python
def _create_managed_bot(self, entity_name: str, config: Config, agent_user: AgentMatrixUser) -> AgentBot | TeamBot:
    bot = cast(
        "AgentBot | TeamBot",
        create_bot_for_entity(
            entity_name,
            agent_user,
            config,
            self.runtime_paths,
            self.storage_path,
            config_path=self.config_path,
        ),
    )
```

Delete production use of `create_temp_user(...)`.

- [ ] **Step 3: Prepare accounts during initialize**

In `initialize()`, prepare accounts before creating bots.

```python
entity_names = self._configured_entity_names(config)
entity_users = await self._prepare_entity_accounts(config, entity_names)
for entity_name in entity_names:
    self._create_managed_bot(entity_name, config, entity_users[entity_name])
```

- [ ] **Step 4: Prepare accounts during config reload added-entity startup**

In `_create_and_start_entities(...)`, prepare accounts before calling `_create_managed_bot(...)`.

```python
entity_users = await self._prepare_entity_accounts(config, sorted(entity_names))
for entity_name in entity_names:
    self._create_managed_bot(entity_name, config, entity_users[entity_name])
```

- [ ] **Step 5: Make bots require actual users**

In `src/mindroom/bot.py`, delete account creation from `AgentBot.ensure_user_account()`.
Replace it with validation.

```python
def _require_prepared_agent_user(self) -> None:
    if not self.agent_user.user_id or not self.agent_user.password:
        msg = f"Matrix account for {self.agent_name!r} was not prepared before bot startup"
        raise PermanentMatrixStartupError(msg)
```

Call `_require_prepared_agent_user()` from `start()` before `login_agent_user(...)`.

- [ ] **Step 6: Remove blank-user runtime fallback**

In `_init_runtime_components()`, rely on `self.matrix_id`.

```python
runtime_matrix_id = self.matrix_id
```

Tests that construct bots directly must pass actual `AgentMatrixUser` objects.
Do not add a generated ID fallback.

- [ ] **Step 7: Add fresh startup and reload tests**

Add tests that prove the lifecycle boundary.

```python
async def test_initialize_prepares_accounts_before_bot_construction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    prepared: list[str] = []

    async def fake_prepare(config: Config, entity_names: Iterable[str]) -> dict[str, AgentMatrixUser]:
        prepared.extend(entity_names)
        return {
            entity_name: AgentMatrixUser(
                agent_name=entity_name,
                user_id=f"@actual_{entity_name}:localhost",
                display_name=entity_name,
                password="pw",
            )
            for entity_name in entity_names
        }

    monkeypatch.setattr(orchestrator, "_prepare_entity_accounts", fake_prepare)

    await orchestrator.initialize()

    assert "router" in prepared
    assert all(bot.agent_user.user_id for bot in orchestrator.agent_bots.values())
```

Add a config-reload-added-entity test using the same assertion pattern.

- [ ] **Step 8: Run startup tests**

Run:

```bash
UV_PYTHON=3.13 uv run pytest -n 0 --no-cov tests/test_config_reload.py tests/test_router_rooms.py tests/test_matrix_agent_manager.py -q
```

Expected: pass.

## Task 5: Replace Bot, Team, Prompt, And Config Runtime Shortcuts

**Files:**
- Modify: `src/mindroom/config/main.py`
- Modify: `src/mindroom/bot.py`
- Modify: `src/mindroom/agents.py`
- Modify: `src/mindroom/teams.py`
- Test: `tests/test_agents.py`
- Test: `tests/test_team_collaboration.py`
- Test: `tests/test_team_mode_decision.py`
- Test: `tests/test_agent_order_preservation.py`

- [ ] **Step 1: Remove `Config.get_ids()`**

Replace production callers with `entity_identity_registry(config, runtime_paths)`.
Delete `Config.get_ids()` after callers are gone.

Run:

```bash
rg -n "\.get_ids\(" src/mindroom tests
```

Expected after this task: no production output.

- [ ] **Step 2: Use actual registry IDs in team bot construction**

In `create_bot_for_entity(...)`, replace team member ID construction.

```python
registry = entity_identity_registry(config, runtime_paths)
team_matrix_ids = [registry.current_id(agent_name) for agent_name in team_config.agents]
```

- [ ] **Step 3: Use actual registry IDs in agent prompt identity context**

In `src/mindroom/agents.py`, replace generated ID lookups.

```python
matrix_id = entity_identity_registry(config, runtime_paths).current_id(agent_name).full_id
```

- [ ] **Step 4: Update team member filtering**

In `src/mindroom/teams.py`, replace generated or `extract_agent_name(...)` member filtering with registry-backed alias lookup.

```python
registry = entity_identity_registry(config, runtime_paths)
member_alias = registry.alias_for_user_id(member_id)
```

- [ ] **Step 5: Run team and agent tests**

Run:

```bash
UV_PYTHON=3.13 uv run pytest -n 0 --no-cov tests/test_agents.py tests/test_team_collaboration.py tests/test_team_mode_decision.py tests/test_agent_order_preservation.py -q
```

Expected: pass.

## Task 6: Replace Mention And Voice Semantics

**Files:**
- Modify: `src/mindroom/matrix/mentions.py`
- Modify: `src/mindroom/voice_handler.py`
- Test: `tests/test_mentions.py`
- Test: `tests/test_voice_agent_mentions.py`
- Test: `tests/test_voice_handler.py`

- [ ] **Step 1: Make alias tokens exact configured aliases**

In `src/mindroom/matrix/mentions.py`, replace `_localpart_candidate_names(...)` with exact alias matching.
Alias parsing must check configured aliases only.

```python
def _alias_for_mention_localpart(localpart: str, config: Config) -> str | None:
    if _is_reserved_user_alias_localpart(localpart):
        return None
    lower_localpart = localpart.lower()
    for alias in (*config.agents, *config.teams):
        if alias.lower() == lower_localpart:
            return alias
    return None
```

- [ ] **Step 2: Keep full Matrix IDs explicit**

Full Matrix IDs resolve only through exact full-ID matching.

```python
if alias := registry.alias_for_user_id(explicit_user_id, include_router=False):
    return _entity_mention_resolution(alias, registry=registry, config=config)
return _literal_user_resolution(explicit_user_id)
```

- [ ] **Step 3: Do not resolve bare actual localparts**

Add this test to `tests/test_mentions.py`.

```python
def test_bare_actual_matrix_username_is_not_an_alias(tmp_path: Path) -> None:
    runtime_paths = constants_mod.resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": ""},
    )
    config = _make_config(runtime_paths)
    persist_entity_accounts(config, runtime_paths, usernames={"router": "actual_router", "general": "actual_general"})

    content = _format_message_with_mentions(config, "@actual_general please help")

    assert content["body"] == "@actual_general please help"
    assert "m.mentions" not in content
```

- [ ] **Step 4: Do not resolve generated-looking localparts**

Add this test to `tests/test_mentions.py`.

```python
def test_generated_looking_localpart_is_literal_when_not_configured_alias(tmp_path: Path) -> None:
    runtime_paths = constants_mod.resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": ""},
    )
    config = _make_config(runtime_paths)
    persist_entity_accounts(config, runtime_paths, usernames={"router": "actual_router", "general": "actual_general"})

    content = _format_message_with_mentions(config, "@mindroom_general please help")

    assert content["body"] == "@mindroom_general please help"
    assert "m.mentions" not in content
```

- [ ] **Step 5: Keep configured generated-looking aliases valid**

Add this test to `tests/test_mentions.py`.

```python
def test_generated_looking_config_alias_still_resolves_as_alias(tmp_path: Path) -> None:
    runtime_paths = constants_mod.resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": ""},
    )
    config = _bind_config(runtime_paths, {"mindroom_dev": AgentConfig(display_name="Dev")})
    persist_entity_accounts(config, runtime_paths, usernames={"router": "actual_router", "mindroom_dev": "actual_dev"})

    content = _format_message_with_mentions(config, "@mindroom_dev please help")

    assert content["body"] == "@actual_dev:localhost please help"
    assert content["m.mentions"]["user_ids"] == ["@actual_dev:localhost"]
```

- [ ] **Step 6: Keep alias mentions friendly**

Add this test to `tests/test_mentions.py`.

```python
def test_alias_mention_resolves_to_actual_persisted_matrix_id(tmp_path: Path) -> None:
    runtime_paths = constants_mod.resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": ""},
    )
    config = _make_config(runtime_paths)
    persist_entity_accounts(config, runtime_paths, usernames={"router": "actual_router", "general": "actual_general"})

    content = _format_message_with_mentions(config, "@general could you help?")

    assert content["body"] == "@actual_general:localhost could you help?"
    assert content["m.mentions"]["user_ids"] == ["@actual_general:localhost"]
```

- [ ] **Step 7: Keep remote full Matrix IDs literal**

Add this test to `tests/test_mentions.py`.

```python
def test_remote_mindroom_like_full_matrix_id_stays_literal(tmp_path: Path) -> None:
    runtime_paths = constants_mod.resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": ""},
    )
    config = _make_config(runtime_paths)
    persist_entity_accounts(config, runtime_paths, usernames={"router": "actual_router", "code": "actual_code"})

    content = _format_message_with_mentions(config, "@mindroom_code:remote.example please look")

    assert content["body"] == "@mindroom_code:remote.example please look"
    assert content["m.mentions"]["user_ids"] == ["@mindroom_code:remote.example"]
```

- [ ] **Step 8: Update voice sanitizer to use exact alias semantics**

`_sanitize_unavailable_mentions(...)` should preserve remote full MXIDs.
It should strip unavailable `@code`.
It should not strip unavailable `@actual_code` because that is not an alias.
It should strip unavailable full local actual MXIDs only when the full ID exactly maps to an unavailable entity.

- [ ] **Step 9: Run mention and voice tests**

Run:

```bash
UV_PYTHON=3.13 uv run pytest -n 0 --no-cov tests/test_mentions.py tests/test_voice_agent_mentions.py tests/test_voice_handler.py -q
```

Expected: pass.

## Task 7: Replace Authorization, Routing, Thread, And Conversation Callers

**Files:**
- Modify: `src/mindroom/authorization.py`
- Modify: `src/mindroom/thread_utils.py`
- Modify: `src/mindroom/turn_policy.py`
- Modify: `src/mindroom/turn_controller.py`
- Modify: `src/mindroom/routing.py`
- Modify: `src/mindroom/conversation_resolver.py`
- Modify: `src/mindroom/conversation_state_writer.py`
- Modify: `src/mindroom/response_runner.py`
- Modify: `src/mindroom/edit_regenerator.py`
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Modify: `src/mindroom/matrix/client_visible_messages.py`
- Test: `tests/test_authorization.py`
- Test: `tests/test_router_configured_agents.py`
- Test: `tests/test_routing_regression.py`
- Test: `tests/test_thread_history.py`
- Test: `tests/test_edit_response_regeneration.py`
- Test: `tests/test_ai_user_id.py`

- [ ] **Step 1: Replace sender-to-alias wrappers**

Run:

```bash
rg -n "extract_agent_name\\(|is_agent_id\\(|active_internal_sender_ids\\(" src/mindroom tests
```

Each production hit must either move to `entity_alias_for_user_id(...)`, `is_managed_entity_id(...)`, or registry-derived `active_internal_sender_ids(...)`.
No production hit may keep generated fallback behavior.

- [ ] **Step 2: Reshape permission filtering around aliases**

In `src/mindroom/authorization.py`, change permission filtering to keep alias and Matrix ID together.

```python
@dataclass(frozen=True)
class ResponderCandidate:
    alias: str
    matrix_id: MatrixID
```

`filter_agents_by_sender_permissions(...)` should resolve permissions from `candidate.alias`.
It should not call `MatrixID.agent_name(...)`.

- [ ] **Step 3: Keep configured-room and ad-hoc boundaries unchanged**

Configured rooms start from configured aliases.
Ad-hoc rooms start from present actual Matrix IDs and map to aliases.

```python
registry = entity_identity_registry(config, runtime_paths)
configured_aliases = configured_routable_entity_names_for_room(config, room.room_id, runtime_paths)
if configured_aliases:
    return [ResponderCandidate(alias, registry.current_id(alias)) for alias in configured_aliases]
```

- [ ] **Step 4: Update routing and team flows**

In `src/mindroom/routing.py`, router suggestions should return aliases.
In `src/mindroom/teams.py`, team request member filtering should use registry aliases.
No route or team path should compare generated usernames.

- [ ] **Step 5: Update conversation and response flows**

In `conversation_resolver`, `conversation_state_writer`, `response_runner`, and `edit_regenerator`, replace sender classification with exact actual ID lookup.
Thread and history ownership must use aliases from actual IDs.

- [ ] **Step 6: Run focused conversation tests**

Run:

```bash
UV_PYTHON=3.13 uv run pytest -n 0 --no-cov tests/test_authorization.py tests/test_router_configured_agents.py tests/test_routing_regression.py tests/test_thread_history.py tests/test_edit_response_regeneration.py tests/test_ai_user_id.py -q
```

Expected: pass.

## Task 8: Replace User-Facing Runtime Callers And Tools

**Files:**
- Modify: `src/mindroom/commands/handler.py`
- Modify: `src/mindroom/topic_generator.py`
- Modify: `src/mindroom/scheduling.py`
- Modify: `src/mindroom/custom_tools/subagents.py`
- Modify: `src/mindroom/custom_tools/config_manager.py`
- Modify: `src/mindroom/matrix/room_cleanup.py`
- Modify: `src/mindroom/matrix/room_member_joins.py`
- Modify: `src/mindroom/matrix/stale_stream_cleanup.py`
- Modify: `src/mindroom/execution_preparation.py`
- Modify: `src/mindroom/api/openai_compat.py`
- Modify: `src/mindroom/interactive.py`
- Modify: `src/mindroom/tool_approval.py`
- Test: `tests/test_commands.py`
- Test: `tests/test_topic_generator.py`
- Test: `tests/test_scheduling.py`
- Test: `tests/test_matrix_state_cache.py`
- Test: `tests/test_stale_stream_cleanup.py`
- Test: `tests/test_config_manager_consolidated.py`
- Test: `tests/test_tool_approval.py`
- Test: `tests/test_message_content.py`
- Test: `tests/test_interactive.py`
- Test: `tests/test_interactive_thread_fix.py`
- Test: `tests/test_room_member_hooks.py`

- [ ] **Step 1: Update welcome, topic, and scheduling paths**

These paths should display configured aliases but emit actual Matrix mentions when sending Matrix content.
They should never format `@mindroom_{alias}` directly.

- [ ] **Step 2: Update subagent and config-manager tools**

`sessions_spawn` and `sessions_send` should mention the actual Matrix ID for the requested alias.
Unknown aliases should return structured tool errors.
Config manager agent listings should report aliases and actual current IDs from the registry.

- [ ] **Step 3: Update room cleanup and join hooks**

Room cleanup and join hooks should classify managed users through actual persisted IDs.
They should not rely on generated prefixes except where Matrix account keys are being read from state.

- [ ] **Step 4: Update OpenAI compatibility and execution prep**

Any prompt or history path that needs the responder identity should use actual registry IDs.
Any display-only path should use aliases or display names.

- [ ] **Step 5: Run focused user-facing tests**

Run:

```bash
UV_PYTHON=3.13 uv run pytest -n 0 --no-cov tests/test_commands.py tests/test_topic_generator.py tests/test_scheduling.py tests/test_matrix_state_cache.py tests/test_stale_stream_cleanup.py tests/test_config_manager_consolidated.py tests/test_tool_approval.py tests/test_message_content.py tests/test_interactive.py tests/test_interactive_thread_fix.py tests/test_room_member_hooks.py -q
```

Expected: pass.

## Task 9: Rewrite Tests So Incorrect Examples Cannot Multiply

**Files:**
- Modify: tests returned by the grep commands in this task.

- [ ] **Step 1: Classify generated-looking examples**

Run:

```bash
rg -n "MatrixID\.from_agent|agent_username_localpart|@mindroom_[A-Za-z0-9_]+:localhost|@mindroom_[A-Za-z0-9_]+:[^\\s'\"]+" tests src/mindroom docs skills/mindroom-docs/references README.md AGENTS.md CLAUDE.md
```

Classify each hit as one of these exact categories:

```text
provisioning-only
actual-persisted-fixture
docs-explaining-initial-provisioning
incorrect-runtime-shortcut
```

- [ ] **Step 2: Convert runtime identity tests to persisted fixtures**

For `incorrect-runtime-shortcut`, seed Matrix state before creating runtime objects.

```python
persist_entity_accounts(
    config,
    runtime_paths,
    usernames={"router": "actual_router", "code": "actual_code", "ops": "actual_ops"},
)
registry = entity_identity_registry(config, runtime_paths)
code_id = registry.current_id("code")
```

- [ ] **Step 3: Keep generated examples only in provisioning tests**

Provisioning tests may assert proposed localparts.

```python
assert users._proposed_username_for_new_entity("code", runtime_paths) == "mindroom_code"
```

- [ ] **Step 4: Run the identity API grep gate**

Run:

```bash
rg -n "MatrixID\.from_agent|entity_matrix_identity|entity_matrix_ids\(|Config\.get_ids|\.get_ids\(|\.agent_name\(|extract_agent_name\(|is_agent_id\(|active_internal_sender_ids\(|is_stale_localpart|is_stale_user_id|bootstrap_ids|_bootstrap_ids" src/mindroom tests
```

Expected production output: no runtime identity shortcut hits.
Any retained helper in tests must assert persisted-only behavior.

- [ ] **Step 5: Run the generated username import gate**

Run:

```bash
rg -n "agent_username_localpart|MatrixID\.AGENT_PREFIX" src/mindroom tests
```

Expected production output:

```text
src/mindroom/matrix/users.py
src/mindroom/matrix_identifiers.py
```

Any other production hit must be removed or justified as provisioning-only in the PR description.

## Task 10: Update Docs, Boundaries, And Generated References

**Files:**
- Modify: `tach.toml`
- Modify: `docs/architecture/matrix.md`
- Modify: `docs/configuration/router.md`
- Modify: `docs/dev/agent_configuration.md`
- Modify: `docs/chat-commands.md`
- Modify: `docs/voice.md`
- Modify: `docs/scheduling.md`
- Modify: `docs/tools/agent-orchestration.md`
- Modify: `docs/authorization.md`
- Modify: `docs/hooks.md`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`
- Modify: `src/mindroom/config_template.yaml`
- Modify: `src/mindroom/cli/config.py`
- Regenerate or modify: `skills/mindroom-docs/references/*`

- [ ] **Step 1: Update docs language**

Docs must describe this model.

```text
Users mention configured aliases like @code or @ops.
MindRoom resolves those aliases to the actual Matrix accounts persisted for those entities.
Generated Matrix usernames are only initial account-provisioning defaults.
```

Do not document generated `@mindroom_<agent>` names as normal user-facing handles.
Check `README.md`, `AGENTS.md`, and `CLAUDE.md` as well as `docs/` because agent instructions can become future examples.

- [ ] **Step 2: Regenerate docs references**

Run:

```bash
UV_PYTHON=3.13 uv run pre-commit run --files docs/architecture/matrix.md docs/configuration/router.md docs/dev/agent_configuration.md docs/chat-commands.md docs/voice.md docs/scheduling.md docs/tools/agent-orchestration.md docs/authorization.md docs/hooks.md README.md AGENTS.md CLAUDE.md src/mindroom/config_template.yaml src/mindroom/cli/config.py
```

Expected: source docs pass and generated references update when required.

- [ ] **Step 3: Update Tach boundaries**

Run:

```bash
UV_PYTHON=3.13 uv run tach check --dependencies --interfaces
```

If Tach reports changed dependencies, update only the affected `tach.toml` blocks.

## Task 11: Final Accountability Gates

**Files:**
- Modify: no files unless a gate exposes a real bug.

- [ ] **Step 1: Run the hard identity grep gate**

Run:

```bash
rg -n "MatrixID\.from_agent|entity_matrix_identity|entity_matrix_ids\(|Config\.get_ids|\.get_ids\(|\.agent_name\(|extract_agent_name\(|is_agent_id\(|active_internal_sender_ids\(|_configured_active_account_sender_ids|is_stale_localpart|is_stale_user_id|bootstrap_ids|_bootstrap_ids" src/mindroom tests
```

Expected: no production runtime shortcut hits.

- [ ] **Step 2: Run the generated username grep gate**

Run:

```bash
rg -n "agent_username_localpart|MatrixID\.AGENT_PREFIX|@mindroom_[A-Za-z0-9_]+|mindroom_\\{.*\\}|f\"mindroom_|f'@mindroom_|f\"@mindroom_" src/mindroom tests docs skills/mindroom-docs/references scripts README.md AGENTS.md CLAUDE.md
```

Expected hits must be manually classified in the PR description as one of:

```text
provisioning-only
actual-persisted-fixture
docs-explaining-initial-provisioning
```

No hit may be classified as runtime identity construction.

- [ ] **Step 3: Run import smoke checks**

Run:

```bash
UV_PYTHON=3.13 uv run python -c "from mindroom.config.main import Config; print(Config.__name__)"
UV_PYTHON=3.13 uv run python -c "import mindroom.cli.main; print('cli ok')"
```

Expected:

```text
Config
cli ok
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
UV_PYTHON=3.13 uv run pytest -n 0 --no-cov tests/test_entity_resolution.py tests/test_matrix_identity.py tests/test_mentions.py tests/test_voice_agent_mentions.py tests/test_voice_handler.py tests/test_authorization.py tests/test_router_configured_agents.py tests/test_routing_regression.py tests/test_commands.py tests/test_topic_generator.py tests/test_scheduling.py tests/test_matrix_agent_manager.py tests/test_agents.py tests/test_team_collaboration.py tests/test_team_mode_decision.py tests/test_agent_order_preservation.py tests/test_ai_user_id.py tests/test_thread_history.py tests/test_edit_response_regeneration.py tests/test_config_reload.py tests/test_config_manager_consolidated.py tests/test_tool_approval.py tests/test_stale_stream_cleanup.py tests/test_matrix_state_cache.py tests/test_message_content.py tests/test_interactive.py tests/test_interactive_thread_fix.py tests/test_room_member_hooks.py tests/test_router_rooms.py -q
```

Expected: pass.

- [ ] **Step 5: Run full backend tests if focused tests pass**

Run:

```bash
UV_PYTHON=3.13 uv run pytest -n 0 --no-cov tests -q
```

Expected: pass.

- [ ] **Step 6: Run boundary and formatting checks**

Run:

```bash
UV_PYTHON=3.13 uv run tach check --dependencies --interfaces
git diff --check origin/main..HEAD
git diff --check
```

Expected: pass and no whitespace output.

- [ ] **Step 7: Run pre-commit on touched files**

Run:

```bash
UV_PYTHON=3.13 uv run pre-commit run --files $(git diff --name-only origin/main..HEAD)
```

Expected: pass.

- [ ] **Step 8: Run a local Matrix smoke test**

Run this after tests pass.

```bash
just local-matrix-up
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false UV_PYTHON=3.13 uv run mindroom run
```

In another shell, send an alias mention and verify a thread response.

```bash
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false uv run --python 3.13 matty send "Lobby" "Hello @general, reply with pong."
MATRIX_HOMESERVER=http://localhost:8008 MATRIX_SSL_VERIFY=false uv run --python 3.13 matty threads "Lobby"
```

Expected: the agent responds in a thread and the emitted mention uses the actual persisted Matrix account.

- [ ] **Step 9: Update the PR description with the accountability summary**

Add this summary to the PR description.
This summary records completed gates; it is not a substitute for the grep, test, Tach, pre-commit, and smoke checks above.

```markdown
### Runtime Entity Identity Accountability

- Runtime identity is now `configured alias -> actual persisted Matrix ID`.
- Generated usernames are used only for provisioning missing accounts and config-time collision validation.
- Fresh startup prepares Matrix accounts before runtime bot construction.
- Friendly aliases such as `@code` and `@ops` resolve to actual Matrix IDs.
- Bare generated-looking localparts and bare actual usernames do not resolve as aliases.
- Full remote Matrix IDs remain literal.
- Duplicate managed Matrix IDs fail fast.
- The final grep and test gates found no runtime generated-ID construction.
- Focused identity, startup, mention, routing, scheduling, topic, voice, team, conversation, and provisioning tests passed.
- Tach, pre-commit, import smoke, whitespace, and local Matrix smoke checks passed.
```

## External Reviewer Prompt

Use this prompt only if another feedback pass is explicitly requested.

```text
You are reviewing this implementation plan, not implementing it.
Plan file: docs/superpowers/plans/2026-05-10-runtime-entity-matrix-identity.md
Repository: MindRoom.

Context:
Runtime identity must be only configured alias -> actual persisted Matrix ID.
Generated Matrix usernames are proposed usernames for provisioning missing accounts and config-time collision validation only.
They must not be runtime identities.

Review goal:
Find blocking design flaws, missing affected modules, weak invariants, overreach, and missing accountability gates.
Assume there will be no second feedback round, so prioritize only high-signal issues.

Please answer in this format:

1. Blocking design issues
List only issues that would make this refactor incomplete or unsafe.
For each issue, cite the plan section and the concrete code path or test category it misses.

2. Overreach or unnecessary work
List plan items that add complexity without enforcing the invariant.

3. Missing accountability gates
List grep checks, tests, docs, or boundary checks that should be added before implementation is called complete.

4. Verdict
Use exactly one of:
READY
READY WITH SMALL EDITS
NEEDS DESIGN CHANGE

Do not rewrite the plan.
Do not propose compatibility shims.
Do not ask for a second review round.
```

## Self-Review

Spec coverage:

- The corrected plan covers the startup account-preparation barrier.
- The corrected plan preserves config-time proposed username collision checks.
- The corrected plan removes generated-looking localparts and bare actual usernames from alias resolution.
- The corrected plan covers duplicated actual Matrix IDs.
- The corrected plan covers wrapper trust helpers and omitted runtime modules.
- The corrected plan includes hard grep gates to stop incorrect examples from multiplying.

Placeholder scan:

- The plan contains no unresolved placeholder tokens or vague implementation instructions.
- Steps that change code include concrete code shapes.
- Commands include expected results.

Type consistency:

- Runtime identity is consistently named `EntityIdentityRegistry`.
- Config keys are consistently named `alias`.
- Actual Matrix identity is consistently represented as `MatrixID`.
- Proposed username generation is scoped to `_proposed_username_for_new_entity(...)`.

## Execution Choice

Plan complete and saved to `docs/superpowers/plans/2026-05-10-runtime-entity-matrix-identity.md`.

Two execution options:

1. Subagent-Driven.
This dispatches a fresh subagent per task, reviews between tasks, and keeps iteration tight.

2. Inline Execution.
This executes tasks in this session using checkpoints.
