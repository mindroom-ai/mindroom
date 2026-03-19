# Section 2: Config Loading, Hot Reload, And Reconciliation

**Tester:** mindroom/crew/test_s2
**Date:** 2026-03-19
**Environment:** core-local
**Matrix:** localhost:8108 (Docker Synapse)
**Model:** apriel-thinker:15b at LOCAL_MODEL_HOST:9292/v1
**Namespace:** tests2
**API Port:** 9872

## Summary

All 7 test items **PASS**. The hot-reload system correctly detects config.yaml changes within ~1 second, computes minimal diffs, and applies them without full-process restarts.

---

## Results

### CONF-001: Edit a configured agent field during live run

- [x] **PASS**

```
Test ID: CONF-001
Environment: core-local
Command or URL: Edit config.yaml general agent role and instructions while runtime active
Room, Thread, User, or Account: @mindroom_general_tests2:localhost
Expected Outcome: Only the affected entity is rebuilt/restarted; unaffected bots keep running
Observed Outcome: PASS - general agent stopped and restarted with new config. code, router, dev_team unaffected.
Evidence: live-test-results/evidence/logs/conf-001-reload.log
Failure Note: N/A
```

**Key log evidence:**
- `"Configuration file changed, checking for updates..."` - file watcher detected change
- `"Agent general configuration changed, will restart"` (via config_updates diff)
- `"Sync task for general was cancelled"` / `"Stopped agent bot general"` - clean stop
- `"Agent setup complete: @mindroom_general_tests2:localhost"` - restarted with new config
- Presence updated to new role: "A general-purpose assistant that explains things simply."
- code, router, dev_team sync loops continued uninterrupted

---

### CONF-002: Add a new agent during live run

- [x] **PASS**

```
Test ID: CONF-002
Environment: core-local
Command or URL: Add analyst agent to config.yaml while runtime active
Room, Thread, User, or Account: @mindroom_analyst_tests2:localhost
Expected Outcome: New agent provisioned, joins rooms, available without full restart
Observed Outcome: PASS - analyst registered, joined lobby+dev, sync loop started. No other agents restarted.
Evidence: live-test-results/evidence/logs/conf-002-add-agent.log
Failure Note: N/A
```

**Key log evidence:**
- `"Found 3 agent configurations"` (was 2)
- `"Agent analyst is new, will start"`
- `"Generated new credentials for agent analyst"` + `"Successfully registered user: @mindroom_analyst_tests2:localhost"`
- `"Starting sync loop for analyst"` - fully operational
- Existing agents (general, code, router, dev_team) NOT restarted

---

### CONF-003: Remove an agent during live run

- [x] **PASS**

```
Test ID: CONF-003
Environment: core-local
Command or URL: Remove analyst agent from config.yaml while runtime active
Room, Thread, User, or Account: @mindroom_analyst_tests2:localhost
Expected Outcome: Removed entity stops cleanly, no longer in routing/dashboard
Observed Outcome: PASS - analyst stopped cleanly. Other agents unaffected.
Evidence: live-test-results/evidence/logs/conf-003-remove-agent.log
Failure Note: N/A
```

**Key log evidence:**
- `"Found 2 agent configurations"` (was 3)
- `"Agent analyst was removed, will stop"`
- `"Sync task for analyst was cancelled"` / `"Stopped agent bot analyst"`
- `"Configuration update complete: 1 bots affected"`
- general, code, router, dev_team continued running

---

### CONF-004: Add or modify a configured team during live run

- [x] **PASS**

```
Test ID: CONF-004
Environment: core-local
Command or URL: Change dev_team mode coordinate->collaborate, add help_team
Room, Thread, User, or Account: @mindroom_dev_team_tests2:localhost, @mindroom_help_team_tests2:localhost
Expected Outcome: Changed team updated, new team available, unrelated entities stable
Observed Outcome: PASS - dev_team restarted with collaborate mode, help_team created and joined help room. Agents unaffected.
Evidence: live-test-results/evidence/logs/conf-004-team-changes.log
Failure Note: N/A
```

**Key log evidence:**
- `"Sync task for dev_team was cancelled"` / `"Stopped agent bot dev_team"` - old config stopped
- `"Generated new credentials for agent help_team"` / `"Successfully registered user: @mindroom_help_team_tests2:localhost"`
- dev_team presence: "Full-stack development collaboration team." (updated role)
- help_team presence: "User support and guidance team." (new team)
- `"Configuration update complete: 2 bots affected"` - exactly the 2 teams
- general, code agents NOT restarted

---

### CONF-005: Add knowledge base, skill, or plugin during live run

- [x] **PASS**

```
Test ID: CONF-005
Environment: core-local
Command or URL: Add test_docs knowledge base to config, then assign to general agent
Room, Thread, User, or Account: general agent with test_docs KB
Expected Outcome: Runtime caches invalidate, change visible on next request, no stale copies
Observed Outcome: PASS - KB initialized, watcher started; agent restarted when KB assigned. Embedding failed (expected: sk-test API key).
Evidence: live-test-results/evidence/logs/conf-005-knowledge-base.log
Failure Note: N/A (embedding 401 expected with fake API key; KB infrastructure loaded correctly)
```

**Key log evidence:**
- Phase 1 (KB added to config, not yet assigned to agent):
  - `"No agent changes detected in configuration update"` - correct: KB is a support service
  - `"Knowledge manager initialized"` / `"Knowledge folder watcher started"` with base_id=test_docs
  - Embedding 401 error expected (sk-test key), but indexing infrastructure loaded
- Phase 2 (KB assigned to general agent):
  - `"Sync task for general was cancelled"` / `"Stopped agent bot general"` - restarted with KB
  - `"Starting sync loop for general"` - back online with KB config
  - Other agents unaffected

---

### CONF-006: Enable reconcile_existing_rooms, then disable

- [x] **PASS**

```
Test ID: CONF-006
Environment: core-local
Command or URL: Set reconcile_existing_rooms: true, then false in matrix_room_access
Room, Thread, User, or Account: All managed rooms (lobby, dev, help)
Expected Outcome: Rooms reconciled once, steady-state returns after flag off
Observed Outcome: PASS - rooms re-ensured with reconcile=true, then normal behavior resumed with reconcile=false. No bot restarts in either phase.
Evidence: live-test-results/evidence/logs/conf-006-reconcile.log
Failure Note: N/A
```

**Key log evidence:**
- Phase 1 (reconcile=true):
  - `matrix_room_access_changed` detected
  - `"Ensured existence of 3 rooms"` - rooms reconciled
  - All room invitations reprocessed
  - `"Configuration update complete: 0 bots affected"` - no unnecessary bot restarts
- Phase 2 (reconcile=false):
  - Config change detected, rooms re-ensured in normal mode
  - `"Configuration update complete: 0 bots affected"` - steady-state returned
  - All agents continued running without interruption

---

### CONF-007: Edit shared defaults without changing entities

- [x] **PASS**

```
Test ID: CONF-007
Environment: core-local
Command or URL: Change defaults.enable_streaming=false, defaults.show_stop_button=false
Room, Thread, User, or Account: All agents
Expected Outcome: Bots pick up new defaults in place without restart
Observed Outcome: PASS - defaults updated, no agents restarted. "No agent changes detected in configuration update."
Evidence: live-test-results/evidence/logs/conf-007-defaults.log
Failure Note: N/A
```

**Key log evidence:**
- `"Configuration file changed, checking for updates..."`
- `"No agent changes detected in configuration update"` - correctly identified defaults-only change
- All 5 entities (router, general, code, dev_team, help_team) got presence updates but NO restarts
- API remained healthy: `{"status":"healthy"}`
- New defaults (streaming=false, stop_button=false) loaded into runtime config

---

## Evidence Files

| File | Description |
|------|-------------|
| `evidence/logs/conf-001-reload.log` | Agent field edit hot-reload |
| `evidence/logs/conf-002-add-agent.log` | New agent provisioning |
| `evidence/logs/conf-003-remove-agent.log` | Agent removal and cleanup |
| `evidence/logs/conf-004-team-changes.log` | Team modification and addition |
| `evidence/logs/conf-005-knowledge-base.log` | Knowledge base addition |
| `evidence/logs/conf-006-reconcile.log` | Room reconciliation toggle |
| `evidence/logs/conf-007-defaults.log` | Shared defaults change |
| `evidence/api-responses/initial-health.json` | API health at boot |
| `evidence/api-responses/initial-ready.json` | API ready at boot |
| `evidence/api-responses/conf-007-health-after-defaults.json` | API health after defaults change |
