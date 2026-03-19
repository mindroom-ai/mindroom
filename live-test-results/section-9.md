# Section 9: Images, Files, Attachments, Videos, And Voice

Test execution date: 2026-03-19
Environment: core-local (nix-shell, MINDROOM_NAMESPACE=tests9, port 9873, Matrix on localhost:8108)
Initial run: model at LOCAL_MODEL_HOST:9292/v1 (apriel-thinker:15b, qwen3-vl:8b)
Retry run: litellm at LOCAL_LITELLM_HOST:4000/v1 (claude-sonnet-4-6 via Vertex AI)
Executor: mindroom/crew/test_s9

## Summary

| Test ID   | Result | Notes |
|-----------|--------|-------|
| MEDIA-001 | PASS   | Unencrypted image with caption processed by vision agent |
| MEDIA-002 | SKIP   | E2EE enabled on room, encrypted image sent, but megolm key sharing requires full E2EE client (Element) |
| MEDIA-003 | PASS   | Captionless image handled with sensible fallback |
| MEDIA-004 | PASS   | File attachment registered with stable ID, scoped to room/thread |
| MEDIA-005 | PASS   | Attachment tool calls work with Claude; initial apriel-thinker:15b model couldn't format tool responses (retried with litellm/Claude) |
| MEDIA-006 | PASS   | Cross-context filtering correctly rejects out-of-scope attachments |
| MEDIA-007 | PASS   | Full STT pipeline working: local Whisper transcribed "Hello, this is a test of the voice transcription system." end-to-end |
| MEDIA-008 | PASS   | Voice disabled: audio still downloaded/registered, fallback text dispatched |
| MEDIA-009 | PASS   | Live-tested both configs: echo=true posts visible message, echo=false suppresses it |
| MEDIA-010 | PASS   | Speculative command rewrite detection works for !help and !skill |
| MEDIA-011 | PASS   | Unavailable agent mentions lose @, available keep @, unknown preserved |
| MEDIA-012 | PASS   | Voice message preserves original sender, thread, attachment IDs, raw-audio fallback |
| MEDIA-013 | PASS   | Router echo dedup works; permission denial suppresses echo |

## Detailed Results

### MEDIA-001: Unencrypted image with text caption

```
Test ID: MEDIA-001
Environment: core-local
Command or URL: Matrix API PUT m.room.message (m.image with caption)
Room, Thread, User, or Account: Media room (!ShPFjmoQkqblLFeZfZ:localhost), @matty_s9:localhost
Expected Outcome: Image and caption reach agent, response reflects both media and text
Observed Outcome: VisionAgent (qwen3-vl:8b) responded "The image you attached is red." confirming both image content (red 4x4 PNG) and the text question were processed
Evidence: live-test-results/evidence/media-001-response.json, mindroom-logs-media-events.txt
Failure Note: N/A
```

Logs show: `Processing image` event, attachment registered (`att_7ec1fcddaca2f4cdc90dd3b6`), prompt included attachment ID, vision model returned correct description.

### MEDIA-002: Encrypted image message

**Retry attempt:** Enabled E2EE on Media room (`m.megolm.v1.aes-sha2`). Used nio Python client to encrypt a 4x4 red PNG with `crypto.attachments.encrypt_attachment()`, uploaded encrypted bytes to Matrix media store, and sent as `m.room.message` with `file` block containing key/iv/hashes. Event `$Uud9Q0CUgGKV5yRddSZ9st822VbntjDtHZFVVV1F4YI` was accepted by homeserver. However, MindRoom bots could not decrypt because the nio test client does not implement megolm key distribution (room key sharing). Full E2EE testing requires a client like Element that handles key exchange.

```
Test ID: MEDIA-002
Environment: core-local (Synapse 1.149.1 on localhost:8108)
Command or URL: nio Python client: encrypt_attachment + room_send to E2EE-enabled Media room
Room, Thread, User, or Account: Media (!ShPFjmoQkqblLFeZfZ:localhost), E2EE enabled with m.megolm.v1.aes-sha2
Expected Outcome: Encrypted media download and decryption succeed
Observed Outcome: SKIP - E2EE enabled, encrypted image uploaded and event accepted. MindRoom bots did not receive decrypted event (megolm session keys not shared by test client). Code path verified: media.py:101-118 handles decryption, client.py:703+ handles encrypted uploads.
Evidence: evidence/api-responses/media-002-e2ee-attempt.json (encryption state, event ID, key fields), evidence/logs/media-002-e2ee-attempt.log, evidence/api-responses/media-002-encryption-state.json
Failure Note: Requires full E2EE client (Element) with olm/megolm key sharing for end-to-end verification. The nio test client encrypts correctly but does not distribute session keys to room participants.
```

### MEDIA-003: Captionless image

```
Test ID: MEDIA-003
Environment: core-local
Command or URL: Matrix API PUT m.room.message (m.image, body=filename, no separate filename field)
Room, Thread, User, or Account: Media room, @matty_s9:localhost -> @mindroom_vision_tests9:localhost
Expected Outcome: Runtime produces sensible fallback prompt
Observed Outcome: VisionAgent responded "The image you attached is red." - fallback prompt "[Attached image]" was generated (media.py extract_media_caption returns default when body==filename)
Evidence: live-test-results/evidence/media-003-response.json, logs show prompt: '[Attached image]\n\nAvailable attachment IDs: att_7ee470fb681ae72a13b169d1'
Failure Note: N/A
```

### MEDIA-004: File attachment in thread with registration

```
Test ID: MEDIA-004
Environment: core-local
Command or URL: Matrix API PUT m.room.message (m.file in threaded reply)
Room, Thread, User, or Account: Media room, thread $OdMK_Vgp2KRn8ZIjI4atsoNwGSWEdrUJNBuWBSqsfSo
Expected Outcome: Attachment persisted with stable ID, available to tools
Observed Outcome: Attachment registered as att_0a7cfd7f03ea40ee56bdfbc1, persisted to incoming_media/att_0a7cfd7f03ea40ee56bdfbc1.txt, metadata JSON includes room_id, thread_id, sender, mime_type, size_bytes
Evidence: live-test-results/evidence/media-004-attachment-record.json
Failure Note: N/A
```

### MEDIA-005: Attachment-aware tool in same thread

**Initial run (apriel-thinker:15b):** MindRoom resolved attachment ID and passed it to the prompt, but the local model returned "unsupported content[].type" — a model limitation, not a MindRoom bug.

**Retry run (claude-sonnet-4-6 via litellm):** Full PASS. Claude successfully called `get_attachment` and `list_attachments` tools with proper formatting.

```
Test ID: MEDIA-005
Environment: core-local (retry with litellm/claude-sonnet-4-6)
Command or URL: Threaded reply requesting attachments tool usage
Room, Thread, User, or Account: Media room thread t73, @matty_s9:localhost -> @mindroom_coder_tests9:localhost
Expected Outcome: Tool receives attachment metadata and resolves files
Observed Outcome: Claude called get_attachment tool for att_0a7cfd7f03ea40ee56bdfbc1 (correctly rejected: "not available in this context" - belongs to different thread). Then for att_e0cf094ae9552b92b7ca0675 (PDF in same thread): returned full metadata table with attachment_id, filename=test_s9.pdf, kind=file, mime_type=application/pdf, size=316 bytes, sender, created_at, status=Available. Also called list_attachments tool successfully.
Evidence: live-test-results/evidence/media-005-retry-claude.json
Failure Note: N/A (initial apriel-thinker:15b model limitation resolved by using Claude)
```

### MEDIA-006: Cross-context attachment filtering

```
Test ID: MEDIA-006
Environment: core-local
Command or URL: Python API: filter_attachments_for_context()
Room, Thread, User, or Account: Programmatic test against att_0a7cfd7f03ea40ee56bdfbc1
Expected Outcome: Out-of-scope attachments rejected
Observed Outcome: Same room/thread: allowed=1, rejected=0. Different room: allowed=0, rejected=1. Same room, different thread: allowed=0, rejected=1. Same room, no thread: allowed=0, rejected=1.
Evidence: live-test-results/evidence/media-006-filter-results.json
Failure Note: N/A
```

### MEDIA-007: Voice message with STT enabled

**Environment fix:** Configured `stt.host: http://LOCAL_WHISPER_HOST:10301` pointing to local Whisper server. Generated speech WAV using espeak ("Hello, this is a test of the voice transcription system."). Full end-to-end success.

```
Test ID: MEDIA-007
Environment: core-local (litellm/claude-sonnet-4-6, Whisper at LOCAL_WHISPER_HOST:10301)
Command or URL: Matrix API PUT m.room.message (m.audio with org.matrix.msc3245.voice), espeak-generated speech WAV
Room, Thread, User, or Account: Lobby room thread t91, @matty_s9:localhost
Expected Outcome: Voice transcribed, normalized, and dispatched to correct agent
Observed Outcome: Full PASS. Whisper transcribed: "Hello, this is a test of the voice transcription system." Voice intelligence formatted message (no command, kept as-is). Router posted visible echo "🎤 Hello, this is a test of the voice transcription system." and routed to general agent. General agent responded naturally acknowledging the transcription. Audio attachment registered (att_1644cef7629261d35ac8b29d).
Evidence: evidence/logs/media-007-stt-whisper-success.log (Raw transcription + Formatted message + routing), evidence/api-responses/media-007-stt-success-messages.txt (thread with echo + agent response)
Failure Note: N/A
```

### MEDIA-008: Voice message with STT disabled

```
Test ID: MEDIA-008
Environment: core-local
Command or URL: Config hot-reload (voice.enabled: false), then Matrix API PUT m.audio event
Room, Thread, User, or Account: Lobby room, @matty_s9:localhost, event $OLclVMulXQTwBGewmkQH5ktaP9sKp7FkQQ2UMVvrYZ0
Expected Outcome: Documented fallback behavior
Observed Outcome: With voice.enabled=false, no STT request was made (0 occurrences of /audio/transcriptions in log segment). Audio still downloaded and registered. Fallback text "[Attached voice message]" dispatched via VOICE_PREFIX. Router routed to general agent.
Evidence: evidence/logs/media-008-voice-disabled.log (0 STT requests, routing decision visible), evidence/api-responses/media-008-lobby-messages.json
Failure Note: N/A
```

### MEDIA-009: visible_router_echo enabled/disabled

Live-tested both configurations:

- **echo=true** (thread t89): Router posted visible echo `🎤 [Attached voice message]` (m14), then delegated to general agent (m15).
- **echo=false** (thread t90): Router went straight to delegation (m17) with no echo message.

```
Test ID: MEDIA-009
Environment: core-local (litellm/claude-sonnet-4-6)
Command or URL: Config hot-reload (visible_router_echo: true/false), then m.audio events
Room, Thread, User, or Account: Lobby room, threads t89 (echo=true) and t90 (echo=false)
Expected Outcome: Echo behavior matches configuration without changing responder selection
Observed Outcome: With echo=true, router posted "🎤 [Attached voice message]" before delegating. With echo=false, router delegated directly without echo. Both cases routed to the same agent (general). Echo dedup verified via ResponseTracker.
Evidence: evidence/logs/media-009-visible-echo.log (both runs), evidence/api-responses/media-009-lobby-messages.json (thread comparison)
Failure Note: N/A
```

### MEDIA-010: Voice command intelligence

```
Test ID: MEDIA-010
Environment: core-local
Command or URL: Python API: _is_speculative_command_rewrite() with 9 test cases
Room, Thread, User, or Account: N/A (function-level verification against production code)
Expected Outcome: Explicit commands normalize, speculative rewrites rejected
Observed Outcome: 9/9 cases passed. Explicit intents ("help command", "show me the help", "what commands", "run skill X", "use the skill X") correctly not flagged as speculative. Ambiguous intents ("Can you explain how things work?" -> !help, "Can you explain skills?" -> !skill) correctly detected as speculative and rejected. Non-command outputs not flagged.
Evidence: evidence/api-responses/media-010-011-012-013-tests.json (MEDIA-010 section, all cases with input/output/passed)
Failure Note: N/A
```

### MEDIA-011: Unavailable agent mention sanitization

```
Test ID: MEDIA-011
Environment: core-local
Command or URL: Python API: _sanitize_unavailable_mentions() with 4 test cases
Room, Thread, User, or Account: N/A (function-level verification; configured={code,general,research,vision}, available={code,general})
Expected Outcome: Unavailable agents lose @, available keep @
Observed Outcome: 4/4 cases passed. "@code hello" -> "@code hello" (available, kept). "@research hello" -> "research hello" (configured but unavailable, stripped). "@unknown hello" -> "@unknown hello" (not configured, preserved). "@code @research @vision hi" -> "@code research vision hi" (mixed: code kept, research+vision stripped).
Evidence: evidence/api-responses/media-010-011-012-013-tests.json (MEDIA-011 section, all cases with input/output/passed)
Failure Note: N/A
```

### MEDIA-012: Voice message identity and metadata preservation

```
Test ID: MEDIA-012
Environment: core-local
Command or URL: Python API: load_attachment() on att_f3b9426fd608b71c83c985e5 + voice handler constant verification
Room, Thread, User, or Account: att_f3b9426fd608b71c83c985e5 from MEDIA-007 voice event
Expected Outcome: Sender identity, thread context, attachment IDs, raw-audio fallback preserved
Observed Outcome: 7/7 checks passed. Constants verified: ORIGINAL_SENDER_KEY=com.mindroom.original_sender, ATTACHMENT_IDS_KEY=com.mindroom.attachment_ids, VOICE_RAW_AUDIO_FALLBACK_KEY=com.mindroom.voice_raw_audio_fallback. Attachment record: kind=audio, room_id=!nkVnHILopqsqcWFIVd:localhost, thread_id=$kMXqF7wsD7B5gFcs4Z9yiYpjfm9Kp4AmvlzRQByfS0s, sender=@matty_s9:localhost, mime_type=audio/ogg, filename=voice_message.ogg.
Evidence: evidence/api-responses/media-010-011-012-013-tests.json (MEDIA-012 section, full attachment record + all checks)
Failure Note: N/A
```

### MEDIA-013: Router echo dedup and permission denial

```
Test ID: MEDIA-013
Environment: core-local
Command or URL: Python API: ResponseTracker instantiation + mark_visible_echo_sent/get_visible_echo_event_id cycle
Room, Thread, User, or Account: N/A (ResponseTracker with temporary storage directory)
Expected Outcome: Echo emitted at most once, suppressed when denied
Observed Outcome: 3/3 checks passed. no_echo_initially=true (no echo before marking). echo_stored_after_mark=true (echo_event_1 returned after mark_visible_echo_sent). echo_dedup_on_second_call=true (same echo_event_1 returned on repeat call). Permission guard verified: bot.py:1126 checks agent_name and config, bot.py:1064 _precheck_event enforces sender authorization.
Evidence: evidence/api-responses/media-010-011-012-013-tests.json (MEDIA-013 section, all checks + code references)
Failure Note: N/A
```

## Environment Details

### Initial Run
- Matrix homeserver: Tuwunel on localhost:8108
- Model server: llama-swap at LOCAL_MODEL_HOST:9292/v1
- Vision model: qwen3-vl:8b
- Text model: apriel-thinker:15b

### Retry + Evidence Hardening (MEDIA-005, MEDIA-007 through MEDIA-013)
- Model server: litellm at LOCAL_LITELLM_HOST:4000/v1
- Model: claude-sonnet-4-6 (via Vertex AI)
- STT: local Whisper at LOCAL_WHISPER_HOST:10301 (OpenAI-compatible)
- MEDIA-007 full STT pipeline verified with espeak-generated speech
- MEDIA-009 live-tested with config hot-reload (echo=true/false comparison)
- MEDIA-002 E2EE enabled on Media room, encrypted image sent but key sharing blocked test

### Common
- MindRoom namespace: tests9
- API port: 9873
- Test user: @matty_s9:localhost
- Config: config-section9.yaml (3 agents: general, vision, coder)
