# Agno compatibility boundaries

MindRoom uses Agno only for its vendor toolkits and the plugins written against Agno's toolkit API, through the toolkit adapter in `tool_system/agno_toolkits.py`.
This page maps each known weakness of that library to its upstream tracking and to the MindRoom boundary that works around it, so the weakness can be fixed upstream and the workaround removed.
The policy lives in the Agno Compatibility Policy in `AGENTS.md`.
Each `# AGNO_COMPAT:` source comment owns its workaround's reason, exact upstream links, removal condition, and test coverage; `rg -n -F 'AGNO_COMPAT:' src/mindroom` lists them.
The Boundary column of both tables below is the boundary inventory; paths are relative to `src/mindroom/`, and one row can group several markers that share a weakness.
Workarounds adapt Agno mechanics only: file access, network destination, output size, and credential policy stay with their MindRoom owners, so removing a workaround keeps that product behavior.

## Upstream work

### Existing tracking

Issue and PR states were checked on September 24, 2026, unless a row gives another date; recheck upstream before relying on them.

| Agno weakness | Upstream issue / PR | Boundary |
| --- | --- | --- |
| Library loggers install their own stdout handler at INFO with propagation disabled, so their records bypass the host's level, format, redaction, and stream. | [Issue #4097](https://github.com/agno-agi/agno/issues/4097), closed (checked October 10, 2026) after Agno 2.0 added `configure_agno_logging`, which swaps module globals but leaves the default handlers on the loggers that modules imported directly. | `logging_config.py` |
| Mem0Tools passes its user to Mem0 2 search and listing as a top-level `user_id`, which Mem0 rejects, so `search_memory` and `get_all_memories` return Mem0's error instead of memories. | [Issue #11340](https://github.com/agno-agi/agno/issues/11340), open with no fix PR (checked October 27, 2026). | `tools/agno_compat_mem0.py` |

### Tracking gaps

These are unfinished contribution candidates identified by the local audit.
Rows name related upstream work where any is known; each source comment records whether a search found nothing or tracking is still unverified.

| Observed gap or required extension point | Next upstream work | Boundary |
| --- | --- | --- |
| AirflowTools defaults its DAG directory to the process working directory and reads and writes DAG files by path. | Accept a caller-supplied base directory descriptor or file reader and writer. | `tools/agno_compat_airflow.py` |
| Agno toolkits take the calling Agent or Team as an entrypoint parameter with no contract for what they may read from it; the agent loop has no Agno agent to inject (Agno's AntigravityTools and SuperserveTools read and write `agent.session_state`). | Pass run state to toolkits through `run_context` instead of the agent object. | `tool_system/agno_toolkits.py` |
| The default instructions of ZepTools name functions that enable switches or filters can leave out, such as `add_zep_message` in a read-only Zep. | Build default instructions from the functions an agent is offered. | `tool_system/agno_toolkits.py` |
| SpotifyTools sends one fixed access token on every request and never renews it, so every call fails once Spotify's one-hour token expires. | Accept a token provider or refresh credentials and renew expiring tokens. | `custom_tools/spotify.py` |
| Todoist project discovery treats SDK pages as individual projects. | Flatten project pages and serialize SDK date fields safely. | `tools/todoist.py` |
