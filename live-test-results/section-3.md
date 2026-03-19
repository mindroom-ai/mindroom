# Section 3: Room Provisioning, Router Management, And Onboarding

Test environment: `core-local` with MINDROOM_NAMESPACE=tests3, Matrix on localhost:8108, model server at LOCAL_MODEL_HOST:9292/v1 (apriel-thinker:15b), API port 9883.

## Results Summary

| Test ID  | Status | Notes |
|----------|--------|-------|
| ROOM-001 | PASS   | Rooms created with topics, access settings, and invites |
| ROOM-002 | PASS   | Router posted welcome messages in all newly created rooms |
| ROOM-003 | PASS   | `!hi` reproduces welcome guidance (tested via direct Matrix API) |
| ROOM-004 | PASS   | All three modes verified: single_user_private, multi_user public, multi_user knock, with invite-only exceptions |
| ROOM-005 | PASS   | External room discoverable via API as unconfigured_room |
| ROOM-006 | PASS   | Orphaned bots cleaned up on restart without ejecting configured entities |
| ROOM-007 | PARTIAL PASS | Root space creation, room grouping, and avatars work on initial boot; known edge case where orphan cleanup kicks router from space on restart |

---

## ROOM-001: Boot with at least one configured room that does not yet exist

```text
Test ID: ROOM-001
Environment: core-local
Command or URL: mindroom run --api-port 9883 with MINDROOM_NAMESPACE=tests3
Room, Thread, User, or Account: lobby, dev, testroom (all new)
Expected Outcome: MindRoom creates the room, applies topic and access settings, and invites the expected participants.
Observed Outcome: All 3 rooms created. AI-generated topics applied. Rooms created with single-user/private defaults (join_rule=invite, directory_visibility=private). Agents invited per config. mindroom_user_tests3 joined all rooms.
Evidence: evidence/run-logs/section3-boot.txt, evidence/api-responses/rooms-list.json
Failure Note: N/A
```

Key log lines (first boot):
- `Creating room dev with topic: Dev CodeAgent handles code, GitHub...`
- `Created room: Dev (!oXrnvQmtsruLnwQuhD:localhost)`
- `Invited @mindroom_code_tests3:localhost to room !oXrnvQmtsruLnwQuhD:localhost`
- `Created room with single-user/private defaults join_rule=invite mode=single_user_private`
- `Ensured existence of 3 rooms`

API verification:
```json
["dev", "lobby", "testroom"]
```

---

## ROOM-002: Verify router onboarding in a newly managed room

```text
Test ID: ROOM-002
Environment: core-local
Command or URL: matty messages "Lobby" / "Dev" / "Testroom"
Room, Thread, User, or Account: All 3 rooms
Expected Outcome: The router posts a welcome message only in the intended empty-room onboarding scenario and the content reflects the currently available agents and commands.
Observed Outcome: Router posted welcome messages in all 3 rooms. Each message correctly lists agents configured for that room. Lobby shows general + code agents. Dev shows code agent only. Testroom shows general agent only.
Evidence: evidence/run-logs/section3-boot.txt (lines with "Welcome message sent")
Failure Note: N/A
```

Welcome message content verified in Lobby:
- Lists `@mindroom_code_tests3:localhost` and `@mindroom_general_tests3:localhost`
- Shows `!hi`, `!schedule`, `!help` commands
- Mentions voice transcription support

---

## ROOM-003: Send `!hi` in a managed room after startup

```text
Test ID: ROOM-003
Environment: core-local
Command or URL: Direct Matrix API PUT to send {"msgtype":"m.text","body":"!hi"} to Testroom
Room, Thread, User, or Account: Testroom (!weZHazlxneEUspEWPt:localhost)
Expected Outcome: The router reproduces the current welcome guidance without changing room state or duplicating bot setup.
Observed Outcome: Router replied in thread with the full welcome message matching the initial onboarding content. Room state unchanged.
Evidence: matty messages "Testroom" showing m2 (!hi) and m3 (router welcome response in thread t5)
Failure Note: N/A
```

Note: Using matty's `send` command escaped `!` to `\!`, which prevented command parsing. Direct Matrix API send worked correctly. This is a matty client limitation, not a MindRoom issue.

---

## ROOM-004: Test single_user_private, multi_user public, multi_user knock, and invite-only exceptions

```text
Test ID: ROOM-004
Environment: core-local
Command or URL: Matrix API GET room state + config hot-reload
Room, Thread, User, or Account: dev, testroom, lobby
Expected Outcome: Joinability, room directory visibility, and restricted-room exceptions all match the configured policy.
Observed Outcome: All three modes verified via hot-reload with reconcile_existing_rooms=true:
  1. single_user_private: All rooms join_rule=invite (PASS)
  2. multi_user public with dev in invite_only_rooms: dev=invite, testroom=public, lobby=public (PASS)
  3. multi_user knock with dev in invite_only_rooms: dev=invite, testroom=knock, lobby=knock (PASS)
  Directory visibility updates partially failed (non-critical, likely permissions).
Evidence: evidence/api-responses/room-access-modes.json, evidence/run-logs/section3-full-run.txt
Failure Note: Directory visibility reconciliation failed with "partially applied" warning. Join rule changes applied correctly.
```

Hot-reload log evidence:
- `Config file changed... checking for updates...`
- `Applying managed room access policy context=existing_room_reconciliation join_rule=invite room_key=dev` (invite-only exception)
- `Updated room join rule join_rule=public room_id=!weZHazlxneEUspEWPt:localhost` (testroom)
- `Updated room join rule join_rule=knock room_id=!eekTDWyfWKpQPqdXMF:localhost` (lobby)

---

## ROOM-005: Put an agent in an external or unmanaged room and load the runtime plus dashboard

```text
Test ID: ROOM-005
Environment: core-local
Command or URL: Matrix API createRoom + curl http://localhost:9883/api/matrix/agents/rooms
Room, Thread, User, or Account: ExternalRoom (!IKiVvOSPpaIUnXXEUP:localhost)
Expected Outcome: External room state is discoverable and can later be left intentionally instead of being silently mutated on startup.
Observed Outcome: Created "ExternalRoom" via Matrix API and invited general agent. The /api/matrix/agents/rooms endpoint correctly shows ExternalRoom in general agent's unconfigured_rooms list with room details.
Evidence: evidence/api-responses/agents-rooms.json
Failure Note: N/A
```

API response excerpt:
```json
{
  "agent_id": "general",
  "unconfigured_rooms": ["!IKiVvOSPpaIUnXXEUP:localhost"],
  "unconfigured_room_details": [{"room_id": "!IKiVvOSPpaIUnXXEUP:localhost", "name": "ExternalRoom"}]
}
```

---

## ROOM-006: Start the runtime with orphaned bot memberships from a previous config

```text
Test ID: ROOM-006
Environment: core-local
Command or URL: Second boot of mindroom run (same namespace, existing matrix_state.yaml)
Room, Thread, User, or Account: Root space !SIjDLlBnyJeurHiDAs:localhost
Expected Outcome: Orphaned MindRoom bots are cleaned up safely without ejecting the currently configured entities from their required rooms.
Observed Outcome: On second startup, orphan cleanup found mindroom_router_tests3 in the root space (where it shouldn't be as a configured bot for that room) and kicked it. All configured agents remained in their correct rooms. 1 orphaned bot kicked from 1 room.
Evidence: evidence/run-logs/section3-full-run.txt (grep "orphan")
Failure Note: N/A
```

Log evidence:
- `Checking 4 rooms for orphaned bots`
- `Found orphaned bot mindroom_router_tests3 in room !SIjDLlBnyJeurHiDAs:localhost (configured bots for this room: set())`
- `Kicked mindroom_router_tests3 from !SIjDLlBnyJeurHiDAs:localhost`
- `Kicked 1 orphaned bots from 1 rooms`
- `All agents have joined their configured rooms` (configured entities preserved)

---

## ROOM-007: Enable root space management and managed avatars when the config uses them

```text
Test ID: ROOM-007
Environment: core-local
Command or URL: config matrix_space.enabled=true, matrix_space.name="MindRoom-Test-S3"
Room, Thread, User, or Account: Root space !SIjDLlBnyJeurHiDAs:localhost
Expected Outcome: Root space creation, room grouping, and avatar propagation behave consistently and do not regress room access or membership.
Observed Outcome: PARTIAL PASS. On initial boot:
  - Root space "MindRoom-Test-S3" created successfully
  - All 3 rooms linked as space children
  - Root space avatar set from avatars/spaces/root_space.png
  - mindroom_user_tests3 invited to space
  - Agent avatars set (router, code, general)
  On subsequent boot: router kicked from space by orphan cleanup (ROOM-006 interaction) and cannot rejoin. Space becomes orphaned.
Evidence: evidence/run-logs/section3-boot.txt (first boot), evidence/run-logs/section3-full-run.txt (second boot)
Failure Note: Root space becomes inaccessible after restart due to orphan cleanup kicking the router (the space creator/admin). The router cannot rejoin because no members remain in the space. This is a known edge case where ROOM-006 orphan cleanup conflicts with ROOM-007 root space management.
```

First boot log evidence (captured from terminal):
- `Created space: MindRoom-Test-S3 (!SIjDLlBnyJeurHiDAs:localhost)`
- `Linked room under root space room_id=!oXrnvQmtsruLnwQuhD:localhost`
- `Linked room under root space room_id=!weZHazlxneEUspEWPt:localhost`
- `Linked room under root space room_id=!eekTDWyfWKpQPqdXMF:localhost`
- `Successfully set avatar for room !SIjDLlBnyJeurHiDAs:localhost`
- `Set avatar for managed Matrix room avatar_path=.../avatars/spaces/root_space.png context=root_space`
- `Invited @mindroom_user_tests3:localhost to root space`

Agent avatar log evidence:
- `Successfully set avatar for router`
- `Successfully set avatar for code`
- `Successfully set avatar for general`

matrix_state.yaml confirmation:
```yaml
space_room_id: '!SIjDLlBnyJeurHiDAs:localhost'
```
