# Media & Content

Use these tools to caption and extract audio from local videos, search GIFs and stock photos, read YouTube metadata and transcripts, look up brand assets, and work with Spotify.

## Tools On This Page

- [`moviepy_video_tools`] - Extract audio, save SRT files, and burn word-highlighted captions into local videos.
- [`giphy`] - Search Giphy for animated GIFs.
- [`youtube`] - Read metadata, captions, and timestamped transcript lines for a YouTube URL.
- [`unsplash`] - Search Unsplash stock photos and read photo metadata.
- [`brandfetch`] - Look up logos, colors, fonts, and other brand data by domain, brand ID, ISIN, stock ticker, or name.
- [`spotify`] - Search music, manage playlists, get recommendations, and control playback.

## Setup

`moviepy_video_tools` and `youtube` need no credentials.
`giphy` and `unsplash` need an API key, `brandfetch` needs an API key or a client ID depending on the enabled function, and `spotify` needs a Spotify OAuth connection from the dashboard.
Enter `api_key` and `access_key` in the dashboard **Tools** tab, not in `config.yaml` (see [Security Restrictions](https://docs.mindroom.chat/tools/#security-restrictions)).
`giphy`, `unsplash`, and `brandfetch` also read the environment variables named in their configuration tables when no key is stored.
Missing Python dependencies install on first use (see [Automatic Dependency Installation](https://docs.mindroom.chat/tools/#automatic-dependency-installation)).

## [`moviepy_video_tools`]

`moviepy_video_tools` processes video files on disk; it cannot search for, download, or host videos.

### What It Does

- `extract_audio(video_path, output_path)` saves a video's audio track to `output_path`.
- `create_srt(transcription, output_path)` writes the given text to disk unchanged, so the text must already be SRT-formatted.
- `embed_captions(video_path, srt_path, output_path=None, font_size=24, font_color="white", stroke_color="black", stroke_width=1)` renders an MP4 with word-by-word highlighted captions from an SRT file.

In `embed_captions()`, `font_size` is the caption text size in pixels and `font_color` is the base text color, while the word being spoken is always yellow.
`stroke_color` and `stroke_width` set the text outline, and `stroke_width=0` removes it.
Captions sit at the bottom of the video, and a word or caption line too large to fit at the requested size returns an error.
Without `output_path`, the output is `<video>_captioned.mp4` next to the input, where `<video>` is the input file name without its extension.

### Files And Limits

All inputs are local file paths, not URLs or attachment IDs.
Paths follow the agent's [`file_access`](https://docs.mindroom.chat/configuration/agents/): with the default `workspace`, relative paths resolve from the agent workspace and paths outside it are refused, while `unrestricted` allows any file the runtime can reach.
A video larger than 1 GiB or a caption file larger than 1 MiB returns an error.
Video inputs must be MP4, MOV, M4A, 3GP, 3G2, Motion JPEG 2000, Matroska, WebM, AVI, MPEG-TS, MPEG program stream (`.mpg`, `.vob`), FLV, WMV or other ASF, GIF, Ogg, WAV, MP3, FLAC, or AAC.
Other formats, including HLS and DASH playlists and manifests, return an error containing `Video input must be a supported plain media file; playlists and manifests are refused.`
Real audio and video processing needs FFmpeg in the runtime that executes the tool.

### Configuration

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `enable_process_video` | `boolean` | `true` | Enable `extract_audio()`. |
| `enable_generate_captions` | `boolean` | `true` | Enable `create_srt()`. |
| `enable_embed_captions` | `boolean` | `true` | Enable `embed_captions()`. |
| `all` | `boolean` | `false` | Enable all three functions regardless of the `enable_*` options. |

### Example

```yaml
agents:
  editor:
    tools:
      - moviepy_video_tools:
          enable_process_video: false
```

```python
create_srt(transcription_srt, "clips/demo.srt")
embed_captions("clips/demo.mp4", "clips/demo.srt", output_path="clips/demo_captioned.mp4")
```

## [`giphy`]

`giphy` provides `search_gifs(query)`, which returns Giphy-hosted GIF URLs and attaches the GIFs as images to the tool result.
The number of GIFs per search is set by `limit`, not per call.
A Giphy API key is needed for searches to succeed, even though the field is marked optional.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Giphy API key; falls back to `GIPHY_API_KEY`. |
| `limit` | `number` | `1` | GIFs returned per search. |
| `enable_search_gifs` | `boolean` | `true` | Enable `search_gifs()`. |
| `all` | `boolean` | `false` | Enable all functions. |

```yaml
agents:
  social:
    tools:
      - giphy:
          limit: 3
```

## [`youtube`]

`youtube` works on one YouTube video URL; it cannot search YouTube by keyword, so use a search tool such as `serpapi` for discovery.

It exposes `youtube_video(action, url, languages=None)` with three actions:

- `data` returns title, author, thumbnail, size, and provider fields.
- `captions` returns the video's transcript text.
- `timestamps` returns transcript lines with timestamps.

Invalid or unsupported URLs return an error message.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `enable_get_video_captions` | `boolean` | `true` | Enable `action="captions"`. |
| `enable_get_video_data` | `boolean` | `true` | Enable `action="data"`. |
| `enable_get_video_timestamps` | `boolean` | `true` | Enable `action="timestamps"`. |
| `all` | `boolean` | `false` | Enable all actions. |
| `languages` | `string[]` | `null` | Preferred transcript languages, for example `["en", "es"]`; affects only `captions` and `timestamps`. |
| `timeout` | `number` | `30` | Timeout in seconds for `action="data"`; caption and timestamp requests do not use it. |

Proxy settings are not configurable.

```yaml
agents:
  researcher:
    tools:
      - youtube:
          languages: [en]
```

## [`unsplash`]

`unsplash` provides a unified `unsplash(action, ...)` tool that returns stock photo metadata and image URLs, not downloaded files, and is not a source for logos or brand assets.

- `action="search"` (`query`, optional `count`, `page`, `orientation`, `color`) returns the total match count and a list of photos with author and image URLs.
- `action="get"` (`photo_id`) adds details such as EXIF data, views, downloads, and location when Unsplash provides them.
- `action="random"` (optional `query`, `orientation`, `count`) returns one or more random photos, optionally matching a query.
- `action="download"` (`photo_id`) reports a download to Unsplash, as its API guidelines require, and returns the download URL without fetching the image.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `access_key` | `password` | `null` | Required Unsplash access key; falls back to `UNSPLASH_ACCESS_KEY`. Get one from [Unsplash Developers](https://unsplash.com/developers). |
| `enable_search_photos` | `boolean` | `true` | Enable `action="search"` in `unsplash()`. |
| `enable_get_photo` | `boolean` | `true` | Enable `action="get"` in `unsplash()`. |
| `enable_get_random_photo` | `boolean` | `true` | Enable `action="random"` in `unsplash()`. |
| `enable_download_photo` | `boolean` | `false` | Enable `action="download"` in `unsplash()`. |
| `all` | `boolean` | `false` | Enable all actions in `unsplash()`. |
| `timeout` | `number` | `30` | Request timeout in seconds. |

```python
unsplash("search", query="conference stage lighting", count=5, orientation="landscape")
unsplash("random", query="workspace desk", count=3)
```

## [`brandfetch`]

`brandfetch` returns brand identity data such as logos, colors, and fonts.

- `brandfetch_lookup(query, action="by_identifier")` looks up a domain, Brandfetch brand ID, ISIN, or stock ticker and needs `api_key`.
- `brandfetch_lookup(query, action="by_brand")` finds brands by name when you do not know the domain, needs `client_id`, and is off by default.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | Brand API key for `brandfetch_lookup(action="by_identifier")`; falls back to `BRANDFETCH_API_KEY`. |
| `client_id` | `text` | `null` | Brand Search client ID for `brandfetch_lookup(action="by_brand")`; falls back to `BRANDFETCH_CLIENT_ID`. |
| `enable_search_by_identifier` | `boolean` | `true` | Enable `brandfetch_lookup(action="by_identifier")`. |
| `enable_search_by_brand` | `boolean` | `false` | Enable `brandfetch_lookup(action="by_brand")`. |
| `base_url` | `url` | `https://api.brandfetch.io/v2` | Brandfetch API base URL. |
| `timeout` | `number` | `20.0` | Request timeout in seconds. |
| `all` | `boolean` | `false` | Enable all functions. |

Get keys from [Brandfetch Developers](https://developers.brandfetch.com/).

```yaml
agents:
  branding:
    tools:
      - brandfetch:
          enable_search_by_brand: true
```

```python
brandfetch_lookup("openai.com", action="by_identifier")
brandfetch_lookup("OpenAI", action="by_brand")
```

## [`spotify`]

`spotify` acts on the connected Spotify account.
It provides `spotify_search()` (`search`, `recommendations`, `album_tracks`, `artist_top_tracks`), `spotify_library()` (`current_user`, `currently_playing`, `user_playlists`, `playlist`, `my_top_tracks`, `my_top_artists`), `spotify_playlist_write()` (`create`, `update`, `add_tracks`, `remove_tracks`), and `spotify_play()`.

### Connect Spotify

1. Create an app in the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) and add the redirect URI `<MINDROOM_PUBLIC_URL>/api/oauth/spotify/callback` (see [the callback URL rule](https://docs.mindroom.chat/oauth-framework/#connect-an-account)).
   Spotify refuses `localhost` redirect URIs, so on a local install set `MINDROOM_PUBLIC_URL=http://127.0.0.1:8765`, register `http://127.0.0.1:8765/api/oauth/spotify/callback`, and open the dashboard at `http://127.0.0.1:8765`.
2. On the dashboard credentials page, save the app's `client_id` and `client_secret` under the `spotify_oauth_client` credential service.
3. In the dashboard **Tools** tab, connect Spotify and approve access.

The connection requests the scopes `user-read-private`, `user-read-email`, `user-read-playback-state`, `user-read-currently-playing`, `user-top-read`, `playlist-read-private`, `playlist-modify-public`, `playlist-modify-private`, and `user-modify-playback-state`.
MindRoom refreshes the access token automatically, so a connection keeps working after Spotify's one-hour token lifetime.
Until Spotify is connected, `spotify` calls return an `OAuthConnectionRequired` result containing the connect link.

`spotify` requires `worker_scope` unset or `shared` and always runs in the primary runtime (see [Shared-only integrations](https://docs.mindroom.chat/deployment/sandbox-proxy/#shared-only-integrations)).

### Upgrading from the environment-variable setup

`/api/integrations/spotify/callback` is gone, and MindRoom no longer configures Spotify from `SPOTIFY_CLIENT_SECRET` or `SPOTIFY_REDIRECT_URI`.
Register the new callback URL from [Connect Spotify](#connect-spotify) as a redirect URI of your Spotify app and save its client ID and secret under `spotify_oauth_client`; in place of `SPOTIFY_REDIRECT_URI`, set `MINDROOM_PUBLIC_URL` or store a `redirect_uri` in `spotify_oauth_client`.
An existing connection carries over to the same app the first time an agent or the dashboard uses it, as long as `SPOTIFY_CLIENT_ID` is still set then; otherwise connect Spotify again.
Remove the variable once connections have carried over.
A shared-scope agent that used the global connection through `defaults.worker_grantable_credentials` needs a Spotify connection of its own, because OAuth connections are never granted.

### Configuration

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `default_market` | `text` | `US` | Default market code for search and album lookups. |
| `timeout` | `number` | `30` | Request timeout in seconds. |

```yaml
agents:
  dj:
    tools:
      - spotify:
          default_market: GB
```

```python
spotify_search(action="search", query="ambient coding music", limit=5)
spotify_playlist_write(action="create", name="MindRoom Picks", description="Tracks from this week's chat")
```

### Troubleshooting

- If playlist changes or playback control fail with a permissions error, disconnect and reconnect Spotify to grant the current scopes.
- `spotify_play()` needs an active Spotify device and returns a `NO_ACTIVE_DEVICE` error when no device can play.
- `spotify_search(action="recommendations")` also needs a Spotify app with access to the Recommendations endpoint, which OAuth scopes cannot grant; Spotify [restricts this endpoint](https://developer.spotify.com/blog/2024-11-27-changes-to-the-web-api) for new and affected Development Mode apps.

## Related Docs

- [Tools Overview](https://docs.mindroom.chat/tools/)
- [Per-Agent Tool Configuration](https://docs.mindroom.chat/tools/#per-agent-tool-configuration)
- [Sandbox Proxy Isolation](https://docs.mindroom.chat/deployment/sandbox-proxy/)
