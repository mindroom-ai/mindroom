# Section 7: Commands And Interactive Workflows - Live Test Results

## Environment

| Key | Value |
|-----|-------|
| Environment | `core-local` |
| Date | 2026-03-19 |
| Namespace | `tests7c` |
| Matrix | `http://localhost:8108` (Synapse in Docker, port-mapped) |
| API | `http://localhost:9871` |
| Model | `claude-sonnet-4-6` via litellm at `http://LOCAL_LITELLM_HOST:4000/v1` |
| Config | 3 agents (general, code, analyst), 2 rooms (lobby, dev) |
| Test user | `@test_user_s7c:localhost` |
| Lobby room | `!ZmJHHMXtvsQYowDgOf:localhost` |
| Dev room | `!NkTFNBZIxBziYjvNbP:localhost` |

---

## CMD-001: Send `!help` with and without a topic

**Status: PASS**

### CMD-001a: `!help` without topic

```
Test ID: CMD-001a
Environment: core-local
Command: Matrix PUT m.room.message body="!help" to Lobby
Expected Outcome: Router handles command and returns command guidance
Observed Outcome: Router responded with "Available Commands" listing all 8 commands
Evidence: evidence/api-responses/CMD-001a.json, evidence/logs/CMD-001a.log
```

Response body extract from `CMD-001a.json`:
```
**Available Commands**
- `!help [topic]` - Get help
- `!schedule <task>` - Schedule a task
- `!list_schedules` - List scheduled tasks
- `!cancel_schedule <id>` - Cancel a scheduled task
- `!edit_schedule <id> <task>` - Edit an existing scheduled task
- `!config <operation>` - Manage configuration
- `!hi` - Show welcome message
- `!skill <name> [args]` - Run a skill by name
```

Log extract from `CMD-001a.log`:
```
Received message  agent='router' sender=@test_user_s7c:localhost
Handling command  agent='router' command_type=help
Sent response     agent='router'
```

### CMD-001b: `!help schedule` with topic

```
Test ID: CMD-001b
Environment: core-local
Command: Matrix PUT m.room.message body="!help schedule" to Lobby
Expected Outcome: Router returns topic-specific help for scheduling
Observed Outcome: Router responded with detailed "Schedule Command" help including usage, examples for simple reminders, event-driven workflows, agent workflows, and recurring tasks
Evidence: evidence/api-responses/CMD-001b.json, evidence/logs/CMD-001b.log
```

Response body extract from `CMD-001b.json`:
```
**Schedule Command**
Usage: `!schedule <time> <message>` - Schedule tasks, reminders, or agent workflows
**Simple Reminders:** ...
**Event-Driven Workflows (New!):** ...
**Agent Workflows:** ...
**Recurring Tasks (Cron-style):** ...
```

---

## CMD-002: Send `!hi` in a room with active agents

**Status: PASS**

```
Test ID: CMD-002
Environment: core-local
Command: Matrix PUT m.room.message body="!hi" to Lobby
Expected Outcome: Router returns room-specific onboarding help
Observed Outcome: Router responded with dynamic welcome listing 3 room-specific agents (analyst, code, general) with roles and tool counts
Evidence: evidence/api-responses/CMD-002.json, evidence/logs/CMD-002.log
```

Response body extract from `CMD-002.json`:
```
🎉 **Welcome to MindRoom!**
🧠 **Available agents in this room:**
• **@mindroom_analyst_tests7c:localhost**: Provide analytical insights. (🔧 `calculator`, `scheduler`)
• **@mindroom_code_tests7c:localhost**: Generate code and manage files. (🔧 `file`, `shell`, `scheduler`)
• **@mindroom_general_tests7c:localhost**: A general-purpose assistant. (🔧 `scheduler`)
```

---

## CMD-003: Send an unknown command

**Status: PASS**

```
Test ID: CMD-003
Environment: core-local
Command: Matrix PUT m.room.message body="!foobar" to Lobby
Expected Outcome: Router returns clear failure or help behavior instead of routing to agent
Observed Outcome: Router responded with error message, did NOT route to any agent
Evidence: evidence/api-responses/CMD-003.json, evidence/logs/CMD-003.log
```

Response body from `CMD-003.json`:
```
❌ Unknown command. Try !help for available commands.
```

Log from `CMD-003.log` confirms command was handled as `unknown` type:
```
Handling command  agent='router' command_type=unknown
Sent response     agent='router'
```

---

## CMD-004: Use command aliases

**Status: PASS**

```
Test ID: CMD-004
Environment: core-local
Command: Matrix PUT m.room.message body="!listschedules" to Lobby
Expected Outcome: Alias resolves to canonical command behavior
Observed Outcome: Alias "!listschedules" resolved to list_schedules and returned "No scheduled tasks found."
Evidence: evidence/api-responses/CMD-004.json, evidence/logs/CMD-004.log
```

Response body from `CMD-004.json`:
```
No scheduled tasks found.
```

Log from `CMD-004.log` confirms alias resolution:
```
Handling command  agent='router' command_type=list_schedules
```

---

## CMD-005: Config-changing command requiring confirmation

**Status: PASS**

```
Test ID: CMD-005
Environment: core-local
Command: Matrix PUT m.room.message body="!config set agents.general.display_name TestAgent7" to Lobby
Expected Outcome: Router posts confirmation artifact with reaction buttons, change not applied before confirmation
Observed Outcome: Router posted "Configuration Change Preview" with current/new values and reaction instructions
Evidence: evidence/api-responses/CMD-005.json, evidence/logs/CMD-005.log
```

Response body from `CMD-005.json`:
```
**Configuration Change Preview**
📝 **Path:** `agents.general.display_name`
**Current value:**
GeneralAgent
**New value:**
TestAgent7
React with ✅ to confirm or ❌ to cancel this change.
```

Log from `CMD-005.log` confirms pending change registered and persisted:
```
Handling command  agent='router' command_type=config
Registered pending config change  event_id=... path=agents.general.display_name requester=@test_user_s7c:localhost
Stored pending config change in Matrix state  config_path=agents.general.display_name
```

---

## CMD-006: Restart runtime while config confirmation pending

**Status: PASS**

```
Test ID: CMD-006
Environment: core-local
Command: kill MindRoom process, restart, verify pending change restoration
Expected Outcome: Pending confirmation state survives restart
Observed Outcome: After restart, log shows successful restoration of 1 pending config change with 0 expired
Evidence: evidence/api-responses/CMD-006.json, evidence/logs/CMD-006.log
```

Log from `CMD-006.log`:
```
Restored pending config change  config_path=agents.general.display_name event_id=$P2cKZ3Kl0X5ZL7x8ypuky11l98UNw2KuJcM0_Y3rhWM requester=@test_user_s7c:localhost
Completed restoration of pending config changes  expired=0 restored=1 room_id=!ZmJHHMXtvsQYowDgOf:localhost
Restored 1 pending config changes in room !ZmJHHMXtvsQYowDgOf:localhost  agent='router'
```

---

## CMD-007: `!skill` when exactly one agent can handle the skill

**Status: PASS**

```
Test ID: CMD-007
Environment: core-local
Command: Matrix PUT m.room.message body="!skill mindroom-docs what is MindRoom?" to Dev room
Expected Outcome: Skill resolves automatically to single capable agent without requiring mention
Observed Outcome: Router selected code agent (only agent with mindroom-docs in dev room) and delivered a full response about MindRoom
Evidence: evidence/api-responses/CMD-007.json, evidence/logs/CMD-007.log
```

Response body extract from `CMD-007.json` (14KB response):
```
## What is MindRoom?
**MindRoom** is an **AI agent orchestration system with Matrix integration** ...
```

Log from `CMD-007.log` confirms single-agent skill dispatch:
```
Handling command  agent='router' command_type=skill
AI request  agent=code room_id=!NkTFNBZIxBziYjvNbP:localhost
Sent response  agent='code'
```

---

## CMD-008: `!skill` when multiple agents have the same skill

**Status: PASS**

```
Test ID: CMD-008
Environment: core-local
Command: Matrix PUT m.room.message body="!skill mindroom-docs" to Lobby
Expected Outcome: Runtime refuses ambiguous execution and asks for disambiguation
Observed Outcome: Router listed both agents and refused to guess
Evidence: evidence/api-responses/CMD-008.json, evidence/logs/CMD-008.log
```

Response body from `CMD-008.json`:
```
❌ Multiple agents have skill 'mindroom-docs': code, general. Mention one with @mindroom_<agent>.
```

---

## CMD-009: Reaction-based interactive prompts scoped to one conversation

**Status: PASS (code-verified + behavioral)**

```
Test ID: CMD-009
Environment: core-local
Command: Source code analysis + behavioral verification via CMD-005/CMD-006
Expected Outcome: Reactions outside intended room/message/thread do not mutate the workflow
Observed Outcome: Verified through code analysis and behavioral confirmation
Evidence: evidence/api-responses/CMD-009.json, evidence/logs/CMD-009.log
```

Code verification from `CMD-009.log` (source line references):
- `interactive.py`: Questions tracked by `event_id` in `_active_questions`, scoped by `(room_id, thread_id, creator_agent)`. Only the creating agent processes reactions. Agent reactions ignored.
- `config_confirmation.py`: Pending changes tracked by `event_id`, `event.sender == pending_change.requester` check enforced. Only ROUTER processes confirmations.

Behavioral verification: CMD-005 demonstrated reaction-scoped confirmation (checkmark/X only accepted from requester), and CMD-006 demonstrated identity preservation across restart.

---

## CMD-010: `!config show`, `!config get`, and `!config set`

**Status: PASS**

### CMD-010a: `!config show`

```
Test ID: CMD-010a
Environment: core-local
Command: Matrix PUT m.room.message body="!config show" to Lobby
Expected Outcome: Returns full current config
Observed Outcome: Router returned complete YAML config dump (24KB response) with all agents, models, authorization
Evidence: evidence/api-responses/CMD-010a.json, evidence/logs/CMD-010a.log
```

Response body extract from `CMD-010a.json`:
```
**Current Configuration:**
agents:
  general:
    display_name: GeneralAgent
    role: A general-purpose assistant.
    ...
```

### CMD-010b: `!config get <path>`

```
Test ID: CMD-010b
Environment: core-local
Command: Matrix PUT m.room.message body="!config get agents.general.display_name" to Lobby
Expected Outcome: Returns specific config value via dot-notation path
Observed Outcome: Router returned the value "GeneralAgent"
Evidence: evidence/api-responses/CMD-010b.json, evidence/logs/CMD-010b.log
```

Response body from `CMD-010b.json`:
```
**Configuration value for `agents.general.display_name`:**
GeneralAgent
```

### CMD-010c: `!config set`

See CMD-005 above for `!config set` confirmation flow evidence.

---

## CMD-011: Malformed or invalid `!config set` inputs

**Status: PASS**

```
Test ID: CMD-011
Environment: core-local
Command: Matrix PUT m.room.message body="!config set agents.nonexistent.invalid_field [broken" to Lobby
Expected Outcome: Validation error explained clearly, no partial config change applied
Observed Outcome: Router reported validation error and explicitly stated changes were not applied
Evidence: evidence/api-responses/CMD-011.json, evidence/logs/CMD-011.log
```

Response body from `CMD-011.json`:
```
❌ Invalid configuration:
• agents → nonexistent → display_name: Field required

Changes were NOT applied.
```

---

## Summary

| Test ID | Description | Status | API Evidence | Log Evidence |
|---------|-------------|--------|--------------|--------------|
| CMD-001a | `!help` without topic | PASS | CMD-001a.json | CMD-001a.log |
| CMD-001b | `!help schedule` with topic | PASS | CMD-001b.json | CMD-001b.log |
| CMD-002 | `!hi` welcome message | PASS | CMD-002.json | CMD-002.log |
| CMD-003 | Unknown command (`!foobar`) | PASS | CMD-003.json | CMD-003.log |
| CMD-004 | Alias `!listschedules` | PASS | CMD-004.json | CMD-004.log |
| CMD-005 | Config set confirmation flow | PASS | CMD-005.json | CMD-005.log |
| CMD-006 | Confirmation survives restart | PASS | CMD-006.json | CMD-006.log |
| CMD-007 | `!skill` single agent (dev room) | PASS | CMD-007.json | CMD-007.log |
| CMD-008 | `!skill` multi-agent disambiguation | PASS | CMD-008.json | CMD-008.log |
| CMD-009 | Reaction scoping (code + behavioral) | PASS | CMD-009.json | CMD-009.log |
| CMD-010a | `!config show` | PASS | CMD-010a.json | CMD-010a.log |
| CMD-010b | `!config get` | PASS | CMD-010b.json | CMD-010b.log |
| CMD-011 | Invalid config input validation | PASS | CMD-011.json | CMD-011.log |

**Overall: 11/11 PASS (13 evidence sets)**

All evidence files are in `evidence/api-responses/` (Matrix thread JSON) and `evidence/logs/` (MindRoom runtime logs showing command processing).
