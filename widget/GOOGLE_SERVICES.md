# Google Services Integration Guide

## Architecture Overview

Google services (Gmail, Calendar, Sheets) use a unified OAuth flow where users authenticate once with Google and all services become available based on the granted scopes.

## Adding a New Google Service

To add a new Google service (e.g., Google Docs), follow these steps:

### 1. Update Backend Helper (`widget/backend/src/api/google_tools_helper.py`)

Add the tool to the list and its required scopes:

```python
def is_google_managed_tool(tool_name: str) -> bool:
    google_oauth_tools = {"google_calendar", "google_sheets", "gmail", "google_docs"}  # Add here
    return tool_name in google_oauth_tools

def get_google_tool_scopes(tool_name: str) -> list[str]:
    scope_map = {
        # ... existing scopes ...
        "google_docs": [  # Add new scopes
            "https://www.googleapis.com/auth/documents",
            "https://www.googleapis.com/auth/documents.readonly",
        ],
    }
```

### 2. Update Frontend Helper (`widget/frontend/src/lib/googleTools.ts`)

Add the tool to the managed tools list:

```typescript
const GOOGLE_MANAGED_TOOLS = ['google_calendar', 'google_sheets', 'gmail', 'google_docs'];  // Add here
```

### 3. Update OAuth Scopes (`widget/backend/src/api/google_integration.py`)

Add any new OAuth scopes needed (if not already present):

```python
SCOPES = [
    # ... existing scopes ...
    "https://www.googleapis.com/auth/documents",  # If needed
    "https://www.googleapis.com/auth/documents.readonly",
]
```

### 4. Create Tool Definition (`src/mindroom/tools/google_docs.py`)

Create the tool definition following the pattern of other Google tools:

```python
@register_tool_with_metadata(
    name="google_docs",
    display_name="Google Docs",
    description="Create and edit Google Docs",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.SPECIAL,  # Important: Use SPECIAL for Google services
    icon="FaFileText",
    icon_color="text-blue-600",
    config_fields=None,  # Important: No config fields for Google services
    dependencies=["google-api-python-client", "google-auth", ...],
    docs_url="https://docs.agno.com/tools/toolkits/google_docs",
)
def google_docs_tools() -> type[GoogleDocsTools]:
    from agno.tools.googledocs import GoogleDocsTools
    return GoogleDocsTools
```

## Key Points

- **No config_fields**: Google services should have `config_fields=None`
- **SetupType.SPECIAL**: Use this for all Google OAuth services
- **Centralized management**: Update helper files instead of hardcoding lists
- **Single OAuth flow**: Users authenticate once for all Google services

## Testing

After adding a new Google service:

1. Run the widget and connect to Google Services
2. Verify the new tool appears in the Agent Editor when Google is connected
3. Verify it shows "Via Google Services" badge
4. Verify it appears in the Integrations tab with proper status

## Current Google Services

| Service | Tool Name | Status |
|---------|-----------|--------|
| Gmail | `gmail` | ✅ Integrated |
| Calendar | `google_calendar` | ✅ Integrated |
| Sheets | `google_sheets` | ✅ Integrated |
| Maps | `google_maps` | ❌ Uses API key (different pattern) |
