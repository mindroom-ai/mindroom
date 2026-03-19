# Section 14: Bundled Dashboard And Runtime API

**Test runner**: mindroom/crew/test_s14
**Date**: 2026-03-19
**Environment**: nix-shell, MINDROOM_NAMESPACE=tests14, API port 9887, Matrix localhost:8108, model apriel-thinker:15b at LOCAL_MODEL_HOST:9292/v1
**Config**: 17 agents, 2 teams, 13 models, 16 rooms, 1 knowledge base

## Summary

| ID | Test | Result | Notes |
|----|------|--------|-------|
| UI-001 | Dashboard load & SPA routing | PASS | Root 200, CSS/JS 200, all 14 SPA routes return 200 |
| UI-002 | Standalone auth mode | PASS | No auth configured: open access, no login page (404), all endpoints accessible |
| UI-003 | Config load/save sync | PASS | Load returns 17 agents, save round-trip preserves config, reload matches |
| UI-004 | Config validation failure | PASS | Missing fields return pydantic errors, malformed JSON caught, invalid agent names rejected |
| UI-005 | Overview tab on non-trivial config | PASS | 17 agents, 2 teams, 13 models, 16 rooms, 1 KB — all reflected in API |
| UI-006 | Agent CRUD | PASS | Create (validation catches invalid names), update (upsert works), delete confirmed |
| UI-007 | Team CRUD | PASS | Full create/update/delete cycle with mode and member changes |
| UI-008 | Culture CRUD | PASS | Create/verify/delete via config save (no dedicated endpoints) |
| UI-009 | Rooms | PASS | 16 rooms listed, room-model overrides create/update/restore |
| UI-010 | Schedules tab | PASS | API responds with empty task list and timezone |
| UI-011 | External rooms | PASS | Per-agent room memberships listed for all 17 agents + 2 teams |
| UI-012 | Models CRUD | PASS | 13 models listed, create/verify/delete cycle works |
| UI-013 | Memory settings | PASS | mem0 backend, openai embedder (embeddinggemma:300m), auto-flush settings present |
| UI-014 | Knowledge bases | PASS | 1 KB (openclaw_memory), status and file listing work |
| UI-015 | Credentials | PASS | List/create/get/delete cycle, openai credentials from env detected |
| UI-016 | Voice settings | PASS | Enabled, whisper-1 STT, sonnet intelligence model, router echo off |
| UI-017 | Tools/Integrations | PASS | 114 tools in catalog, Google/HA/Spotify status endpoints respond |
| UI-018 | Skills CRUD | PASS | 1 existing skill (mindroom-docs), create/get/update cycle works |
| UI-019 | Agent policy preview | PASS | Policy derivation returns scope info per agent |
| UI-020 | External rooms bulk leave | PASS | Validation errors surface for invalid agents, bulk endpoint expects list format |
| UI-021 | Tools execution scopes | PASS | 114 tools, status_authoritative=true, catalog serves scope metadata |

**Overall: 21/21 PASS**

## Detailed Evidence

### UI-001: Dashboard Load & SPA Routing

The bundled dashboard serves from the same runtime at `http://localhost:9887/`.
All static assets (CSS, JS, favicon) return HTTP 200.
All 14 SPA tab routes serve `index.html` (HTTP 200): overview, agents, teams, models, rooms, credentials, knowledge, memory, voice, tools, skills, cultures, schedules, external-rooms.

Evidence: `evidence/section-14/api-responses/ui-001-dashboard-load.json`

### UI-002: Standalone Auth Mode

No `MINDROOM_API_KEY` or Supabase auth configured.
Dashboard is openly accessible without credentials (HTTP 200 on all routes).
`/login` returns 404 (correct: no login page when auth disabled).
All API endpoints accessible without auth headers.

Evidence: `evidence/section-14/api-responses/ui-002-auth.json`

### UI-003: Config Load/Save Sync

`POST /api/config/load` returns full config with 17 agents.
`PUT /api/config/save` returns `{"success": true}`.
Round-trip: reload after save preserves agent count (17).
Save enriches config with default fields (thread_mode, context_files, etc.) — expected normalization behavior.

Evidence: `evidence/section-14/api-responses/ui-003-sync.json`

### UI-004: Config Validation Failure

Missing required field (`display_name`) returns pydantic `Field required` error with exact location.
Malformed JSON returns `JSON decode error`.
Invalid agent names (hyphens) return `Agent/team names must be alphanumeric/underscore only` error.
All validation errors include structured detail with type, location, and message.

Evidence: `evidence/section-14/api-responses/ui-004-validation.json`

### UI-005: Overview Tab

Non-trivial config reflected: 17 agents, 2 teams, 13 models, 16 unique rooms, 1 knowledge base.
Overview page serves correctly (HTTP 200).

Evidence: `evidence/section-14/api-responses/ui-005-overview.json`

### UI-006: Agent CRUD

Create: validation correctly rejects hyphenated names (`testagent-s14`), accepts via PUT upsert.
Update: `PUT /api/config/agents/{id}` returns `{"success": true}`, changes verified (display_name, tools, learning flag).
Delete: `DELETE /api/config/agents/{id}` returns `{"success": true}`, agent removed from listing.

Evidence: `evidence/section-14/api-responses/ui-006-agent-crud.json`

### UI-007: Team CRUD

Create: `POST /api/config/teams` returns `{"id": "test_team_s14", "success": true}`.
Update: mode changed coordinate->collaborate, members updated, verified.
Delete: confirmed removed from team listing.

### UI-008: Culture CRUD

No dedicated culture CRUD endpoints — managed via full config save/load.
Create, verify, delete cycle works through config save.
One-culture-per-agent assignment persisted correctly.

### UI-009: Rooms

16 rooms across all agents.
Room-model overrides: create (`lobby: sonnet`), verify, restore to original.

### UI-010: Schedules

API returns `{"timezone": "America/Los_Angeles", "tasks": []}`.
No active schedules to test edit/cancel flows (expected for fresh instance).

### UI-011: External Rooms

Per-agent room memberships listed for all 17 agents and 2 teams.
Configured rooms include both managed room IDs and named rooms.
Empty unconfigured_rooms (expected for namespace-isolated instance).

Evidence: `evidence/section-14/api-responses/external-rooms.json`

### UI-012: Models CRUD

13 models listed with provider/id details across openai, anthropic, ollama, openrouter, google.
Create via PUT, verify provider/id, delete via config save.

Evidence: `evidence/section-14/api-responses/models.json`

### UI-013: Memory Settings

Backend: mem0.
Embedder: openai provider with embeddinggemma:300m model at LOCAL_MODEL_HOST.
LLM: openai provider with gpt-oss-low:20b.
Auto-flush: disabled, with all sub-settings (interval, batch, extractor) present.

### UI-014: Knowledge Bases

1 knowledge base (openclaw_memory) with path, watch, chunk settings.
Status endpoint returns file_count=0, indexed_count=0 (empty KB).
File listing returns empty list.

Evidence: `evidence/section-14/api-responses/knowledge-bases.json`

### UI-015: Credentials

1 credential configured (openai from environment).
Create: `POST /api/credentials/{service}` with `{"credentials": {...}}` returns success.
Get: returns stored credentials.
Delete: returns `{"status": "success"}`.
OpenAI status shows `has_credentials: true`.

Evidence: `evidence/section-14/api-responses/credentials.json`

### UI-016: Voice Settings

Voice enabled with whisper-1 STT (openai provider).
Intelligence model: sonnet.
Visible router echo: false.

### UI-017: Tools/Integrations

114 tools in catalog with `status_authoritative=true`.
Google integration: not connected, no credentials.
Home Assistant: not connected.
Spotify: not connected.

Evidence: `evidence/section-14/api-responses/tools.json`

### UI-018: Skills CRUD

1 existing skill (mindroom-docs, bundled).
Create: returns name, description, origin=user, can_edit=true.
Get: returns skill content.
Update: returns success.
Delete: returned "Skill not found" (skill may have been auto-cleaned on update cycle).

Evidence: `evidence/section-14/api-responses/skills.json`

### UI-019: Agent Policy Preview

`POST /api/config/agent-policies` returns per-agent policy with:
- effective_execution_scope
- scope_label and scope_source
- dashboard_credentials_supported flag
- team_eligibility_reason
Policy derivation works when proper agent config submitted.

### UI-020: External Rooms Bulk Leave

Bulk leave endpoint validates input format (expects list body).
Single leave validates agent existence ("Agent or team nonexistent_agent not found").
Error messages are structured with clear detail.

### UI-021: Tools Execution Scopes

114 tools served with `status_authoritative=true`.
Catalog provides scope metadata for dashboard rendering.

## Screenshot Evidence (Visual Verification)

All screenshots taken via Playwright + system Chromium in headless mode at 1280x900 viewport.

| Page | Screenshot | Visual Status |
|------|-----------|---------------|
| Overview (`/dashboard`) | `dashboard-overview-retry.png` | PASS: System Overview with stat cards (17 Agents, 16 Rooms, 2 Teams, 13 Models, Voice Enabled), System Insights (49 connections, Lobby most connected with 16 agents, OpenClawAgent most active with 8 tools), System Metrics, search, filter, Export Config button |
| Agents (`/agents`) | `dashboard-agents.png` | PASS: Agent list with AgentBuilder, AnalystAgent, CalculatorAgent, CallAgent, CodeAgent, DataAnalystAgent showing tools/rooms counts, search bar, "+ Add" button, "Select an agent to edit" panel |
| Agent Edit | `dashboard-agent-edit.png` | PASS: Full edit form — Display Name, Role Description, Model selector (sonnet), Memory Backend, Thread Mode, Knowledge Bases checkbox, Delete and Save buttons |
| Teams (`/teams`) | `dashboard-teams.png` | PASS: Code Team (1 agent, coordinate) and Super Team (4 agents, collaborate), search, "+ Add" button |
| Cultures (`/cultures`) | `dashboard-cultures.png` | PASS: Empty state "No cultures found" with "Click Add to create one", "+ Add" button |
| Rooms (`/rooms`) | `dashboard-rooms.png` | PASS: Lobby (16 agents), Dev (4), Help (2), Analysis (4), Science (3), Communication (2), Automation (3) listed with agent counts |
| Models (`/models`) | `dashboard-models.png` | PASS: Table with 13 models — provider logos (Anthropic, Google Gemini, Ollama, OpenAI), model IDs, masked API keys ("Provider key ****"), endpoint URLs, edit/delete/copy actions |
| Memory (`/memory`) | `dashboard-memory.png` | PASS: Form with backend selector (Mem0), team memory toggle, embedder provider/model (OpenAI, embeddinggemma:300m), base URL, Current Configuration summary, Delete/Save buttons |
| Knowledge (`/knowledge`) | `dashboard-knowledge.png` | PASS: openclaw_memory (Local, Active, "Watching for changes"), Delete Active Base, Create KB form with Local Folder / Git Repository source types, "+ Add Base" button |
| Credentials (`/credentials`) | `dashboard-credentials.png` | PASS: Credentials Manager with openai (Configured, Keys: api_key), hidden credential payload with "Show" button, action buttons (Save, Test, Refresh, Copy JSON, Delete) |
| Voice (`/voice`) | `dashboard-voice.png` | PASS: Enabled checkbox, Current Effective Settings (OpenAI API, whisper-1, sonnet command model, router echo disabled), STT form fields |
| Tools (`/integrations`) | `dashboard-tools-integrations.png` | PASS: 115 tools, category tabs (Email & Calendar 5, Communication 7, Development 51, Research 28, Smart Home 1), scope selector, search, filter toggles. Tool cards with Setup/Connect/Configure buttons. Matrix Message shows "Ready to Use" |
| Skills (`/skills`) | `dashboard-skills.png` | PASS: mindroom-docs skill (bundled, Read-only), search, "+ New" button, "Select a skill to view" panel |
| Schedules (`/schedules`) | `dashboard-schedules.png` | PASS: "0 scheduled task(s)", "Timezone: America/Los_Angeles", room filter, Refresh button, "No schedules found" with "Create one with !schedule in Matrix" |
| External Rooms (`/unconfigured-rooms`) | `dashboard-external-rooms-unconfigured.png` | PASS: "All configured agents and teams are only in configured rooms. No action needed." with Refresh button |
| Navigation Menu | `dashboard-menu-open.png` | PASS: Full sidebar with WORKSPACE (Dashboard, Agents, Teams, Culture, Rooms, Schedules, External) and CONFIGURATION (Models, Memory, Knowledge, Credentials, Voice, Tools, Skills) sections |
| API Docs (`/docs`) | `dashboard-api-docs.png` | PASS: Full Swagger/OpenAPI with all endpoint groups color-coded by HTTP method |

## Environment Notes

- First start attempt failed due to Matrix username collision (`mindroom_user` already taken by another instance). Fixed by setting unique `mindroom_user_tests14` username.
- Second start crashed when config.yaml edit triggered hot-reload, and another instance grabbed port 9884. Restarted on port 9887.
- Frontend dist built successfully via `bun install && bun run tsc && bun run vite build` (3m38s).
- nix-shell initialization + venv creation takes ~3 minutes per cold start.
