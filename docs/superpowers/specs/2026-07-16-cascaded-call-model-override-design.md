# Cascaded Call Model Override Design

## Context

Cascaded MatrixRTC calls currently run every finalized transcript through the normal MindRoom agent response path.
This preserves the agent model, room overrides, prompts, memory, history, tools, and execution identity.
Voice calls need lower latency than text conversations, so one agent must be able to use a fast model during calls while keeping a larger model for text.

## Goals

Add an optional model override to each cascaded call profile.
Keep current model resolution unchanged when the override is omitted.
Preserve the normal agent execution path and all non-model behavior during cascaded calls.
Reject references to models that are not defined in the top-level `models` configuration.

## Configuration

`CascadedCallProfile` gains an optional `model` field.
The field contains a configured model alias from the top-level `models` mapping rather than a provider model ID.

```yaml
models:
  chat:
    provider: anthropic
    id: claude-opus-4-8
  call_fast:
    provider: anthropic
    id: claude-haiku-4-5

agents:
  assistant:
    model: chat

calls:
  enabled: true
  profiles:
    fast-cascaded:
      backend: cascaded
      model: call_fast
      stt:
        provider: openai
        model: gpt-4o-transcribe
        credentials_service: openai-voice
      tts:
        provider: openai
        model: tts-1
        credentials_service: openai-voice
  agents:
    assistant: fast-cascaded
```

Realtime profiles remain unchanged because their existing `model` field names the provider-specific OpenAI realtime speech model.
Documentation will state the different semantics for the discriminated profile variants.

## Runtime Resolution

The selected cascaded profile supplies its optional model alias when the call responder is built.
The responder carries that alias into the immutable response-turn context for each finalized transcript.
Normal agent preparation passes the alias to `Config.resolve_runtime_model` as its explicit active model.
An explicit call model therefore takes precedence over room and authored agent models.
When the profile omits `model`, the active model remains unset and the existing room and agent resolution path runs unchanged.
The resolved model continues to populate normal run metadata and token accounting.
The override applies only to the calls-enabled agent's turn and does not replace model selection for delegated agents.

## Validation and Errors

Configuration validation checks every explicit cascaded profile model against the top-level `models` mapping.
An unknown alias fails configuration loading with a message that identifies the call profile and invalid model.
There is no runtime fallback when an explicitly configured call model cannot be loaded or called.
Provider failures use the existing agent-response error behavior.

## Tests and Documentation

Configuration tests cover parsing an optional cascaded model, accepting omission, and rejecting an unknown alias.
Call-path tests prove that an explicit call model reaches runtime resolution and takes precedence over the agent and room model.
Regression tests prove that an omitted call model preserves current resolution and that realtime profiles are unchanged.
Run metadata assertions prove that the recorded model is the call-selected model.
Voice-call configuration documentation gains both override and fallback examples.

## Non-Goals

This change does not add inline provider configuration to call profiles.
This change does not add call-specific model overrides to realtime profiles.
This change does not change STT, TTS, call membership, transcript, memory, tool, or delegation behavior.
