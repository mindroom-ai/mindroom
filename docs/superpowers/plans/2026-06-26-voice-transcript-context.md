# Voice Transcript Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add hidden model-facing guidance so agents understand successful voice transcripts already came from the current audio attachment.

**Architecture:** Keep Matrix-visible voice behavior unchanged.
Add a small attachment-prompt helper that appends voice transcript guidance only when the current turn is a successful voice transcript with audio attachments.
Thread the voice source and raw-fallback flags through `DispatchPayloadWithAttachmentsRequest`.

**Tech Stack:** Python 3.13, pytest, existing MindRoom voice, attachment, and response payload modules.

---

## File Structure

- Modify `src/mindroom/inbound_turn_normalizer.py` to carry voice metadata into payload construction and append hidden guidance.
- Modify `src/mindroom/response_payload_preparation.py` to pass dispatch source kind and raw audio fallback into payload construction.
- Modify `src/mindroom/attachments.py` to render the hidden voice guidance from current audio attachment records.
- Modify `docs/voice.md` to document the hidden guidance behavior.
- Test in `tests/test_multi_agent_bot.py` because existing payload assembly tests live there with attachment/media helpers.

### Task 1: Add Hidden Guidance For Successful Voice Transcript Payloads

**Files:**
- Modify: `tests/test_multi_agent_bot.py`
- Modify: `src/mindroom/attachments.py`
- Modify: `src/mindroom/inbound_turn_normalizer.py`
- Modify: `src/mindroom/response_payload_preparation.py`
- Modify: `docs/voice.md`

- [ ] **Step 1: Write failing test for successful transcript guidance**

Add a test near `test_dispatch_payload_media_is_current_turn_only`:

```python
    @pytest.mark.asyncio
    async def test_voice_transcript_payload_adds_hidden_audio_guidance(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        current_attachment_id = "att_current_audio"
        current_path = _register_payload_media_attachment(
            tmp_path,
            kind="audio",
            attachment_id=current_attachment_id,
            filename="voice.ogg",
            content=b"audio bytes",
        )

        payload = await bot._inbound_turn_normalizer.build_dispatch_payload_with_attachments(
            DispatchPayloadWithAttachmentsRequest(
                room_id="!test:localhost",
                prompt="🎤 Please summarize the standup.",
                current_attachment_ids=[current_attachment_id],
                thread_id=None,
                media_thread_id=None,
                thread_history=[],
                source_kind=VOICE_SOURCE_KIND,
                raw_audio_fallback=False,
            ),
        )

        assert payload.prompt == "🎤 Please summarize the standup."
        assert payload.model_prompt is not None
        assert "MindRoom already transcribed the current voice message." in payload.model_prompt
        assert current_attachment_id in payload.model_prompt
        assert "Only inspect or re-transcribe" in payload.model_prompt
        assert [audio.id for audio in payload.media.audio] == [current_attachment_id]
        assert payload.media.audio[0].filepath == current_path
```

- [ ] **Step 2: Run failing test**

Run: `uv run pytest tests/test_multi_agent_bot.py::TestAgentBot::test_voice_transcript_payload_adds_hidden_audio_guidance -q`
Expected: FAIL because `DispatchPayloadWithAttachmentsRequest` does not accept `source_kind`.

- [ ] **Step 3: Write failing fallback test**

Add a second test next to the first one:

```python
    @pytest.mark.asyncio
    async def test_raw_voice_fallback_payload_does_not_claim_audio_was_transcribed(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        current_attachment_id = "att_raw_audio"
        _register_payload_media_attachment(
            tmp_path,
            kind="audio",
            attachment_id=current_attachment_id,
            filename="voice.ogg",
            content=b"audio bytes",
        )

        payload = await bot._inbound_turn_normalizer.build_dispatch_payload_with_attachments(
            DispatchPayloadWithAttachmentsRequest(
                room_id="!test:localhost",
                prompt="🎤 [Attached voice message]",
                current_attachment_ids=[current_attachment_id],
                thread_id=None,
                media_thread_id=None,
                thread_history=[],
                source_kind=VOICE_SOURCE_KIND,
                raw_audio_fallback=True,
            ),
        )

        assert payload.model_prompt is not None
        assert "Attachments sent with the current message" in payload.model_prompt
        assert "MindRoom already transcribed the current voice message." not in payload.model_prompt
```

- [ ] **Step 4: Run fallback test to verify it fails for the same missing field**

Run: `uv run pytest tests/test_multi_agent_bot.py::TestAgentBot::test_raw_voice_fallback_payload_does_not_claim_audio_was_transcribed -q`
Expected: FAIL because `DispatchPayloadWithAttachmentsRequest` does not accept `source_kind`.

- [ ] **Step 5: Add request fields and helper rendering**

Update `DispatchPayloadWithAttachmentsRequest` with:

```python
    source_kind: str | None = None
    raw_audio_fallback: bool = False
```

Add `format_voice_transcript_attachment_guidance()` in `attachments.py`:

```python
def format_voice_transcript_attachment_guidance(current_records: list[AttachmentRecord]) -> str | None:
    audio_ids = [record.attachment_id for record in current_records if record.kind == "audio"]
    if not audio_ids:
        return None
    rendered_ids = ", ".join(audio_ids)
    return (
        "MindRoom already transcribed the current voice message. "
        f"The raw audio attachment ID is available for verification or deeper audio work: {rendered_ids}. "
        "Only inspect or re-transcribe the raw audio if the user asks, the transcript seems wrong, "
        "or the task specifically requires audio-level analysis."
    )
```

- [ ] **Step 6: Append guidance only for successful voice transcripts**

In `build_dispatch_payload_with_attachments`, combine the normal attachment prompt with voice guidance only when `request.source_kind == VOICE_SOURCE_KIND` and `not request.raw_audio_fallback`.
Keep `payload.prompt` unchanged.

- [ ] **Step 7: Pass source metadata from response payload preparation**

Add a defaulted field to `DispatchPayloadInputs`:

```python
    raw_audio_fallback: bool = False
```

Set it at the call site in `text_ingress_dispatch.py`:

```python
    payload_inputs = DispatchPayloadInputs(
        message_attachment_ids=tuple(message_attachment_ids),
        trusted_attachment_ids=tuple(trusted_attachment_ids),
        media_events=tuple(media_events or ()),
        raw_audio_fallback=prepared.payload_metadata.raw_audio_fallback is True
        if prepared.payload_metadata is not None
        else False,
    )
```

Pass the metadata into `DispatchPayloadWithAttachmentsRequest` in `ResponsePayloadPreparer._build_payload`:

```python
                    source_kind=dispatch.envelope.source_kind,
                    raw_audio_fallback=payload_inputs.raw_audio_fallback,
```

- [ ] **Step 8: Run targeted tests**

Run: `uv run pytest tests/test_multi_agent_bot.py::TestAgentBot::test_voice_transcript_payload_adds_hidden_audio_guidance tests/test_multi_agent_bot.py::TestAgentBot::test_raw_voice_fallback_payload_does_not_claim_audio_was_transcribed -q`
Expected: PASS.

- [ ] **Step 9: Update docs**

In `docs/voice.md`, update Attachment access to say successful STT turns include hidden model guidance explaining that the transcript is already available and the raw audio is optional.

- [ ] **Step 10: Run verification**

Run: `uv run pytest tests/test_multi_agent_bot.py::TestAgentBot::test_voice_transcript_payload_adds_hidden_audio_guidance tests/test_multi_agent_bot.py::TestAgentBot::test_raw_voice_fallback_payload_does_not_claim_audio_was_transcribed tests/test_multi_agent_bot.py::TestAgentBot::test_dispatch_payload_media_is_current_turn_only -q`
Expected: PASS.
