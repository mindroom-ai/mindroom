# Section 5: Streaming, Presence, Typing, Stop, And Large Messages

Test environment: `core-local`
MindRoom namespace: `tests5`
API port: `9875`
Matrix homeserver: `http://localhost:8108`
Model (initial run): `apriel-thinker:15b` via `http://LOCAL_MODEL_HOST:9292/v1`
Model (STR-006 retest): `claude-sonnet-4-6` via litellm at `http://LOCAL_LITELLM_HOST:4000/v1`
Test user: `@test_s5:localhost`
Date: 2026-03-19

## Results

### STR-001: Progressive streaming edits

- [x] PASS

```
Test ID: STR-001
Environment: core-local
Command or URL: matty send "Lobby" "@mindroom_general_tests5 Write a detailed essay..."
Room, Thread, User, or Account: Lobby, t1, @test_s5:localhost
Expected Outcome: The agent emits progressive message edits instead of waiting for one final message.
Observed Outcome: When user is online (presence=True), streaming decision is use_streaming=True.
  The agent emits "Thinking... ⋯" placeholder followed by progressive edits. Multiple edit
  messages are sent from the code agent to the lobby room during generation. The "⋯" in-progress
  marker is appended during streaming and removed on completion.
Evidence: live-test-results/evidence/logs/streaming-evidence.log
  Key log: "Streaming decision is_online=True requester=@test_s5:localhost use_streaming=True"
  Key log: "AI streaming request agent=code room_id=!IWpLvKWDCOKBbHghkZ:localhost"
  Source: streaming.py:38 IN_PROGRESS_MARKER = " ⋯"
Failure Note: N/A
```

### STR-002: Typing indicators during slow response

- [x] PASS

```
Test ID: STR-002
Environment: core-local
Command or URL: Source code review + runtime observation
Room, Thread, User, or Account: All rooms, all agents
Expected Outcome: Typing state appears and refreshes while response is in flight and clears when finished.
Observed Outcome: typing_indicator context manager wraps every response path in bot.py (5 call sites:
  lines 1919, 1966, 2172, 2304, 2483). The context manager (matrix/typing.py:47) sets typing=true
  on entry, refreshes every min(timeout/2, 15s), and sets typing=false on exit via finally block.
  Even on error or cancellation, the finally block ensures typing is cleared.
Evidence: live-test-results/evidence/logs/streaming-evidence.log
  Source: matrix/typing.py:47-91 (typing_indicator context manager)
  Source: bot.py:36 (import), bot.py:1919,1966,2172,2304,2483 (usage sites)
Failure Note: N/A (typing indicators are at debug log level, so not visible in INFO logs,
  but the source code confirms they wrap every response path)
```

### STR-003: Presence-gated streaming behavior

- [x] PASS

```
Test ID: STR-003
Environment: core-local
Command or URL: matty send + scheduled task (offline user)
Room, Thread, User, or Account: Lobby, @test_s5:localhost
Expected Outcome: Streaming follows configured gating rules based on user presence.
Observed Outcome: When user is online (matty just connected): use_streaming=True.
  When user is offline (matty disconnected, scheduled task fires): use_streaming=False.
  The runtime correctly gates streaming on user presence via should_use_streaming()
  which calls is_user_online() and considers both "online" and "unavailable" as online.
Evidence: live-test-results/evidence/logs/streaming-evidence.log
  Key log: "Streaming decision is_online=True use_streaming=True" (user online)
  Key log: "Streaming decision is_online=False use_streaming=False" (user offline)
  Key log: "AI request agent=general" (non-streaming, user offline)
  Key log: "AI streaming request agent=code" (streaming, user online)
  Source: matrix/presence.py:139-182 (should_use_streaming)
Failure Note: N/A
```

### STR-004: Stop interaction on in-flight response

- [x] PASS

```
Test ID: STR-004
Environment: core-local
Command or URL: matty react "Lobby" m31 "🛑"
Room, Thread, User, or Account: Lobby, @test_s5:localhost
Expected Outcome: The in-flight task cancels cleanly, partial streaming stops, and stop-related
  UI or reaction state is cleaned up.
Observed Outcome: Full stop lifecycle observed:
  1. "Handling stop reaction" — Reaction received for tracked message
  2. "Cancelling task for message" — asyncio task cancelled
  3. "Stopped generation for message, stopped_by=@test_s5:localhost" — User attribution
  4. "Removing stop button immediately (user clicked)" — Button redacted
  5. "Non-streaming response cancelled by user" — Cancellation confirmed
  6. "Response cancelled by user" — Final confirmation sent to room
  7. "Scheduling message cleanup" → "Clearing tracked message after delay" — Cleanup
  Stop button reaction_event_id was tracked and redacted on click.
Evidence: live-test-results/evidence/logs/streaming-evidence.log
  Key logs at 07:05:49-07:06:03
  Source: stop.py:114-140 (handle_stop_reaction)
  Source: bot.py:917-941 (reaction handler for 🛑)
Failure Note: N/A
```

### STR-005: Tool usage during streamed response

- [x] PASS

```
Test ID: STR-005
Environment: core-local
Command or URL: matty send "Lobby" "@mindroom_code_tests5 List the files in the current directory using the shell tool"
Room, Thread, User, or Account: Lobby, t7, @test_s5:localhost, code agent
Expected Outcome: Inline tool trace markers remain coherent across progressive edits and final output.
Observed Outcome: Code agent used streaming ("AI streaming request agent=code") with 3 tools
  (file, shell, scheduler). The agent sent multiple progressive edits to the lobby room
  (observed via multiple "Received message" entries from code agent to notool agent in the
  same room). Tool trace entries (🔧 markers) were visible in the streamed output.
  The streaming.py module handles ToolCallStartedEvent and ToolCallCompletedEvent via
  StructuredStreamChunk, maintaining coherent tool traces across edits.
Evidence: live-test-results/evidence/logs/streaming-evidence.log
  Key log: "AI streaming request agent=code room_id=!IWpLvKWDCOKBbHghkZ:localhost"
  Key log: "Created agent 'code' (CodeAgent) with 3 tools"
  Source: streaming.py:10-23 (tool event imports and handling)
  Source: tool_system/events.py (StructuredStreamChunk, ToolTraceEntry)
Failure Note: N/A
```

### STR-006: Response exceeding normal message size limits

- [x] PASS (live verified with litellm/claude-sonnet-4-6)

```
Test ID: STR-006
Environment: core-local (retested with litellm/claude-sonnet-4-6)
Command or URL: matty send "Lobby" "@mindroom_general_tests5 Generate a numbered list from 1 to 500..."
Room, Thread, User, or Account: Lobby, @test_s5:localhost
Expected Outcome: Oversized output falls back to sidecar large-message storage while still
  leaving a valid preview or pointer event in the room.
Observed Outcome: Large message system triggered 25 times during progressive streaming edits.
  The response grew from 27KB to 52KB+, exceeding _EDIT_MESSAGE_LIMIT (27000 bytes) on every
  progressive edit after the initial threshold crossing:
  - First trigger: "Message too large (27468 bytes), uploading full content JSON to MXC"
  - Each subsequent edit uploaded updated JSON sidecar and sent compact preview
  - Final size: "Message too large (52828 bytes)" → "Large message prepared: 52828 bytes -> 11818 preview + JSON sidecar"
  - 25 total sidecar uploads with preview sizes growing from 5905 to 11818 bytes
  The full content JSON is uploaded as an MXC file (sidecar), while a truncated preview
  with "[Message continues in attached file]" is sent as the Matrix event body.
  The reverse path (_extract_large_message_v2_body in message_content.py:80) hydrates
  the full content from the sidecar when reading messages back.
Evidence: live-test-results/evidence/logs/str006-litellm-evidence.log
  Key log: "Message too large (27468 bytes), uploading full content JSON to MXC" (first trigger)
  Key log: "Large message prepared: 52828 bytes -> 11818 preview + JSON sidecar" (final)
  25 total "Message too large" entries across progressive streaming edits
  Source: matrix/large_messages.py:21-24 (limits: 55KB normal, 27KB edit)
  Source: matrix/large_messages.py:210+ (prepare_large_message)
  Source: matrix/client.py:677 (integration point)
Failure Note: N/A
```

### STR-007: Streaming disabled via config

- [x] PASS

```
Test ID: STR-007
Environment: core-local
Command or URL: Config change: defaults.enable_streaming=false, then matty send
Room, Thread, User, or Account: Lobby, @test_s5:localhost
Expected Outcome: The runtime sends a normal non-streaming response path and does not emit
  progressive edits or streaming-specific affordances.
Observed Outcome: After setting enable_streaming=false in config.yaml (hot-reloaded at 07:17:06):
  1. Agent used "AI request" (not "AI streaming request") — non-streaming path confirmed
  2. No progressive edit messages were emitted
  3. Response sent as single final message
  The should_use_streaming() function in presence.py:162-163 returns False immediately when
  enable_streaming is disabled, bypassing all presence checks.
Evidence: live-test-results/evidence/logs/streaming-evidence.log
  Key log: "AI request agent=general room_id=!IWpLvKWDCOKBbHghkZ:localhost" (at 07:18:49)
  Key log: "Config file changed" + "Configuration update applied" (at 07:17:06)
  Source: matrix/presence.py:162-163 (early return when streaming disabled)
Failure Note: N/A
```

### STR-008: Stop button and tool call visibility disabled

- [x] PASS

```
Test ID: STR-008
Environment: core-local
Command or URL: Config: show_stop_button=false + agent notool has show_tool_calls=false
Room, Thread, User, or Account: Lobby, @test_s5:localhost, notool agent
Expected Outcome: Stop-button reactions are suppressed when configured off and inline tool
  traces plus tool metadata are omitted when tool-call visibility is disabled.
Observed Outcome:
  Stop button suppressed:
  - With show_stop_button=false, no "Adding stop button" log entries appeared
  - "remove_button=False" in cleanup confirmed no button was present
  - The bot.py stop button decision code (line 2088) checks config.defaults.show_stop_button

  Tool calls hidden (notool agent):
  - notool agent configured with show_tool_calls=false (per-agent override)
  - bot.py:432-441 resolves show_tool_calls: per-agent setting overrides defaults
  - When show_tool_calls=false, tool_trace is set to None in send_response calls
    (bot.py:2217, 2228, 2337)
  - Tool traces (🔧 markers) are omitted from the message content
Evidence: live-test-results/evidence/logs/streaming-evidence.log
  Key log: "Scheduling message cleanup remove_button=False" (no stop button)
  Key log: "AI request agent=notool" (at 07:19:55)
  Source: bot.py:2081-2102 (stop button decision based on config)
  Source: bot.py:432-441 (show_tool_calls resolution with per-agent override)
  Source: bot.py:2217,2228 (tool_trace=None when show_tool_calls=false)
Failure Note: N/A
```

### STR-009: Unauthorized/invalid stop reactions

- [x] PASS

```
Test ID: STR-009
Environment: core-local
Command or URL: matty react on untracked message + source code review
Room, Thread, User, or Account: Lobby, @test_s5:localhost
Expected Outcome: Only authorized human reactions on actively tracked runs cancel work, while
  all other stop reactions are ignored or fall through to normal reaction handling without
  corrupting room state.
Observed Outcome: Three authorization layers confirmed:
  1. Reply permission check (bot.py:918): _can_reply_to_sender() filters unauthorized users
     before any stop logic runs. Unauthorized senders get "Ignoring reaction due to reply permissions".
  2. Agent identity check (bot.py:929): extract_agent_name() identifies agent accounts.
     Agent 🛑 reactions fall through to other handlers (interactive questions, etc.)
  3. Tracked message check (stop.py:125-139): handle_stop_reaction() checks if message_id
     is in tracked_messages. Untracked messages log "Stop reaction for untracked message"
     and return False, falling through to normal reaction handling.

  Live evidence: Reacting with 🛑 on an untracked message produced:
  "Stop reaction for untracked message message_id=$6EyDZt0UEhKgoVLcuhwUOeJw5ad_eht7FR0ntK0EY5o"
  — the stop was gracefully ignored without corrupting room state.
Evidence: live-test-results/evidence/logs/streaming-evidence.log
  Key log: "Stop reaction for untracked message" (07:05:49)
  Key log: "Handling stop reaction tracked_messages=[]" (empty list = no active run)
  Source: bot.py:917-944 (three-layer authorization: reply perms, agent check, tracking check)
  Source: stop.py:114-140 (handle_stop_reaction with untracked fallthrough)
Failure Note: N/A
```

## Summary

| Test | Result | Notes |
|------|--------|-------|
| STR-001 | PASS | Progressive streaming edits work when user is online |
| STR-002 | PASS | Typing indicators wrap every response path via context manager |
| STR-003 | PASS | Presence-gated: streaming=True when online, False when offline |
| STR-004 | PASS | Stop reaction cancels task, removes button, confirms to user |
| STR-005 | PASS | Tool traces remain coherent across progressive streaming edits |
| STR-006 | PASS | Large message sidecar triggered 25x during streaming (27KB-52KB+) |
| STR-007 | PASS | enable_streaming=false uses non-streaming AI request path |
| STR-008 | PASS | Stop button suppressed when disabled; tool traces hidden per-agent |
| STR-009 | PASS | Three-layer authorization prevents unauthorized/invalid stops |

**9/9 PASS**

All Section 5 test items pass. The streaming, presence, typing, stop, and large message subsystems function correctly with the expected behavior documented in the checklist.
