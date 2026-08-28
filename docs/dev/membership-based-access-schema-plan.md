# Membership-Based Access Schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use baspowers:subagent-driven-development (recommended) or baspowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace overlapping room, responder, invitation, and credential policies with a membership-based access schema whose fields each have one user-visible purpose.

**Architecture:** Existing configurations remain on the legacy authorization path. Configurations opt into the new path with `access_model: room_membership`; rooms own membership and Matrix state, responders own conversation access, and agents own credential managers. Runtime resolvers produce effective immutable policies so ingress, room reconciliation, and credential APIs consume one interpretation instead of reimplementing inheritance.

**Tech Stack:** Python 3.13, Pydantic v2, Matrix/Nio, Typer, pytest, YAML

**Spec:** `docs/dev/membership-based-access-schema-plan.md#design`

## Global Constraints

- Existing configurations that omit `access_model` retain their current behavior.
- `access_model: room_membership` rejects overlapping legacy access fields instead of guessing precedence.
- Room-specific values replace room defaults; list values are never implicitly combined.
- `invite_users` controls invitations only and never grants administrative or credential authority.
- Platform administrators are not automatically invited and do not receive Matrix room power.
- Matrix room administrators are not automatically invited and do not become platform administrators.
- Responder access never grants credential or OAuth management.
- Credential managers do not gain conversation access unless a separate responder access rule allows it.
- Current authoritative Matrix membership, not an invitation or cached historical membership, satisfies membership grants.
- Unknown room keys, duplicate users, unresolved managed rooms, and unavailable membership state fail closed.
- Legacy identity aliases and bot-account classification remain supported and are not redesigned here.
- External-trigger authentication and tool-approval policy are out of scope.

---

## Design

### Problem

The current authored model combines several unrelated decisions:

- `authorization.room_permissions` is both a room-level sender allowlist and an invitation source.
- `authorization.default_room_access` changes the meaning of rooms missing from that map.
- `authorization.agent_reply_permissions` adds a second responder-specific gate after room authorization.
- Static `agent_reply_permissions.<entity>.users` entries also authorize agent credential management.
- `matrix_room_access` owns global room defaults while `rooms.<key>` owns only some room-specific overrides.

Because both the room gate and responder gate must pass, a current room member can satisfy the responder membership policy and still be rejected by the room allowlist. Conversely, using authorization lists as invitation sources makes changes intended for conversation access mutate room membership.

### User-facing schema

```yaml
access_model: room_membership

administrators:
  - "@owner:example.com"

room_defaults:
  join_policy: invite        # invite, knock, or public
  listed: false
  encrypted: false
  invite_users: []
  admins: []                 # Matrix room administrators

rooms:
  talent:
    display_name: Talent
    description: Shared Talent agent room
    invite_users:
      - "@talent-owner:example.com"
    admins:
      - "@talent-owner:example.com"

agents:
  talent:
    rooms:
      - talent
    credential_managers:
      - "@talent-owner:example.com"
```

The common case has no authored responder `access` block. An agent or team defaults to membership in its configured `rooms`; the router defaults to membership in the current room.

An exceptional policy is explicit:

```yaml
agents:
  specialist:
    rooms:
      - specialist
    access:
      current_room_members: false
      members_of_rooms:
        - specialist-core
      users:
        - "@external-adviser:example.com"
    credential_managers:
      - "@specialist-owner:example.com"
```

The three access clauses are additive within one responder policy:

1. `current_room_members: true` allows the current authoritative members of the room receiving the message.
2. `members_of_rooms` allows current members of the named managed grant rooms, including when the responder is invoked elsewhere.
3. `users` allows canonical Matrix user IDs or existing supported user patterns.

Administrators and internal system identities bypass responder conversation policies, but administrators still need Matrix membership to send a Matrix event in the first place.

### Room inheritance

`room_defaults` provides desired state for every managed room, including rooms created implicitly by an agent or team assignment. Each field on `rooms.<key>` is an override:

- Omitted field: inherit the corresponding default.
- Scalar value: replace the default.
- List value: replace the default list completely.
- Empty list: explicitly configure no users.

For example:

```yaml
room_defaults:
  invite_users:
    - "@default-user:example.com"

rooms:
  project:
    invite_users:
      - "@project-owner:example.com"
  isolated:
    invite_users: []
```

Only the project owner is invited to `project`; the default user is not combined with the override. Nobody is automatically invited to `isolated`.

`invite_users` is declarative desired invitation state. A listed user who leaves or is kicked is invited again on reconciliation; remove the user from configuration before intentionally removing their access.

### Separation of authority

| Field | Sole responsibility |
| --- | --- |
| `administrators` | Platform-wide configuration and credential authority plus responder-policy bypass |
| `room_defaults` | Default desired Matrix room state |
| `rooms.<key>.invite_users` | Automatic invitations for one room |
| `rooms.<key>.admins` | Matrix power level for one room |
| `agents.<name>.rooms` / `teams.<name>.rooms` | Rooms where a responder is configured to operate |
| `<responder>.access` | Who may use that responder |
| `agents.<name>.credential_managers` | Who may manage that agent's credentials and OAuth connections |

No field in this table implies another row.

### Effective policy resolution

Runtime code consumes two frozen resolved objects:

```python
@dataclass(frozen=True)
class EffectiveRoomPolicy:
    join_policy: Literal["invite", "knock", "public"]
    listed: bool
    encrypted: bool
    invite_users: tuple[str, ...]
    admins: tuple[str, ...]


@dataclass(frozen=True)
class EffectiveResponderAccess:
    current_room_members: bool
    members_of_rooms: tuple[str, ...]
    users: tuple[str, ...]
```

`resolve_room_policy(config, room_key)` applies replacement inheritance once. `resolve_responder_access(config, entity_name)` applies responder defaults once. Callers never merge lists or reinterpret omitted values.

Responder defaults are:

- Agent: `members_of_rooms` is the agent's configured `rooms`.
- Team: `members_of_rooms` is the team's configured `rooms`.
- Router: `current_room_members` is `true`.
- Explicit `access.members_of_rooms: []` disables the inferred room grants.
- Explicit `access.users: []` grants no static users.

### Conversation authorization

In `room_membership` mode, the legacy room-level sender allowlist is removed from ingress. Matrix admission already establishes that an event sender was able to send into the current room. The selected responder then applies one authoritative policy:

1. Resolve aliases to a canonical requester.
2. Allow internal system identities.
3. Allow platform administrators.
4. Match static responder users.
5. Query the authoritative membership index for current-room and managed grant-room clauses.
6. Deny when no clause matches or membership state is unavailable.

Commands with administrative effects check `administrators`; ordinary responder commands use the same responder policy as messages, media, calls, reactions, delegated runs, and schedules.

### Invitations and Matrix state

Room reconciliation consumes only `EffectiveRoomPolicy`:

- Set the configured join policy and directory visibility.
- Enable encryption when requested; never disable encryption.
- Invite `invite_users` for that room.
- Grant Matrix power to `admins` after membership exists.
- Invite configured bots through the existing responder-room assignment path.

Neither `administrators`, responder `access.users`, nor `credential_managers` participates in room invitations.

Membership mode is declarative for existing managed rooms. It does not consult the legacy `matrix_room_access.reconcile_existing_rooms` opt-in before applying reversible join-policy, directory-visibility, invitation, or power-level changes. Encryption remains one-way: reconciliation may enable it but never disable it.

### Credential management

Agent credential and OAuth endpoints authorize only:

- a platform administrator, or
- a canonical user listed in `agents.<name>.credential_managers`.

Room membership and responder access do not authorize credential operations. Alias resolution remains supported before exact manager matching. Credential-manager entries must be concrete Matrix user IDs; wildcard credential administrators are not accepted.

### Compatibility

The transition has two explicit modes:

- `access_model` omitted: parse and execute the current legacy fields unchanged.
- `access_model: room_membership`: execute only the new access model.

In membership mode, configuration validation rejects authored non-default values for these overlapping legacy fields:

- `authorization.global_users`
- `authorization.room_permissions`
- `authorization.default_room_access`
- `authorization.agent_reply_permissions`
- `matrix_room_access.mode`
- `matrix_room_access.multi_user_join_rule`
- `matrix_room_access.publish_to_room_directory`
- `matrix_room_access.invite_only_rooms`
- `matrix_room_access.encrypt_managed_rooms`
- `matrix_room_access.room_admins`

Non-overlapping legacy fields such as alias resolution, bot-account classification, and independent feature flags remain valid. Documentation shows a manual migration table because the old fields intentionally combined capabilities that cannot always be split safely without operator intent.

### Non-goals

- Automatically kick room members omitted from `invite_users`.
- Automatically invite platform administrators or credential managers.
- Automatically promote invitees to Matrix room admins.
- Infer credential managers from responder access.
- Redesign identity aliases, bridge bot classification, external triggers, or tool approval.
- Automatically rewrite a legacy configuration whose overloaded entries have ambiguous intent.

---

### Task 1: Add the authored schema and effective policy resolvers

**Files:**
- Create: `src/mindroom/config/access.py`
- Create: `src/mindroom/access_policy.py`
- Create: `tests/access_schema_support.py`
- Modify: `src/mindroom/config/agent.py:176-455`
- Modify: `src/mindroom/config/models.py:599-690`
- Modify: `src/mindroom/config/main.py:397-470`
- Test: `tests/test_access_schema.py`

**Interfaces:**
- Produces: `RoomDefaultsConfig`, `ResponderAccessConfig`, `EffectiveRoomPolicy`, `EffectiveResponderAccess`, `resolve_room_policy(config, room_key)`, and `resolve_responder_access(config, entity_name)`.
- Consumes: existing `AgentConfig.rooms`, `TeamConfig.rooms`, `RouterConfig`, `RoomConfig`, and managed-room key validation.
- Produces these test-only helpers for later tasks:
  - `membership_config(*, administrators=(), room_defaults=None, rooms=None, agent_rooms=(), access=None, credential_managers=()) -> Config`
  - `membership_index(config, memberships_by_room_key) -> Awaitable[AgentReplyMembershipIndex]`
  - `unresolved_membership_index(config, room_key) -> AgentReplyMembershipIndex`
  - `reconcile_invites(config, tmp_path) -> Awaitable[set[tuple[str, str]]]`

- [ ] **Step 1: Write parsing, inheritance, and validation tests**

```python
def test_room_list_override_replaces_default() -> None:
    config = Config.model_validate(
        {
            "access_model": "room_membership",
            "room_defaults": {"invite_users": ["@default:example.com"]},
            "rooms": {"project": {"invite_users": ["@owner:example.com"]}},
        }
    )

    policy = resolve_room_policy(config, "project")

    assert policy.invite_users == ("@owner:example.com",)


def test_omitted_agent_access_uses_configured_rooms() -> None:
    config = Config.model_validate(
        {
            "access_model": "room_membership",
            "agents": {
                "research": {
                    "display_name": "Research",
                    "role": "Research assistant",
                    "rooms": ["research"],
                }
            },
        }
    )

    access = resolve_responder_access(config, "research")

    assert access.members_of_rooms == ("research",)
    assert access.current_room_members is False
```

- [ ] **Step 2: Run the focused tests and confirm the new fields are rejected**

Run: `uv run pytest -q tests/test_access_schema.py`

Expected: FAIL because `access_model`, `room_defaults`, responder `access`, and `credential_managers` do not exist.

- [ ] **Step 3: Implement the Pydantic models and frozen resolvers**

```python
class ResponderAccessConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_room_members: bool = False
    members_of_rooms: list[str] | None = None
    users: list[str] = Field(default_factory=list)


class RoomDefaultsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    join_policy: Literal["invite", "knock", "public"] = "invite"
    listed: bool = False
    encrypted: bool = False
    invite_users: list[str] = Field(default_factory=list)
    admins: list[str] = Field(default_factory=list)
```

Add optional room overrides to `RoomConfig`, optional `access` to agents, teams, and router, `credential_managers` to agents, and top-level `access_model`, `administrators`, and `room_defaults` to `Config`. Validate duplicate entries, concrete administrator and credential-manager IDs, and known managed-room keys.

- [ ] **Step 4: Run focused model tests and formatting**

Run: `uv run pytest -q tests/test_access_schema.py tests/test_config_validation_helpers.py`

Expected: PASS.

Run: `uv run ruff check src/mindroom/config/access.py src/mindroom/access_policy.py src/mindroom/config/agent.py src/mindroom/config/models.py src/mindroom/config/main.py tests/test_access_schema.py`

Expected: PASS.

- [ ] **Step 5: Commit the schema foundation**

```bash
git add src/mindroom/config/access.py src/mindroom/access_policy.py src/mindroom/config/agent.py src/mindroom/config/models.py src/mindroom/config/main.py tests/access_schema_support.py tests/test_access_schema.py
git commit -m "feat: add membership access schema"
```

### Task 2: Reconcile room state from effective room policy

**Files:**
- Modify: `src/mindroom/matrix/rooms.py:80-220`
- Modify: `src/mindroom/orchestration/rooms.py:1-65`
- Modify: `src/mindroom/orchestrator.py:2079-2150`
- Test: `tests/test_matrix_room_access.py`
- Test: `tests/test_orchestrator_runtime.py`

**Interfaces:**
- Consumes: `resolve_room_policy(config, room_key) -> EffectiveRoomPolicy` from Task 1.
- Produces: room reconciliation whose invitations, power levels, join rules, visibility, and encryption come from one resolved room policy.

- [ ] **Step 1: Add replacement-inheritance and invitation-isolation regressions**

```python
@pytest.mark.asyncio
async def test_membership_schema_invites_only_effective_room_invite_users(tmp_path: Path) -> None:
    config = membership_config(
        room_defaults={"invite_users": ["@default:localhost"]},
        rooms={
            "one": {"invite_users": ["@one:localhost"]},
            "two": {"invite_users": []},
        },
    )

    invited = await reconcile_invites(config, tmp_path)

    assert invited == {("one", "@one:localhost")}
```

Add assertions proving that administrators, room admins, responder users, and credential managers are not invitation candidates. Add an existing-room regression with legacy `reconcile_existing_rooms: false` that proves membership-mode join policy and visibility are still reconciled.

- [ ] **Step 2: Run the regressions and verify legacy invitation behavior cannot satisfy them**

Run: `uv run pytest -q tests/test_matrix_room_access.py tests/test_orchestrator_runtime.py -k 'membership_schema or effective_room'`

Expected: FAIL because room reconciliation still consumes `matrix_room_access` and legacy authorization candidates.

- [ ] **Step 3: Route membership-mode reconciliation through `EffectiveRoomPolicy`**

Update `ensure_all_rooms_exist` to resolve each managed room key before applying state. Replace membership-mode calls to `get_authorized_user_ids_to_invite` with the effective `invite_users` tuple for that room. Keep the legacy branch unchanged when `access_model` is omitted.

In membership mode, do not gate existing-room desired state on `matrix_room_access.reconcile_existing_rooms`. Retain the legacy guard only for legacy mode. Preserve the irreversible encryption check in both modes.

Do not call `is_authorized_sender` for membership-mode invitees; the invitation roster is intentionally independent from conversation access.

- [ ] **Step 4: Run room and orchestrator suites**

Run: `uv run pytest -q tests/test_matrix_room_access.py tests/test_room_invites.py tests/test_orchestrator_runtime.py`

Expected: PASS.

- [ ] **Step 5: Commit room reconciliation**

```bash
git add src/mindroom/matrix/rooms.py src/mindroom/orchestration/rooms.py src/mindroom/orchestrator.py tests/test_matrix_room_access.py tests/test_orchestrator_runtime.py
git commit -m "feat: reconcile membership room policy"
```

### Task 3: Make responder membership the conversation authorization boundary

**Files:**
- Modify: `src/mindroom/authorization.py:94-230`
- Modify: `src/mindroom/agent_reply_membership.py:1-410`
- Modify: `src/mindroom/approval_inbound.py:70-105`
- Modify: `src/mindroom/reaction_dispatch.py:60-205`
- Modify: `src/mindroom/api/external_triggers.py:240-265`
- Modify: `src/mindroom/orchestration/script_runtime.py:175-210`
- Modify: `src/mindroom/custom_tools/delegate.py:105-130`
- Modify: `src/mindroom/custom_tools/attachment_helpers.py:25-50`
- Modify: `src/mindroom/matrix_rtc/call_manager.py:625-650`
- Modify: `src/mindroom/visible_voice_echo.py:270-310,495-515`
- Modify: `src/mindroom/bot_room_lifecycle.py:325-415`
- Modify: `src/mindroom/bot.py:2750-2780`
- Modify: `src/mindroom/thread_utils.py:330-355`
- Modify: `src/mindroom/ingress_validation.py:260-325`
- Modify: `src/mindroom/turn_policy.py`
- Test: `tests/test_authorization.py`
- Test: `tests/test_bot_reactions_approvals.py`
- Test: `tests/api/test_external_triggers_api.py`
- Test: `tests/test_script_runtime_lifecycle.py`
- Test: `tests/test_delegate_tools.py`
- Test: `tests/test_attachments_tool.py`
- Test: `tests/test_matrix_rtc_call_manager.py`
- Test: `tests/test_visible_voice_echo.py`
- Test: `tests/test_turn_policy.py`
- Test: `tests/test_edit_response_regeneration.py`

**Interfaces:**
- Consumes: `resolve_responder_access(config, entity_name) -> EffectiveResponderAccess` from Task 1 and the existing authoritative membership index.
- Produces: `is_sender_allowed_for_responder(sender_id, entity_name, room_id, config, runtime_paths, membership_index) -> bool`.

- [ ] **Step 1: Write two-gate elimination and fail-closed tests**

```python
def test_membership_mode_does_not_apply_legacy_room_gate() -> None:
    config = membership_config(agent_rooms=["talent"])
    memberships = membership_index({"talent": {"@member:example.com"}})

    assert is_sender_allowed_for_responder(
        "@member:example.com",
        "talent",
        "!talent:example.com",
        config,
        runtime_paths_for(config),
        memberships,
    )


def test_membership_mode_fails_closed_when_grant_room_is_unresolved() -> None:
    config = membership_config(agent_rooms=["talent"])
    memberships = unresolved_membership_index("talent")

    assert not is_sender_allowed_for_responder(
        "@member:example.com",
        "talent",
        "!other:example.com",
        config,
        runtime_paths_for(config),
        memberships,
    )
```

Cover text, media, approval reactions, Matrix RTC calls, external triggers, background scripts, delegation, attachment access, visible voice echoes, room lifecycle responses, and scheduled-resume entry points with membership-mode regressions. Each regression must prove a current grant-room member is accepted and an unresolved membership snapshot is denied.

- [ ] **Step 2: Run focused tests and confirm the legacy precheck rejects the member**

Run: `uv run pytest -q tests/test_authorization.py tests/test_turn_policy.py -k 'membership_mode'`

Expected: FAIL because `is_authorized_sender` remains an independent room gate.

- [ ] **Step 3: Implement one membership-mode responder gate**

Keep `is_authorized_sender` unchanged for legacy mode. In membership mode, remove that legacy room allowlist from ingress and require every selected responder to pass `is_sender_allowed_for_responder`. Make the existing `is_sender_allowed_for_agent_reply` and `is_sender_allowed_for_agent_reply_in_room` entry points delegate to the new resolver in membership mode so secondary callers cannot retain legacy semantics accidentally. Reuse the current authoritative membership snapshot; do not issue a Matrix request per candidate or per event.

Apply aliases before static and administrator matching. Preserve internal-system bypasses. Treat missing, stale, or unresolved membership snapshots as denial for membership clauses.

Replace standalone `is_authorized_sender` calls at approval continuation, attachment, and room-lifecycle boundaries with context-specific membership-mode checks. Tool-approval policy remains unchanged, but the human acting on an approval must still pass the responder policy captured by that continuation before it is consumed.

- [ ] **Step 4: Run authorization and ingress suites**

Run: `uv run pytest -q tests/test_authorization.py tests/test_turn_policy.py tests/test_edit_response_regeneration.py tests/test_voice_command_processing.py tests/test_routing_regression.py tests/test_bot_reactions_approvals.py tests/api/test_external_triggers_api.py tests/test_script_runtime_lifecycle.py tests/test_delegate_tools.py tests/test_attachments_tool.py tests/test_matrix_rtc_call_manager.py tests/test_visible_voice_echo.py`

Expected: PASS.

- [ ] **Step 5: Commit responder authorization**

```bash
git add src/mindroom/authorization.py src/mindroom/agent_reply_membership.py src/mindroom/approval_inbound.py src/mindroom/reaction_dispatch.py src/mindroom/api/external_triggers.py src/mindroom/orchestration/script_runtime.py src/mindroom/custom_tools/delegate.py src/mindroom/custom_tools/attachment_helpers.py src/mindroom/matrix_rtc/call_manager.py src/mindroom/visible_voice_echo.py src/mindroom/bot_room_lifecycle.py src/mindroom/bot.py src/mindroom/thread_utils.py src/mindroom/ingress_validation.py src/mindroom/turn_policy.py tests/test_authorization.py tests/test_bot_reactions_approvals.py tests/api/test_external_triggers_api.py tests/test_script_runtime_lifecycle.py tests/test_delegate_tools.py tests/test_attachments_tool.py tests/test_matrix_rtc_call_manager.py tests/test_visible_voice_echo.py tests/test_turn_policy.py tests/test_edit_response_regeneration.py tests/test_voice_command_processing.py tests/test_routing_regression.py
git commit -m "feat: authorize responders by room membership"
```

### Task 4: Separate platform and credential authority

**Files:**
- Modify: `src/mindroom/authorization.py:210-230`
- Modify: `src/mindroom/api/dashboard_credential_scope.py:210-250`
- Modify: `src/mindroom/commands/handler.py:300-425`
- Modify: `src/mindroom/commands/config_confirmation.py:510-545`
- Modify: `src/mindroom/commands/parsing.py:325-415`
- Modify: `src/mindroom/custom_tools/config_manager.py:330-370`
- Modify: `src/mindroom/custom_tools/usage_stats.py:60-90`
- Modify: `src/mindroom/oauth/reset.py:70-105`
- Modify: `src/mindroom/commands/config_commands.py`
- Modify: `src/mindroom/orchestrator.py:1835-1855`
- Test: `tests/test_authorization.py`
- Test: `tests/api/test_dashboard_credential_scope.py`
- Test: `tests/test_oauth_connection_tools.py`
- Test: `tests/test_commands.py`
- Test: `tests/test_config_commands.py`
- Test: `tests/test_bot_reactions_approvals.py`
- Test: `tests/test_usage_stats_tool.py`
- Test: `tests/test_config_reload.py`

**Interfaces:**
- Consumes: top-level `administrators` and `AgentConfig.credential_managers` from Task 1.
- Produces: `is_platform_administrator(sender_id, config) -> bool` and membership-mode `is_sender_allowed_for_agent_credential_management(sender_id, agent_name, config) -> bool`.

- [ ] **Step 1: Write authority-separation tests**

```python
def test_room_member_cannot_manage_agent_credentials() -> None:
    config = membership_config(
        agent_rooms=["talent"],
        credential_managers=["@manager:example.com"],
    )

    assert not is_sender_allowed_for_agent_credential_management(
        "@member:example.com",
        "talent",
        config,
    )


def test_administrator_manages_credentials_without_being_an_invitee() -> None:
    config = membership_config(administrators=["@admin:example.com"])

    assert is_sender_allowed_for_agent_credential_management(
        "@admin:example.com",
        "talent",
        config,
    )
    assert "@admin:example.com" not in resolve_room_policy(config, "talent").invite_users
```

Add tests proving a credential manager does not bypass responder access and a Matrix room admin is neither a platform administrator nor a credential manager.

- [ ] **Step 2: Run focused authority tests and confirm current static reply users conflate them**

Run: `uv run pytest -q tests/test_authorization.py tests/api/test_dashboard_credential_scope.py tests/test_oauth_connection_tools.py -k 'credential_manager or platform_administrator'`

Expected: FAIL because credential management still reads static reply users.

- [ ] **Step 3: Implement separate administrator and credential checks**

In membership mode, authorize agent credential management only when the canonical requester is in `administrators` or the target agent's concrete `credential_managers`. Keep the legacy static-reply-user path unchanged in legacy mode.

Route plugin reload, configuration commands, configuration-confirmation reactions, administrative usage statistics, config-manager operations, and related config-reload reporting through `is_platform_administrator` while preserving their existing feature-enable flags. Update command help text to name platform administrators rather than legacy global users. Do not make administrators room members, Matrix room admins, or invite candidates.

- [ ] **Step 4: Run credential and command suites**

Run: `uv run pytest -q tests/test_authorization.py tests/api/test_dashboard_credential_scope.py tests/test_oauth_connection_tools.py tests/test_commands.py tests/test_config_commands.py tests/test_bot_reactions_approvals.py tests/test_usage_stats_tool.py tests/test_config_reload.py tests/api/test_oauth_api.py`

Expected: PASS.

- [ ] **Step 5: Commit authority separation**

```bash
git add src/mindroom/authorization.py src/mindroom/api/dashboard_credential_scope.py src/mindroom/commands/handler.py src/mindroom/commands/config_confirmation.py src/mindroom/commands/parsing.py src/mindroom/custom_tools/config_manager.py src/mindroom/custom_tools/usage_stats.py src/mindroom/oauth/reset.py src/mindroom/commands/config_commands.py src/mindroom/orchestrator.py tests/test_authorization.py tests/api/test_dashboard_credential_scope.py tests/test_oauth_connection_tools.py tests/test_commands.py tests/test_config_commands.py tests/test_bot_reactions_approvals.py tests/test_usage_stats_tool.py tests/test_config_reload.py tests/api/test_oauth_api.py
git commit -m "feat: separate credential and platform authority"
```

### Task 5: Enforce compatibility boundaries and document migration

**Files:**
- Modify: `src/mindroom/config/main.py:634-670`
- Modify: `src/mindroom/cli/config.py`
- Modify: `tests/test_cli_config.py`
- Modify: `README.md`
- Modify: `config.yaml`
- Modify: `docs/authorization.md`
- Modify: `docs/chat-commands.md`
- Modify: `docs/configuration/index.md`
- Modify: `docs/dashboard.md`
- Modify: `docs/dev/agent_configuration.md`
- Modify: `docs/matrix-space.md`
- Modify: `docs/oauth-framework.md`
- Modify: `docs/tools/agent-orchestration.md`
- Test: `tests/test_access_schema.py`

**Interfaces:**
- Consumes: all new fields and legacy models from Tasks 1-4.
- Produces: a config-level compatibility validator and `mindroom config explain-access` migration report.

- [ ] **Step 1: Write mixed-mode rejection and migration-report tests**

```python
def test_membership_mode_rejects_legacy_room_permissions() -> None:
    with pytest.raises(ValidationError, match="room_permissions"):
        Config.model_validate(
            {
                "access_model": "room_membership",
                "authorization": {
                    "room_permissions": {"talent": ["@owner:example.com"]},
                },
            }
        )


def test_legacy_mode_remains_unchanged() -> None:
    config = Config.model_validate(
        {
            "authorization": {
                "default_room_access": False,
                "room_permissions": {"talent": ["@owner:example.com"]},
            }
        }
    )

    assert config.access_model is None
    assert is_authorized_sender(
        "@owner:example.com",
        config,
        "#talent:example.com",
        runtime_paths_for(config),
    )
```

CLI tests must assert that the report separates invitation intent, conversation access, Matrix power, and credential authority instead of generating an unsafe automatic rewrite.

- [ ] **Step 2: Run compatibility tests and verify mixed fields currently parse**

Run: `uv run pytest -q tests/test_access_schema.py tests/test_cli_config.py -k 'membership_mode or explain_access or legacy_mode'`

Expected: FAIL because compatibility validation and the report command do not exist.

- [ ] **Step 3: Add the validator, report command, and migration documentation**

The validation error must list every conflicting path and explain that old overloaded entries require an operator to decide separately:

```text
authorization.room_permissions.talent must be split into rooms.talent.invite_users and responder access; MindRoom cannot infer whether every listed user should receive both capabilities
```

`mindroom config explain-access` is read-only. It prints the effective legacy behavior and a skeleton membership-mode configuration but never writes configuration or assumes that global users are administrators.

Document the new model first, followed by a legacy compatibility section and a field-by-field manual migration table.

- [ ] **Step 4: Run CLI, config, and documentation checks**

Run: `uv run pytest -q tests/test_access_schema.py tests/test_cli_config.py tests/test_config_commands.py`

Expected: PASS.

Run: `uv run pre-commit run --files src/mindroom/config/main.py src/mindroom/cli/config.py tests/test_cli_config.py docs/authorization.md docs/configuration/index.md docs/dev/agent_configuration.md`

Expected: PASS.

- [ ] **Step 5: Commit compatibility and documentation**

```bash
git add src/mindroom/config/main.py src/mindroom/cli/config.py tests/test_cli_config.py tests/test_access_schema.py README.md config.yaml docs/authorization.md docs/chat-commands.md docs/configuration/index.md docs/dashboard.md docs/dev/agent_configuration.md docs/matrix-space.md docs/oauth-framework.md docs/tools/agent-orchestration.md
git commit -m "docs: add membership access migration"
```

### Task 6: Verify the complete access model

**Files:**
- Modify only files needed to correct failures found by the commands below.

**Interfaces:**
- Consumes: the complete schema, policy resolvers, room reconciliation, responder authorization, and credential authorization from Tasks 1-5.
- Produces: one verified implementation with no legacy regression and no new policy ambiguity.

- [ ] **Step 1: Run the complete access-focused test set**

Run:

```bash
uv run pytest -q \
  tests/test_access_schema.py \
  tests/test_authorization.py \
  tests/test_matrix_room_access.py \
  tests/test_room_invites.py \
  tests/test_orchestrator_runtime.py \
  tests/test_turn_policy.py \
  tests/test_routing_regression.py \
  tests/test_edit_response_regeneration.py \
  tests/test_voice_command_processing.py \
  tests/test_bot_reactions_approvals.py \
  tests/api/test_external_triggers_api.py \
  tests/test_script_runtime_lifecycle.py \
  tests/test_delegate_tools.py \
  tests/test_attachments_tool.py \
  tests/test_matrix_rtc_call_manager.py \
  tests/test_visible_voice_echo.py \
  tests/api/test_dashboard_credential_scope.py \
  tests/test_oauth_connection_tools.py \
  tests/test_commands.py \
  tests/test_config_commands.py \
  tests/test_usage_stats_tool.py \
  tests/test_config_reload.py \
  tests/test_cli_config.py
```

Expected: PASS.

- [ ] **Step 2: Run repository architecture and quality gates**

Run: `uv run pre-commit run --all-files`

Expected: PASS.

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest -q`

Expected: PASS.

- [ ] **Step 4: Review authored examples for one-purpose fields**

Search:

```bash
rg -n "global_users|room_permissions|default_room_access|agent_reply_permissions|invite_users|credential_managers|administrators" README.md config.yaml docs local cluster
```

Every primary example must use the membership model. Legacy fields may appear only in explicitly labeled compatibility or migration sections.

- [ ] **Step 5: Confirm verification left no uncommitted corrections**

```bash
git status --short
```

Expected: no output. If a verification command required a correction, return to the task that owns that file, rerun that task's focused tests, and commit the correction with that task rather than creating an unscoped cleanup commit.
