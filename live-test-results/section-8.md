# Section 8: Authorization And Room Access Policy

Tested: 2026-03-19
Environment: nix-shell, MINDROOM_NAMESPACE=tests8, API port 9877, Matrix localhost:8108, model apriel-thinker:15b at LOCAL_MODEL_HOST:9292/v1
Source anchors: `src/mindroom/authorization.py`, `src/mindroom/config/auth.py`, `src/mindroom/config/matrix.py`, `src/mindroom/bot.py`, `src/mindroom/voice_handler.py`

## Test Users

| User | Matrix ID | Role |
|------|-----------|------|
| Owner (global) | `@tests8_owner:localhost` | In `authorization.global_users` |
| Denied | `@tests8_denied:localhost` | Not in any permission list |
| Room User | `@tests8_roomuser:localhost` | In `room_permissions.restricted` |
| Bridge | `@tests8_bridge:localhost` | Alias for `@tests8_owner:localhost` |
| Bot Account | `@tests8_botacct:localhost` | In `bot_accounts` config |

## Test Rooms

| Room | ID | Agents |
|------|----|--------|
| Lobby | `!PHzefThWhXpIHifkPN:localhost` | general, restricted_agent |
| Restricted | `!dAXOKdgnqnNbEdEHZo:localhost` | general, restricted_agent |
| Open | `!IxWdyXUtTskauMRCHC:localhost` | general |

## Configuration

```yaml
authorization:
  default_room_access: false
  global_users: ["@tests8_owner:localhost"]
  room_permissions:
    restricted: ["@tests8_roomuser:localhost", "@tests8_botacct:localhost"]
  aliases:
    "@tests8_owner:localhost": ["@tests8_bridge:localhost"]
  agent_reply_permissions:
    "*": ["@tests8_owner:localhost"]
    restricted_agent: ["@tests8_roomuser:localhost"]
bot_accounts: ["@tests8_botacct:localhost"]
```

## Results

### AUTH-001: Global user access across rooms -- PASS

**Expected**: Global user can interact across managed rooms without room-specific entries.

**Test**: `@tests8_owner:localhost` (in `global_users`) sent messages to lobby, restricted, and open rooms.

**Result**: All three rooms created response threads. The agent attempted to generate a response in each room (failed at AI model layer due to test API key, but authorization passed).

- Lobby thread t35: general agent replied
- Restricted thread t20: general agent replied
- Open thread t37: general agent replied

**Evidence**: `evidence/logs/mindroom_full.log` -- "Sent response" entries for all three rooms from owner.

### AUTH-002: Room permissions by room ID, full alias, and managed room key -- PASS

**Expected**: Each identifier format matches the same access rule and does not fall through unexpectedly.

**Test**: Three sequential hot-reload tests with `room_permissions` keyed differently:

1. **Managed room key** (`restricted`): `@tests8_roomuser:localhost` sent to restricted room -> thread t21 created, restricted_agent replied. Sent to lobby (no permission) -> no thread, silently ignored.
2. **Full alias** (`#restricted_tests8:localhost`): Hot-reloaded config. `@tests8_roomuser:localhost` sent to restricted room -> thread t45 created, restricted_agent replied.
3. **Room ID** (`!dAXOKdgnqnNbEdEHZo:localhost`): Hot-reloaded config. `@tests8_roomuser:localhost` sent to restricted room -> thread t47 created, restricted_agent replied (showed "Thinking..." streaming indicator).

All three key formats resolved to the same access rule.

**Evidence**: Matty thread output showing threads t21, t45, t47 in Restricted room; lobby messages showing roomuser's message (m5) with no reply thread.

### AUTH-003: Room not in room_permissions controlled by default_room_access -- PASS

**Expected**: Access is controlled solely by `default_room_access` for unconfigured rooms.

**Test**: `@tests8_denied:localhost` (not in `global_users`, no `room_permissions` for lobby) sent to lobby room with `default_room_access: false`.

**Result**: Message m4 visible in lobby but NO response thread created. Agent silently ignored the message.

**Evidence**: Matty lobby messages output showing m4 (`@tests8_denied:localhost`) with no thread indicator, while m2 (`@tests8_owner:localhost`) has thread t35.

### AUTH-004: Bridge alias resolution -- PASS

**Expected**: Alias mapping resolves to the canonical user ID before room access and reply-permission checks.

**Test**: `@tests8_bridge:localhost` (alias for `@tests8_owner:localhost` via `authorization.aliases`) sent to lobby room. The bridge user is NOT in `global_users` directly, but its alias resolves to the global user.

**Result**: Thread t36 created in lobby with general agent reply attempt. The bridge identity was resolved to the canonical owner ID before permission checks.

**Evidence**: Matty lobby messages showing m6/t36 (`@tests8_bridge:localhost`) with thread reply from `@mindroom_general_tests8:localhost`.

### AUTH-005: Agent reply permissions with wildcard and per-agent entries -- PASS

**Expected**: Default reply rules and explicit per-entity overrides both enforce exactly as configured.

**Test configuration**:
- `"*"` (default): allows `@tests8_owner:localhost`
- `restricted_agent`: allows `@tests8_roomuser:localhost`

**Test cases**:

1. **t22**: `@tests8_roomuser:localhost` -> `@mindroom_restricted_agent_tests8` in restricted room.
   Result: restricted_agent replied (roomuser in restricted_agent's list). CORRECT.

2. **t23**: `@tests8_owner:localhost` -> `@mindroom_restricted_agent_tests8` in restricted room.
   Result: general agent replied (owner in `*` list), restricted_agent did NOT reply (owner NOT in restricted_agent's list). CORRECT.

3. **t24**: `@tests8_roomuser:localhost` -> `@mindroom_general_tests8` in restricted room.
   Result: restricted_agent replied (roomuser in restricted_agent's list), general did NOT reply (roomuser NOT in `*` list). CORRECT.

**Evidence**: Matty restricted room threads showing correct agent selection per reply permission rules.

### AUTH-006: Internal MindRoom identities vs bot_accounts -- PASS

**Expected**: Internal system identities bypass authorization checks; `bot_accounts` still obey reply permission enforcement.

**Test part 1 (internal bypass)**: Router (`@mindroom_router_tests8:localhost`) sent welcome messages to all rooms on startup. Agents received and processed these messages without any denial, despite router not being in `global_users` or `room_permissions`. The `extract_agent_name()` function identifies the router as an internal identity, which returns `True` from `is_authorized_sender()`.

**Evidence**: `evidence/logs/mindroom_full.log` -- Multiple "Received message" entries from router processed by general and restricted_agent bots. Router "Sent response" entries confirming welcome messages were delivered.

**Test part 2 (bot_accounts not exempt)**:
- AUTH-006 first test: `@tests8_botacct:localhost` NOT in `room_permissions` -> message m12 in restricted room, NO thread (silently denied by `is_authorized_sender`).
- AUTH-006b: After adding `@tests8_botacct:localhost` to `room_permissions.restricted` via hot-reload, bot account passed `is_authorized_sender` but was denied by `agent_reply_permissions` -> message m13 in restricted room, NO thread. Bot accounts are intentionally NOT exempt from per-user reply lists (code comment: "Bridge bot accounts are intentionally not exempt").

**Evidence**: Matty restricted messages showing m12 and m13 from `@tests8_botacct:localhost` with no thread/reply. Unit test `test_agent_reply_permissions_do_not_bypass_bot_accounts` also passes.

### AUTH-007: Voice message from denied user uses original sender -- PASS

**Expected**: Voice dispatch uses the original human sender for permission evaluation instead of the router or transcription sender.

**Test approach**: Code review + unit test verification. The voice handler (`src/mindroom/voice_handler.py:234`) sets `ORIGINAL_SENDER_KEY: event.sender` when creating the synthetic transcribed message. The bot's `_requester_user_id_for_event()` and `get_effective_sender_id_for_reply_permissions()` extract this key for internal MindRoom senders, ensuring the original human sender is used for reply permission checks.

**Unit test results**: All 4 `effective_sender` tests pass:
- `test_effective_sender_uses_voice_original_sender_for_router_messages`: Router relaying voice message -> uses original sender
- `test_effective_sender_ignores_voice_original_sender_for_non_internal_messages`: Non-internal sender -> uses actual sender (prevents spoofing)
- `test_effective_sender_does_not_trust_cross_domain_router_like_ids`: Cross-domain router-like IDs -> not trusted
- `test_effective_sender_uses_original_sender_for_internal_agent_messages`: Internal agent messages -> uses original sender

**Full authorization test suite**: 32/32 tests pass (130.14s).

**Evidence**: `evidence/logs/test_authorization_results.txt`, voice_handler.py:234, authorization.py:157-186.

## Summary

| Test | Status | Method |
|------|--------|--------|
| AUTH-001 | PASS | Live (matty + logs) |
| AUTH-002 | PASS | Live (matty + hot-reload x3) |
| AUTH-003 | PASS | Live (matty) |
| AUTH-004 | PASS | Live (matty) |
| AUTH-005 | PASS | Live (matty) |
| AUTH-006 | PASS | Live (matty + logs) |
| AUTH-007 | PASS | Unit tests (4/4) + code review |

All 7 test items in Section 8 pass. Authorization unit test suite: 32/32 pass.
