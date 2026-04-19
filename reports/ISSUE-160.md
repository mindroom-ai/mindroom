# ISSUE-160

## Summary

Added `action="search"` to `matrix_api` so agents can call Matrix `POST /_matrix/client/v3/search` for single-room full-text event search.

The new action validates `search_term`, `order_by`, and `limit`, defaults `room_id` to the current room context, constrains `filter.rooms` to the target room, and returns normalized result payloads with snippets and optional event context.

## Phase Log

- Read the issue spec and existing `matrix_api` implementation before changing code.
- Added search request/response handling in `src/mindroom/custom_tools/matrix_api.py`.
- Updated the wrapper metadata in `src/mindroom/tools/matrix_api.py` and `src/mindroom/tools_metadata.json`.
- Added focused unit coverage in `tests/test_matrix_api_tool.py` for happy path, pagination token passthrough, room scoping, validation, raw passthrough, and zero-result response parsing.
- Live-tested the new action against the lab Tuwunel homeserver with a restored lab Matrix session and a throwaway nio store path to avoid sharing the running lab service store.

## Files Touched

- `src/mindroom/custom_tools/matrix_api.py`
- `src/mindroom/tools/matrix_api.py`
- `src/mindroom/tools_metadata.json`
- `tests/test_matrix_api_tool.py`
- `reports/ISSUE-160.md`

## Test Output

Focused unit test command:

```bash
nix-shell --run 'uv run pytest tests/test_matrix_api_tool.py -x -n 0 --no-cov -v'
```

Focused unit test result:

```text
75 passed, 1 warning in 1.50s
```

Pre-flight checks after the report update:

- Ran `pre-commit run --all-files`.
- The repo-wide run passed the relevant Python hooks for this change and regenerated unrelated `skills/mindroom-docs` reference files on this branch, so those unrelated generated changes were reverted to keep the ISSUE-160 diff scoped to `matrix_api`.
- Re-ran `pre-commit run --files src/mindroom/custom_tools/matrix_api.py src/mindroom/tools/matrix_api.py src/mindroom/tools_metadata.json tests/test_matrix_api_tool.py reports/ISSUE-160.md` and it passed cleanly.
- Ran `git diff origin/main --stat` and the scope stayed limited to the expected `matrix_api` files plus this report.

## Live Test Evidence

Lab invocation details:

- Homeserver: `http://localhost:8008`
- Agent session restored as: `@mindroom_general_adb4d443:mindroom.lab.mindroom.chat`
- Search room: `!UwGVYUWTNV07MS2EGy:mindroom.lab.mindroom.chat`
- Search term: `ISSUE160_SEARCH_SEED_20260419T035613Z`
- Tool call path: `MatrixApiTools().matrix_api(action="search", ...)`

Full search response with matches:

```json
{
  "action": "search",
  "count": 3,
  "next_batch": null,
  "results": [
    {
      "event_id": "$dY_93lmXzi2ZlCGtbWiynvsBstmxmbVVMMe-QOIMpAA",
      "origin_server_ts": 1776570973114,
      "rank": 0.0,
      "room_id": "!UwGVYUWTNV07MS2EGy:mindroom.lab.mindroom.chat",
      "sender": "@mindroom_general_adb4d443:mindroom.lab.mindroom.chat",
      "snippet": "ISSUE160_SEARCH_SEED_20260419T035613Z gamma live search evidence",
      "type": "m.room.message"
    },
    {
      "event_id": "$MgV6kQFJVjUPY3np1qasqiIFbWAR78EWxWmxhw1Sybk",
      "origin_server_ts": 1776570973112,
      "rank": 0.0,
      "room_id": "!UwGVYUWTNV07MS2EGy:mindroom.lab.mindroom.chat",
      "sender": "@mindroom_general_adb4d443:mindroom.lab.mindroom.chat",
      "snippet": "ISSUE160_SEARCH_SEED_20260419T035613Z beta live search evidence",
      "type": "m.room.message"
    },
    {
      "event_id": "$RlCBuXSXaY5Hcd8apeskAMgCQWOV4U3fV3V5Jmwd2iw",
      "origin_server_ts": 1776570973111,
      "rank": 0.0,
      "room_id": "!UwGVYUWTNV07MS2EGy:mindroom.lab.mindroom.chat",
      "sender": "@mindroom_general_adb4d443:mindroom.lab.mindroom.chat",
      "snippet": "ISSUE160_SEARCH_SEED_20260419T035613Z alpha live search evidence",
      "type": "m.room.message"
    }
  ],
  "room_id": "!UwGVYUWTNV07MS2EGy:mindroom.lab.mindroom.chat",
  "status": "ok",
  "tool": "matrix_api"
}
```

Paginated search response with `limit=2`, demonstrating `count=3` and `next_batch="2"` when total matches exceed the requested page size:

```json
{
  "action": "search",
  "count": 3,
  "next_batch": "2",
  "results": [
    {
      "event_id": "$dY_93lmXzi2ZlCGtbWiynvsBstmxmbVVMMe-QOIMpAA",
      "origin_server_ts": 1776570973114,
      "rank": 0.0,
      "room_id": "!UwGVYUWTNV07MS2EGy:mindroom.lab.mindroom.chat",
      "sender": "@mindroom_general_adb4d443:mindroom.lab.mindroom.chat",
      "snippet": "ISSUE160_SEARCH_SEED_20260419T035613Z gamma live search evidence",
      "type": "m.room.message"
    },
    {
      "event_id": "$MgV6kQFJVjUPY3np1qasqiIFbWAR78EWxWmxhw1Sybk",
      "origin_server_ts": 1776570973112,
      "rank": 0.0,
      "room_id": "!UwGVYUWTNV07MS2EGy:mindroom.lab.mindroom.chat",
      "sender": "@mindroom_general_adb4d443:mindroom.lab.mindroom.chat",
      "snippet": "ISSUE160_SEARCH_SEED_20260419T035613Z beta live search evidence",
      "type": "m.room.message"
    }
  ],
  "room_id": "!UwGVYUWTNV07MS2EGy:mindroom.lab.mindroom.chat",
  "status": "ok",
  "tool": "matrix_api"
}
```
