# Agent Configuration Widget - Proof of Concept

## Overview

A Matrix widget that provides a visual interface for managing MindRoom agent configurations, with two-way synchronization between the widget UI and `config.yaml`. This allows both technical users (who prefer editing YAML) and non-technical users (who prefer UI) to manage agents and API keys.

## Core Features

### 1. Agent Management
- View all configured agents in a visual interface
- Add/edit/delete agents
- Configure agent properties:
  - Display name
  - Role description
  - Available tools
  - Instructions
  - Room assignments
  - Model selection
  - History settings

### 2. Model & API Key Management
- Configure AI model providers (OpenAI, Anthropic, Ollama, etc.)
- Securely store API keys
- Test model connections
- Set default models per agent or globally

### 3. Two-Way Sync
- **YAML → Widget**: Read config.yaml and populate UI
- **Widget → YAML**: Save changes back to config.yaml
- **Conflict Resolution**: Handle cases where both are edited
- **Real-time Updates**: Watch for file changes

## Technical Architecture

### Frontend (React/TypeScript)

```typescript
// Core components structure
components/
  AgentList/          # List of all agents
  AgentEditor/        # Edit individual agent
  ModelConfig/        # Configure AI models
  APIKeyManager/      # Manage API keys securely
  ToolSelector/       # Multi-select for agent tools
  RoomSelector/       # Multi-select for rooms
  YAMLPreview/        # Show YAML representation
  SyncStatus/         # Show sync state
```

### Backend (FastAPI)

```python
# API endpoints
POST   /api/config/load      # Load config from YAML
PUT    /api/config/save      # Save config to YAML
GET    /api/config/agents    # Get all agents
PUT    /api/config/agents/{id}  # Update agent
POST   /api/config/agents    # Create agent
DELETE /api/config/agents/{id}  # Delete agent
GET    /api/config/models    # Get model configs
PUT    /api/config/models/{id}  # Update model config
POST   /api/keys/encrypt    # Encrypt API key
GET    /api/tools           # Get available tools
GET    /api/rooms           # Get available rooms
POST   /api/test/model      # Test model connection
```

## UI Design

### Main Dashboard
```
┌─────────────────────────────────────────────────────────┐
│ MindRoom Agent Configuration                    [Sync ✓] │
├─────────────────────────────────────────────────────────┤
│ ┌─────────────┐ ┌─────────────────────────────────────┐ │
│ │   Agents    │ │          Agent Details              │ │
│ │             │ │                                     │ │
│ │ ▶ General   │ │ Name: GeneralAgent                  │ │
│ │ ▶ Calculator│ │ Role: [___________________________] │ │
│ │ ▶ Code      │ │                                     │ │
│ │ ▶ Research  │ │ Tools: □ calculator □ file □ shell  │ │
│ │             │ │                                     │ │
│ │ [+ Add]     │ │ Instructions:                       │ │
│ └─────────────┘ │ • [___________________________]    │ │
│                 │ • [___________________________]    │ │
│ ┌─────────────┐ │ [+ Add instruction]                 │ │
│ │   Models    │ │                                     │ │
│ │             │ │ Rooms: ☑ lobby ☑ help □ dev        │ │
│ │ ▶ Default   │ │                                     │ │
│ │ ▶ Anthropic │ │ Model: [Default ▼]                  │ │
│ │ ▶ Ollama    │ │                                     │ │
│ │             │ │ [Save] [Cancel] [View YAML]         │ │
│ └─────────────┘ └─────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

### Model Configuration
```
┌─────────────────────────────────────────────────────────┐
│ Model Configuration: Anthropic                          │
├─────────────────────────────────────────────────────────┤
│ Provider: anthropic                                     │
│ Model ID: claude-3-5-haiku-latest                      │
│ API Key: ****************************[Show] [Change]   │
│                                                         │
│ [Test Connection]  Status: ✓ Connected                  │
│                                                         │
│ [Save] [Cancel]                                         │
└─────────────────────────────────────────────────────────┘
```

## Implementation Plan

### Phase 1: Basic Functionality (Week 1)
1. **Setup Widget Infrastructure**
   - Create React app with Vite
   - Set up Matrix widget integration
   - Create FastAPI backend

2. **Config Loading**
   - Parse config.yaml
   - Load into UI state
   - Display agents list

3. **Basic Agent Editing**
   - Edit agent properties
   - Save back to YAML
   - Handle validation

### Phase 2: Advanced Features (Week 2)
1. **API Key Management**
   - Secure storage in Matrix state
   - Encryption/decryption
   - UI for managing keys

2. **Model Testing**
   - Test connections
   - Show available models
   - Handle errors gracefully

3. **Two-Way Sync**
   - File watcher for YAML changes
   - Conflict detection
   - Merge strategies

### Phase 3: Polish (Week 3)
1. **Tool & Room Management**
   - Dynamic tool discovery
   - Room creation/management
   - Validation rules

2. **YAML Preview**
   - Real-time YAML generation
   - Syntax highlighting
   - Diff view for changes

3. **Error Handling**
   - Comprehensive validation
   - User-friendly error messages
   - Recovery mechanisms

## Security Considerations

### API Key Storage
```python
# Store encrypted in Matrix room state
{
  "type": "m.mindroom.api_keys",
  "content": {
    "encrypted_keys": {
      "openai": "encrypted_base64_string",
      "anthropic": "encrypted_base64_string"
    },
    "encryption_method": "AES-256-GCM",
    "key_derivation": "PBKDF2"
  }
}
```

### Access Control
- Widget only accessible to room admins
- API keys never sent to backend unencrypted
- Audit log for all configuration changes

## File Sync Strategy

### Detecting Changes
```python
import watchdog
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class ConfigFileHandler(FileSystemEventHandler):
    def on_modified(self, event):
        if event.src_path.endswith('config.yaml'):
            # Reload config and notify frontend
            self.reload_config()
```

### Conflict Resolution
1. **Last Write Wins**: Simple but may lose changes
2. **Merge Changes**: Attempt to merge non-conflicting changes
3. **User Prompt**: Ask user to resolve conflicts
4. **Version History**: Keep backups of each save

## Example Data Flow

### Adding a New Agent
1. User clicks "Add Agent" in widget
2. Fills in agent details form
3. Clicks "Save"
4. Frontend sends to backend API
5. Backend validates configuration
6. Backend updates config.yaml
7. File watcher detects change
8. UI updates to reflect saved state

### Updating API Key
1. User clicks "Change" next to API key
2. Enters new key in secure input
3. Frontend encrypts key locally
4. Sends encrypted key to Matrix state
5. Tests connection with new key
6. Shows success/failure status

## Benefits

### For Non-Technical Users
- No need to edit YAML files
- Visual tool selection
- Easy API key management
- Immediate validation feedback

### for Technical Users
- Can still edit config.yaml directly
- Changes reflected in UI
- YAML preview for verification
- Git-friendly configuration

### For MindRoom Project
- Lower barrier to entry
- Fewer configuration errors
- Secure API key management
- Better user experience

## Next Steps

1. **Prototype Development**
   - Set up basic widget structure
   - Implement config loading
   - Create agent editor UI

2. **User Testing**
   - Test with both technical and non-technical users
   - Gather feedback on UI/UX
   - Iterate on design

3. **Integration**
   - Integrate with main MindRoom system
   - Add to documentation
   - Create setup guide

## Success Criteria

- ✓ Can view all agents from config.yaml
- ✓ Can edit agent properties and save
- ✓ Can manage API keys securely
- ✓ Changes sync both directions
- ✓ No data loss during sync
- ✓ Works in Element Web/Desktop
- ✓ Accessible to non-technical users
