# Media & Content

Use these tools to process local video files, search GIFs and stock images, inspect YouTube videos, fetch brand assets, and work with Spotify content.

## What This Page Covers

This page documents the built-in tools in the `media-and-content` group.
Use these tools when you need local video processing, media lookup, brand asset retrieval, or Spotify-backed search and playlist workflows.

## Tools On This Page

- \[`moviepy_video_tools`\] - Local video helpers for audio extraction, SRT creation, and caption burn-in.
- \[`giphy`\] - GIF search that returns Giphy-hosted animated images.
- \[`youtube`\] - YouTube URL inspection for video metadata, captions, and timestamped transcript lines.
- \[`unsplash`\] - Stock photo search and photo metadata lookup from Unsplash.
- \[`brandfetch`\] - Brand asset and identity lookup by domain, brand ID, ISIN, stock ticker, or brand name.
- \[`spotify`\] - Spotify search, playlist, profile, recommendation, and playback actions.

## Common Setup Notes

`moviepy_video_tools` and `youtube` are `setup_type: none`, so they do not need dashboard OAuth or stored API credentials.
`giphy`, `unsplash`, `brandfetch`, and `spotify` all use stored credentials, and password-type fields such as `api_key`, `access_key`, and `access_token` should be managed through the dashboard or credential store instead of inline YAML.
The upstream toolkits for `giphy`, `unsplash`, and `brandfetch` also fall back to provider-specific environment variables such as `GIPHY_API_KEY`, `UNSPLASH_ACCESS_KEY`, `BRANDFETCH_API_KEY`, and `BRANDFETCH_CLIENT_ID`.
These tools operate on external URLs or local file paths rather than Matrix attachment IDs directly.
When you pass local files, the paths must exist inside the runtime that executes the tool.
Missing optional Python dependencies can auto-install at first use unless `MINDROOM_NO_AUTO_INSTALL_TOOLS=1` is set.
That matters most on this page for `moviepy_video_tools`, `giphy`, `brandfetch`, `spotify`, and `youtube`.
`spotify` is the only tool on this page with dedicated integration routes in `src/mindroom/api/integrations.py`.
MindRoom treats `spotify` as a shared-only integration, so dashboard credential management and tool support require `worker_scope` unset or `shared`, not `user` or `user_agent`.

## \[`moviepy_video_tools`\]

`moviepy_video_tools` is the local video-processing toolkit for extracting audio, saving SRT text, and burning captions into a rendered video file.

### What It Does

`moviepy_video_tools` exposes `extract_audio(video_path, output_path)`, `create_srt(transcription, output_path)`, and `embed_captions(video_path, srt_path, output_path=None, font_size=24, font_color="white", stroke_color="black", stroke_width=1)`.
Despite the `enable_process_video` config name, the current upstream method it enables is specifically `extract_audio()`, not a general-purpose video editing surface.
`create_srt()` writes the provided transcription text directly to disk, so it expects the caller to already have SRT-formatted content.
`embed_captions()` reads an SRT file, converts it to word timings, and renders word-highlighted captions onto a new MP4 output.
This tool works entirely on local files, so it is only useful when the agent runtime can read the source media and write the output paths.

### Configuration

| Option                     | Type      | Required | Default | Notes                                     |
| -------------------------- | --------- | -------- | ------- | ----------------------------------------- |
| `enable_process_video`     | `boolean` | `no`     | `true`  | Enable `extract_audio()`.                 |
| `enable_generate_captions` | `boolean` | `no`     | `true`  | Enable `create_srt()`.                    |
| `enable_embed_captions`    | `boolean` | `no`     | `true`  | Enable `embed_captions()`.                |
| `all`                      | `boolean` | `no`     | `false` | Enable the full upstream toolkit surface. |

### Example

```yaml
agents:
  editor:
    tools:
      - moviepy_video_tools:
          enable_embed_captions: true
```

```python
extract_audio("clips/demo.mp4", "clips/demo.wav")
create_srt(transcription_srt, "clips/demo.srt")
embed_captions("clips/demo.mp4", "clips/demo.srt", output_path="clips/demo_captioned.mp4")
```

### Notes

- `moviepy` is the declared Python dependency, and the upstream toolkit also expects FFmpeg support for real audio and video processing.
- `embed_captions()` defaults the output filename to `<video>_captioned.mp4` when `output_path` is omitted.
- Use this tool for simple local media transforms, not remote video discovery or hosting.

## \[`giphy`\]

`giphy` searches Giphy for animated GIFs and returns image artifacts that agents can reuse in a response.

### What It Does

`giphy` exposes `search_gifs(query)`.
The upstream method signature includes the active agent or team object, but MindRoom callers only provide the search query because the runtime injects the current tool context.
Successful calls return a `ToolResult` with both plain-text URLs and attached image artifacts for each GIF.
`limit` is fixed at toolkit construction time, so callers do not set result count per request.

### Configuration

| Option               | Type       | Required | Default | Notes                                                            |
| -------------------- | ---------- | -------- | ------- | ---------------------------------------------------------------- |
| `api_key`            | `password` | `yes`    | `null`  | Giphy API key. The upstream toolkit also checks `GIPHY_API_KEY`. |
| `limit`              | `number`   | `no`     | `1`     | Number of GIFs returned per search.                              |
| `enable_search_gifs` | `boolean`  | `no`     | `true`  | Enable `search_gifs()`.                                          |
| `all`                | `boolean`  | `no`     | `false` | Enable the full upstream toolkit surface.                        |

### Example

```yaml
agents:
  social:
    tools:
      - giphy:
          limit: 3
```

```python
search_gifs("matrix code review celebration")
```

### Notes

- The metadata marks `api_key` as optional, but successful requests effectively require a real Giphy API key.
- `search_gifs()` returns hosted GIF URLs, not downloaded local files.
- Use this when you want animated reaction media rather than stock photography or brand assets.

## \[`youtube`\]

`youtube` works from a YouTube video URL and extracts metadata, captions, or timestamped transcript lines.

### What It Does

`youtube` exposes `get_youtube_video_data(url)`, `get_youtube_video_captions(url)`, and `get_video_timestamps(url)`.
`get_youtube_video_data()` uses YouTube's oEmbed endpoint and returns metadata such as title, author, thumbnail, size, and provider fields.
`get_youtube_video_captions()` and `get_video_timestamps()` use `youtube_transcript_api` against the parsed video ID.
The current tool does not perform keyword-based YouTube search.
It expects a specific YouTube URL and then fetches metadata or transcript-derived output for that video.

### Configuration

| Option                        | Type       | Required | Default | Notes                                                         |
| ----------------------------- | ---------- | -------- | ------- | ------------------------------------------------------------- |
| `enable_get_video_captions`   | `boolean`  | `no`     | `true`  | Enable `get_youtube_video_captions()`.                        |
| `enable_get_video_data`       | `boolean`  | `no`     | `true`  | Enable `get_youtube_video_data()`.                            |
| `enable_get_video_timestamps` | `boolean`  | `no`     | `true`  | Enable `get_video_timestamps()`.                              |
| `all`                         | `boolean`  | `no`     | `false` | Enable the full upstream toolkit surface.                     |
| `languages`                   | `string[]` | `no`     | `null`  | Preferred transcript languages, for example `["en", "es"]`.   |
| `proxies`                     | `text`     | `no`     | `null`  | Optional proxy mapping forwarded to `youtube_transcript_api`. |

### Example

```yaml
agents:
  researcher:
    tools:
      - youtube:
          languages: [en]
```

```python
get_youtube_video_data("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
get_youtube_video_captions("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
get_video_timestamps("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
```

### Notes

- If you need keyword-based YouTube discovery rather than URL-based transcript or metadata extraction, use a search tool such as `serpapi` instead of `youtube`.
- `languages` only affects transcript retrieval methods, not `get_youtube_video_data()`.
- Invalid or unsupported URLs return plain-text error strings from the upstream toolkit.

## \[`unsplash`\]

`unsplash` searches Unsplash for stock photography, fetches one photo's metadata, or requests random photo selections.

### What It Does

`unsplash` exposes `search_photos(query, per_page=10, page=1, orientation=None, color=None)`, `get_photo(photo_id)`, `get_random_photo(query=None, orientation=None, count=1)`, and optionally `download_photo(photo_id)`.
`search_photos()` returns a JSON payload with total counts plus a simplified list of photo metadata, author info, and image URLs.
`get_photo()` adds extra fields such as EXIF data, views, downloads, and location when the API returns them.
`get_random_photo()` supports an optional query filter and returns one or more formatted photo records.
`download_photo()` does not fetch the image binary.
It triggers Unsplash's required download-tracking endpoint and returns the download URL that the caller can fetch separately.

### Configuration

| Option                    | Type       | Required | Default | Notes                                                                        |
| ------------------------- | ---------- | -------- | ------- | ---------------------------------------------------------------------------- |
| `access_key`              | `password` | `yes`    | `null`  | Unsplash access key. The upstream toolkit also checks `UNSPLASH_ACCESS_KEY`. |
| `enable_search_photos`    | `boolean`  | `no`     | `true`  | Enable `search_photos()`.                                                    |
| `enable_get_photo`        | `boolean`  | `no`     | `true`  | Enable `get_photo()`.                                                        |
| `enable_get_random_photo` | `boolean`  | `no`     | `true`  | Enable `get_random_photo()`.                                                 |
| `enable_download_photo`   | `boolean`  | `no`     | `false` | Enable `download_photo()`.                                                   |
| `all`                     | `boolean`  | `no`     | `false` | Enable the full upstream toolkit surface.                                    |

### Example

```yaml
agents:
  designer:
    tools:
      - unsplash:
          enable_download_photo: true
```

```python
search_photos("conference stage lighting", per_page=5, orientation="landscape")
get_random_photo(query="workspace desk", count=3)
get_photo("abcd1234")
```

### Notes

- `download_photo()` is off by default because it exists mainly for Unsplash API compliance and usage tracking.
- The tool returns URLs and metadata, not local downloaded image files.
- Use `unsplash` for stock photography, not logos or brand identity assets.

## \[`brandfetch`\]

`brandfetch` retrieves brand identity data such as logos, colors, fonts, and related brand metadata.

### What It Does

`brandfetch` exposes `search_by_identifier(identifier)` and optionally `search_by_brand(name)`.
`search_by_identifier()` uses the Brand API and accepts domains, Brandfetch brand IDs, ISINs, or stock tickers.
`search_by_brand()` uses the Brand Search API and is useful when you only know the brand name and need to discover the canonical brand entry first.
The two methods use different credentials.
`search_by_identifier()` requires `api_key`, while `search_by_brand()` requires `client_id`.

### Configuration

| Option                        | Type       | Required | Default                        | Notes                                                                                                   |
| ----------------------------- | ---------- | -------- | ------------------------------ | ------------------------------------------------------------------------------------------------------- |
| `api_key`                     | `password` | `no`     | `null`                         | Brandfetch API key for `search_by_identifier()`. The upstream toolkit also checks `BRANDFETCH_API_KEY`. |
| `client_id`                   | `text`     | `no`     | `null`                         | Brandfetch Client ID for `search_by_brand()`. The upstream toolkit also checks `BRANDFETCH_CLIENT_ID`.  |
| `enable_search_by_identifier` | `boolean`  | `no`     | `true`                         | Enable `search_by_identifier()`.                                                                        |
| `enable_search_by_brand`      | `boolean`  | `no`     | `false`                        | Enable `search_by_brand()`.                                                                             |
| `base_url`                    | `url`      | `no`     | `https://api.brandfetch.io/v2` | Base Brandfetch API URL.                                                                                |
| `timeout`                     | `number`   | `no`     | `20.0`                         | Request timeout in seconds.                                                                             |
| `all`                         | `boolean`  | `no`     | `false`                        | Enable the full upstream toolkit surface.                                                               |
| `async_tools`                 | `boolean`  | `no`     | `false`                        | Deprecated upstream flag that is no longer needed for normal use.                                       |

### Example

```yaml
agents:
  branding:
    tools:
      - brandfetch:
          enable_search_by_brand: true
          timeout: 10
```

```python
search_by_identifier("openai.com")
search_by_brand("OpenAI")
```

### Notes

- The credential you need depends on which Brandfetch API surface you enable.
- `search_by_identifier()` is the better default when you already know the brand domain or ticker.
- `async_tools` is kept only for upstream compatibility and should be left at its default.

## \[`spotify`\]

`spotify` is the richest content tool on this page, covering music search, recommendations, playlists, profile lookups, and limited playback control.

### What It Does

`spotify` exposes a broad toolkit including `search_tracks()`, `search_playlists()`, `search_artists()`, `search_albums()`, `get_user_playlists()`, `get_track_recommendations()`, `get_artist_top_tracks()`, `get_album_tracks()`, `get_my_top_tracks()`, `get_my_top_artists()`, `create_playlist()`, `add_tracks_to_playlist()`, `get_playlist()`, `update_playlist_details()`, `remove_tracks_from_playlist()`, `get_current_user()`, `play_track()`, and `get_currently_playing()`.
The tool itself consumes an `access_token`, but MindRoom also provides a dedicated dashboard OAuth flow in `src/mindroom/api/integrations.py` via `/api/integrations/spotify/connect`, `/spotify/status`, `/spotify/callback`, and `/spotify/disconnect`.
That OAuth flow stores `access_token` plus extra metadata such as `refresh_token`, `expires_at`, and `username`.
By default the connect flow requests the scopes `user-read-private`, `user-read-email`, `user-read-playback-state`, `user-read-currently-playing`, and `user-top-read`.
The upstream playlist and playback methods need additional Spotify scopes beyond that base dashboard flow, so manual token provisioning or a broadened OAuth scope set is still required if you want playlist modification or playback control to succeed.

### Configuration

| Option           | Type       | Required | Default | Notes                                                    |
| ---------------- | ---------- | -------- | ------- | -------------------------------------------------------- |
| `access_token`   | `password` | `yes`    | `null`  | Spotify OAuth access token used by the toolkit.          |
| `default_market` | `text`     | `no`     | `US`    | Default market code for search and album lookup methods. |
| `timeout`        | `number`   | `no`     | `30`    | Request timeout in seconds.                              |

### Example

```yaml
agents:
  dj:
    worker_scope: shared
    tools:
      - spotify:
          default_market: GB
```

```python
search_tracks("ambient coding music", max_results=5)
get_my_top_tracks(time_range="short_term", limit=10)
create_playlist("MindRoom Picks", description="Tracks from this week's chat")
get_currently_playing()
```

### Notes

- `spotify` is shared-only in MindRoom, so agents using `worker_scope=user` or `worker_scope=user_agent` will see it marked unsupported and the dashboard status/connect routes will reject that scope.
- The redirect URI defaults to the API callback URL, but `SPOTIFY_REDIRECT_URI` can override it when the dashboard is behind a different public URL.
- `play_track()` requires an active Spotify device and returns a specific `NO_ACTIVE_DEVICE` error when playback cannot start anywhere.
- The current OAuth helper marks saved Spotify credentials as UI-managed so unscoped and shared execution can mirror them correctly.

## Related Docs

- [Tools Overview](https://docs.mindroom.chat/tools/index.md)
- [Per-Agent Tool Configuration](https://docs.mindroom.chat/configuration/agents/#per-agent-tool-configuration)
- [Sandbox Proxy Isolation](https://docs.mindroom.chat/deployment/sandbox-proxy/index.md)
