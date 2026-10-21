# Agno parity fixtures

These fixtures pin what Agno 3.0.9 and MindRoom write, send, and read, so the owned replacements can prove they match.

## Storage, history, and events

`tests/agno_parity/capture_storage.py` writes `storage/`, `runs/`, `history/`, and `events/`.
Every scenario runs MindRoom's real code path with real Agno agents and teams: `ai_response`, `stream_agent_response`, `team_response`, `team_response_stream`, the OpenAI-compatible API, and MindRoom's storage and compaction functions.
Only the provider is replaced, by the synthetic provider answering from a script.
Regenerate with `.venv/bin/python -m tests.agno_parity.capture_storage`, which re-runs itself with `PYTHONHASHSEED=0`; never edit these files by hand.
`tests/agno_parity/test_storage_goldens_current.py` proves the current code reproduces every file.

### Scenarios

| Store | Session | Runs |
| --- | --- | --- |
| `agent_code` | `!room:example.org:$conversation` | `run-text` (blocking, cache and reasoning counters), `run-tools` (streamed, a tool call, then two parallel calls, then reasoning, `redacted_reasoning_content`, and `provider_data`), `run-media` (prompt with an image, audio, a video, a PDF, a UTF-8 text file, and a file path), `run-view` (`view_file` puts a viewed image on the tool-media follow-up message), `run-error` and `run-cancelled` (MindRoom's interrupted-turn records, stored `COMPLETED`), `run-paused-approval` (`PAUSED` on an approval-gated `multiply`, system message kept) |
| `agent_code` | `!room:example.org:$delegation` | `run-paused-delegation`: `PAUSED` on `run_subagent` external execution, with `metadata.mindroom_delegation` projecting the child's pending approval |
| `agent_code` | `!room:example.org:$native` | `run-native-error` (`ERROR`) and `run-native-cancelled` (`CANCELLED`), as Agno saves them when no MindRoom turn record replaces them |
| `agent_code` | `!room:example.org:$compacted` | `run-c1` to `run-c5`; a compaction summary row covers `run-c1` and `run-c2`, an edit cut removed `run-c4` (a `_removed_runs` row), and a mid-turn fold row holds a segment of `run-c5`'s tool exchange, so `run-c5` loads without those messages; a `compaction_summary` usage row |
| `agent_helper` | `delegate:code:helper:<id>` | The delegated child run, `PAUSED` on its approval |
| `team_squad` | `!room:example.org:$squad` | Coordinate team runs `team-completed` (member used a tool), `team-member-error` (member row `ERROR`), and `team-paused` (member approval; the team row embeds the paused member) |
| `team_panel` | `!room:example.org:$panel` | Streamed collaborate run `panel-completed` with two member rows |
| `agno2_migrated` | `session-1` | `tests/fixtures/agno_2_6_12_code_sessions.sql` after MindRoom's open-time migration (runs moved out of the `runs` blob, `session_data` still double-encoded) |

Every run with a provider request also has its usage row in `<table>_usage`.

### Files

- `storage/<store>.sql`: `iterdump` of each database; restore it with `sqlite3` `executescript`, as `tests.conftest.create_agno_2_sessions_db` does.
- `storage/stores.json`: each store's database path under the storage root, its sessions table, and its sessions with their stored session type.
  Open a restored store with `create_state_storage(name, <storage root>/<path without sessions/<name>.db>, subdir="sessions", session_table=...)`.
- `storage/loaded_sessions.json`: `AgentSession.to_dict()` or `TeamSession.to_dict()` of every session as `get_agent_session` or `get_team_session` loads it: runs after the scope's latest summary row plus every non-visible run and its descendants, folded messages removed, and the summary taken from the latest summary row.
- `runs/<type>.json`: `{case: dict}` for every value of that type nested in the stored `run_data` columns, summary-row segments, and `session_data`, exactly as stored.
  A case key is `<store>:<run id or segment:<row id>>` followed by its path, such as `agent_code:run-tools.messages[3].metrics`.
  The types are `run_output` (`RunOutput`), `team_run_output` (`TeamRunOutput`), `message` (`Message`), `image`, `audio`, `video`, and `file` (media dicts, read through `Message.from_dict` because Agno media has no `from_dict`), `tool_execution` (`ToolExecution`), `tool_call_metrics` (`ToolCallMetrics`), `run_requirement` (`RunRequirement`), `message_metrics` (`MessageMetrics`), `run_metrics` (`RunMetrics`), `model_metrics` (`ModelMetrics`), and `session_metrics` (`SessionMetrics`).
- `history/<store>__<session>.json`: `get_messages` results for the loaded session under each selection's keyword arguments, and `filter_tool_calls` applied to `copy_history_message` copies of a selection, as Agno's message builder does.
  Agno's builder passes `num_history_runs` as `last_n_runs`, `num_history_messages` as `limit`, `[system_message_role]` as `skip_roles` only for a role other than `user`, `assistant`, or `tool`, and the member's id as `agent_id` only for a team member.
- `events/ai_run.json`: the `run_metadata_collector` content, the `io.mindroom.ai_run` Matrix metadata, of every run that produced one; stopped and paused turns produce none.
- `events/openai_compat.json`: each `/v1/chat/completions` request with its status and body, parsed JSON when blocking and the raw lines of the SSE stream when streaming.

Agno's own readers are not lossless.
Re-serializing a stored run adds `"events": []` to it and to every member response, drops an empty `tools` list, and drops `Audio.format`, so rows MindRoom rewrites with `save_runs` carry those changes.
The current-behavior test pins exactly these differences.

The `run-error` record holds the previous turn's answer followed by `(turn failed before completion)`, because MindRoom's interrupted-turn text falls back to replayed history when the failed request produced no assistant message; the golden keeps that current behavior.

### Normalization

- The wall clock is frozen, and every turn starts at the next minute after 2027-01-15T08:00:00Z, so Unix timestamps run `1800000060`, `1800000120`, and so on.
- `time.time`, `datetime.now` in every `agno` and `mindroom` module, Agno's message, run, and tool timestamps, summary rows, requirement `created_at`, and the system prompt's date follow that clock, and local time is UTC.
- Agno's timers report every timed span as 0.5 seconds and every `time_to_first_token` as 0.25 seconds, since parallel tool calls run concurrently and would otherwise time in whatever order they ran.
- Random ids become sequential ones in first-appearance order across the dumps and then the events: UUIDs become `00000000-0000-4000-8000-<n>`, 32-digit hex ids such as delegation, child run, correlation, and viewed-image ids become `<n>` padded to 32 hex digits, and completion ids become `chatcmpl-<n>` padded to 12 hex digits.
- The capture's temporary directory becomes `/mindroom-capture`.
- Dumps sort their `CREATE INDEX` statements, because Agno creates indexes in a different order on every run, and strip the trailing spaces SQLAlchemy leaves in table definitions.
- The capture runs with `PYTHONHASHSEED=0`, because Agno's `Model.to_dict` orders `agent_data.model` and `team_data.model` by iterating a set.
