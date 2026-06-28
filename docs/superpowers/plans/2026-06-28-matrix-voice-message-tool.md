# Matrix Voice Message Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a one-call tool that synthesizes text to speech and sends it as a Matrix voice message.

**Architecture:** Add `matrix_voice_message` as a focused OpenAI-backed Matrix runtime tool.
Reuse `matrix.client_delivery` upload, encryption, thread fallback, and conversation-cache notification behavior.

**Tech Stack:** Python 3.13, Agno Toolkit, OpenAI Python client, matrix-nio, pytest.

---

## File Structure

- `src/mindroom/matrix/client_delivery.py`: add reusable byte-upload helper and `send_audio_message`.
- `src/mindroom/custom_tools/matrix_voice_message.py`: add model-facing Matrix voice message toolkit.
- `src/mindroom/tools/matrix_voice_message.py`: register tool metadata and config fields.
- `src/mindroom/tools/__init__.py`: import and export the new tool registration.
- `src/mindroom/tools_metadata.json`: regenerate committed registry metadata.
- `pyproject.toml`: add the empty optional extra group required for registered tool extras sync.
- `uv.lock`: refresh the extras list after updating `pyproject.toml`.
- `tach.toml`: add the new module boundary and expose the delivery helper.
- `tests/test_send_file_message.py`: cover `send_audio_message` payloads and encrypted upload reuse.
- `tests/test_matrix_voice_message.py`: cover tool validation, generation, targeting, and payload docs.

## Task 1: Matrix Audio Send Helper

- [x] Add failing tests in `tests/test_send_file_message.py`.

Required cases:

- `send_audio_message` sends unencrypted bytes with `msgtype: m.audio`, `url`, `info.mimetype`, `info.size`, and `org.matrix.msc3245.voice`.
- `send_audio_message` sends encrypted-room bytes with `file` payload and no `url`.
- `send_audio_message` fails before upload when encrypted-room E2EE support is unavailable.
- `send_audio_message` sets thread fallback relation when `thread_id` and `latest_thread_event_id` are provided.

- [x] Run the new focused tests and confirm they fail because `send_audio_message` does not exist.

Command:

```bash
uv run pytest tests/test_send_file_message.py -x -n 0 --no-cov -v
```

- [x] Refactor `client_delivery.py` to share byte upload logic between files and in-memory audio.

Implementation notes:

- Extract `_upload_media_bytes_as_mxc(client, room_id, upload_bytes, filename, mimetype)`.
- Keep `_upload_file_as_mxc` as a thin file-reading caller.
- Add `send_audio_message(client, room_id, audio_bytes, *, config, mimetype, filename="voice-message.mp3", caption=None, thread_id=None, latest_thread_event_id=None, conversation_cache=None)`.
- Build content with `msgtype: "m.audio"` and `org.matrix.msc3245.voice: {}`.
- Notify `conversation_cache` exactly like `send_file_message`.

- [x] Run the focused test file and confirm it passes.

## Task 2: Model-Facing Tool

- [x] Add failing tests in `tests/test_matrix_voice_message.py`.

Required cases:

- Empty `text` returns structured `error`.
- Missing runtime context returns structured `error`.
- Unauthorized target room returns structured `error`.
- Successful call generates speech with configured model, voice, and format, then sends audio to the current room and thread.
- `thread_id="room"` sends at room level.
- Processed function schema documents `text`, `caption`, and `thread_id="room"`.

- [x] Run the new tests and confirm they fail because the tool does not exist.

Command:

```bash
uv run pytest tests/test_matrix_voice_message.py -x -n 0 --no-cov -v
```

- [x] Add `src/mindroom/custom_tools/matrix_voice_message.py`.

Implementation notes:

- Define `MatrixVoiceMessageTools(Toolkit)`.
- Constructor fields: `api_key`, `model`, `voice`, `response_format`.
- Use `get_tool_runtime_context`.
- Use `resolve_context_thread_id` with current-thread fallback and room sentinel.
- Use `room_access_allowed`.
- Use `check_rate_limit`.
- Generate speech with the OpenAI client inside `asyncio.to_thread`.
- Return `custom_tool_payload("matrix_voice_message", ...)`.

- [x] Add metadata registration in `src/mindroom/tools/matrix_voice_message.py`.

Implementation notes:

- Use `SetupType.API_KEY`, `ToolCategory.COMMUNICATION`, `ToolStatus.REQUIRES_CONFIG`, and `requires_room_context=True`.
- Config fields: `api_key`, `model`, `voice`, and `response_format`.
- Function name: `matrix_voice_message`.

- [x] Wire the metadata module into `src/mindroom/tools/__init__.py`.

- [x] Regenerate `src/mindroom/tools_metadata.json`.

Command:

```bash
./.venv/bin/python -c "import json; import mindroom.tools; from pathlib import Path; from mindroom.tool_system.metadata import export_tools_metadata; Path('src/mindroom/tools_metadata.json').write_text(json.dumps({'tools': export_tools_metadata()}, indent=2, sort_keys=True) + '\n', encoding='utf-8')"
```

- [x] Run the new focused test file and confirm it passes.

## Task 3: Focused Regression Set

- [x] Run related focused tests.

Command:

```bash
uv run pytest tests/test_send_file_message.py tests/test_matrix_voice_message.py tests/test_tools_metadata.py tests/test_dynamic_toolkits.py -x -n 0 --no-cov -v
```

- [x] Run full pytest before any completion claim.

Command:

```bash
uv run pytest -x -n 0 --no-cov -v
```

- [x] Run pre-commit after `uv sync --all-extras`.

Command:

```bash
uv sync --all-extras
uv run pre-commit run --all-files
```

## Task 4: Commit, PR, and Native Review Loop

- [ ] Stage only changed files.

Command:

```bash
git add docs/superpowers/specs/2026-06-28-matrix-voice-message-tool-design.md docs/superpowers/plans/2026-06-28-matrix-voice-message-tool.md pyproject.toml src/mindroom/matrix/client_delivery.py src/mindroom/custom_tools/matrix_voice_message.py src/mindroom/tools/matrix_voice_message.py src/mindroom/tools/__init__.py src/mindroom/tools_metadata.json tach.toml tests/test_send_file_message.py tests/test_matrix_voice_message.py uv.lock
```

- [ ] Commit with a focused feature message.

Command:

```bash
git commit -m "Add Matrix voice message tool"
```

- [ ] Push the branch and open a PR.

- [ ] After opening the PR, run two fresh native Codex PR-review subagents against the latest head.

- [ ] Verify every finding in the main thread.

- [ ] Push follow-up commits for real in-scope issues and repeat with fresh reviewers until both approve the same head.
