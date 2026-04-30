# PR 809 Implementation Notes

## Summary

- Migrated Google Drive, Calendar, Sheets, and Gmail to the generic OAuth provider framework.
- Removed the legacy `/api/google/*` integration, helper code, and the custom-tools Google OAuth mixin.
- Added shared Google OAuth provider helpers, signed OAuth state/connect tokens, snapshot-cached provider loading, provider-driven custom tool clients, and structured `OAuthConnectionRequired` tool results.
- Updated disconnect to clear both OAuth token credentials and per-tool configuration credentials.
- Replaced the frontend legacy Google integration with per-service Google OAuth providers.
- Rewrote Google OAuth docs for the per-provider model and regenerated the MindRoom docs skill references.

## Paths Touched

- Backend OAuth/API: `src/mindroom/oauth/`, `src/mindroom/api/auth.py`, `src/mindroom/api/credentials.py`, `src/mindroom/api/oauth.py`, `src/mindroom/api/tools.py`, `src/mindroom/api/main.py`.
- Google tools: `src/mindroom/custom_tools/google_drive.py`, `src/mindroom/custom_tools/google_calendar.py`, `src/mindroom/custom_tools/google_sheets.py`, `src/mindroom/custom_tools/gmail.py`, `src/mindroom/tools/google_calendar.py`, `src/mindroom/tools/google_sheets.py`, `src/mindroom/tools/gmail.py`.
- Removed legacy code: `src/mindroom/api/google_integration.py`, `src/mindroom/api/google_tools_helper.py`, `src/mindroom/custom_tools/_google_oauth.py`.
- Tool execution and worker metadata: `src/mindroom/tool_system/tool_hooks.py`, `src/mindroom/tool_system/worker_routing.py`, `src/mindroom/config/models.py`, `src/mindroom/tools_metadata.json`, `tach.toml`.
- Frontend: `frontend/src/components/Integrations/integrations/index.ts`, `frontend/src/lib/api.ts`, `frontend/scripts/generate-icon-imports.cjs`, removed legacy Google integration components.
- Docs: `docs/deployment/google-services-oauth.md`, `docs/deployment/google-services-user-oauth.md`, `docs/oauth-framework.md`, related tool/config docs and generated skill references.

## Tests Added

- `tests/test_google_calendar_oauth_tool.py`
- `tests/test_google_sheets_oauth_tool.py`
- Extended `tests/api/test_oauth_api.py` for migrated providers, signed state/connect tokens, and disconnect clearing tool config.

## Validation

- `uv sync --all-extras`
- `uv run pytest -x -n auto --no-cov -v`
- `uv run pre-commit run --all-files`
- `uv run tach check --dependencies --interfaces`
- `cd frontend && npm test`
- `cd frontend && npm run build`
- `uv run python -c "from mindroom.oauth.registry import load_oauth_providers; from mindroom.config.main import Config; from mindroom.constants import RuntimePaths; print(sorted(load_oauth_providers(Config.model_validate({}), RuntimePaths.from_env())))"`

Provider smoke output:

```text
['google_calendar', 'google_drive', 'google_gmail', 'google_sheets']
```

## Git Log

```text
52d8e92d2 feat(oauth): migrate Google services to generic providers
34b9975eb Tighten OAuth credential invariants
```
