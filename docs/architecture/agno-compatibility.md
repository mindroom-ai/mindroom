# Agno compatibility boundaries

This page maps each known Agno weakness to its upstream tracking and to the MindRoom boundary that works around it, so the weakness can be fixed upstream and the workaround removed.
The policy lives in the Agno Compatibility Policy in `AGENTS.md`.
Each `# AGNO_COMPAT:` source comment owns its workaround's reason, exact upstream links, removal condition, and test coverage; `rg -n -F 'AGNO_COMPAT:' src/mindroom` lists them.
The Boundary column of both tables below is the boundary inventory; paths are relative to `src/mindroom/`, and one row can group several markers that share a weakness.
Workarounds adapt Agno mechanics only: approval outcomes, storage ownership, retry limits, provider configuration, request-log context, canonical replay, and OAuth policy stay with their MindRoom owners, so removing a workaround keeps that product behavior.

## Upstream work

### Existing tracking

Issue and PR states were checked on September 24, 2026, unless a row gives another date; recheck upstream before relying on them.

| Agno weakness | Upstream issue / PR | Boundary |
| --- | --- | --- |
| Tool schemas render `Any`, such as the values of `dict[str, Any]`, as an object that only accepts `{}`. | [Issue #10422](https://github.com/agno-agi/agno/issues/10422), [PR #10423](https://github.com/agno-agi/agno/pull/10423), both open (checked October 21, 2026). | `custom_tools/google_sheets.py` (`batch_update_sheet`), `custom_tools/microsoft_sharepoint.py` (`microsoft_sharepoint_create_item`, `microsoft_sharepoint_update_item`) |
| Library loggers install their own stdout handler at INFO with propagation disabled, and every run resets their levels, so their records bypass the host's level, format, redaction, and stream. | [Issue #4097](https://github.com/agno-agi/agno/issues/4097), closed (checked October 10, 2026) after Agno 2.0 added `configure_agno_logging`, which swaps module globals but leaves the default handlers on the loggers that modules imported directly. | `logging_config.py` |

### Tracking gaps

These are unfinished contribution candidates identified by the local audit.
Rows name related upstream work where any is known; each source comment records whether a search found nothing or tracking is still unverified.

| Observed gap or required extension point | Next upstream work | Boundary |
| --- | --- | --- |
| ModelsLab polls a local artifact UUID and discards completed URLs and wait outcomes. | Poll the provider job ID, accept numeric ETAs, retain completed outputs, and distinguish retryable status failures from terminal rejection. | `tools/agno_compat_modelslabs.py` |
| MoviePy caption rendering assumes an Arial font file, ignores advertised styles, clips glyph bounds, overlaps wrapped words, leaves temporary audio after encoding failures, names the default output from the last dot anywhere in the video path, and rasterizes every caption line before encoding starts. | Fix each listed defect. | `custom_tools/agno_compat_moviepy.py` |
| MoviePyVideoTools hands model-chosen video, caption, and output paths to `open()`, `os.replace`, and FFmpeg by name, and FFmpeg follows the paths and URLs listed in HLS playlists and DASH manifests. | Accept caller-supplied input readers and output writers. | `custom_tools/agno_compat_moviepy.py` |
| OpenAITools and GroqTools open model-chosen audio paths by name, and GroqTools reads any existing path before treating a source as a URL. | Accept a caller-supplied file opener for local audio. | `tools/agno_compat_openai.py`, `tools/agno_compat_groq.py` |
| AirflowTools defaults its DAG directory to the process working directory and reads and writes DAG files by path. | Accept a caller-supplied base directory descriptor or file reader and writer. | `tools/agno_compat_airflow.py` |
| WebBrowserTools hands any URI scheme, including `file:` paths and application links, to the host's URL handlers. | Open only `http` and `https` URLs. | `tools/agno_compat_webbrowser.py` |
| Agno toolkits take the calling Agent or Team as an entrypoint parameter with no contract for what they may read from it; the owned loop has no Agno agent to inject, and of the toolkits MindRoom registers only DaytonaTools reads the parameter (`agent.session_state`, which Agno's AntigravityTools and SuperserveTools also read and write). | Pass run state to toolkits through `run_context` instead of the agent object. | `tool_system/agno_toolkits.py` |
| FileTools content search relativizes hits and exclusions against `base_dir`, so it cannot search other directories. | Accept an absolute search directory outside `base_dir` and report absolute hit paths. | `tools/file.py` |
| FileTools content search checks each walked file through a path sanitizer that strips trailing dots and spaces, then opens the unsanitized path by name and follows its links, so a file the check never saw is read. | Open exactly the validated path without following links out of `base_dir`, or accept a caller-supplied file reader. | `tools/file.py` |
| CalculatorTools computes `factorial` for any argument, although `json.dumps` refuses results above 4300 digits, so one call can hold the GIL for minutes. | Refuse factorial arguments whose result cannot be serialized. | `custom_tools/calculator.py` |
| CalculatorTools trial-divides `is_prime` up to the square root of any argument, so one large prime checks for hours. | Bound prime-check work. | `custom_tools/calculator.py` |
| SleepTools blocks a thread in `time.sleep` for any requested duration, so parallel calls exhaust the primary's default executor. | Bound and await the pause. | `custom_tools/sleep.py` |
| E2BTools `run_python_code` reads code output through the E2B SDK's `run_code`, which reads each output line whole, so one large print or image grows the primary's memory. | Stop reading code output past a caller-supplied byte limit. | `custom_tools/e2b.py` |
| E2BTools `run_command` waits on the E2B SDK's `commands.run`, which keeps a command's whole stdout and stderr until it ends. | Stop collecting command output past a caller-supplied byte limit. | `custom_tools/e2b.py` |
| SpotifyTools sends one fixed access token on every request and never renews it, so every call fails once Spotify's one-hour token expires. | Accept a token provider or refresh credentials and renew expiring tokens. | `custom_tools/spotify.py` |
| Concise PubMed output drops title/year when the abstract is short or absent. | Keep identifying metadata independent of summary truncation. | `tools/pubmed.py` |
| DaytonaTools treats a null `verify_ssl` like `false` and then disables certificate checks by patching `daytona_api_client.Configuration.__init__` for the whole process until it restarts. | Disable verification only for an explicit `False` and scope it to the toolkit's own client configuration. | `tools/daytona.py` |
| Luma video creation omits the required model argument in both public methods. | Pass a configurable model on both creation paths. | `tools/lumalabs.py` |
| Todoist project discovery treats SDK pages as individual projects. | Flatten project pages and serialize SDK date fields safely. | `tools/todoist.py` |
| Calendar construction requires broad scopes even when granular scopes cover the operations. | Validate effective permissions per registered operation. | `custom_tools/google_calendar.py` |
| Google authentication requires private resolver and Function entrypoint replacement. | Propose injectable credential resolution and an entrypoint middleware hook. | `oauth/agno_compat_google_auth.py` |
| TrafilaturaTools extraction and crawling download through fixed module-level fetchers, and Trafilatura's spider has no fetch hook. | Accept a caller-supplied fetcher for extraction, batch extraction, and every crawler download. | `tools/agno_compat_trafilatura.py` |
| Newspaper4kTools downloads through newspaper4k's article helper, which opens `file://` paths, follows redirects anywhere, and downloads every image URL a page lists. | Accept caller-supplied HTML or a fetcher and allow skipping image downloads. | `tools/agno_compat_newspaper4k.py` |
| FileGenerationTools saves model-named files by path into its output directory, or the process working directory, and follows links there. | Accept a caller-supplied writer or directory descriptor for saved files. | `tools/agno_compat_file_generation.py` |
| VisualizationTools saves model-named charts by path below an output directory in the process working directory and follows links there. | Accept a caller-supplied writer or directory descriptor for charts; [PR #9470](https://github.com/agno-agi/agno/pull/9470), open on October 3, 2026, keeps chart file names inside the output directory but still saves by path below the working directory. | `tools/agno_compat_visualization.py` |
| GithubTools serializes provider failures and logs provider detail through fixed message formats. | Expose typed errors; separately provide structured logging or a redaction hook. | `custom_tools/agno_compat_github_errors.py` |
| ReplicateTools checks its API key but calls an independent SDK default client. | Bind SDK requests to the toolkit key and expose client lifetime ownership while preserving media results. | `custom_tools/agno_compat_replicate.py` |
| AgentQLTools stores its resolved key without handing it to AgentQL queries. | Pass the resolved key through a per-request or per-page SDK credential API. | `custom_tools/agno_compat_agentql.py` |
| AgentQLTools launches Chromium with no proxy and navigates to the model-chosen URL, so neither the URL nor redirects and subresources follow a destination policy. | Accept caller-supplied browser launch options or a page factory. | `custom_tools/agno_compat_agentql.py` |
| AgentQLTools custom scraping returns response keys and discards extracted values. | Preserve the complete JSON response, including nested collections. | `custom_tools/agno_compat_agentql.py` |
| Google Drive `read_file` refuses text stored as `application/octet-stream`, names `download_file` even when a toolkit renames or omits it, and extracts Office text without tables, grouped shapes, or line breaks. | Decide text by content, let a toolkit name its download function, and extract complete Office text or refuse it; [PR #10501](https://github.com/agno-agi/agno/pull/10501) covers only `.docx` tables. | `custom_tools/google_drive.py` |
