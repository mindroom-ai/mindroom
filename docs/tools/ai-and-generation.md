---
icon: lucide/wrench
---

# AI & Generation

Use these tools to transcribe audio, generate images and videos, synthesize speech, and call provider-hosted media generation APIs.

## What This Page Covers

This page documents the built-in tools in the `ai-and-generation` group.
Use these tools when you need OpenAI- or Google-style multimodal generation, provider-specific media APIs, or text-to-speech and audio workflows.

## Tools On This Page

- [`openai`] - OpenAI-backed transcription, image generation, and text-to-speech.
- [`gemini`] - Google-backed image generation and Vertex-only video generation.
- [`groq`] - Groq-backed audio transcription, translation, and speech generation.
- [`replicate`] - Replicate-hosted image or video generation from prompt-driven models.
- [`fal`] - Fal-hosted media generation and a fixed image-to-image workflow.
- [`dalle`] - Dedicated OpenAI DALL-E image generation.
- [`cartesia`] - Voice listing, voice localization, and text-to-speech.
- [`eleven_labs`] - Voice listing, sound effect generation, and text-to-speech.
- [`desi_vocal`] - Hindi and Indian-language voice listing and text-to-speech.
- [`lumalabs`] - Luma AI video generation and image-to-video workflows.
- [`modelslabs`] - ModelsLab media generation for MP4, GIF, MP3, and WAV outputs.

## Common Setup Notes

Every tool on this page is `status=requires_config` in the live registry and is meant to be configured with provider credentials.
These tools do not use an `auth_provider`, and `src/mindroom/api/integrations.py` currently only exposes Spotify OAuth routes, so setup is done through stored tool credentials or provider SDK environment variables rather than a dedicated dashboard OAuth flow.
Password fields such as `api_key` should be stored through the dashboard or credential store instead of inline YAML.
Missing optional dependencies can auto-install at first use unless `MINDROOM_NO_AUTO_INSTALL_TOOLS=1` is set.
Most generation calls on this page return `ToolResult` media attachments rather than only raw text, so they are best suited to agents that can pass generated images, videos, or audio back to the user.
`openai` and `dalle` both use the OpenAI Python SDK and the same `OPENAI_API_KEY`, but they expose different tool surfaces.
`gemini` uses `GOOGLE_API_KEY` in Gemini API mode, and MindRoom also maps provider name `gemini` to shared Google credentials in its provider credential helpers.
The current upstream SDK implementations also honor provider env vars such as `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `GROQ_API_KEY`, `REPLICATE_API_KEY`, `FAL_API_KEY`, `CARTESIA_API_KEY`, `ELEVEN_LABS_API_KEY`, `DESI_VOCAL_API_KEY`, `LUMAAI_API_KEY`, and `MODELS_LAB_API_KEY`.

## [`openai`]

`openai` is the general OpenAI media toolkit for audio transcription, image generation, and text-to-speech.

### What It Does

`openai` exposes `transcribe_audio(audio_path)`, `generate_image(prompt)`, and `generate_speech(text_input)`.
`transcribe_audio()` expects a local file path and sends it to the configured transcription model, which defaults to `whisper-1`.
`generate_image()` uses the configured `image_model`, defaults to `dall-e-3`, and returns attached image bytes rather than only a remote URL.
The current implementation handles both `gpt-image-*` style models and older DALL-E response formats internally.
`generate_speech()` uses the configured OpenAI TTS model, voice, and output format and returns an attached audio artifact.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `api_key` | `password` | `yes` | `null` | OpenAI API key. The upstream SDK also checks `OPENAI_API_KEY`. |
| `enable_transcription` | `boolean` | `no` | `true` | Enable `transcribe_audio()`. |
| `enable_image_generation` | `boolean` | `no` | `true` | Enable `generate_image()`. |
| `enable_speech_generation` | `boolean` | `no` | `true` | Enable `generate_speech()`. |
| `all` | `boolean` | `no` | `false` | Enable all three OpenAI media functions. |
| `transcription_model` | `text` | `no` | `whisper-1` | Model used by `transcribe_audio()`. |
| `text_to_speech_voice` | `text` | `no` | `alloy` | Default voice for `generate_speech()`. |
| `text_to_speech_model` | `text` | `no` | `tts-1` | Default TTS model for `generate_speech()`. |
| `text_to_speech_format` | `text` | `no` | `mp3` | Output format for generated speech, such as `mp3`, `wav`, or `opus`. |
| `image_model` | `text` | `no` | `dall-e-3` | Image generation model for `generate_image()`. |
| `image_quality` | `text` | `no` | `null` | Optional image quality override passed through to the API. |
| `image_size` | `text` | `no` | `null` | Optional image size override passed through to the API. |
| `image_style` | `text` | `no` | `null` | Optional image style override passed through to the API. |

### Example

```yaml
agents:
  creator:
    tools:
      - openai:
          transcription_model: whisper-1
          image_model: dall-e-3
          text_to_speech_voice: alloy
```

```python
transcribe_audio("recordings/intro.wav")
generate_image("A retro-futurist Matrix control room with warm lighting.")
generate_speech("Status update complete.")
```

### Notes

- `openai` is the broad OpenAI media tool, while [`dalle`] is the narrower image-only wrapper.
- `transcribe_audio()` expects a readable local path, not a URL.
- If you only want image generation with explicit DALL-E-specific options like `n`, `size`, `quality`, and `style`, use [`dalle`] instead.

## [`gemini`]

`gemini` is the Google media toolkit for image generation through Imagen and video generation through Veo.

### What It Does

`gemini` exposes `generate_image(prompt)` and `generate_video(prompt)`.
`generate_image()` uses the configured `image_generation_model`, which defaults to `imagen-3.0-generate-002`, and returns attached image bytes.
`generate_video()` uses the configured `video_generation_model`, which defaults to `veo-2.0-generate-001`, polls until the long-running operation completes, and returns attached video artifacts.
The current implementation requires Vertex AI mode for video generation and returns an error if `vertexai` is not enabled.
In non-Vertex mode, the tool uses the Gemini API through `GOOGLE_API_KEY`.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `api_key` | `password` | `no` | `null` | Google API key for Gemini API mode. Required unless `vertexai: true` with working Vertex configuration. |
| `vertexai` | `boolean` | `no` | `false` | Use Vertex AI instead of the direct Gemini API. Required for `generate_video()`. |
| `project_id` | `text` | `no` | `null` | Vertex project override. Falls back to `GOOGLE_CLOUD_PROJECT` when omitted. |
| `location` | `text` | `no` | `null` | Vertex location override. Falls back to `GOOGLE_CLOUD_LOCATION` when omitted. |
| `image_generation_model` | `text` | `no` | `imagen-3.0-generate-002` | Model used by `generate_image()`. |
| `video_generation_model` | `text` | `no` | `veo-2.0-generate-001` | Model used by `generate_video()`. |
| `enable_generate_image` | `boolean` | `no` | `true` | Enable `generate_image()`. |
| `enable_generate_video` | `boolean` | `no` | `true` | Enable `generate_video()`. |
| `all` | `boolean` | `no` | `false` | Enable both generation functions. |

### Example

```yaml
agents:
  studio:
    tools:
      - gemini:
          vertexai: true
          project_id: my-gcp-project
          location: us-central1
          image_generation_model: imagen-3.0-generate-002
          video_generation_model: veo-2.0-generate-001
```

```python
generate_image("A minimal poster for a Matrix developer conference.")
generate_video("A slow cinematic flythrough of a neon data center.")
```

### Notes

- `generate_video()` only works in Vertex AI mode on this branch.
- In MindRoom's provider credential helpers, `gemini` maps to shared Google credentials rather than its own independent provider bucket.
- The current tool polls every 5 seconds until the video operation finishes, and that polling interval is not exposed as a tool config field.

## [`groq`]

`groq` is the audio-focused toolkit for fast transcription, translation, and speech generation.

### What It Does

`groq` exposes `transcribe_audio(audio_source)`, `translate_audio(audio_source)`, and `generate_speech(text_input)`.
`transcribe_audio()` and `translate_audio()` accept either a local file path or a public URL.
`translate_audio()` translates the source audio to English using the configured translation model.
`generate_speech()` uses the configured Groq TTS model and voice and returns an attached WAV artifact.
All three functions use the Groq SDK directly and require a Groq API key.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `api_key` | `password` | `yes` | `null` | Groq API key. The upstream SDK also checks `GROQ_API_KEY`. |
| `transcription_model` | `text` | `no` | `whisper-large-v3` | Model used by `transcribe_audio()`. |
| `translation_model` | `text` | `no` | `whisper-large-v3` | Model used by `translate_audio()`. |
| `tts_model` | `text` | `no` | `playai-tts` | Model used by `generate_speech()`. |
| `tts_voice` | `text` | `no` | `Chip-PlayAI` | Voice used by `generate_speech()`. |
| `enable_transcribe_audio` | `boolean` | `no` | `true` | Enable `transcribe_audio()`. |
| `enable_translate_audio` | `boolean` | `no` | `true` | Enable `translate_audio()`. |
| `enable_generate_speech` | `boolean` | `no` | `true` | Enable `generate_speech()`. |
| `all` | `boolean` | `no` | `false` | Enable all three audio functions. |

### Example

```yaml
agents:
  audio:
    tools:
      - groq:
          transcription_model: whisper-large-v3
          tts_model: playai-tts
          tts_voice: Chip-PlayAI
```

```python
transcribe_audio("samples/interview.mp3")
translate_audio("https://example.com/spanish-briefing.mp3")
generate_speech("Your transcript is ready.")
```

### Notes

- `transcribe_audio()` and `translate_audio()` are more flexible than [`openai`] because they accept either local files or public URLs.
- The current Groq TTS path always asks the API for `wav` output and returns an `audio/wav` artifact.
- Use [`openai`] instead if you want OpenAI Whisper or OpenAI TTS specifically.

## [`replicate`]

`replicate` is the generic Replicate wrapper for prompt-driven image or video generation.

### What It Does

`replicate` exposes one call, `generate_media(prompt)`.
It runs the configured Replicate model with `input={"prompt": prompt}` and expects one `FileOutput` or an iterable of `FileOutput` objects.
The current implementation infers whether each output is an image or a video from the returned file URL extension.
Generated artifacts are attached by remote URL rather than downloaded into MindRoom-managed bytes.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `api_key` | `password` | `yes` | `null` | Replicate API key. The upstream implementation also checks `REPLICATE_API_KEY`. |
| `model` | `text` | `no` | `minimax/video-01` | Replicate model ref used by `generate_media()`. |
| `enable_generate_media` | `boolean` | `no` | `true` | Enable `generate_media()`. |
| `all` | `boolean` | `no` | `false` | Enable the full toolkit, which is currently just `generate_media()`. |

### Example

```yaml
agents:
  video:
    tools:
      - replicate:
          model: minimax/video-01
```

```python
generate_media("A short looping animation of code flowing across a terminal.")
```

### Notes

- The current wrapper only supports models that accept a single `prompt` input field.
- Output parsing depends on file extensions in returned URLs, so nonstandard model outputs can fail even if the Replicate run itself succeeds.
- Use [`fal`], [`lumalabs`], or [`modelslabs`] instead when you want a narrower wrapper with a more opinionated provider-specific flow.

## [`fal`]

`fal` is the Fal wrapper for prompt-driven media generation plus a dedicated image-to-image path.

### What It Does

`fal` exposes `generate_media(prompt)` and, when enabled, `image_to_image(prompt, image_url=None)`.
`generate_media()` calls `fal_client.subscribe()` with the configured `model` and a single `prompt` argument and returns the first `image` or `video` URL from the provider result.
`image_to_image()` is a separate fixed workflow that always uses `fal-ai/flux/dev/image-to-image` rather than the configured `model`.
The current implementation streams queue log messages to the MindRoom process logs while the job is running.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `api_key` | `password` | `yes` | `null` | Fal API key. The upstream implementation also checks `FAL_API_KEY`. |
| `model` | `text` | `no` | `fal-ai/hunyuan-video` | Model used by `generate_media()`. |
| `enable_generate_media` | `boolean` | `no` | `true` | Enable `generate_media()`. |
| `enable_image_to_image` | `boolean` | `no` | `false` | Enable `image_to_image()`. |
| `all` | `boolean` | `no` | `false` | Enable both Fal functions. |

### Example

```yaml
agents:
  visuals:
    tools:
      - fal:
          model: fal-ai/hunyuan-video
          enable_image_to_image: true
```

```python
generate_media("A cinematic drone shot over a rainy cyberpunk street.")
image_to_image(
    "Turn this product photo into a watercolor illustration.",
    image_url="https://example.com/source.png",
)
```

### Notes

- `model` only affects `generate_media()`.
- `image_to_image()` ignores `model` and always calls Fal's `fal-ai/flux/dev/image-to-image` route on this branch.
- Returned media are attached by remote URL rather than stored bytes.

## [`dalle`]

`dalle` is the dedicated DALL-E image generation wrapper.

### What It Does

`dalle` exposes one call, `create_image(prompt)`.
It uses the OpenAI image API directly with the configured `model`, `n`, `size`, `quality`, and `style`.
Unlike [`openai`], this wrapper is image-only and exposes DALL-E-specific request options directly in the tool config.
Generated images are returned as provider-hosted URLs with optional revised prompts when the API supplies them.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `model` | `text` | `no` | `dall-e-3` | DALL-E model used by `create_image()`. The current implementation only accepts `dall-e-3` or `dall-e-2`. |
| `n` | `number` | `no` | `1` | Number of images to request. `dall-e-3` only supports `1` in the current implementation. |
| `size` | `text` | `no` | `1024x1024` | Output size. The current implementation validates it against a fixed allowed set. |
| `quality` | `text` | `no` | `standard` | Image quality, currently `standard` or `hd`. |
| `style` | `text` | `no` | `vivid` | Image style, currently `vivid` or `natural`. |
| `api_key` | `password` | `yes` | `null` | OpenAI API key. The upstream SDK also checks `OPENAI_API_KEY`. |
| `enable_create_image` | `boolean` | `no` | `true` | Enable `create_image()`. |
| `all` | `boolean` | `no` | `false` | Enable the full toolkit, which is currently just `create_image()`. |

### Example

```yaml
agents:
  illustrator:
    tools:
      - dalle:
          model: dall-e-3
          size: 1792x1024
          quality: hd
          style: vivid
```

```python
create_image("A cover illustration for a Matrix automation handbook.")
```

### Notes

- Use [`dalle`] when you want explicit DALL-E request controls instead of the broader [`openai`] toolkit.
- `dall-e-3` plus `n > 1` is rejected before the API call.
- The current implementation does not expose image edits, variations, or `response_format` controls.

## [`cartesia`]

`cartesia` is the voice toolkit for listing voices, localizing voices into new languages, and generating speech.

### What It Does

`cartesia` exposes `list_voices()`, `localize_voice(name, description, language, original_speaker_gender, voice_id=None)`, and `text_to_speech(transcript, voice_id=None)`.
`list_voices()` returns a filtered JSON list of voice IDs, names, descriptions, and languages.
`localize_voice()` creates a localized derivative of an existing voice, using `default_voice_id` unless you pass a different `voice_id`.
`text_to_speech()` uses the configured `model_id` and voice ID and returns attached MP3 audio bytes.
The current implementation hardcodes MP3 output at 44.1 kHz and 128 kbps.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `api_key` | `password` | `yes` | `null` | Cartesia API key. The upstream SDK also checks `CARTESIA_API_KEY`. |
| `model_id` | `text` | `no` | `sonic-2` | Model used by `text_to_speech()`. |
| `default_voice_id` | `text` | `no` | `78ab82d5-25be-4f7d-82b3-7ad64e5b85b2` | Default source voice for localization and TTS when no call-specific `voice_id` is supplied. |
| `enable_text_to_speech` | `boolean` | `no` | `true` | Enable `text_to_speech()`. |
| `enable_list_voices` | `boolean` | `no` | `true` | Enable `list_voices()`. |
| `enable_localize_voice` | `boolean` | `no` | `false` | Enable `localize_voice()`. |
| `all` | `boolean` | `no` | `false` | Enable all Cartesia functions. |

### Example

```yaml
agents:
  voice:
    tools:
      - cartesia:
          model_id: sonic-2
          enable_localize_voice: true
```

```python
list_voices()
localize_voice(
    name="French Support Voice",
    description="Warm and clear support voice.",
    language="fr",
    original_speaker_gender="female",
)
text_to_speech("Deployment complete.")
```

### Notes

- `localize_voice()` is disabled by default, so voice cloning or localization is opt-in.
- `voice_id` can be overridden per call for both `localize_voice()` and `text_to_speech()`.
- The current TTS path always returns MP3 bytes even though the tool config does not expose an output-format option.

## [`eleven_labs`]

`eleven_labs` is the ElevenLabs toolkit for voices, sound effects, and text-to-speech.

### What It Does

`eleven_labs` exposes `get_voices()`, `generate_sound_effect(prompt, duration_seconds=None)`, and `text_to_speech(prompt)`.
`get_voices()` returns voice IDs, names, and descriptions from the ElevenLabs account.
`generate_sound_effect()` turns a text description into an attached audio artifact.
`text_to_speech()` uses the configured `voice_id`, `model_id`, and `output_format` and returns attached audio bytes.
If `target_directory` is set, the current implementation also saves generated audio files to disk in that directory.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `voice_id` | `text` | `no` | `JBFqnCBsd6RMkjVDRZzb` | Default voice used by `text_to_speech()`. |
| `api_key` | `password` | `yes` | `null` | ElevenLabs API key. The upstream implementation also checks `ELEVEN_LABS_API_KEY`. |
| `target_directory` | `text` | `no` | `null` | Optional directory where generated audio is also saved locally. |
| `model_id` | `text` | `no` | `eleven_multilingual_v2` | Model used by `text_to_speech()`. |
| `output_format` | `text` | `no` | `mp3_44100_64` | Output codec and bitrate preset for generated audio. |
| `enable_get_voices` | `boolean` | `no` | `true` | Enable `get_voices()`. |
| `enable_generate_sound_effect` | `boolean` | `no` | `true` | Enable `generate_sound_effect()`. |
| `enable_text_to_speech` | `boolean` | `no` | `true` | Enable `text_to_speech()`. |
| `all` | `boolean` | `no` | `false` | Enable all ElevenLabs functions. |

### Example

```yaml
agents:
  audio_fx:
    tools:
      - eleven_labs:
          model_id: eleven_multilingual_v2
          output_format: mp3_44100_64
          target_directory: generated-audio
```

```python
get_voices()
generate_sound_effect("Mechanical keyboard typing in a quiet office.", duration_seconds=4)
text_to_speech("The build succeeded.")
```

### Notes

- `target_directory` is optional and only affects local file saving, not the returned attachment.
- The current implementation always emits `audio/mpeg` artifacts, even when you choose a PCM- or u-law-style output format.
- `generate_sound_effect()` is useful when you want non-speech audio from the same provider toolkit.

## [`desi_vocal`]

`desi_vocal` is the speech toolkit for Hindi and other Indian-language voices.

### What It Does

`desi_vocal` exposes `get_voices()` and `text_to_speech(prompt, voice_id=None)`.
`get_voices()` returns a provider voice list with ID, name, gender, voice type, supported languages, and preview URL.
`text_to_speech()` posts the prompt to DesiVocal's generation API and returns the resulting audio as a remote URL attachment.
The default `voice_id` can be overridden per call.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `api_key` | `password` | `yes` | `null` | DesiVocal API key. The current TTS request sends it as `X_API_KEY`, and the upstream implementation also checks `DESI_VOCAL_API_KEY`. |
| `voice_id` | `text` | `no` | `f27d74e5-ea71-4697-be3e-f04bbd80c1a8` | Default voice used by `text_to_speech()`. |
| `enable_get_voices` | `boolean` | `no` | `true` | Enable `get_voices()`. |
| `enable_text_to_speech` | `boolean` | `no` | `true` | Enable `text_to_speech()`. |
| `all` | `boolean` | `no` | `false` | Enable both DesiVocal functions. |

### Example

```yaml
agents:
  hindi_voice:
    tools:
      - desi_vocal:
          voice_id: f27d74e5-ea71-4697-be3e-f04bbd80c1a8
```

```python
get_voices()
text_to_speech("नमस्ते, आपकी रिपोर्ट तैयार है।")
```

### Notes

- This is the most language-specific TTS tool on this page and is the best fit when you want Hindi or Indian-language voices.
- The current `get_voices()` implementation reads a public voice list endpoint, but `text_to_speech()` needs the API key.
- Generated audio is returned as a provider-hosted URL rather than inline bytes.

## [`lumalabs`]

`lumalabs` is the Luma AI toolkit for text-to-video and image-to-video generation.

### What It Does

`lumalabs` exposes `generate_video(prompt, loop=False, aspect_ratio="16:9", keyframes=None)` and `image_to_video(prompt, start_image_url, end_image_url=None, loop=False, aspect_ratio="16:9")`.
Both calls create a Luma generation job and poll until it completes or times out.
`generate_video()` optionally accepts provider-style keyframes, while `image_to_video()` builds the required keyframe structure from one or two image URLs.
Completed jobs return remote video URL attachments.
If `wait_for_completion` is false, the current implementation returns `Async generation unsupported`.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `api_key` | `password` | `yes` | `null` | Luma AI API key. The upstream implementation also checks `LUMAAI_API_KEY`. |
| `wait_for_completion` | `boolean` | `no` | `true` | Poll until the provider job completes. Setting it to `false` is not useful on this branch because async return is not implemented. |
| `poll_interval` | `number` | `no` | `3` | Seconds between status polls. |
| `max_wait_time` | `number` | `no` | `300` | Maximum wait time in seconds before timing out. |
| `enable_generate_video` | `boolean` | `no` | `true` | Enable `generate_video()`. |
| `enable_image_to_video` | `boolean` | `no` | `true` | Enable `image_to_video()`. |
| `all` | `boolean` | `no` | `false` | Enable both Luma functions. |

### Example

```yaml
agents:
  motion:
    tools:
      - lumalabs:
          poll_interval: 5
          max_wait_time: 600
```

```python
generate_video("A calm flythrough of a futuristic coworking space.", aspect_ratio="16:9")
image_to_video(
    "Animate this concept art into a short reveal shot.",
    start_image_url="https://example.com/frame0.png",
    end_image_url="https://example.com/frame1.png",
)
```

### Notes

- `image_to_video()` requires remote image URLs, not local file paths.
- `wait_for_completion: false` does not currently provide a job handle or async response.
- Use [`gemini`] instead when you specifically want Google's Veo-backed video path.

## [`modelslabs`]

`modelslabs` is the ModelsLab wrapper for provider-hosted MP4, GIF, MP3, or WAV generation.

### What It Does

`modelslabs` exposes one call, `generate_media(prompt)`.
The current wrapper chooses one of several provider endpoints based on `file_type` and sends a fixed payload template for that media class.
For MP4 and GIF generation, it currently uses the provider's text-to-video endpoint and returns future-link URLs with an ETA.
For MP3 and WAV generation, it uses provider voice endpoints and returns audio URLs.
If `wait_for_completion` is enabled, the tool polls the provider fetch endpoint until the media is ready or the timeout is reached.

### Configuration

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `api_key` | `password` | `yes` | `null` | ModelsLab API key. The upstream implementation also checks `MODELS_LAB_API_KEY`. |
| `file_type` | `text` | `no` | `mp4` | Output type for `generate_media()`, currently `mp4`, `gif`, or audio types such as `mp3` and `wav`. |
| `wait_for_completion` | `boolean` | `no` | `false` | Poll the provider fetch endpoint until the output is ready. |
| `add_to_eta` | `number` | `no` | `15` | Extra seconds added to the provider ETA before timing out. |
| `max_wait_time` | `number` | `no` | `60` | Maximum total wait time in seconds. |

### Example

```yaml
agents:
  generator:
    tools:
      - modelslabs:
          file_type: gif
          wait_for_completion: true
          max_wait_time: 90
```

```python
generate_media("A looping animation of messages flowing through a Matrix bridge.")
```

### Notes

- Despite the broad provider branding, the current wrapper exposes one opinionated `generate_media()` path rather than a generic arbitrary-model interface.
- MP4 and GIF generation currently use a fixed provider-side video template, including default dimensions and a hardcoded model ID.
- Returned media are provider URLs, and the success message usually includes the provider ETA rather than immediate ready-to-view bytes.

## Related Docs

- [Tools Overview](index.md)
- [OpenAI-Compatible API](../openai-api.md)
