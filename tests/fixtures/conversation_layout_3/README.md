# Conversation databases in layout version 3

These files are conversation databases as v2026.10.667's store wrote them, the last release whose sessions table names each row's kind and owner and keeps `session_state` inside `session_data`, so the conversion to layout version 4 is tested against rows its writer stored instead of hand-written ones.
`tests/session_storage/layout_3_recorder.py` recorded them at v2026.10.667 by driving the store's own writes under the storage oracle's fixed clock and counted ids; the recorder was removed with the layout it describes.

| File | Database |
| --- | --- |
| `agent_code.sql` | Agent `code`: a conversation with session state, metadata, usage, a compaction summary and its usage, a conversation an edit cut, and one only helper usage created. |
| `team_crew.sql` | Team `crew`'s scope: a team turn with two member runs, and a conversation whose first reply, on the team bot's agent path, was interrupted, so its row is an agent row that a team turn's load does not return, followed by a team turn saved into it. |
| `system.sql` | The system usage database: internal provider calls of two kinds. |

Each `<name>.json` beside its dump is what every reader showed of that database at that release (`tests/session_storage/layout_3_readers.py`): each session's loads, history views, stored runs, and metadata read under the scope that owns the database, the usage report's rows, and an agent's updated sessions.

## Recording again

The recorder lives in commit `70eb34a75836023fbbfd8364d8a4bfb6d4f26dff`, which is v2026.10.667 (`1672953bf94f3e7cab752d57b80a46217ab900a1`) plus the recorder and the readers, and stays reachable from pull request #3339 (`git fetch origin pull/3339/head`).
Check that commit out in a separate worktree and run `.venv/bin/python -m tests.session_storage.layout_3_recorder` from its root; recording again there writes identical files.
