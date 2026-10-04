---
icon: lucide/sparkles
---

# AI & Generation

These tools let an agent transcribe and translate audio, generate images, video, speech, music, and sound effects, and work with provider voices.
Pick the toolkit for the provider you have an API key for:

| Tool | Provider | Functions |
| --- | --- | --- |
| [`openai`](#openai) | OpenAI | Transcription, image generation, text-to-speech |
| [`gemini`](#gemini) | Google | Image generation (Nano Banana), video generation (Veo, Vertex AI only) |
| [`groq`](#groq) | Groq | Transcription, translation to English, text-to-speech |
| [`replicate`](#replicate) | Replicate | Image or video from any prompt-only model |
| [`fal`](#fal) | Fal | Image or video generation, image editing |
| [`cartesia`](#cartesia) | Cartesia | Voice listing, voice localization, text-to-speech |
| [`eleven_labs`](#eleven_labs) | ElevenLabs | Voice listing, sound effects, text-to-speech |
| [`lumalabs`](#lumalabs) | Luma AI | Text-to-video, image-to-video |
| [`modelslabs`](#modelslabs) | ModelsLab | Image, video, GIF, music, or sound-effect generation |

## Setup

Every tool on this page needs a provider API key, except `gemini` in Vertex AI mode, which uses Google Cloud authentication.
Set `api_key` through the dashboard or credential store, not inline in YAML (see [Security Restrictions](index.md#security-restrictions)).
When `api_key` is unset, each toolkit reads its provider environment variable, listed in its configuration table.
Missing Python dependencies install automatically on first use (see [Automatic Dependency Installation](index.md#automatic-dependency-installation)).
Every toolkit except `modelslabs` has `enable_*` fields to turn individual functions on or off, and `all: true` enables every function.
Generated media is attached to the agent's reply.
`openai`, `gemini`, `groq`, `cartesia`, and `eleven_labs` return the media files, while `replicate`, `fal`, `lumalabs`, and `modelslabs` return provider-hosted URLs.

## `openai`

`openai` provides `transcribe_audio(audio_path)`, `generate_image(prompt)`, and `generate_speech(text_input)`.
`transcribe_audio()` takes a local file path, not a URL.
The path follows the agent's [`file_access`](../architecture/security-posture.md#file-access): with the default `workspace` it must be a regular file inside the agent workspace, and relative paths resolve from there.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Falls back to `OPENAI_API_KEY`. |
| `enable_transcription` | `boolean` | `true` | Enable `transcribe_audio()`. |
| `enable_image_generation` | `boolean` | `true` | Enable `generate_image()`. |
| `enable_speech_generation` | `boolean` | `true` | Enable `generate_speech()`. |
| `all` | `boolean` | `false` | Enable all three functions. |
| `transcription_model` | `text` | `gpt-transcribe` | Model for `transcribe_audio()`. |
| `text_to_speech_voice` | `text` | `alloy` | Voice for `generate_speech()`. |
| `text_to_speech_model` | `text` | `gpt-4o-mini-tts` | Model for `generate_speech()`. |
| `text_to_speech_format` | `text` | `mp3` | Speech format, such as `mp3`, `wav`, or `opus`. |
| `image_model` | `text` | `gpt-image-2.5-sunburst` | Model for `generate_image()`. |
| `image_quality` | `text` | `null` | Passed through to the image API. |
| `image_size` | `text` | `null` | Passed through to the image API. |
| `image_style` | `text` | `null` | Passed through to the image API. |

```yaml
agents:
  illustrator:
    tools:
      - openai:
          enable_transcription: false
          enable_speech_generation: false
```

The `dalle` toolkit no longer exists, and a config that lists it fails with `Unknown tool 'dalle'.`
Replace the whole `dalle` block with the image-only `openai` block above, move supported overrides to the `image_*` options, and change instructions that call `create_image()` to `generate_image()`.
The `desi_vocal` toolkit also no longer exists; use `openai` or `eleven_labs` for speech.

## `gemini`

`gemini` provides `generate_image(prompt)` and `generate_video(prompt)`.
Generated images are square (1:1).
`generate_video()` works only in Vertex AI mode and otherwise returns `Video generation requires Vertex AI mode.`

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Gemini API key, falling back to `GOOGLE_API_KEY`. Required unless Vertex AI mode is on. |
| `vertexai` | `boolean` | `false` | Use Vertex AI instead of the Gemini API. `GOOGLE_GENAI_USE_VERTEXAI=true` also enables it. Required for `generate_video()`. |
| `project_id` | `text` | `null` | Vertex project, falling back to `GOOGLE_CLOUD_PROJECT`. |
| `location` | `text` | `null` | Vertex location, falling back to `GOOGLE_CLOUD_LOCATION`. |
| `image_generation_model` | `text` | `gemini-3.1-flash-image` | Model for `generate_image()` (Nano Banana 2). |
| `video_generation_model` | `text` | `veo-3.1-generate-001` | Model for `generate_video()` (Veo 3.1). |
| `enable_generate_image` | `boolean` | `true` | Enable `generate_image()`. |
| `enable_generate_video` | `boolean` | `true` | Enable `generate_video()`. |
| `all` | `boolean` | `false` | Enable both functions. |

One `gemini` toolkit uses a single Vertex location for both functions.
Google lists `global`, `us`, and `eu` for [Gemini 3.1 Flash Image](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-1-flash-image) and `us-central1` for [Veo 3.1](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/veo/3-1-generate), with no shared location.
To use both on Vertex AI, give each to a separate agent and disable the other function:

```yaml
agents:
  illustrator:
    tools:
      - gemini:
          vertexai: true
          project_id: my-gcp-project
          location: global
          enable_generate_video: false
  filmmaker:
    tools:
      - gemini:
          vertexai: true
          project_id: my-gcp-project
          location: us-central1
          enable_generate_image: false
```

## `groq`

`groq` provides `transcribe_audio(audio_source)`, `translate_audio(audio_source)`, and `generate_speech(text_input)`.
`transcribe_audio()` and `translate_audio()` accept a public `http` or `https` URL or a local file path.
A local path follows the agent's [`file_access`](../architecture/security-posture.md#file-access): with the default `workspace` it must be a regular file inside the agent workspace, and relative paths resolve from there.
`translate_audio()` translates the audio to English.
`generate_speech()` always returns WAV audio.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Falls back to `GROQ_API_KEY`. |
| `transcription_model` | `text` | `whisper-large-v3` | Model for `transcribe_audio()`. |
| `translation_model` | `text` | `whisper-large-v3` | Model for `translate_audio()`. |
| `tts_model` | `text` | `canopylabs/orpheus-v1-english` | Model for `generate_speech()`. |
| `tts_voice` | `text` | `troy` | Voice for `generate_speech()`. |
| `enable_transcribe_audio` | `boolean` | `true` | Enable `transcribe_audio()`. |
| `enable_translate_audio` | `boolean` | `true` | Enable `translate_audio()`. |
| `enable_generate_speech` | `boolean` | `true` | Enable `generate_speech()`. |
| `all` | `boolean` | `false` | Enable all three functions. |

## `replicate`

`replicate` provides `generate_media(prompt)`, which runs the configured Replicate model with only a `prompt` input.
It works only with models that accept a single `prompt` field and return files whose URLs end in an image or video extension.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Falls back to `REPLICATE_API_KEY`. `REPLICATE_API_TOKEN` is not read. |
| `model` | `text` | `minimax/h3` | Replicate model reference. |
| `enable_generate_media` | `boolean` | `true` | Enable `generate_media()`. |
| `all` | `boolean` | `false` | Enable `generate_media()`. |

## `fal`

`fal` provides `generate_media(prompt)` and, when enabled, `image_to_image(prompt, image_url)`.
`generate_media()` runs the configured `model` with only a `prompt` input.
`image_to_image()` edits the image at `image_url` with [Fal's FLUX.2 edit model](https://fal.ai/models/fal-ai/flux-2/edit/api), regardless of `model`.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Falls back to `FAL_API_KEY`. |
| `model` | `text` | `fal-ai/hunyuan-video-v1.5/text-to-video` | Model for `generate_media()`. |
| `enable_generate_media` | `boolean` | `true` | Enable `generate_media()`. |
| `enable_image_to_image` | `boolean` | `false` | Enable `image_to_image()`. |
| `all` | `boolean` | `false` | Enable both functions. |

## `cartesia`

`cartesia` provides `list_voices()`, `localize_voice(name, description, language, original_speaker_gender, voice_id=None)`, and `text_to_speech(transcript, voice_id=None)`.
`localize_voice()` creates a new voice in another language from an existing one.
Both `localize_voice()` and `text_to_speech()` use `default_voice_id` unless the call passes `voice_id`.
`text_to_speech()` always returns MP3 audio at 44.1 kHz and 128 kbps.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Falls back to `CARTESIA_API_KEY`. |
| `model_id` | `text` | `sonic-3.6` | Model for `text_to_speech()`. |
| `default_voice_id` | `text` | `78ab82d5-25be-4f7d-82b3-7ad64e5b85b2` | Voice used when a call passes no `voice_id`. |
| `enable_text_to_speech` | `boolean` | `true` | Enable `text_to_speech()`. |
| `enable_list_voices` | `boolean` | `true` | Enable `list_voices()`. |
| `enable_localize_voice` | `boolean` | `false` | Enable `localize_voice()`. |
| `all` | `boolean` | `false` | Enable all functions. |

## `eleven_labs`

`eleven_labs` provides `get_voices()`, `generate_sound_effect(prompt, duration_seconds=None)`, and `text_to_speech(prompt)`.
The default [Eleven v4 model](https://elevenlabs.io/docs/overview/models) accepts up to 10,000 characters per request.
Audio attachments are always labeled MP3 (`audio/mpeg`), even when `output_format` selects PCM or u-law.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Falls back to `ELEVEN_LABS_API_KEY`. |
| `voice_id` | `text` | `JBFqnCBsd6RMkjVDRZzb` | Voice for `text_to_speech()`. |
| `model_id` | `text` | `eleven_v4` | Model for `text_to_speech()`. |
| `output_format` | `text` | `mp3_44100_64` | Codec and bitrate preset. |
| `target_directory` | `text` | `null` | Also save generated audio files to this directory. |
| `enable_get_voices` | `boolean` | `true` | Enable `get_voices()`. |
| `enable_generate_sound_effect` | `boolean` | `true` | Enable `generate_sound_effect()`. |
| `enable_text_to_speech` | `boolean` | `true` | Enable `text_to_speech()`. |
| `all` | `boolean` | `false` | Enable all functions. |

## `lumalabs`

`lumalabs` provides `generate_video(prompt, loop=False, aspect_ratio="16:9", keyframes=None)` and `image_to_video(prompt, start_image_url, end_image_url=None, loop=False, aspect_ratio="16:9")`.
`image_to_video()` animates between one or two remote image URLs; local file paths are not accepted.
Both functions wait for the video until it finishes or `max_wait_time` passes.
Keep `wait_for_completion: true`, because `false` makes both functions return `Async generation unsupported` without a job handle.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Falls back to `LUMAAI_API_KEY`. |
| `model` | `select` | `ray-2` | [Dream Machine](https://docs.lumalabs.ai/docs/video-generation) model: `ray-2` or `ray-flash-2`. `null` means `ray-2`. |
| `wait_for_completion` | `boolean` | `true` | Wait for the finished video. |
| `poll_interval` | `number` | `3` | Seconds between status checks. |
| `max_wait_time` | `number` | `300` | Seconds to wait before timing out. |
| `enable_generate_video` | `boolean` | `true` | Enable `generate_video()`. |
| `enable_image_to_video` | `boolean` | `true` | Enable `image_to_video()`. |
| `all` | `boolean` | `false` | Enable both functions. |

## `modelslabs`

`modelslabs` provides `generate_media(prompt)`, and `file_type` decides what it generates:

| `file_type` | Output | Default model |
| --- | --- | --- |
| `png`, `jpg` | Image | `flux` |
| `mp4`, `gif` | Video | `cogvideox` |
| `mp3` | Music | Provider default |
| `wav` | 10-second sound effect | Provider default |

`model_id`, `width`, and `height` apply to images and video.
Without `wait_for_completion`, a queued job returns its URLs and the provider ETA, and those URLs may not be ready yet.
With `wait_for_completion: true`, the tool checks the job about once per second for the provider ETA plus `add_to_eta` attempts, capped at `max_wait_time` attempts, and returns the finished URLs.
If the job is still unfinished at the cap, the tool returns the queued URLs, and the job may still complete on the provider side.
A job the provider rejects returns the provider's message instead of media.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Falls back to `MODELS_LAB_API_KEY`. |
| `file_type` | `text` | `mp4` | `png`, `jpg`, `mp4`, `gif`, `mp3`, or `wav`. |
| `model_id` | `text` | `null` | Image or video model override. |
| `width` | `number` | `512` | Image or video width. |
| `height` | `number` | `512` | Image or video height. |
| `wait_for_completion` | `boolean` | `false` | Wait for the finished output. |
| `add_to_eta` | `number` | `15` | Extra one-second attempts added to the provider ETA. |
| `max_wait_time` | `number` | `60` | Maximum one-second attempts; `0` skips waiting. HTTP request time is additional. |

```yaml
agents:
  generator:
    tools:
      - modelslabs:
          file_type: gif
          wait_for_completion: true
          max_wait_time: 90
```

## Related Docs

- [Tools Overview](index.md)
- [OpenAI-Compatible API](../openai-api.md)
